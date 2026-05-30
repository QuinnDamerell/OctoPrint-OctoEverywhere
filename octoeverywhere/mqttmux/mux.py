import itertools
import logging
import ssl
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as mqtt
from paho.mqtt.enums import MQTTErrorCode

from .retainedcache import RetainedCache
from .subtable import SubscriptionTable
from .topicmatch import TopicMatcher
from .types import (
    MqttMessage,
    ProtocolLevel,
    QoS,
    SubAckReturnCode,
)


# Connection context the mux receives from a vendor-supplied provider on every
# connect attempt. Mirrors octoeverywhere.mqttwebsocketproxy.MqttConnectionContext
# but is owned by the mux package to avoid a circular import. Vendor code wraps
# its own context format into one of these.
@dataclass
class MqttConnectionContext:
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    client_id: Optional[str] = None
    use_tls: bool = False
    allow_invalid_cert: bool = False
    transport: str = "tcp"
    keep_alive_sec: int = 60
    websocket_path: Optional[str] = None
    # Optional protocol level for the upstream client. Default 3.1.1 - the only
    # one paho can speak today against our printers.
    protocol_level: int = ProtocolLevel.MQTT_3_1_1


# Optional initial-subscription hook: filters the vendor wants subscribed
# immediately on every connect (e.g. Bambu's device/{SN}/report). The mux
# re-issues these on reconnect before any virtual-client subs.
@dataclass
class InitialSubscription:
    filter: str
    qos: int = 0


# Interface a virtual client implements so the mux can talk to it.
#
# Lifetime: created by the virtual client itself, attached to a mux via
# mux.Attach(client) which returns a VirtualClientHandle. The handle is the
# token the client passes to all mux methods. Detach releases all subscriptions
# owned by this client.
#
# Threading: every callback below is invoked on paho's loop thread. Handlers
# must not call back into the mux while holding their own session locks, and
# must not block paho for long - heavy work should hand off to a worker thread.
class IVirtualClient(ABC):

    # Fired when the upstream is fully connected (paho on_connect with rc=0).
    # Also fired immediately on Attach if the upstream is already connected.
    @abstractmethod
    def OnUpstreamConnected(self, handle: "VirtualClientHandle") -> None:
        pass

    # Fired exactly once per upstream disconnect transition. Subsequent
    # reconnects fire a new OnUpstreamConnected.
    @abstractmethod
    def OnUpstreamDisconnected(self, handle: "VirtualClientHandle", reason: Optional[Exception]) -> None:
        pass

    # Fired for every inbound PUBLISH that matches one of this client's
    # subscriptions. The mux has already de-duplicated and demoted QoS per
    # MQTT 3.1.1 §3.3.5; the message reflects what should be delivered.
    #
    # For wire-based clients, this means encoding a PUBLISH and writing it to
    # their peer. For LocalPluginClient, it fans out to per-token callbacks.
    @abstractmethod
    def DeliverMessage(self, handle: "VirtualClientHandle", message: MqttMessage) -> None:
        pass


# Token returned by Attach. Each virtual client gets a unique handle_id used
# as the subscription-table key.
class VirtualClientHandle:
    __slots__ = ("handle_id", "client", "_detached")

    def __init__(self, handle_id: int, client: IVirtualClient) -> None:
        self.handle_id = handle_id
        self.client = client
        self._detached = False

    def IsDetached(self) -> bool:
        return self._detached

    def _MarkDetached(self) -> None:
        self._detached = True


# Result of mux.Subscribe(). granted_qos is the QoS code the caller should
# put into a SUBACK to send back to its peer:
#   0/1/2 = granted at that level
#   0x80  = failure
@dataclass
class SubscribeResult:
    granted_qos: int

    def IsFailure(self) -> bool:
        return self.granted_qos == SubAckReturnCode.FAILURE


# Result of mux.Publish().
@dataclass
class PublishResult:
    success: bool
    # paho's error code if known. Useful for logging only.
    rc: Optional[int] = None


# The shared upstream MQTT multiplexer.
#
# Owns one paho.mqtt.Client and its connect/reconnect supervisor thread. All
# attached virtual clients share that single connection - so however many local
# TCP/remote WS/in-process consumers the plugin has, the printer sees one.
#
# The class is thread-safe; multiple virtual clients may call Publish/Subscribe
# concurrently from any thread.
class MqttUpstreamMux:
    _next_mux_id = itertools.count(1)

    # connection_context_provider: zero-arg callable returning a fresh context.
    #   Called on every connect attempt so the vendor can update the IP after
    #   a LAN scan or refresh a cloud access token.
    # initial_subscriptions: filters re-subscribed on every connect before
    #   virtual-client subs replay.
    # subscribe_timeout_sec / publish_timeout_sec: how long the synchronous
    #   Subscribe/Publish methods will block waiting for upstream ack.
    # retained_cache_max_entries: LRU bound on the retained cache.
    # client_factory: override paho.mqtt.client.Client - tests use this.
    def __init__(
        self,
        logger: logging.Logger,
        printer_key: str,
        connection_context_provider: Callable[[], MqttConnectionContext],
        initial_subscriptions: Optional[List[InitialSubscription]] = None,
        subscribe_timeout_sec: float = 15.0,
        publish_timeout_sec: float = 20.0,
        retained_cache_max_entries: int = 1024,
        retained_cache_max_payload_bytes: int = 4 * 1024 * 1024,
        client_factory: Optional[Callable[..., "mqtt.Client"]] = None,
        backoff_min_sec: float = 1.0,
        backoff_max_sec: float = 60.0,
    ) -> None:
        self._logger = logger
        self._printer_key = printer_key
        self._context_provider = connection_context_provider
        self._initial_subs = initial_subscriptions or []
        self._subscribe_timeout_sec = subscribe_timeout_sec
        self._publish_timeout_sec = publish_timeout_sec
        self._backoff_min_sec = backoff_min_sec
        self._backoff_max_sec = backoff_max_sec
        self._client_factory = client_factory or mqtt.Client
        self.mux_id: int = next(MqttUpstreamMux._next_mux_id)

        # Shared state - all touches under StateLock.
        self._state_lock = threading.RLock()
        self._handles: Dict[int, VirtualClientHandle] = {}
        self._next_handle_id = 1
        self._sub_table = SubscriptionTable()
        self._retained = RetainedCache(max_entries=retained_cache_max_entries, max_payload_bytes=retained_cache_max_payload_bytes)
        self._is_connected = False
        self._is_shutdown = False
        self._last_context: Optional[MqttConnectionContext] = None

        # paho client + reconnect wakeup signal.
        self._client: Optional[mqtt.Client] = None
        # The supervisor thread sleeps on this between attempts. Set() bumps it.
        self._wake_event = threading.Event()
        # Set whenever the active paho client transitions to disconnected.
        self._disconnect_event = threading.Event()

        # Pending operations awaiting their paho ack callback. Each holds an
        # Event the issuing thread waits on plus a slot to receive the result.
        self._pending_subs_lock = threading.Lock()
        self._pending_subs: Dict[int, _PendingSubscribe] = {}      # mid -> pending
        self._pending_unsubs: Dict[int, _PendingUnsubscribe] = {}  # mid -> pending
        # Secondary index by filter so a concurrent Subscribe to the same
        # filter can wait for the in-flight subscribe instead of synthesizing
        # a (potentially false) success against a subtable entry that may
        # still be rolled back if the SUBACK comes back as a failure.
        self._pending_subs_by_filter: Dict[str, _PendingSubscribe] = {}

        # Optional callback for the host process to know when connection
        # state changes (used by Bambu's LocalWebApi notification).
        self._on_connection_state_changed: Optional[Callable[[bool], None]] = None

        # Supervisor thread.
        self._supervisor_thread: Optional[threading.Thread] = None


    # ----- lifecycle -----

    # Spawn the supervisor thread. The mux is otherwise inert until this is
    # called. Returns immediately; the actual connect happens asynchronously.
    def Start(self) -> None:
        with self._state_lock:
            if self._is_shutdown:
                raise RuntimeError("Cannot Start a shutdown mux")
            if self._supervisor_thread is not None:
                return
            self._supervisor_thread = threading.Thread(
                target=self._SupervisorLoop,
                name=f"MqttMuxSupervisor[{self._printer_key}]",
                daemon=True,
            )
            self._supervisor_thread.start()


    # Stop the supervisor, disconnect the paho client, detach all handles.
    def Shutdown(self) -> None:
        with self._state_lock:
            if self._is_shutdown:
                return
            self._is_shutdown = True
        self._wake_event.set()
        self._disconnect_event.set()
        client = None
        with self._state_lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.disconnect()
            except Exception as e:
                self._logger.debug("MqttMux disconnect during shutdown: %s", e)
            try:
                client.loop_stop()
            except Exception as e:
                self._logger.debug("MqttMux loop_stop during shutdown: %s", e)


    # Bump the supervisor: if it's sleeping after a failed attempt, wake it
    # up to try again right now. Mirrors BambuClient.SleepEvent.set().
    def WakeReconnect(self) -> None:
        self._wake_event.set()


    # Force the active upstream connection to drop. The supervisor will
    # reconnect automatically (unless we've been shut down). Used by vendor
    # code that detects an application-level liveness failure (e.g. Elegoo
    # CC2 PONG timeout) and wants a fresh handshake.
    def ForceReconnect(self) -> None:
        with self._state_lock:
            if self._is_shutdown:
                return
            client = self._client
        if client is None:
            self._wake_event.set()
            return
        try:
            client.disconnect()
        except Exception as e:
            self._logger.debug("MqttMux ForceReconnect disconnect raised: %s", e)
        # Bump the wake event so even if disconnect was silent we'll retry soon.
        self._wake_event.set()


    def IsUpstreamConnected(self) -> bool:
        with self._state_lock:
            return self._is_connected


    def GetLastConnectionContext(self) -> Optional[MqttConnectionContext]:
        with self._state_lock:
            return self._last_context


    def SetConnectionStateChangedCallback(self, cb: Callable[[bool], None]) -> None:
        with self._state_lock:
            self._on_connection_state_changed = cb


    # ----- attach / detach -----

    def Attach(self, client: IVirtualClient) -> VirtualClientHandle:
        # Fire OnUpstreamConnected under the lock when we're currently
        # connected. Doing it outside the lock would race against paho
        # transitioning the state (the client could see
        # Disconnected->Connected->Connected, with the second Connected
        # arriving after a quick reconnect that the late Attach fire stomps
        # over). The callback contract says callers should not do heavy work
        # in OnUpstreamConnected; they should spawn a thread - which keeps
        # the lock-held duration short.
        with self._state_lock:
            if self._is_shutdown:
                raise RuntimeError("Cannot Attach to a shutdown mux")
            handle = VirtualClientHandle(self._next_handle_id, client)
            self._next_handle_id += 1
            self._handles[handle.handle_id] = handle
            if self._is_connected:
                try:
                    client.OnUpstreamConnected(handle)
                except Exception as e:
                    self._logger.error("Virtual client OnUpstreamConnected raised: %s", e)
        return handle


    def Detach(self, handle: VirtualClientHandle) -> None:
        if handle.IsDetached():
            return
        with self._state_lock:
            self._handles.pop(handle.handle_id, None)
            handle._MarkDetached()  # pylint: disable=protected-access  # mux owns the handle's lifetime
            now_empty = self._sub_table.DetachHandle(handle.handle_id)
            paho_client = self._client
            is_connected = self._is_connected
        # Issue real upstream unsubscribes for any filter whose refcount hit
        # zero. We don't block on these acks - detach is fire-and-forget.
        if paho_client is not None and is_connected:
            for filter_ in now_empty:
                try:
                    paho_client.unsubscribe(filter_)
                except Exception as e:
                    self._logger.debug("MqttMux Detach unsubscribe(%s) failed: %s", filter_, e)


    # ----- public publish/subscribe API for virtual clients -----

    # Subscribe handle to filter at the given downstream QoS. Blocks until the
    # upstream SUBACK arrives (or the synthesized one is immediately available)
    # or timeout. Caller supplies the optional callback for in-process clients;
    # wire clients pass None.
    def Subscribe(self, handle: VirtualClientHandle, filter_: str, qos: int,
                  callback: Optional[Callable[[MqttMessage], None]] = None,
                  subscription_identifier: Optional[int] = None) -> SubscribeResult:
        if handle.IsDetached():
            return SubscribeResult(granted_qos=SubAckReturnCode.FAILURE)
        if not TopicMatcher.ValidateFilter(filter_) or qos < QoS.AT_MOST_ONCE or qos > QoS.EXACTLY_ONCE:
            return SubscribeResult(granted_qos=SubAckReturnCode.FAILURE)

        # Serialize concurrent Subscribes for the same filter. Without this,
        # a second subscriber that lands while the first is still waiting on
        # its real SUBACK would see the first's subtable entry and get a
        # synthesized "success" SUBACK - and if the first's SUBACK comes
        # back as failure, the rollback would leave the second believing it
        # is subscribed when nothing is flowing from upstream.
        while True:
            with self._pending_subs_lock:
                in_flight = self._pending_subs_by_filter.get(filter_)
                if in_flight is None:
                    break
                wait_pending = in_flight
            if not wait_pending.finalized_event.wait(timeout=self._subscribe_timeout_sec):
                # Earlier in-flight subscribe timed out. Bail rather than
                # pile up on top of it.
                return SubscribeResult(granted_qos=SubAckReturnCode.FAILURE)

        outcome = self._sub_table.Subscribe(handle.handle_id, filter_, qos, callback, subscription_identifier)
        if outcome.synthesized_granted_qos is not None:
            # Already subscribed upstream at >= the requested QoS.
            # Replay any cached retained messages to just this subscriber.
            self._ReplayRetainedTo(handle, filter_, outcome.synthesized_granted_qos)
            return SubscribeResult(granted_qos=outcome.synthesized_granted_qos)

        # Need a real upstream subscribe.
        paho_client = self._GetConnectedClient()
        if paho_client is None:
            # Roll back the subtable entry; we never actually subscribed.
            self._sub_table.Unsubscribe(handle.handle_id, filter_)
            return SubscribeResult(granted_qos=SubAckReturnCode.FAILURE)

        pending = _PendingSubscribe(filter_=filter_, requested_qos=outcome.upstream_qos)
        # Hold _pending_subs_lock across the paho subscribe call. paho's
        # subscribe() returns the mid synchronously after queuing the packet;
        # if the SUBACK arrives on paho's network thread before we install
        # the pending entry, _OnPahoSubscribe blocks on this same lock until
        # we release - so the pending entry is guaranteed to be installed
        # when _OnPahoSubscribe runs its pop.
        with self._pending_subs_lock:
            try:
                result, mid = paho_client.subscribe(filter_, qos=outcome.upstream_qos)
            except Exception as e:
                self._logger.error("MqttMux paho subscribe(%s) raised: %s", filter_, e)
                self._sub_table.Unsubscribe(handle.handle_id, filter_)
                return SubscribeResult(granted_qos=SubAckReturnCode.FAILURE)
            if result != MQTTErrorCode.MQTT_ERR_SUCCESS or mid is None:
                self._logger.warning("MqttMux paho subscribe(%s) returned rc=%s mid=%s",
                                     filter_, result, mid)
                self._sub_table.Unsubscribe(handle.handle_id, filter_)
                return SubscribeResult(granted_qos=SubAckReturnCode.FAILURE)
            self._pending_subs[mid] = pending
            self._pending_subs_by_filter[filter_] = pending

        # Wait for SUBACK.
        if not pending.event.wait(timeout=self._subscribe_timeout_sec):
            self._logger.warning("MqttMux SUBACK timeout for filter=%s mid=%s", filter_, mid)
            with self._pending_subs_lock:
                self._pending_subs.pop(mid, None)
            # Wake any other waiters so they don't sit on the deadline.
            pending.event.set()
            # Roll back the virtual subscription. The upstream broker may still
            # later accept the SUBSCRIBE, but with no downstream subscriber in
            # the table those PUBLISHes won't be delivered to a client that saw
            # SUBACK failure.
            self._sub_table.Unsubscribe(handle.handle_id, filter_)
            self._FinalizePendingSubscribe(filter_, pending)
            return SubscribeResult(granted_qos=SubAckReturnCode.FAILURE)

        granted = pending.granted_qos
        if granted == SubAckReturnCode.FAILURE:
            self._sub_table.Unsubscribe(handle.handle_id, filter_)
            self._FinalizePendingSubscribe(filter_, pending)
            return SubscribeResult(granted_qos=SubAckReturnCode.FAILURE)
        # Update the table to the actually-granted QoS.
        self._sub_table.UpdateGrantedQos(filter_, granted)
        self._FinalizePendingSubscribe(filter_, pending)
        self._ReplayRetainedTo(handle, filter_, granted)
        return SubscribeResult(granted_qos=granted)


    def Unsubscribe(self, handle: VirtualClientHandle, filter_: str) -> bool:
        if handle.IsDetached():
            return True
        outcome = self._sub_table.Unsubscribe(handle.handle_id, filter_)
        if not outcome.needs_upstream_unsubscribe:
            return outcome.removed_any
        paho_client = self._GetConnectedClient()
        if paho_client is None:
            # Connection's gone; the supervisor's re-subscribe-on-reconnect logic
            # will skip this filter since the table no longer has it.
            return True
        pending = _PendingUnsubscribe()
        # Same race-prevention pattern as Subscribe: install pending under
        # the lock around the paho call.
        with self._pending_subs_lock:
            try:
                result, mid = paho_client.unsubscribe(filter_)
            except Exception as e:
                self._logger.debug("MqttMux paho unsubscribe(%s) raised: %s", filter_, e)
                return True
            if result != MQTTErrorCode.MQTT_ERR_SUCCESS or mid is None:
                return True
            self._pending_unsubs[mid] = pending
        # Best-effort wait; unsubscribe success is rarely interesting.
        pending.event.wait(timeout=self._subscribe_timeout_sec)
        with self._pending_subs_lock:
            self._pending_unsubs.pop(mid, None)
        return True


    # Publish from a virtual client. For QoS 0 returns as soon as paho accepts.
    # For QoS 1/2 blocks for the upstream handshake to complete or timeout.
    def Publish(self, handle: VirtualClientHandle, topic: str, payload: bytes,
                qos: int = 0, retain: bool = False) -> PublishResult:
        if handle.IsDetached():
            return PublishResult(success=False)
        if not TopicMatcher.ValidateTopicName(topic) or qos < QoS.AT_MOST_ONCE or qos > QoS.EXACTLY_ONCE:
            return PublishResult(success=False)
        paho_client = self._GetConnectedClient()
        if paho_client is None:
            return PublishResult(success=False)
        try:
            info = paho_client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as e:
            self._logger.error("MqttMux paho publish(%s) raised: %s", topic, e)
            return PublishResult(success=False)
        if qos == QoS.AT_MOST_ONCE:
            success = info.rc == MQTTErrorCode.MQTT_ERR_SUCCESS
            if success:
                self._UpdateRetainedFromDownstreamPublish(topic, payload, qos, retain)
            return PublishResult(success=success, rc=info.rc)
        # QoS > 0: block for the handshake.
        try:
            info.wait_for_publish(timeout=self._publish_timeout_sec)
        except Exception as e:
            self._logger.warning("MqttMux publish(%s) wait raised: %s", topic, e)
            return PublishResult(success=False, rc=info.rc)
        success = info.is_published()
        if success:
            self._UpdateRetainedFromDownstreamPublish(topic, payload, qos, retain)
        return PublishResult(success=success, rc=info.rc)


    # Returns the active paho client iff we're currently connected, else None.
    def _GetConnectedClient(self) -> Optional["mqtt.Client"]:
        with self._state_lock:
            if not self._is_connected or self._client is None:
                return None
            return self._client


    def _FinalizePendingSubscribe(self, filter_: str, pending: "_PendingSubscribe") -> None:
        with self._pending_subs_lock:
            if self._pending_subs_by_filter.get(filter_) is pending:
                self._pending_subs_by_filter.pop(filter_, None)
        pending.finalized_event.set()


    def _SupervisorLoop(self) -> None:
        backoff = self._backoff_min_sec
        while True:
            with self._state_lock:
                if self._is_shutdown:
                    return
            try:
                self._ConnectOnce()
                # Connect succeeded - reset backoff and wait for disconnect.
                backoff = self._backoff_min_sec
                self._disconnect_event.wait()
                self._disconnect_event.clear()
            except Exception as e:
                self._logger.warning("MqttMux connect attempt failed: %s", e)
            # If we were shut down, exit.
            with self._state_lock:
                if self._is_shutdown:
                    return
            # Sleep before next attempt, with the wake-event giving an early
            # exit when something tries to use the connection.
            self._logger.info("MqttMux sleeping %.1fs before reconnect", backoff)
            woken = self._wake_event.wait(backoff)
            self._wake_event.clear()
            if woken:
                # Reset backoff so a user action gets a quick retry.
                backoff = self._backoff_min_sec
            else:
                backoff = min(backoff * 2, self._backoff_max_sec)


    def _ConnectOnce(self) -> None:
        ctx = self._context_provider()
        with self._state_lock:
            self._last_context = ctx
        transport = ctx.transport
        if transport not in ("tcp", "websockets"):
            raise ValueError(f"MqttMux unsupported transport: {transport}")
        client_id = ctx.client_id if ctx.client_id is not None else ""
        try:
            client = self._client_factory(
                mqtt.CallbackAPIVersion.VERSION2,  # type: ignore[attr-defined]
                client_id=client_id,
                transport=transport,
                reconnect_on_failure=False,
            )
        except TypeError:
            # Some tests/integration hooks provide small paho-compatible
            # factories that predate this kwarg. Real paho supports it.
            client = self._client_factory(
                mqtt.CallbackAPIVersion.VERSION2,  # type: ignore[attr-defined]
                client_id=client_id,
                transport=transport,
            )
        # Aggressive reconnect bounds at the paho level shouldn't matter
        # because we drive reconnect ourselves, but set them defensively.
        client.reconnect_delay_set(min_delay=1, max_delay=5)
        client.on_connect = self._OnPahoConnect
        client.on_disconnect = self._OnPahoDisconnect
        client.on_message = self._OnPahoMessage
        client.on_subscribe = self._OnPahoSubscribe
        client.on_unsubscribe = self._OnPahoUnsubscribe
        client.on_log = self._OnPahoLog
        if ctx.use_tls:
            if ctx.allow_invalid_cert:
                tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                tls_ctx.check_hostname = False
                tls_ctx.verify_mode = ssl.CERT_NONE
                client.tls_set_context(tls_ctx)  #pyright: ignore[reportUnknownMemberType]
                client.tls_insecure_set(True)
            else:
                client.tls_set()  #pyright: ignore[reportUnknownMemberType]
        if transport == "websockets" and ctx.websocket_path is not None:
            client.ws_set_options(path=ctx.websocket_path)
        if ctx.username is not None or ctx.password is not None:
            client.username_pw_set(ctx.username, ctx.password)
        self._logger.info("MqttMux connecting to %s:%s (transport=%s)", ctx.host, ctx.port, transport)
        # connect() blocks for the TCP handshake but not for CONNACK; loop_start
        # spins up paho's network thread which then processes CONNACK and fires
        # on_connect.
        try:
            client.connect(ctx.host, int(ctx.port), keepalive=ctx.keep_alive_sec)
            client.loop_start()
        except Exception:
            try:
                client.loop_stop()
            except Exception:
                pass
            raise
        # Install only after connect + loop_start succeed so a failed attempt
        # doesn't leave a half-initialized client visible to other threads.
        with self._state_lock:
            self._client = client


    def _IsActivePahoClient(self, client: Any) -> bool:
        with self._state_lock:
            return self._client is client and not self._is_shutdown


    def _OnPahoConnect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        if not self._IsActivePahoClient(client):
            return
        try:
            if reason_code is not None and getattr(reason_code, "is_failure", False):
                self._logger.warning("MqttMux upstream connect refused: %s", reason_code)
                # Signal supervisor to retry; mark disconnected.
                with self._state_lock:
                    self._is_connected = False
                self._disconnect_event.set()
                return
        except AttributeError:
            pass
        self._logger.info("MqttMux upstream connected")
        with self._state_lock:
            self._is_connected = True
            handles_snapshot = list(self._handles.values())
            initial_subs = list(self._initial_subs)
            sub_table_snapshot = self._sub_table.SnapshotFilters()
            on_state = self._on_connection_state_changed
        # Replay initial subscriptions first so vendor topics are live before
        # any dynamic ones. These don't have any per-handle subscribers - they
        # are just held by the upstream to keep the stream flowing.
        for sub in initial_subs:
            try:
                result, _mid = client.subscribe(sub.filter, qos=sub.qos)
                if result != MQTTErrorCode.MQTT_ERR_SUCCESS:
                    self._logger.warning("MqttMux initial subscribe(%s) returned rc=%s", sub.filter, result)
            except Exception as e:
                self._logger.warning("MqttMux initial subscribe(%s) failed: %s", sub.filter, e)
        # Replay every refcounted virtual-client subscription. We don't wait
        # for SUBACKs here - they'll arrive on the paho thread; pending
        # Subscribe()s from synchronous callers don't exist post-reconnect
        # because reconnect tears down the connection that caller waited on.
        for filter_, qos in sub_table_snapshot:
            try:
                result, _mid = client.subscribe(filter_, qos=qos)
                if result != MQTTErrorCode.MQTT_ERR_SUCCESS:
                    self._logger.warning("MqttMux replay subscribe(%s) returned rc=%s", filter_, result)
            except Exception as e:
                self._logger.warning("MqttMux replay subscribe(%s) failed: %s", filter_, e)
        # Wipe the retained cache - on a fresh upstream connection any cached
        # state may be stale; let the printer refresh it through fresh retained
        # PUBLISHes.
        self._retained.Clear()
        # Notify host if a callback was registered.
        if on_state is not None:
            try:
                on_state(True)
            except Exception as e:
                self._logger.error("MqttMux connection-state callback raised: %s", e)
        # Fan out OnUpstreamConnected to attached virtual clients.
        for h in handles_snapshot:
            try:
                h.client.OnUpstreamConnected(h)
            except Exception as e:
                self._logger.error("Virtual client OnUpstreamConnected raised: %s", e)


    def _OnPahoDisconnect(self, client: Any, userdata: Any, disconnect_flags: Any,
                          reason_code: Any, properties: Any) -> None:
        if not self._IsActivePahoClient(client):
            return
        self._logger.info("MqttMux upstream disconnected: %s", reason_code)
        with self._state_lock:
            was_connected = self._is_connected
            self._is_connected = False
            handles_snapshot = list(self._handles.values())
            on_state = self._on_connection_state_changed
            # Wake any pending Subscribe/Unsubscribe waiters so they don't hang.
        with self._pending_subs_lock:
            for pending in self._pending_subs.values():
                pending.granted_qos = SubAckReturnCode.FAILURE
                pending.event.set()
            self._pending_subs.clear()
            for pending_u in self._pending_unsubs.values():
                pending_u.event.set()
            self._pending_unsubs.clear()
            for pending_f in self._pending_subs_by_filter.values():
                pending_f.finalized_event.set()
            self._pending_subs_by_filter.clear()
        if was_connected:
            if on_state is not None:
                try:
                    on_state(False)
                except Exception as e:
                    self._logger.error("MqttMux connection-state callback raised: %s", e)
            for h in handles_snapshot:
                try:
                    h.client.OnUpstreamDisconnected(h, None)
                except Exception as e:
                    self._logger.error("Virtual client OnUpstreamDisconnected raised: %s", e)
        # Tell supervisor to reconnect (unless shutdown).
        self._disconnect_event.set()


    def _OnPahoMessage(self, client: Any, userdata: Any, mqtt_msg: Any) -> None:
        if not self._IsActivePahoClient(client):
            return
        # Build our internal MqttMessage. paho's mqtt_msg.topic is already a
        # str; payload is bytes.
        topic = mqtt_msg.topic
        payload = mqtt_msg.payload if mqtt_msg.payload is not None else b""
        msg = MqttMessage(
            topic=topic,
            payload=payload,
            qos=mqtt_msg.qos,
            retain=bool(mqtt_msg.retain),
            packet_id=mqtt_msg.mid if mqtt_msg.qos > 0 else None,
        )
        # Update retained cache if applicable. Note: paho already strips the
        # retain flag for messages we received as a "live" publish (the broker
        # only sets retain=1 on the very first delivery to a new subscriber).
        if msg.retain:
            self._retained.OnRetainedPublish(msg)
        else:
            self._retained.OnLivePublishForCachedTopic(msg)
        # Snapshot the dispatch list, then deliver outside the table lock.
        matches = self._sub_table.GetMatchingSubscribers(topic)
        with self._state_lock:
            handles_by_id = {h.handle_id: h for h in self._handles.values()}
        for match in matches:
            handle = handles_by_id.get(match.handle_id)
            if handle is None or handle.IsDetached():
                continue
            delivery_qos = min(msg.qos, match.qos)
            # Spec: live (non-retained-replay) messages must be delivered with
            # retain=0. We forward msg.retain as-is here - the wire client
            # treats retain on inbound from upstream as "this was a live
            # retained delivery, so propagate retain=true to the new sub". For
            # ongoing broadcasts (retain=0 on the wire from the printer) that
            # bit is already 0.
            delivery = MqttMessage(
                topic=msg.topic,
                payload=msg.payload,
                qos=delivery_qos,
                retain=msg.retain,
                packet_id=None,  # downstream picks its own packet id when needed
            )
            try:
                handle.client.DeliverMessage(handle, delivery)
            except Exception as e:
                self._logger.error("Virtual client DeliverMessage raised: %s", e)


    def _OnPahoSubscribe(self, client: Any, userdata: Any, mid: Any,
                        reason_code_list: List[Any], properties: Any) -> None:
        if not self._IsActivePahoClient(client):
            return
        # paho v2 callback: reason_code_list is a list of ReasonCode. For 3.1.1
        # the .value is one of 0/1/2/0x80.
        granted = SubAckReturnCode.FAILURE
        for rc in reason_code_list:
            try:
                value = int(rc.value)
            except (AttributeError, TypeError):
                value = SubAckReturnCode.FAILURE
            # SUBSCRIBE issued by the mux always has a single filter so a
            # single rc; take the first non-failure as authoritative.
            granted = value
            break
        with self._pending_subs_lock:
            pending = self._pending_subs.pop(mid, None)
        if pending is None:
            # An async/initial subscribe (no waiter) - record granted QoS.
            return
        pending.granted_qos = granted
        pending.event.set()


    def _OnPahoUnsubscribe(self, client: Any, userdata: Any, mid: Any,
                          reason_code_list: List[Any], properties: Any) -> None:
        if not self._IsActivePahoClient(client):
            return
        with self._pending_subs_lock:
            pending = self._pending_unsubs.pop(mid, None)
        if pending is not None:
            pending.event.set()


    def _OnPahoLog(self, client: Any, userdata: Any, level: int, msg: str) -> None:
        # Forward paho's chatter to our logger at appropriate levels.
        if level == mqtt.MQTT_LOG_ERR:
            self._logger.error("paho: %s", msg)
        elif level == mqtt.MQTT_LOG_WARNING:
            self._logger.warning("paho: %s", msg)


    def _ReplayRetainedTo(self, handle: VirtualClientHandle, filter_: str, granted_qos: int) -> None:
        if granted_qos == SubAckReturnCode.FAILURE:
            return
        matched = self._retained.GetMatching(filter_)
        if not matched:
            return
        for m in matched:
            delivery_qos = min(m.qos, granted_qos)
            delivery = MqttMessage(
                topic=m.topic,
                payload=m.payload,
                qos=delivery_qos,
                retain=True,  # retained replay: subscriber expects retain=1
                packet_id=None,
            )
            try:
                handle.client.DeliverMessage(handle, delivery)
            except Exception as e:
                self._logger.error("Retained replay DeliverMessage raised: %s", e)


    def _UpdateRetainedFromDownstreamPublish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        if not retain:
            return
        self._retained.OnRetainedPublish(MqttMessage(
            topic=topic,
            payload=payload,
            qos=qos,
            retain=True,
            packet_id=None,
        ))


# Internal: state for an in-flight Subscribe waiting on its SUBACK.
class _PendingSubscribe:
    __slots__ = ("filter_", "requested_qos", "event", "finalized_event", "granted_qos")
    def __init__(self, filter_: str, requested_qos: int) -> None:
        self.filter_ = filter_
        self.requested_qos = requested_qos
        self.event = threading.Event()
        # Set by the issuing Subscribe call after it has updated or rolled
        # back the subscription table. Other same-filter subscribers wait on
        # this instead of the raw SUBACK event so they never re-evaluate
        # against half-finalized state.
        self.finalized_event = threading.Event()
        # int rather than SubAckReturnCode so callers can store either 0/1/2
        # for granted or 0x80 for failure without a type mismatch.
        self.granted_qos: int = int(SubAckReturnCode.FAILURE)


class _PendingUnsubscribe:
    __slots__ = ("event",)
    def __init__(self) -> None:
        self.event = threading.Event()
