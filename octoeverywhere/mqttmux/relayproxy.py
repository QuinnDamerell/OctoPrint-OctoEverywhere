import logging
import threading
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

from ..buffer import Buffer
from ..interfaces import (
    ICommandWebsocketProvider,
    ICommandWebsocketProviderBuilder,
    IWebSocketClient,
    WebSocketOpCode,
)
from .legacyrelayclient import LegacyJsonRelayClient
from .mux import MqttUpstreamMux
from .muxregistry import MqttMuxRegistry
from .wsrelayclient import WebSocketRelayClient

if TYPE_CHECKING:
    # Proto pulls in octoflatbuffers which is a runtime-only dep; keep it
    # out of the import graph for tests.
    from ..Proto.HttpInitialContext import HttpInitialContext  # noqa: F401  # pylint: disable=unused-import


# Union of the two sub-client types this proxy can route to. Both expose the
# same minimal surface (FeedBytes, OnPeerClosed) so the proxy can treat them
# uniformly.
_SubClient = Union[WebSocketRelayClient, LegacyJsonRelayClient]


# The IWebSocketClient that the OE cloud relay plumbing instantiates for each
# inbound MQTT-relay WebSocket. Routes the connection to either the new
# WebSocketRelayClient (standards-compliant MQTT-over-WS, v2) or the legacy
# JSON-envelope LegacyJsonRelayClient (v1, kept for one release) based on a
# peek of the first inbound frame.
#
# Routing rule:
#   * First frame byte == 0x10 (MQTT 3.1.1 CONNECT control packet type 1) ->
#     WebSocketRelayClient.
#   * Anything else (typically a `{` from JSON) -> LegacyJsonRelayClient if
#     legacy is enabled; otherwise close.
#
# The peek is done once; subsequent frames are forwarded to the chosen
# sub-client without inspection.
class MqttRelayWebSocketProxy(IWebSocketClient):

    def __init__(self, logger: logging.Logger, mux: MqttUpstreamMux,
                 stream_id: int, peer_label: str,
                 on_ws_open: Optional[Callable[[IWebSocketClient], None]] = None,
                 on_ws_data: Optional[Callable[[IWebSocketClient, Buffer, WebSocketOpCode], None]] = None,
                 on_ws_close: Optional[Callable[[IWebSocketClient], None]] = None,
                 on_ws_error: Optional[Callable[[IWebSocketClient, Exception], None]] = None) -> None:
        self._logger = logger
        self._mux = mux
        self._stream_id = stream_id
        self._peer_label = peer_label
        self._on_ws_open = on_ws_open
        self._on_ws_data = on_ws_data
        self._on_ws_close = on_ws_close
        self._on_ws_error = on_ws_error

        # Routing state - the sub-client lives here once the first frame
        # has been peeked.
        self._route_lock = threading.Lock()
        self._sub_client: Optional[_SubClient] = None
        self._mode: Optional[str] = None  # "v2" | "v1" | "closed"
        self._closed = False


    # ---- IWebSocketClient ----

    def RunAsync(self) -> None:
        # No upstream connection to make - we share the mux. Fire onWsOpen
        # immediately so the OE plumbing knows we're ready to receive frames.
        if self._on_ws_open is not None:
            try:
                self._on_ws_open(self)
            except Exception as e:
                self._logger.error("%s onWsOpen raised: %s", self._LogPrefix(), e)


    def Send(self, buffer: Buffer, msgStartOffsetBytes: Optional[int] = None,
             msgSize: Optional[int] = None, isData: bool = True) -> None:
        self.SendWithOptCode(buffer, msgStartOffsetBytes, msgSize,
                             WebSocketOpCode.BINARY if isData else WebSocketOpCode.TEXT)


    def SendWithOptCode(self, buffer: Buffer, msgStartOffsetBytes: Optional[int] = None,
                        msgSize: Optional[int] = None, optCode: Any = WebSocketOpCode.BINARY) -> None:
        if self._closed:
            return
        try:
            buf = buffer.GetBytesLike()
            if msgStartOffsetBytes is not None:
                buf = buf[msgStartOffsetBytes:]
            if msgSize is not None:
                buf = buf[:msgSize]
            # Convert memoryview/bytearray to bytes for sub-clients that
            # need to slice/decode.
            if isinstance(buf, (bytearray, memoryview)):
                payload = bytes(buf)
            else:
                payload = buf
        except Exception as e:
            self._logger.error("%s Send buffer extraction raised: %s", self._LogPrefix(), e)
            self._FireError(e)
            return
        sub = self._RouteIfNeeded(payload, optCode)
        if sub is None:
            return
        try:
            sub.FeedBytes(payload)
        except Exception as e:
            self._logger.error("%s sub-client FeedBytes raised: %s", self._LogPrefix(), e)
            self._FireError(e)


    def Close(self) -> None:
        self._InternalClose(reason=None)


    def SetDisableCertCheck(self, disable: bool) -> None:
        # No outbound connection - we don't need cert validation.
        pass


    # This determines what type of websocket connection relay to use, the standard MQTT raw transport or the legacy JSON envelope.
    def _RouteIfNeeded(self, first_chunk: bytes, opt_code: Any) -> Optional[_SubClient]:
        with self._route_lock:
            if self._sub_client is not None:
                return self._sub_client
            if self._closed:
                return None
            # We try to auto detect the mode based on the first frame.
            mode = self._DetectMode(first_chunk)
            if mode == "v2":
                client = WebSocketRelayClient(
                    logger=self._logger,
                    mux=self._mux,
                    peer_label=self._peer_label,
                    send_bytes=self._SendBytesBinary,
                    close_transport=self._CloseTransportFromSub,
                )
                self._sub_client = client
                self._mode = "v2"
                self._logger.info("%s routing to v2 (MQTT-over-WS)", self._LogPrefix())
                return client
            if mode == "v1":
                client = LegacyJsonRelayClient(
                    logger=self._logger,
                    mux=self._mux,
                    peer_label=self._peer_label,
                    send_text=self._SendBytesText,
                    close_transport=self._CloseTransportFromSub,
                )
                self._sub_client = client
                self._mode = "v1"
                self._logger.info("%s routing to v1 (legacy JSON envelope)", self._LogPrefix())
                return client
            # Unrecognized - close.
            self._logger.error("%s unrecognized first frame; closing", self._LogPrefix())
            self._closed = True
            self._FireClose()
            return None


    @staticmethod
    def _DetectMode(first_chunk: bytes) -> Optional[str]:
        if len(first_chunk) == 0:
            return None
        first = first_chunk[0]
        # MQTT 3.1.1 CONNECT fixed-header byte = (1<<4) | 0 = 0x10.
        if first == 0x10:
            return "v2"
        # JSON envelopes always start with `{` after possible whitespace.
        # Permit a small amount of leading whitespace just in case.
        stripped = first_chunk.lstrip()
        if len(stripped) > 0 and stripped[0:1] == b"{":
            return "v1"
        return None



    def _SendBytesBinary(self, data: bytes) -> None:
        if self._closed or self._on_ws_data is None:
            return
        try:
            self._on_ws_data(self, Buffer(data), WebSocketOpCode.BINARY)
        except Exception as e:
            self._logger.debug("%s onWsData(binary) raised: %s", self._LogPrefix(), e)


    def _SendBytesText(self, data: bytes) -> None:
        if self._closed or self._on_ws_data is None:
            return
        try:
            self._on_ws_data(self, Buffer(data), WebSocketOpCode.TEXT)
        except Exception as e:
            self._logger.debug("%s onWsData(text) raised: %s", self._LogPrefix(), e)


    def _CloseTransportFromSub(self) -> None:
        self._InternalClose(reason=None)


    def _InternalClose(self, reason: Optional[Exception]) -> None:
        with self._route_lock:
            if self._closed:
                return
            self._closed = True
            sub = self._sub_client
        if sub is not None:
            try:
                sub.OnPeerClosed()
            except Exception as e:
                self._logger.debug("%s sub OnPeerClosed raised: %s", self._LogPrefix(), e)
        if reason is not None:
            self._FireError(reason)
        self._FireClose()


    def _FireClose(self) -> None:
        if self._on_ws_close is None:
            return
        try:
            self._on_ws_close(self)
        except Exception as e:
            self._logger.debug("%s onWsClose raised: %s", self._LogPrefix(), e)


    def _FireError(self, exc: Exception) -> None:
        if self._on_ws_error is None:
            return
        try:
            self._on_ws_error(self, exc)
        except Exception as e:
            self._logger.debug("%s onWsError raised: %s", self._LogPrefix(), e)


    def _LogPrefix(self) -> str:
        return f"MQTT RELAY [{self._stream_id}]"


# Implements the existing ICommandWebsocketProvider interface so the OE cloud
# plumbing can plug us in unchanged.
class MqttRelayWebSocketProxyProvider(ICommandWebsocketProvider):

    def __init__(self, logger: logging.Logger, mux: MqttUpstreamMux,
                 args: Optional[Dict[str, Any]] = None) -> None:
        self._logger = logger
        self._mux = mux
        # `args` are passed by clients (e.g. legacy v1 user-name overrides).
        # Step-5 v2 path needs no per-connection config so we don't read these
        # today, but keep the parameter for forward compatibility.
        self._args = args


    def GetWebsocketObject(self, streamId: int, path: str, pathType: int, context: "HttpInitialContext",
                           onWsOpen: Optional[Callable[[IWebSocketClient], None]] = None,
                           onWsData: Optional[Callable[[IWebSocketClient, Buffer, WebSocketOpCode], None]] = None,
                           onWsClose: Optional[Callable[[IWebSocketClient], None]] = None,
                           onWsError: Optional[Callable[[IWebSocketClient, Exception], None]] = None,
                           headers: Optional[Dict[str, str]] = None,
                           subProtocolList: Optional[List[str]] = None) -> Optional[IWebSocketClient]:
        peer_label = path or f"stream-{streamId}"
        return MqttRelayWebSocketProxy(
            logger=self._logger,
            mux=self._mux,
            stream_id=streamId,
            peer_label=peer_label,
            on_ws_open=onWsOpen,
            on_ws_data=onWsData,
            on_ws_close=onWsClose,
            on_ws_error=onWsError,
        )


# Top-level builder vendor relay code constructs and registers with
# Compat.SetMqttWebsocketProxyProviderBuilder.
class MqttRelayWebSocketProxyProviderBuilder(ICommandWebsocketProviderBuilder):

    # mux_key: registry key to look the shared MqttUpstreamMux up under at
    # connect time. Looked up lazily because the vendor may construct the
    # builder before the mux registration finishes (it doesn't, in practice,
    # but lazy is more robust).
    # legacy_v1_enabled: gate for the v1 JSON-envelope coexistence path.
    def __init__(self, logger: logging.Logger, mux_key: str) -> None:
        self._logger = logger
        self._mux_key = mux_key


    def GetMuxKey(self) -> str:
        return self._mux_key


    def GetCommandWebsocketProvider(self, args: Optional[Dict[str, Any]]) -> Optional[ICommandWebsocketProvider]:
        key = self.GetMuxKey()
        mux = MqttMuxRegistry.Get(key)
        if mux is None:
            self._logger.warning("MqttRelay builder: no mux registered for key=%r", key)
            return None
        return MqttRelayWebSocketProxyProvider(self._logger, mux, args)
