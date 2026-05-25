import base64
import json
import logging
import threading
import time
import unittest
from typing import List, Tuple

from octoeverywhere.buffer import Buffer
from octoeverywhere.interfaces import IWebSocketClient, WebSocketOpCode
from octoeverywhere.mqttmux.mux import MqttConnectionContext, MqttUpstreamMux
from octoeverywhere.mqttmux.muxregistry import MqttMuxRegistry
from octoeverywhere.mqttmux.relayproxy import (
    MqttRelayWebSocketProxy,
    MqttRelayWebSocketProxyProviderBuilder,
)
from octoeverywhere.mqttmux.wirecodec import (
    ConnAckPacket,
    ConnectPacket,
    EncodePacket,
    MqttPacketDecoder,
)

from .fakepaho import FakePahoClient


def _silent_logger() -> logging.Logger:
    logger = logging.getLogger("mqttmux.relayproxy.test")
    logger.setLevel(logging.CRITICAL)
    return logger


def _make_mux(fake: FakePahoClient) -> MqttUpstreamMux:
    return MqttUpstreamMux(
        logger=_silent_logger(),
        printer_key="test",
        connection_context_provider=lambda: MqttConnectionContext(host="h", port=1883),
        subscribe_timeout_sec=2.0,
        publish_timeout_sec=2.0,
        client_factory=lambda *a, **kw: fake,
        backoff_min_sec=0.05,
        backoff_max_sec=0.1,
    )


def _start_mux() -> Tuple[MqttUpstreamMux, FakePahoClient]:
    fake = FakePahoClient()
    mux = _make_mux(fake)
    mux.Start()
    deadline = time.time() + 2.0
    while not fake.connect_called and time.time() < deadline:
        time.sleep(0.01)
    fake.FireConnect(0)
    return mux, fake


class _CapturingHooks:
    """Captures the OE plumbing callbacks the proxy invokes."""
    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self.errors: List[Exception] = []
        self.binary_frames: List[bytes] = []
        self.text_frames: List[bytes] = []
        self._lock = threading.Lock()

    def OnOpen(self, ws: IWebSocketClient) -> None:
        self.opened = True

    def OnData(self, ws: IWebSocketClient, buf: Buffer, op: WebSocketOpCode) -> None:
        data = bytes(buf.GetBytesLike())
        with self._lock:
            if op == WebSocketOpCode.BINARY:
                self.binary_frames.append(data)
            else:
                self.text_frames.append(data)

    def OnClose(self, ws: IWebSocketClient) -> None:
        self.closed = True

    def OnError(self, ws: IWebSocketClient, exc: Exception) -> None:
        self.errors.append(exc)


def _wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestRoutingDetection(unittest.TestCase):

    def test_v2_routing_with_real_connect(self):
        mux, fake = _start_mux()
        hooks = _CapturingHooks()
        proxy = MqttRelayWebSocketProxy(
            logger=_silent_logger(), mux=mux, stream_id=1, peer_label="p",
            on_ws_open=hooks.OnOpen, on_ws_data=hooks.OnData,
            on_ws_close=hooks.OnClose, on_ws_error=hooks.OnError,
        )
        proxy.RunAsync()
        self.assertTrue(hooks.opened)
        # Send a real MQTT CONNECT as binary.
        connect_bytes = EncodePacket(ConnectPacket(client_id="c", keep_alive=0))
        proxy.Send(Buffer(connect_bytes), isData=True)
        # The proxy should route to v2 and the WireVirtualClient should send
        # a CONNACK back via binary frame.
        _wait_until(lambda: len(hooks.binary_frames) > 0)
        self.assertEqual(len(hooks.binary_frames), 1)
        d = MqttPacketDecoder()
        packets = d.FeedBytes(hooks.binary_frames[0])
        self.assertEqual(len(packets), 1)
        self.assertIsInstance(packets[0], ConnAckPacket)
        self.assertEqual(packets[0].return_code, 0)  # accepted
        proxy.Close()
        mux.Shutdown()

    def test_v1_routing_with_json_envelope(self):
        mux, fake = _start_mux()
        hooks = _CapturingHooks()
        proxy = MqttRelayWebSocketProxy(
            logger=_silent_logger(), mux=mux, stream_id=2, peer_label="p",
            on_ws_open=hooks.OnOpen, on_ws_data=hooks.OnData,
            on_ws_close=hooks.OnClose, on_ws_error=hooks.OnError,
        )
        proxy.RunAsync()
        # Send a v1 JSON envelope: subscribe to "a".
        envelope = json.dumps({"Type": "subscribe", "Topic": "a", "Id": 42}).encode("utf-8")
        proxy.Send(Buffer(envelope), isData=False)
        # The legacy client spawns a worker thread that issues a paho
        # subscribe. Fire SUBACK.
        _wait_until(lambda: len(fake.subscribes) > 0)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        # Expect text frames back: subscribe_ack + on_subscribe.
        _wait_until(lambda: len(hooks.text_frames) >= 2)
        envelopes = [json.loads(b.decode("utf-8")) for b in hooks.text_frames]
        types = {e["Type"] for e in envelopes}
        self.assertIn("subscribe_ack", types)
        self.assertIn("on_subscribe", types)
        for e in envelopes:
            self.assertEqual(e["Id"], 42)
        proxy.Close()
        mux.Shutdown()


    def test_unrecognised_first_frame_closes(self):
        mux, _ = _start_mux()
        hooks = _CapturingHooks()
        proxy = MqttRelayWebSocketProxy(
            logger=_silent_logger(), mux=mux, stream_id=4, peer_label="p",
            on_ws_open=hooks.OnOpen, on_ws_data=hooks.OnData,
            on_ws_close=hooks.OnClose, on_ws_error=hooks.OnError,
        )
        proxy.RunAsync()
        # Garbage first byte.
        proxy.Send(Buffer(b"\xab\xcd\xef"), isData=True)
        _wait_until(lambda: hooks.closed)
        self.assertTrue(hooks.closed)
        mux.Shutdown()


class TestV1PublishAndDelivery(unittest.TestCase):

    def test_v1_publish_envelope_forwards_upstream(self):
        mux, fake = _start_mux()
        hooks = _CapturingHooks()
        proxy = MqttRelayWebSocketProxy(
            logger=_silent_logger(), mux=mux, stream_id=5, peer_label="p",
            on_ws_open=hooks.OnOpen, on_ws_data=hooks.OnData,
            on_ws_close=hooks.OnClose, on_ws_error=hooks.OnError,
        )
        proxy.RunAsync()
        payload_b64 = base64.b64encode(b"hello").decode("utf-8")
        envelope = json.dumps({
            "Type": "publish", "Topic": "out/topic", "Payload": payload_b64, "Id": 7,
        }).encode("utf-8")
        proxy.Send(Buffer(envelope), isData=False)
        _wait_until(lambda: len(fake.publishes) > 0)
        self.assertEqual(fake.publishes[-1][0], "out/topic")
        self.assertEqual(fake.publishes[-1][1], b"hello")
        # publish_ack should come back.
        _wait_until(lambda: len(hooks.text_frames) > 0)
        envelopes = [json.loads(b.decode("utf-8")) for b in hooks.text_frames]
        ack = next((e for e in envelopes if e["Type"] == "publish_ack"), None)
        self.assertIsNotNone(ack)
        self.assertEqual(ack["Id"], 7)
        proxy.Close()
        mux.Shutdown()

    def test_v1_inbound_message_delivered_as_on_message(self):
        mux, fake = _start_mux()
        hooks = _CapturingHooks()
        proxy = MqttRelayWebSocketProxy(
            logger=_silent_logger(), mux=mux, stream_id=6, peer_label="p",
            on_ws_open=hooks.OnOpen, on_ws_data=hooks.OnData,
            on_ws_close=hooks.OnClose, on_ws_error=hooks.OnError,
        )
        proxy.RunAsync()
        # Subscribe first.
        envelope = json.dumps({"Type": "subscribe", "Topic": "in/topic"}).encode("utf-8")
        proxy.Send(Buffer(envelope), isData=False)
        _wait_until(lambda: len(fake.subscribes) > 0)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        _wait_until(lambda: any(json.loads(b)["Type"] == "subscribe_ack" for b in hooks.text_frames))
        # Now upstream delivers a message.
        fake.FireMessage("in/topic", b"binary-payload", qos=0)
        # Expect an on_message envelope with base64 payload.
        _wait_until(lambda: any(json.loads(b)["Type"] == "on_message" for b in hooks.text_frames))
        on_msg = next(json.loads(b) for b in hooks.text_frames
                      if json.loads(b)["Type"] == "on_message")
        self.assertEqual(base64.b64decode(on_msg["Payload"]), b"binary-payload")
        proxy.Close()
        mux.Shutdown()


class TestBuilderRegistryLookup(unittest.TestCase):

    def test_builder_returns_none_when_mux_not_registered(self):
        builder = MqttRelayWebSocketProxyProviderBuilder(_silent_logger(), mux_key="missing-key")
        self.assertIsNone(builder.GetCommandWebsocketProvider(None))

    def test_builder_finds_registered_mux(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        MqttMuxRegistry.Register("my-key", mux)
        try:
            builder = MqttRelayWebSocketProxyProviderBuilder(_silent_logger(), mux_key="my-key")
            provider = builder.GetCommandWebsocketProvider(None)
            self.assertIsNotNone(provider)
        finally:
            MqttMuxRegistry.Unregister("my-key")


if __name__ == "__main__":
    unittest.main()
