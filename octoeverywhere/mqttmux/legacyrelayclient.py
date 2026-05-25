import base64
import itertools
import json
import logging
import threading
from typing import Any, Callable, Dict, Optional

from .mux import (
    IVirtualClient,
    MqttUpstreamMux,
    PublishResult,
    SubscribeResult,
    VirtualClientHandle,
)
from .types import MqttMessage, SubAckReturnCode


# Legacy v1 JSON-envelope relay client.
#
# Reproduces the wire protocol of octoeverywhere/mqttwebsocketproxy.py
# (`{"Type": "publish", "Topic": "...", "Payload": "<base64>", ...}`) but
# multiplexes through the shared MqttUpstreamMux instead of opening its own
# paho connection. Kept alive for one release behind a config gate so existing
# third-party apps using the JSON envelope keep working while clients migrate
# to the new MQTT-over-WS path (WebSocketRelayClient).
#
# Inbound JSON envelope shapes:
#   {"Type": "subscribe"|"unsubscribe", "Topic": "<filter>", "Id": <int?>,
#    "NoAck": <bool?>}
#   {"Type": "publish", "Topic": "<topic>", "Payload": "<base64>",
#    "Id": <int?>, "NoAck": <bool?>}
#
# Outbound JSON envelope shapes:
#   {"Type": "subscribe_ack"|"unsubscribe_ack"|"publish_ack",
#    "AckResult": bool, "MqttMessageId": <int>, "Id": <int?>}
#   {"Type": "on_subscribe"|"on_unsubscribe", "MqttMessageId": <int>,
#    "ReasonCodeList": [<int>...], "Id": <int?>}
#   {"Type": "on_message", "MqttMessageId": <int>, "Payload": "<base64>"}
#
# Process-wide outgoing message-id counter for synthesized MqttMessageId
# fields. Real paho mids aren't observable to virtual clients, so we
# generate our own to keep the envelope shape stable.
_GLOBAL_MID = itertools.count(1)


def _NextSyntheticMid() -> int:
    return next(_GLOBAL_MID)


class LegacyJsonRelayClient(IVirtualClient):

    def __init__(self, logger: logging.Logger, mux: MqttUpstreamMux, peer_label: str,
                 send_text: Callable[[bytes], None],
                 close_transport: Callable[[], None]) -> None:
        self._logger = logger
        self._mux = mux
        self._peer_label = peer_label
        self._send_text = send_text
        self._close_transport = close_transport
        self._handle: Optional[VirtualClientHandle] = None
        self._closed = False
        self._send_lock = threading.Lock()

        # Attach immediately - the legacy protocol has no CONNECT handshake.
        # If the upstream is down right now, attach still succeeds; we'll get
        # OnUpstreamDisconnected and close on whichever side trips first.
        self._handle = self._mux.Attach(self)


    # ---- public entry points for the proxy ----

    # Inbound bytes from the peer (a JSON envelope).
    def FeedBytes(self, data: bytes) -> None:
        if self._closed or self._handle is None:
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            self._logger.warning("LegacyJsonRelay[%s] invalid UTF-8: %s", self._peer_label, e)
            self._FatalClose()
            return
        try:
            msg = json.loads(text)
        except Exception as e:
            self._logger.warning("LegacyJsonRelay[%s] invalid JSON: %s", self._peer_label, e)
            self._FatalClose()
            return
        if not isinstance(msg, dict):
            self._logger.warning("LegacyJsonRelay[%s] envelope is not a JSON object", self._peer_label)
            self._FatalClose()
            return
        try:
            self._HandleEnvelope(msg)
        except Exception as e:
            self._logger.error("LegacyJsonRelay[%s] envelope handler raised: %s",
                               self._peer_label, e)
            self._FatalClose()


    def OnPeerClosed(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle = self._handle
        self._handle = None
        if handle is not None:
            self._mux.Detach(handle)


    # ---- IVirtualClient ----

    def OnUpstreamConnected(self, handle: VirtualClientHandle) -> None:
        # The legacy protocol has no signal for "upstream is back"; the peer
        # decides when to re-subscribe.
        pass


    def OnUpstreamDisconnected(self, handle: VirtualClientHandle, reason: Optional[Exception]) -> None:
        # When the printer drops, terminate the relay session so the remote
        # app sees the failure and reconnects (matching the existing v1
        # behavior where each WS got its own paho).
        self._FatalClose()


    def DeliverMessage(self, handle: VirtualClientHandle, message: MqttMessage) -> None:
        if self._closed:
            return
        try:
            payload_b64 = base64.b64encode(message.payload).decode("utf-8")
            envelope: Dict[str, Any] = {
                "Type": "on_message",
                "Payload": payload_b64,
            }
            if message.packet_id is not None:
                envelope["MqttMessageId"] = message.packet_id
            else:
                envelope["MqttMessageId"] = _NextSyntheticMid()
            self._SendEnvelope(envelope)
        except Exception as e:
            self._logger.error("LegacyJsonRelay[%s] DeliverMessage raised: %s",
                               self._peer_label, e)
            self._FatalClose()


    def _HandleEnvelope(self, msg: Dict[str, Any]) -> None:
        proxy_msg_type_raw = msg.get("Type", None)
        if not isinstance(proxy_msg_type_raw, str):
            self._logger.warning("LegacyJsonRelay[%s] envelope missing Type", self._peer_label)
            self._FatalClose()
            return
        proxy_msg_type = proxy_msg_type_raw.lower()

        topic = msg.get("Topic", None)
        if not isinstance(topic, str):
            self._logger.warning("LegacyJsonRelay[%s] envelope missing Topic", self._peer_label)
            self._FatalClose()
            return
        proxy_msg_id = msg.get("Id", None)
        if proxy_msg_id is not None and not isinstance(proxy_msg_id, int):
            self._logger.warning("LegacyJsonRelay[%s] envelope Id is not an int", self._peer_label)
            self._FatalClose()
            return
        no_ack_raw = msg.get("NoAck", False)
        if not isinstance(no_ack_raw, bool):
            self._logger.warning("LegacyJsonRelay[%s] envelope NoAck is not a bool", self._peer_label)
            self._FatalClose()
            return
        no_ack: bool = no_ack_raw

        if proxy_msg_type == "subscribe":
            self._DoSubscribe(topic, proxy_msg_id, no_ack)
        elif proxy_msg_type == "unsubscribe":
            self._DoUnsubscribe(topic, proxy_msg_id, no_ack)
        elif proxy_msg_type == "publish":
            self._DoPublish(topic, msg, proxy_msg_id, no_ack)
        else:
            self._logger.warning("LegacyJsonRelay[%s] unknown envelope Type: %s",
                                 self._peer_label, proxy_msg_type)
            self._FatalClose()


    def _DoSubscribe(self, topic: str, proxy_msg_id: Optional[int], no_ack: bool) -> None:
        # Spawn a worker so the JSON parser thread isn't blocked on the
        # upstream SUBACK (which can take a moment).
        def worker():
            if self._handle is None:
                return
            try:
                result: SubscribeResult = self._mux.Subscribe(self._handle, topic, qos=0, callback=None)
            except Exception as e:
                self._logger.error("LegacyJsonRelay[%s] subscribe raised: %s",
                                   self._peer_label, e)
                return
            mid = _NextSyntheticMid()
            success = not result.IsFailure()
            if not no_ack:
                self._SendEnvelope({
                    "Type": "subscribe_ack",
                    "AckResult": success,
                    "MqttMessageId": mid,
                    "Id": proxy_msg_id,
                })
            # Also fire the on_subscribe event the original protocol sent.
            self._SendEnvelope({
                "Type": "on_subscribe",
                "MqttMessageId": mid,
                "ReasonCodeList": [result.granted_qos],
                "Id": proxy_msg_id,
            })
        threading.Thread(target=worker, name=f"legacyrelay-sub[{self._peer_label}]", daemon=True).start()


    def _DoUnsubscribe(self, topic: str, proxy_msg_id: Optional[int], no_ack: bool) -> None:
        def worker():
            if self._handle is None:
                return
            try:
                ok = self._mux.Unsubscribe(self._handle, topic)
            except Exception as e:
                self._logger.debug("LegacyJsonRelay[%s] unsubscribe raised: %s",
                                   self._peer_label, e)
                ok = False
            mid = _NextSyntheticMid()
            if not no_ack:
                self._SendEnvelope({
                    "Type": "unsubscribe_ack",
                    "AckResult": bool(ok),
                    "MqttMessageId": mid,
                    "Id": proxy_msg_id,
                })
            self._SendEnvelope({
                "Type": "on_unsubscribe",
                "MqttMessageId": mid,
                "ReasonCodeList": [SubAckReturnCode.GRANTED_QOS_0],
                "Id": proxy_msg_id,
            })
        threading.Thread(target=worker, name=f"legacyrelay-unsub[{self._peer_label}]", daemon=True).start()


    def _DoPublish(self, topic: str, msg: Dict[str, Any], proxy_msg_id: Optional[int], no_ack: bool) -> None:
        payload_raw = msg.get("Payload", None)
        payload_bytes: bytes = b""
        if payload_raw is not None:
            if not isinstance(payload_raw, str):
                self._logger.warning("LegacyJsonRelay[%s] Payload must be base64 string",
                                     self._peer_label)
                self._FatalClose()
                return
            try:
                payload_bytes = base64.b64decode(payload_raw)
            except Exception as e:
                self._logger.warning("LegacyJsonRelay[%s] Payload base64 decode failed: %s",
                                     self._peer_label, e)
                self._FatalClose()
                return

        def worker():
            if self._handle is None:
                return
            try:
                result: PublishResult = self._mux.Publish(self._handle, topic, payload_bytes, qos=0, retain=False)
            except Exception as e:
                self._logger.error("LegacyJsonRelay[%s] publish raised: %s",
                                   self._peer_label, e)
                result = PublishResult(success=False)
            if not no_ack:
                self._SendEnvelope({
                    "Type": "publish_ack",
                    "AckResult": result.success,
                    "MqttMessageId": _NextSyntheticMid(),
                    "Id": proxy_msg_id,
                })
        threading.Thread(target=worker, name=f"legacyrelay-pub[{self._peer_label}]", daemon=True).start()


    # ---- helpers ----

    def _SendEnvelope(self, envelope: Dict[str, Any]) -> None:
        try:
            data = json.dumps(envelope).encode("utf-8")
        except Exception as e:
            self._logger.error("LegacyJsonRelay[%s] envelope encode raised: %s",
                               self._peer_label, e)
            self._FatalClose()
            return
        with self._send_lock:
            try:
                self._send_text(data)
            except Exception as e:
                self._logger.debug("LegacyJsonRelay[%s] send raised: %s",
                                   self._peer_label, e)
                self._FatalClose()


    def _FatalClose(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle = self._handle
        self._handle = None
        if handle is not None:
            self._mux.Detach(handle)
        try:
            self._close_transport()
        except Exception as e:
            self._logger.debug("LegacyJsonRelay[%s] close transport raised: %s",
                               self._peer_label, e)
