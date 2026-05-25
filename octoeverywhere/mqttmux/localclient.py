import logging
import threading
from typing import Callable, Dict, List, Optional, Tuple, Union

from .mux import (
    IVirtualClient,
    MqttUpstreamMux,
    PublishResult,
    SubscribeResult,
    VirtualClientHandle,
)
from .topicmatch import TopicMatcher
from .types import (
    MessageCallback,
    MqttMessage,
    QoS,
    SubToken,
)


# In-process virtual client. Exposes a paho-like Python API on top of the
# shared MqttUpstreamMux for the Bambu / Elegoo CC2 vendor code (and any
# future plugin internals that need to talk MQTT).
#
# Key differences vs raw paho:
#   * Subscribe takes a callback per token rather than dispatching to a single
#     on_message handler. Multiple subs to overlapping filters each get their
#     own callback fired - which is what the existing vendor code expects when
#     it composes several handlers.
#   * The connection is shared with other downstream consumers (WS relay,
#     local TCP broker). Vendor code doesn't manage paho directly.
#   * Publish and Subscribe block until the upstream ack arrives or timeout,
#     matching the existing BambuClient._Publish semantics.
class LocalPluginClient(IVirtualClient):

    def __init__(self, logger: logging.Logger, mux: MqttUpstreamMux) -> None:
        self._logger = logger
        self._mux = mux
        self._handle: Optional[VirtualClientHandle] = None
        # Token table: a single client may sub to the same filter multiple
        # times with different callbacks. Each gets its own SubToken.
        self._tokens_lock = threading.RLock()
        self._next_seq = 1
        # token -> (filter, qos, callback)
        self._tokens: Dict[SubToken, Tuple[str, int, MessageCallback]] = {}
        # Connection-state listeners.
        self._listeners_lock = threading.RLock()
        self._on_connected: List[Callable[[], None]] = []
        self._on_disconnected: List[Callable[[], None]] = []
        self._connected_event = threading.Event()


    # ----- public API -----

    def Start(self) -> None:
        if self._handle is not None:
            return
        self._handle = self._mux.Attach(self)


    def Stop(self) -> None:
        if self._handle is None:
            return
        self._mux.Detach(self._handle)
        self._handle = None
        with self._tokens_lock:
            self._tokens.clear()


    # Subscribe to filter at the given QoS. The callback is invoked for every
    # matching inbound message (including retained replays on this subscribe).
    # Returns a SubToken the caller uses to Unsubscribe. None on failure.
    def Subscribe(self, filter_: str, qos: int, callback: MessageCallback) -> Optional[SubToken]:
        if self._handle is None:
            self._logger.warning("LocalPluginClient.Subscribe before Start()")
            return None
        if not TopicMatcher.ValidateFilter(filter_):
            self._logger.warning("LocalPluginClient.Subscribe invalid filter: %r", filter_)
            return None
        if qos < QoS.AT_MOST_ONCE or qos > QoS.EXACTLY_ONCE:
            self._logger.warning("LocalPluginClient.Subscribe invalid qos: %s", qos)
            return None
        # Register the token first so a retained-replay DeliverMessage during
        # the Subscribe call can find a callback to fire.
        with self._tokens_lock:
            seq = self._next_seq
            self._next_seq += 1
            token = SubToken(handle_id=self._handle.handle_id, filter=filter_, sequence=seq)
            self._tokens[token] = (filter_, qos, callback)
        # Per-callback callback wrapper isn't passed to the mux - we fan out
        # ourselves on DeliverMessage so all overlapping tokens fire.
        result: SubscribeResult = self._mux.Subscribe(self._handle, filter_, qos, callback=None)
        if result.IsFailure():
            with self._tokens_lock:
                self._tokens.pop(token, None)
            return None
        return token


    def Unsubscribe(self, token: SubToken) -> bool:
        if self._handle is None:
            return False
        with self._tokens_lock:
            entry = self._tokens.pop(token, None)
        if entry is None:
            return False
        filter_, _, _ = entry
        # Only call mux.Unsubscribe if this was the last local token for that
        # filter - otherwise other local callbacks still want it.
        with self._tokens_lock:
            still_has_filter = any(f == filter_ for (f, _q, _cb) in self._tokens.values())
        if not still_has_filter:
            self._mux.Unsubscribe(self._handle, filter_)
        return True


    # Publish a message. Blocks for QoS > 0 until the upstream handshake
    # completes or times out. Returns True on success.
    def Publish(self, topic: str, payload: Union[str, bytes, bytearray], qos: int = 0, retain: bool = False) -> bool:
        if self._handle is None:
            return False
        if not TopicMatcher.ValidateTopicName(topic):
            self._logger.warning("LocalPluginClient.Publish invalid topic: %r", topic)
            return False
        if isinstance(payload, str):
            payload_bytes = payload.encode("utf-8")
        elif isinstance(payload, (bytes, bytearray)):
            payload_bytes = bytes(payload)
        else:
            self._logger.warning("LocalPluginClient.Publish unsupported payload type: %s", type(payload).__name__)
            return False
        result: PublishResult = self._mux.Publish(self._handle, topic, payload_bytes, qos=qos, retain=retain)
        return result.success


    # Blocks until the upstream is connected, or the timeout elapses. Returns
    # True if connected.
    def WaitForConnected(self, timeout_sec: float) -> bool:
        if self._mux.IsUpstreamConnected():
            return True
        return self._connected_event.wait(timeout_sec)


    def IsConnected(self) -> bool:
        return self._mux.IsUpstreamConnected()


    def OnConnected(self, callback: Callable[[], None]) -> None:
        with self._listeners_lock:
            self._on_connected.append(callback)


    def OnDisconnected(self, callback: Callable[[], None]) -> None:
        with self._listeners_lock:
            self._on_disconnected.append(callback)


    # ----- IVirtualClient -----

    def OnUpstreamConnected(self, handle: VirtualClientHandle) -> None:
        self._connected_event.set()
        with self._listeners_lock:
            callbacks = list(self._on_connected)
        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                self._logger.error("LocalPluginClient OnConnected callback raised: %s", e)


    def OnUpstreamDisconnected(self, handle: VirtualClientHandle, reason: Optional[Exception]) -> None:
        self._connected_event.clear()
        with self._listeners_lock:
            callbacks = list(self._on_disconnected)
        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                self._logger.error("LocalPluginClient OnDisconnected callback raised: %s", e)


    def DeliverMessage(self, handle: VirtualClientHandle, message: MqttMessage) -> None:
        # Fan out to every token whose filter matches this topic. The mux's
        # subtable already de-duplicated and demoted QoS per-handle; here we
        # invoke every per-token callback whose filter matches.
        with self._tokens_lock:
            matching = [
                (filter_, cb) for (filter_, _qos, cb) in self._tokens.values()
                if TopicMatcher.Matches(filter_, message.topic)
            ]
        for filter_, cb in matching:
            try:
                cb(message)
            except Exception as e:
                self._logger.error(
                    "LocalPluginClient callback raised for filter=%s topic=%s: %s",
                    filter_, message.topic, e,
                )
