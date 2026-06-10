import logging
import queue
import threading
import time
import uuid
from abc import abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from .mux import (
    IVirtualClient,
    MqttUpstreamMux,
    PublishResult,
    VirtualClientHandle,
)
from .pktid import PacketIdAllocator
from .topicmatch import TopicMatcher
from .types import (
    ConnAckReturnCode,
    MalformedPacketException,
    MqttException,
    MqttMessage,
    ProtocolError,
    ProtocolLevel,
    SubAckReturnCode,
    WillSpec,
)
from .wirecodec import (
    ConnAckPacket,
    ConnectPacket,
    DisconnectPacket,
    EncodePacket,
    MqttPacket,
    MqttPacketDecoder,
    PingReqPacket,
    PingRespPacket,
    PubAckPacket,
    PubCompPacket,
    PubRecPacket,
    PubRelPacket,
    PublishPacket,
    SubAckPacket,
    SubscribePacket,
    UnsubAckPacket,
    UnsubscribePacket,
)


# Base virtual client that owns a real MQTT 3.1.1 wire session with a
# downstream peer (either a TCP MQTT client or a remote app on the other side
# of an OctoEverywhere cloud WebSocket). Subclasses implement the byte
# transport: TcpBrokerClient writes to a socket, WebSocketRelayClient writes
# binary WS frames.
#
# Threading model
# ---------------
#
# Inbound bytes arrive via FeedBytes(), typically from one of:
#   * A per-TCP-connection reader thread (TcpBrokerClient).
#   * The OE WebSocket OnData callback (WebSocketRelayClient).
# The bytes are decoded into packets and dispatched here.
#
# Outbound bytes are produced from two sources:
#   * Inline encoding inside FeedBytes (PINGRESP, SUBACK, etc.) on the
#     reader/WS-data thread.
#   * mux.DeliverMessage / OnUpstreamConnected / OnUpstreamDisconnected
#     callbacks on paho's loop thread.
# Both eventually call self._SendBytes() which subclass implements with its
# own per-connection write lock.
#
# QoS>0 PUBLISHes from the peer are processed on the same ordered control
# worker as SUBSCRIBE/UNSUBSCRIBE. This keeps the reader responsive while
# waiting on upstream paho acks, while still satisfying MQTT 3.1.1 §4.6:
# PUBACK/PUBREC must be sent in the order the corresponding PUBLISHes were
# received ([MQTT-4.6.0-2], [MQTT-4.6.0-3]), and messages must be forwarded
# in order. A single FIFO worker guarantees both.
class WireVirtualClient(IVirtualClient):
    _client_id_registry_lock = threading.Lock()
    _client_id_registry: Dict[Tuple[int, str], "WireVirtualClient"] = {}
    _QOS2_PUBLISHING = "publishing"
    _QOS2_AWAIT_PUBREL = "await_pubrel"

    # Sub-classes must set this in their constructor.
    # peer_label is a free-form string used in log messages (e.g. ip:port).
    #
    # max_packet_size bounds the decoder per packet. 1 MiB is well over any
    # legitimate MQTT message a printer broker would carry (Bambu's largest
    # report tops out around 64 KiB) but small enough that a misbehaving peer
    # cannot exhaust process memory by streaming an oversized PUBLISH. The
    # MQTT spec allows up to ~256 MiB; we tighten by default.
    _DEFAULT_MAX_PACKET_BYTES = 1024 * 1024

    def __init__(self, logger: logging.Logger, mux: MqttUpstreamMux, peer_label: str,
                 keepalive_grace: float = 1.5,
                 allowed_protocol_levels: Optional[List[int]] = None,
                 max_packet_size: Optional[int] = None) -> None:
        self._logger = logger
        self._mux = mux
        self._peer_label = peer_label
        self._keepalive_grace = keepalive_grace
        self._allowed_protocol_levels = allowed_protocol_levels or [ProtocolLevel.MQTT_3_1_1]

        bounded_size = max_packet_size if max_packet_size is not None else WireVirtualClient._DEFAULT_MAX_PACKET_BYTES
        self._decoder = MqttPacketDecoder(max_packet_size=bounded_size)
        self._send_lock = threading.RLock()
        self._close_lock = threading.Lock()

        # Session state. None until CONNECT lands.
        self._connected = False
        self._closed = False
        self._client_id: Optional[str] = None
        self._registered_client_id: Optional[str] = None
        self._clean_session = True
        self._keep_alive_sec = 0
        self._will: Optional[WillSpec] = None
        self._handle: Optional[VirtualClientHandle] = None
        self._last_recv_time = time.time()

        # Downstream packet IDs we allocate for outgoing PUBLISH/PUBREL (paho
        # → us → peer). Per MQTT 3.1.1 §2.3.1, separate ID space per direction.
        self._outbound_pid_alloc = PacketIdAllocator()
        # Pending PUBLISHes we sent the peer waiting for their ack. The value
        # tells us whether we're past PUBREC (i.e. moved to QoS 2's second
        # phase).
        self._pending_outbound_lock = threading.Lock()
        self._pending_qos1: Dict[int, float] = {}      # pid -> sent_time
        self._pending_qos2_step1: Dict[int, float] = {}  # awaiting PUBREC
        self._pending_qos2_step2: Dict[int, float] = {}  # awaiting PUBCOMP

        # Track incoming peer-originated QoS 2 publishes for which we owe a
        # PUBCOMP. Just packet IDs - we've already forwarded the payload.
        self._inbound_qos2_lock = threading.Lock()
        self._inbound_qos2: Dict[int, str] = {}

        # Keepalive watchdog. None until CONNECT lands; started/stopped with
        # the session.
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop = threading.Event()

        # SUBSCRIBE, UNSUBSCRIBE, and QoS>0 PUBLISH are processed on one
        # ordered control worker. They can block on upstream acks, so they
        # stay off the reader thread, but MQTT requires the server to process
        # them in packet order for a single client session and to emit
        # PUBACK/PUBREC in the order the PUBLISHes arrived (§4.6).
        self._control_queue: "queue.Queue[Tuple[str, int, Any]]" = queue.Queue()
        self._control_worker_lock = threading.Lock()
        self._control_worker_thread: Optional[threading.Thread] = None


    # ---- subclass hooks ----

    # Write `data` to the peer. Must be thread-safe; the base ensures
    # _send_lock is held around any encode-and-send sequence so subclasses
    # can rely on it for ordering, but they should still guard internal
    # transport state if needed.
    @abstractmethod
    def _SendBytes(self, data: bytes) -> None:  # pragma: no cover - abstract
        ...


    # Tell the subclass to drop its transport. Called after we send DISCONNECT
    # or detect a fatal protocol error.
    @abstractmethod
    def _CloseTransport(self) -> None:  # pragma: no cover - abstract
        ...


    # ---- public entry points (called by the host that owns this client) ----

    # Feed bytes received from the peer into the decoder and dispatch any
    # complete packets. Safe to call from a single reader thread (the
    # decoder is not internally locked - we assume one caller).
    def FeedBytes(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            packets = self._decoder.FeedBytes(data)
        except MalformedPacketException as e:
            self._logger.warning("WireVirtualClient[%s] malformed packet: %s", self._peer_label, e)
            self._FatalClose()
            return
        except Exception as e:
            self._logger.error("WireVirtualClient[%s] decoder raised: %s", self._peer_label, e)
            self._FatalClose()
            return
        self._last_recv_time = time.time()
        for pkt in packets:
            try:
                self._DispatchPacket(pkt)
                if self._closed:
                    return
            except MqttException as e:
                self._logger.warning("WireVirtualClient[%s] protocol error: %s", self._peer_label, e)
                self._FatalClose()
                return
            except Exception as e:
                self._logger.error("WireVirtualClient[%s] dispatch raised: %s", self._peer_label, e)
                self._FatalClose()
                return


    # Tell the wire client we're going away (peer closed transport, host is
    # shutting down, etc.). Detaches from mux. Idempotent.
    def OnPeerClosed(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._keepalive_stop.set()
        self._DetachFromMux(publish_will=True)


    # ---- packet dispatch ----

    def _DispatchPacket(self, pkt: MqttPacket) -> None:
        if isinstance(pkt, ConnectPacket):
            self._HandleConnect(pkt)
            return
        if not self._connected:
            # Anything other than CONNECT before we're connected is a protocol
            # error (MQTT 3.1.1 §3.1: first packet must be CONNECT).
            raise ProtocolError("first packet must be CONNECT")
        if isinstance(pkt, PublishPacket):
            self._HandleInboundPublish(pkt)
        elif isinstance(pkt, PubAckPacket):
            self._HandlePubAck(pkt.packet_id)
        elif isinstance(pkt, PubRecPacket):
            self._HandlePubRec(pkt.packet_id)
        elif isinstance(pkt, PubRelPacket):
            self._HandleInboundPubRel(pkt.packet_id)
        elif isinstance(pkt, PubCompPacket):
            self._HandlePubComp(pkt.packet_id)
        elif isinstance(pkt, SubscribePacket):
            self._HandleSubscribe(pkt)
        elif isinstance(pkt, UnsubscribePacket):
            self._HandleUnsubscribe(pkt)
        elif isinstance(pkt, PingReqPacket):
            self._SendPacket(PingRespPacket())
        elif isinstance(pkt, DisconnectPacket):
            # §3.14: clean DISCONNECT - do not publish the will.
            self._will = None
            with self._close_lock:
                if self._closed:
                    return
                self._closed = True
            self._keepalive_stop.set()
            self._DetachFromMux(publish_will=False)
            self._CloseTransport()
        else:
            raise ProtocolError(f"unexpected packet type: {type(pkt).__name__}")


    # ---- CONNECT handling ----

    def _HandleConnect(self, pkt: ConnectPacket) -> None:
        if self._connected:
            raise ProtocolError("duplicate CONNECT")
        # Protocol level check. The codec already parsed `protocol_level`; we
        # need to send CONNACK 0x01 if we don't support it. Per spec, we MUST
        # send the CONNACK and then close.
        if pkt.protocol_level not in self._allowed_protocol_levels:
            self._SendPacket(ConnAckPacket(
                session_present=False,
                return_code=ConnAckReturnCode.UNACCEPTABLE_PROTOCOL_VERSION,
            ))
            self._FatalClose()
            return
        # Protocol name check. Only "MQTT" is acceptable for level 4/5.
        if pkt.protocol_name != "MQTT":
            self._SendPacket(ConnAckPacket(
                session_present=False,
                return_code=ConnAckReturnCode.UNACCEPTABLE_PROTOCOL_VERSION,
            ))
            self._FatalClose()
            return
        # Per the user's choice: reject new CONNECTs while upstream is
        # disconnected so peers see a clean failure instead of a half-functional
        # session.
        if not self._mux.IsUpstreamConnected():
            self._logger.info("WireVirtualClient[%s] CONNECT rejected: upstream disconnected", self._peer_label)
            self._SendPacket(ConnAckPacket(
                session_present=False,
                return_code=ConnAckReturnCode.SERVER_UNAVAILABLE,
            ))
            self._FatalClose()
            return
        # Auth hook: subclasses may override _CheckAuth and reject.
        rc = self._CheckAuth(pkt)
        if rc != ConnAckReturnCode.ACCEPTED:
            self._SendPacket(ConnAckPacket(session_present=False, return_code=rc))
            self._FatalClose()
            return
        if len(pkt.client_id) == 0 and not pkt.clean_session:
            self._SendPacket(ConnAckPacket(
                session_present=False,
                return_code=ConnAckReturnCode.IDENTIFIER_REJECTED,
            ))
            self._FatalClose()
            return
        if not pkt.clean_session:
            # The mux does not persist offline subscriptions or queued QoS
            # messages. Reject persistent sessions instead of accepting and
            # then violating MQTT session semantics.
            self._SendPacket(ConnAckPacket(
                session_present=False,
                return_code=ConnAckReturnCode.SERVER_UNAVAILABLE,
            ))
            self._FatalClose()
            return
        # Accept the connection.
        self._client_id = pkt.client_id if len(pkt.client_id) > 0 else self._GenerateClientId()
        self._clean_session = pkt.clean_session
        self._keep_alive_sec = pkt.keep_alive
        self._will = pkt.will
        # session_present is False because we don't persist sessions across
        # reconnects (our model is closer to clean-session always).
        self._SendPacket(ConnAckPacket(session_present=False, return_code=ConnAckReturnCode.ACCEPTED))
        self._connected = True
        # Attach to mux so we start receiving DeliverMessage callbacks.
        self._handle = self._mux.Attach(self)
        if not self._mux.IsUpstreamConnected():
            # The upstream can disconnect between the pre-CONNECT check and
            # Attach(). If the disconnect callback already snapshotted handles,
            # this new client would otherwise miss the forced close.
            self._FatalClose()
            return
        replaced = self._RegisterClientId(self._client_id)
        if replaced is not None:
            replaced._FatalClose()  # pylint: disable=protected-access
        # Start keepalive watchdog if the peer asked for one (keepalive=0
        # means disabled per §3.1.2.10).
        if self._keep_alive_sec > 0:
            self._keepalive_thread = threading.Thread(
                target=self._KeepaliveLoop, name=f"mqttmux-keepalive[{self._peer_label}]", daemon=True,
            )
            self._keepalive_thread.start()


    # Subclasses can override to enforce username/password etc. Default: accept.
    def _CheckAuth(self, pkt: ConnectPacket) -> int:
        return ConnAckReturnCode.ACCEPTED


    def _GenerateClientId(self) -> str:
        # MQTT 3.1.1 §3.1.3.1 allows the server to assign one when the
        # client sends a zero-byte id.
        return f"oe-{uuid.uuid4().hex}"


    # ---- SUBSCRIBE / UNSUBSCRIBE ----

    def _HandleSubscribe(self, pkt: SubscribePacket) -> None:
        if self._handle is None:
            raise ProtocolError("SUBSCRIBE before CONNECT")
        # mux.Subscribe blocks for the upstream SUBACK (or synthesizes one
        # immediately). Run on a worker so a slow upstream doesn't block our
        # reader from processing PINGREQ / DISCONNECT and tripping the
        # keepalive timer.
        self._EnqueueControlOp("sub", pkt.packet_id, list(pkt.subscriptions))


    def _WorkerSubscribe(self, packet_id: int, subscriptions: List[Tuple[str, int]]) -> None:
        handle = self._handle
        if handle is None:
            return
        return_codes: List[int] = []
        for filter_, qos in subscriptions:
            if not TopicMatcher.ValidateFilter(filter_):
                return_codes.append(SubAckReturnCode.FAILURE)
                continue
            if qos < 0 or qos > 2:
                return_codes.append(SubAckReturnCode.FAILURE)
                continue
            try:
                result = self._mux.Subscribe(handle, filter_, qos, callback=None)
            except Exception as e:
                self._logger.error("WireVirtualClient[%s] subscribe(%s) raised: %s",
                                   self._peer_label, filter_, e)
                return_codes.append(SubAckReturnCode.FAILURE)
                continue
            return_codes.append(result.granted_qos)
        # Connection might have closed while we were waiting.
        if self._closed:
            return
        self._SendPacket(SubAckPacket(packet_id=packet_id, return_codes=return_codes))


    def _HandleUnsubscribe(self, pkt: UnsubscribePacket) -> None:
        if self._handle is None:
            raise ProtocolError("UNSUBSCRIBE before CONNECT")
        self._EnqueueControlOp("unsub", pkt.packet_id, list(pkt.filters))


    def _WorkerUnsubscribe(self, packet_id: int, filters: List[str]) -> None:
        handle = self._handle
        if handle is None:
            return
        for f in filters:
            try:
                self._mux.Unsubscribe(handle, f)
            except Exception as e:
                self._logger.debug("WireVirtualClient[%s] unsubscribe(%s) raised: %s",
                                   self._peer_label, f, e)
        if self._closed:
            return
        self._SendPacket(UnsubAckPacket(packet_id=packet_id))


    def _EnqueueControlOp(self, op: str, packet_id: int, payload: Any) -> None:
        if self._closed:
            return
        with self._control_worker_lock:
            self._control_queue.put((op, packet_id, payload))
            if self._control_worker_thread is None or not self._control_worker_thread.is_alive():
                self._control_worker_thread = threading.Thread(
                    target=self._ControlWorkerLoop,
                    name=f"mqttmux-ctl[{self._peer_label}]",
                    daemon=True,
                )
                self._control_worker_thread.start()


    def _ControlWorkerLoop(self) -> None:
        while not self._closed:
            try:
                op, packet_id, payload = self._control_queue.get(timeout=0.5)
            except queue.Empty:
                with self._control_worker_lock:
                    if self._control_queue.empty():
                        self._control_worker_thread = None
                        return
                continue
            try:
                if op == "sub":
                    self._WorkerSubscribe(packet_id, payload)
                elif op == "unsub":
                    self._WorkerUnsubscribe(packet_id, payload)
                elif op == "pub1":
                    topic, body, retain = payload
                    self._WorkerPublishQos1(packet_id, topic, body, retain)
                elif op == "pub2":
                    topic, body, retain = payload
                    self._WorkerPublishQos2(packet_id, topic, body, retain)
                else:
                    self._logger.warning("WireVirtualClient[%s] unknown control op=%s",
                                         self._peer_label, op)
            finally:
                self._control_queue.task_done()


    # ---- inbound PUBLISH from peer (downstream -> upstream) ----

    def _HandleInboundPublish(self, pkt: PublishPacket) -> None:
        if self._handle is None:
            raise ProtocolError("PUBLISH before CONNECT")
        if not TopicMatcher.ValidateTopicName(pkt.topic):
            raise ProtocolError(f"invalid PUBLISH topic: {pkt.topic!r}")
        if pkt.qos == 0:
            # Fire and forget. Do it inline since publish is fast for QoS 0.
            self._mux.Publish(self._handle, pkt.topic, pkt.payload, qos=0, retain=pkt.retain)
            return
        # The decoder enforces that QoS > 0 PUBLISH carries a non-None
        # packet_id (it raises MalformedPacketException otherwise); narrow
        # the type here so the worker threads see a plain int.
        if pkt.packet_id is None:
            raise ProtocolError("PUBLISH with QoS > 0 must have a packet identifier")
        pid: int = pkt.packet_id
        if pkt.qos == 1:
            # Queue on the ordered control worker so the reader doesn't block on the
            # upstream PUBACK. Per MQTT 3.1.1 §4.6 [MQTT-4.6.0-2] the PUBACKs we send
            # must be in the order the PUBLISHes were received, and the messages must
            # be forwarded upstream in order - the single FIFO worker guarantees both.
            self._EnqueueControlOp("pub1", pid, (pkt.topic, pkt.payload, pkt.retain))
            return
        # QoS 2 (MQTT 3.1.1 §4.3.3): PUBREC MUST go out as the response to
        # a PUBLISH only after we have accepted ownership. We treat a
        # successful upstream publish as that ownership handoff; duplicate
        # PUBLISHes while the handoff is in-flight are ignored, and duplicates
        # after PUBREC get PUBREC re-sent without re-publishing upstream.
        # Decide the action under the state lock, then perform the network
        # send outside it so a slow socket can't serialize other QoS 2 work.
        with self._inbound_qos2_lock:
            existing_state = self._inbound_qos2.get(pid)
            if existing_state == self._QOS2_AWAIT_PUBREL:
                send_pubrec = True
                enqueue_work = False
            elif existing_state == self._QOS2_PUBLISHING:
                send_pubrec = False
                enqueue_work = False
            else:
                self._inbound_qos2[pid] = self._QOS2_PUBLISHING
                send_pubrec = False
                enqueue_work = True
        if send_pubrec:
            self._SendPacket(PubRecPacket(packet_id=pid))
            return
        if not enqueue_work:
            return
        # Queue on the ordered control worker, like QoS 1. [MQTT-4.6.0-3] requires
        # PUBRECs in the order the corresponding PUBLISHes were received.
        self._EnqueueControlOp("pub2", pid, (pkt.topic, pkt.payload, pkt.retain))


    def _WorkerPublishQos1(self, packet_id: int, topic: str, payload: bytes, retain: bool) -> None:
        handle = self._handle
        if handle is None:
            return
        result: PublishResult = self._mux.Publish(handle, topic, payload, qos=1, retain=retain)
        if not result.success:
            self._logger.warning("WireVirtualClient[%s] upstream PUBLISH QoS1 failed for topic=%s",
                                 self._peer_label, topic)
            self._FatalClose()
            return
        if self._closed:
            return
        self._SendPacket(PubAckPacket(packet_id=packet_id))


    def _WorkerPublishQos2(self, packet_id: int, topic: str, payload: bytes, retain: bool) -> None:
        handle = self._handle
        if handle is None:
            return
        # Forward to upstream. The PUBREC has already been sent inline by
        # _HandleInboundPublish; PUBCOMP will go out when the peer sends
        # PUBREL via _HandleInboundPubRel. Upstream success/failure is
        # decoupled from the local handshake.
        result: PublishResult = self._mux.Publish(handle, topic, payload, qos=2, retain=retain)
        if not result.success:
            self._logger.warning("WireVirtualClient[%s] upstream PUBLISH QoS2 failed for topic=%s pid=%s",
                                 self._peer_label, topic, packet_id)
            with self._inbound_qos2_lock:
                if self._inbound_qos2.get(packet_id) == self._QOS2_PUBLISHING:
                    self._inbound_qos2.pop(packet_id, None)
            self._FatalClose()
            return
        with self._inbound_qos2_lock:
            if self._inbound_qos2.get(packet_id) != self._QOS2_PUBLISHING:
                return
            self._inbound_qos2[packet_id] = self._QOS2_AWAIT_PUBREL
        if self._closed:
            return
        self._SendPacket(PubRecPacket(packet_id=packet_id))


    def _HandleInboundPubRel(self, packet_id: int) -> None:
        with self._inbound_qos2_lock:
            state = self._inbound_qos2.pop(packet_id, None)
        if state == self._QOS2_PUBLISHING:
            raise ProtocolError(f"PUBREL before PUBREC for pid={packet_id}")
        if state is None:
            # PUBREL for an unknown id. Spec says we should still respond
            # with PUBCOMP (so the peer can clean up), but this is suspicious
            # so we also log.
            self._logger.warning("WireVirtualClient[%s] PUBREL for unknown pid=%s",
                                 self._peer_label, packet_id)
        self._SendPacket(PubCompPacket(packet_id=packet_id))


    # ---- outbound PUBLISH from upstream (mux -> peer) ----

    # Called by the mux on paho's thread. Encode a PUBLISH for the peer.
    def DeliverMessage(self, handle: VirtualClientHandle, message: MqttMessage) -> None:
        if self._closed or not self._connected:
            return
        try:
            qos = message.qos
            if qos == 0:
                pkt = PublishPacket(
                    topic=message.topic,
                    payload=message.payload,
                    qos=0,
                    retain=message.retain,
                    dup=False,
                )
                self._SendPacket(pkt)
                return
            # QoS 1 or 2: allocate a downstream packet id and track it.
            try:
                pid = self._outbound_pid_alloc.Allocate()
            except Exception:
                self._logger.warning("WireVirtualClient[%s] dropped PUBLISH: outbound pid space exhausted",
                                     self._peer_label)
                return
            pkt = PublishPacket(
                topic=message.topic,
                payload=message.payload,
                qos=qos,
                retain=message.retain,
                dup=False,
                packet_id=pid,
            )
            with self._pending_outbound_lock:
                if qos == 1:
                    self._pending_qos1[pid] = time.time()
                else:
                    self._pending_qos2_step1[pid] = time.time()
            self._SendPacket(pkt)
        except Exception as e:
            self._logger.error("WireVirtualClient[%s] DeliverMessage raised: %s",
                               self._peer_label, e)


    # ---- ack handling for outbound PUBLISH ----

    def _HandlePubAck(self, packet_id: int) -> None:
        with self._pending_outbound_lock:
            if packet_id not in self._pending_qos1:
                raise ProtocolError(f"PUBACK for unknown QoS1 pid={packet_id}")
            self._pending_qos1.pop(packet_id, None)
        self._outbound_pid_alloc.Free(packet_id)


    def _HandlePubRec(self, packet_id: int) -> None:
        with self._pending_outbound_lock:
            if packet_id in self._pending_qos2_step2:
                resend_pubrel = True
            elif packet_id in self._pending_qos2_step1:
                resend_pubrel = False
            else:
                raise ProtocolError(f"PUBREC for unknown QoS2 pid={packet_id}")
            if not resend_pubrel:
                self._pending_qos2_step1.pop(packet_id, None)
                self._pending_qos2_step2[packet_id] = time.time()
        self._SendPacket(PubRelPacket(packet_id=packet_id))


    def _HandlePubComp(self, packet_id: int) -> None:
        with self._pending_outbound_lock:
            if packet_id not in self._pending_qos2_step2:
                raise ProtocolError(f"PUBCOMP for unknown QoS2 pid={packet_id}")
            self._pending_qos2_step2.pop(packet_id, None)
        self._outbound_pid_alloc.Free(packet_id)


    # ---- upstream connection-state ----

    def OnUpstreamConnected(self, handle: VirtualClientHandle) -> None:
        # We only attach after sending CONNACK, so this fires only when the
        # upstream recovers under us. Nothing to do; mux re-issues subs.
        pass


    def OnUpstreamDisconnected(self, handle: VirtualClientHandle, reason: Optional[Exception]) -> None:
        # Per user choice: when upstream drops, we close the downstream too -
        # the peer's reconnect logic will eventually try again and either get
        # accepted (if upstream recovered) or rejected (server-unavailable).
        self._FatalClose()


    # ---- keepalive watchdog ----

    def _KeepaliveLoop(self) -> None:
        # Spec §3.1.2.10: server must disconnect when no message has arrived
        # for 1.5x keepalive. We wake periodically and check.
        deadline_grace = self._keepalive_grace
        wakeup = max(self._keep_alive_sec / 3.0, 1.0)
        while not self._keepalive_stop.is_set():
            if self._keepalive_stop.wait(wakeup):
                return
            if self._closed:
                return
            elapsed = time.time() - self._last_recv_time
            if elapsed > self._keep_alive_sec * deadline_grace:
                self._logger.info("WireVirtualClient[%s] keepalive timeout (%.1fs since last recv); closing",
                                  self._peer_label, elapsed)
                self._FatalClose()
                return


    # ---- helpers ----

    def _SendPacket(self, pkt: MqttPacket) -> None:
        try:
            data = EncodePacket(pkt)
        except Exception as e:
            self._logger.error("WireVirtualClient[%s] encode raised: %s", self._peer_label, e)
            self._FatalClose()
            return
        with self._send_lock:
            try:
                self._SendBytes(data)
            except Exception as e:
                self._logger.debug("WireVirtualClient[%s] send raised: %s", self._peer_label, e)
                self._FatalClose()


    def _FatalClose(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._keepalive_stop.set()
        self._DetachFromMux(publish_will=True)
        try:
            self._CloseTransport()
        except Exception as e:
            self._logger.debug("WireVirtualClient[%s] close transport raised: %s", self._peer_label, e)


    def _RegisterClientId(self, client_id: str) -> Optional["WireVirtualClient"]:
        key = (self._mux.mux_id, client_id)
        with WireVirtualClient._client_id_registry_lock:
            previous = WireVirtualClient._client_id_registry.get(key)
            WireVirtualClient._client_id_registry[key] = self
            self._registered_client_id = client_id
        if previous is self:
            return None
        return previous


    def _UnregisterClientId(self) -> None:
        client_id = self._registered_client_id
        if client_id is None:
            return
        key = (self._mux.mux_id, client_id)
        with WireVirtualClient._client_id_registry_lock:
            if WireVirtualClient._client_id_registry.get(key) is self:
                WireVirtualClient._client_id_registry.pop(key, None)
        self._registered_client_id = None


    def _DetachFromMux(self, publish_will: bool) -> None:
        handle = self._handle
        self._handle = None
        self._UnregisterClientId()
        will = self._will if publish_will else None
        self._will = None
        if will is not None and handle is not None and not handle.IsDetached():
            if not TopicMatcher.ValidateTopicName(will.topic):
                self._logger.warning("WireVirtualClient[%s] dropping invalid will topic=%r",
                                     self._peer_label, will.topic)
            else:
                result = self._mux.Publish(handle, will.topic, will.payload, qos=will.qos, retain=will.retain)
                if not result.success:
                    self._logger.debug("WireVirtualClient[%s] will publish failed for topic=%s",
                                       self._peer_label, will.topic)
        if handle is not None:
            self._mux.Detach(handle)


    # Diagnostics for tests.
    def IsConnected(self) -> bool:
        return self._connected and not self._closed
