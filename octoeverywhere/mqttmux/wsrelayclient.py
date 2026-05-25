import logging
from typing import Callable, List, Optional

from .mux import MqttUpstreamMux
from .wireclient import WireVirtualClient


# v2 relay client: a WireVirtualClient driven by raw MQTT 3.1.1 bytes carried
# in WebSocket BINARY frames. The remote app uses a stock MQTT library (paho
# with transport="websockets", MQTT.js, etc.) and gets a normal MQTT session.
#
# The OE cloud WS plumbing owns the actual WebSocket; this class is the bridge
# between that plumbing and the mux. Bytes inbound from the peer arrive via
# FeedBytes() (called by the routing proxy in relayproxy.py); bytes outbound
# to the peer go through the `send_bytes` callable the proxy supplies.
class WebSocketRelayClient(WireVirtualClient):

    def __init__(self, logger: logging.Logger, mux: MqttUpstreamMux, peer_label: str,
                 send_bytes: Callable[[bytes], None],
                 close_transport: Callable[[], None],
                 allowed_protocol_levels: Optional[List[int]] = None) -> None:
        super().__init__(logger, mux, peer_label, allowed_protocol_levels=allowed_protocol_levels)
        self._send_bytes_cb = send_bytes
        self._close_transport_cb = close_transport


    def _SendBytes(self, data: bytes) -> None:
        # Forward to the OE WS's binary-frame sender. The proxy guarantees
        # this is wired up before any incoming bytes are dispatched here.
        self._send_bytes_cb(data)


    def _CloseTransport(self) -> None:
        try:
            self._close_transport_cb()
        except Exception as e:
            self._logger.debug("WebSocketRelayClient close callback raised: %s", e)
