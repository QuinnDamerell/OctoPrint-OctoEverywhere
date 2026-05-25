import logging
import threading
import time
import unittest
from typing import List

from octoeverywhere.mqttmux.mux import MqttConnectionContext, MqttUpstreamMux
from octoeverywhere.mqttmux.types import ConnAckReturnCode
from octoeverywhere.mqttmux.wireclient import WireVirtualClient
from octoeverywhere.mqttmux.wirecodec import (
    ConnAckPacket,
    ConnectPacket,
    DisconnectPacket,
    EncodePacket,
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

from .fakepaho import FakePahoClient


def _silent_logger() -> logging.Logger:
    logger = logging.getLogger("mqttmux.wireclient.test")
    logger.setLevel(logging.CRITICAL)
    return logger


# In-memory wire client used to test the WireVirtualClient base. The "peer"
# side of the bytestream lives in `out_bytes`; the test decodes those bytes
# to assert on the packets we sent.
class _InMemoryWireClient(WireVirtualClient):
    def __init__(self, mux: MqttUpstreamMux, *, allowed_levels=None):
        super().__init__(_silent_logger(), mux, "test-peer", allowed_protocol_levels=allowed_levels)
        self.out_bytes = bytearray()
        self.transport_closed = False
        self._send_lock_internal = threading.Lock()
    def _SendBytes(self, data: bytes) -> None:
        with self._send_lock_internal:
            self.out_bytes.extend(data)
    def _CloseTransport(self) -> None:
        self.transport_closed = True

    # Decode whatever's been "sent" to the peer and return all decoded packets.
    def OutboundPackets(self) -> List[object]:
        d = MqttPacketDecoder()
        return d.FeedBytes(bytes(self.out_bytes))


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


def _wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestConnectHandshake(unittest.TestCase):

    def test_connect_rejected_when_upstream_down(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        # Don't fire connect - upstream stays down.
        _wait_until(lambda: fake.connect_called)
        wc = _InMemoryWireClient(mux)
        wc.FeedBytes(EncodePacket(ConnectPacket(client_id="c", keep_alive=0)))
        # Should get CONNACK with SERVER_UNAVAILABLE and the transport closed.
        packets = wc.OutboundPackets()
        self.assertEqual(len(packets), 1)
        self.assertIsInstance(packets[0], ConnAckPacket)
        self.assertEqual(packets[0].return_code, ConnAckReturnCode.SERVER_UNAVAILABLE)
        self.assertTrue(wc.transport_closed)
        mux.Shutdown()

    def test_connect_rejected_with_unsupported_protocol_level(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        _wait_until(lambda: fake.connect_called)
        fake.FireConnect(0)
        wc = _InMemoryWireClient(mux)
        # Hand-craft a minimal CONNECT packet with protocol_level=5. The
        # encoder won't emit one (by design - 3.1.1 only) but the decoder
        # accepts the byte and surfaces it to the wire client which must
        # reject with CONNACK 0x01.
        body = bytearray()
        body += b"\x00\x04MQTT"  # protocol name
        body.append(5)            # protocol level (v5)
        body.append(0x02)         # connect flags: clean=1
        body += b"\x00\x3c"       # keepalive=60
        body += b"\x00\x01c"      # client id = "c"
        packet = bytes([0x10, len(body)]) + bytes(body)
        wc.FeedBytes(packet)
        packets = wc.OutboundPackets()
        self.assertEqual(packets[0].return_code, ConnAckReturnCode.UNACCEPTABLE_PROTOCOL_VERSION)
        self.assertTrue(wc.transport_closed)
        mux.Shutdown()

    def test_real_mqtt5_connect_gets_unsupported_connack(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        _wait_until(lambda: fake.connect_called)
        fake.FireConnect(0)
        wc = _InMemoryWireClient(mux)
        body = bytearray()
        body += b"\x00\x04MQTT"
        body.append(5)
        body.append(0x02)
        body += b"\x00\x3c"
        body.append(0)          # MQTT 5 CONNECT properties length
        body += b"\x00\x01c"   # client id
        packet = bytes([0x10, len(body)]) + bytes(body)
        wc.FeedBytes(packet)
        packets = wc.OutboundPackets()
        self.assertEqual(packets[0].return_code, ConnAckReturnCode.UNACCEPTABLE_PROTOCOL_VERSION)
        self.assertTrue(wc.transport_closed)
        mux.Shutdown()

    def test_connect_accepted_when_upstream_up(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        _wait_until(lambda: fake.connect_called)
        fake.FireConnect(0)
        wc = _InMemoryWireClient(mux)
        wc.FeedBytes(EncodePacket(ConnectPacket(client_id="c", keep_alive=0)))
        packets = wc.OutboundPackets()
        self.assertEqual(len(packets), 1)
        self.assertIsInstance(packets[0], ConnAckPacket)
        self.assertEqual(packets[0].return_code, ConnAckReturnCode.ACCEPTED)
        self.assertTrue(wc.IsConnected())
        mux.Shutdown()

    def test_clean_session_false_rejected(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        _wait_until(lambda: fake.connect_called)
        fake.FireConnect(0)
        wc = _InMemoryWireClient(mux)
        wc.FeedBytes(EncodePacket(ConnectPacket(client_id="persistent", clean_session=False, keep_alive=0)))
        packets = wc.OutboundPackets()
        self.assertIsInstance(packets[0], ConnAckPacket)
        self.assertEqual(packets[0].return_code, ConnAckReturnCode.SERVER_UNAVAILABLE)
        self.assertTrue(wc.transport_closed)
        mux.Shutdown()

    def test_empty_client_id_with_clean_session_false_rejected(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        _wait_until(lambda: fake.connect_called)
        fake.FireConnect(0)
        wc = _InMemoryWireClient(mux)
        wc.FeedBytes(EncodePacket(ConnectPacket(client_id="", clean_session=False, keep_alive=0)))
        packets = wc.OutboundPackets()
        self.assertIsInstance(packets[0], ConnAckPacket)
        self.assertEqual(packets[0].return_code, ConnAckReturnCode.IDENTIFIER_REJECTED)
        self.assertTrue(wc.transport_closed)
        mux.Shutdown()

    def test_duplicate_client_id_closes_previous_connection(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        _wait_until(lambda: fake.connect_called)
        fake.FireConnect(0)
        first = _InMemoryWireClient(mux)
        first.FeedBytes(EncodePacket(ConnectPacket(client_id="same", keep_alive=0)))
        second = _InMemoryWireClient(mux)
        second.FeedBytes(EncodePacket(ConnectPacket(client_id="same", keep_alive=0)))
        self.assertTrue(first.transport_closed)
        self.assertTrue(second.IsConnected())
        mux.Shutdown()

    def test_will_published_on_abnormal_peer_close(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        _wait_until(lambda: fake.connect_called)
        fake.FireConnect(0)
        wc = _InMemoryWireClient(mux)
        from octoeverywhere.mqttmux.types import WillSpec
        wc.FeedBytes(EncodePacket(ConnectPacket(
            client_id="will-client",
            keep_alive=0,
            will=WillSpec(topic="last/will", payload=b"gone", qos=0, retain=True),
        )))
        wc.OnPeerClosed()
        self.assertEqual(fake.publishes[-1][0], "last/will")
        self.assertEqual(fake.publishes[-1][1], b"gone")
        self.assertEqual(fake.publishes[-1][2], 0)
        self.assertTrue(fake.publishes[-1][3])
        mux.Shutdown()


class TestPingAndDisconnect(unittest.TestCase):

    def setUp(self):
        self.fake = FakePahoClient()
        self.mux = _make_mux(self.fake)
        self.mux.Start()
        _wait_until(lambda: self.fake.connect_called)
        self.fake.FireConnect(0)
        self.wc = _InMemoryWireClient(self.mux)
        self.wc.FeedBytes(EncodePacket(ConnectPacket(client_id="c", keep_alive=0)))
        # Clear the CONNACK from outbound buffer.
        self.wc.out_bytes.clear()

    def tearDown(self):
        self.mux.Shutdown()

    def test_pingreq_returns_pingresp(self):
        self.wc.FeedBytes(EncodePacket(PingReqPacket()))
        packets = self.wc.OutboundPackets()
        self.assertEqual(len(packets), 1)
        self.assertIsInstance(packets[0], PingRespPacket)

    def test_disconnect_closes(self):
        self.wc.FeedBytes(EncodePacket(DisconnectPacket()))
        self.assertTrue(self.wc.transport_closed)


class TestSubscribeFlow(unittest.TestCase):

    def setUp(self):
        self.fake = FakePahoClient()
        self.mux = _make_mux(self.fake)
        self.mux.Start()
        _wait_until(lambda: self.fake.connect_called)
        self.fake.FireConnect(0)
        self.wc = _InMemoryWireClient(self.mux)
        self.wc.FeedBytes(EncodePacket(ConnectPacket(client_id="c", keep_alive=0)))
        self.wc.out_bytes.clear()

    def tearDown(self):
        self.mux.Shutdown()

    def test_subscribe_synthesizes_suback(self):
        # Feed SUBSCRIBE; the wire client's worker will issue mux.Subscribe
        # which spawns a real paho subscribe. Fire SUBACK from fake.
        def feed():
            self.wc.FeedBytes(EncodePacket(SubscribePacket(packet_id=10, subscriptions=[("a/b", 1)])))
        t = threading.Thread(target=feed)
        t.start()
        # Wait for the paho subscribe.
        _wait_until(lambda: len(self.fake.subscribes) > 0)
        self.fake.FireSubAck(mid=1, granted_qos_list=[1])
        t.join(timeout=2.0)
        _wait_until(lambda: len(self.wc.OutboundPackets()) > 0)
        packets = self.wc.OutboundPackets()
        self.assertEqual(len(packets), 1)
        self.assertIsInstance(packets[0], SubAckPacket)
        self.assertEqual(packets[0].packet_id, 10)
        self.assertEqual(packets[0].return_codes, [1])

    def test_subscribe_with_invalid_filter_returns_failure(self):
        def feed():
            # "foo/#/bar" is invalid (# not at end).
            self.wc.FeedBytes(EncodePacket(SubscribePacket(packet_id=20, subscriptions=[("foo/#/bar", 0)])))
        t = threading.Thread(target=feed)
        t.start()
        t.join(timeout=2.0)
        packets = self.wc.OutboundPackets()
        # No upstream subscribe should have been attempted.
        self.assertEqual(len(self.fake.subscribes), 0)
        self.assertEqual(packets[0].return_codes, [0x80])

    def test_unsubscribe_returns_unsuback(self):
        # First subscribe.
        def sub():
            self.wc.FeedBytes(EncodePacket(SubscribePacket(packet_id=30, subscriptions=[("x", 0)])))
        t = threading.Thread(target=sub)
        t.start()
        _wait_until(lambda: len(self.fake.subscribes) > 0)
        self.fake.FireSubAck(mid=1, granted_qos_list=[0])
        t.join(timeout=2.0)
        _wait_until(lambda: len(self.wc.OutboundPackets()) > 0)
        self.wc.out_bytes.clear()

        # Then unsubscribe.
        def unsub():
            self.wc.FeedBytes(EncodePacket(UnsubscribePacket(packet_id=31, filters=["x"])))
        t = threading.Thread(target=unsub)
        t.start()
        _wait_until(lambda: len(self.fake.unsubscribes) > 0)
        self.fake.FireUnsubAck(mid=2)
        t.join(timeout=2.0)
        _wait_until(lambda: len(self.wc.OutboundPackets()) > 0)
        packets = self.wc.OutboundPackets()
        self.assertEqual(len(packets), 1)
        self.assertIsInstance(packets[0], UnsubAckPacket)
        self.assertEqual(packets[0].packet_id, 31)


class TestPublishFlow(unittest.TestCase):

    def setUp(self):
        self.fake = FakePahoClient()
        self.mux = _make_mux(self.fake)
        self.mux.Start()
        _wait_until(lambda: self.fake.connect_called)
        self.fake.FireConnect(0)
        self.wc = _InMemoryWireClient(self.mux)
        self.wc.FeedBytes(EncodePacket(ConnectPacket(client_id="c", keep_alive=0)))
        self.wc.out_bytes.clear()

    def tearDown(self):
        self.mux.Shutdown()

    def test_inbound_qos0_publish_forwards_upstream(self):
        self.wc.FeedBytes(EncodePacket(PublishPacket(topic="t", payload=b"hi", qos=0)))
        _wait_until(lambda: len(self.fake.publishes) > 0)
        self.assertEqual(self.fake.publishes[-1][0], "t")
        self.assertEqual(self.fake.publishes[-1][1], b"hi")
        # No PUBACK sent for QoS 0.
        self.assertEqual(len(self.wc.OutboundPackets()), 0)

    def test_inbound_qos1_publish_acks_peer(self):
        self.wc.FeedBytes(EncodePacket(PublishPacket(topic="t", payload=b"x", qos=1, packet_id=7)))
        # PUBACK should arrive eventually (from worker).
        _wait_until(lambda: len(self.wc.OutboundPackets()) > 0)
        packets = self.wc.OutboundPackets()
        self.assertEqual(len(packets), 1)
        self.assertIsInstance(packets[0], PubAckPacket)
        self.assertEqual(packets[0].packet_id, 7)

    def test_inbound_qos1_publish_failure_closes_without_puback(self):
        self.fake.publish_rc = 4
        self.wc.FeedBytes(EncodePacket(PublishPacket(topic="t", payload=b"x", qos=1, packet_id=7)))
        _wait_until(lambda: self.wc.transport_closed)
        packets = self.wc.OutboundPackets()
        self.assertEqual(len(packets), 0)
        self.assertTrue(self.wc.transport_closed)

    def test_inbound_qos2_publish_does_4step_handshake(self):
        self.wc.FeedBytes(EncodePacket(PublishPacket(topic="t", payload=b"x", qos=2, packet_id=9)))
        # PUBREC should arrive.
        _wait_until(lambda: len(self.wc.OutboundPackets()) > 0)
        packets = self.wc.OutboundPackets()
        self.assertIsInstance(packets[0], PubRecPacket)
        # Now peer sends PUBREL.
        self.wc.out_bytes.clear()
        self.wc.FeedBytes(EncodePacket(PubRelPacket(packet_id=9)))
        # We respond with PUBCOMP.
        packets = self.wc.OutboundPackets()
        self.assertEqual(len(packets), 1)
        self.assertIsInstance(packets[0], PubCompPacket)
        self.assertEqual(packets[0].packet_id, 9)

    def test_inbound_qos2_duplicate_publish_does_not_republish_upstream(self):
        self.fake.publish_auto_complete = False
        self.wc.FeedBytes(EncodePacket(PublishPacket(topic="t", payload=b"x", qos=2, packet_id=9)))
        _wait_until(lambda: len(self.fake.publishes) == 1)
        self.wc.FeedBytes(EncodePacket(PublishPacket(topic="t", payload=b"x", qos=2, packet_id=9, dup=True)))
        time.sleep(0.05)
        self.assertEqual(len(self.fake.publishes), 1)
        self.fake.publish_infos[0].Complete()
        _wait_until(lambda: len(self.wc.OutboundPackets()) > 0)
        packets = self.wc.OutboundPackets()
        self.assertIsInstance(packets[0], PubRecPacket)

    def test_outbound_qos0_publish_encoded(self):
        # Subscribe to set up routing.
        def sub():
            self.wc.FeedBytes(EncodePacket(SubscribePacket(packet_id=1, subscriptions=[("a", 0)])))
        t = threading.Thread(target=sub)
        t.start()
        _wait_until(lambda: len(self.fake.subscribes) > 0)
        self.fake.FireSubAck(mid=1, granted_qos_list=[0])
        t.join(timeout=2.0)
        self.wc.out_bytes.clear()
        # Now upstream delivers a message.
        self.fake.FireMessage("a", b"payload", qos=0)
        packets = self.wc.OutboundPackets()
        self.assertEqual(len(packets), 1)
        self.assertIsInstance(packets[0], PublishPacket)
        self.assertEqual(packets[0].topic, "a")
        self.assertEqual(packets[0].payload, b"payload")
        self.assertEqual(packets[0].qos, 0)
        # qos=0 publish has no packet_id.
        self.assertIsNone(packets[0].packet_id)

    def test_puback_for_unknown_packet_id_closes(self):
        self.wc.FeedBytes(EncodePacket(PubAckPacket(packet_id=99)))
        self.assertTrue(self.wc.transport_closed)


class TestUpstreamDisconnectClosesPeer(unittest.TestCase):

    def test_upstream_disconnect_closes_wire(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        _wait_until(lambda: fake.connect_called)
        fake.FireConnect(0)
        wc = _InMemoryWireClient(mux)
        wc.FeedBytes(EncodePacket(ConnectPacket(client_id="c", keep_alive=0)))
        self.assertTrue(wc.IsConnected())
        fake.FireDisconnect(0)
        # The wire client should close itself.
        _wait_until(lambda: wc.transport_closed)
        self.assertTrue(wc.transport_closed)
        mux.Shutdown()


if __name__ == "__main__":
    unittest.main()
