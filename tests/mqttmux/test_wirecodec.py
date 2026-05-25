import unittest

from octoeverywhere.mqttmux.types import MalformedPacketException, WillSpec
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
    _DecodeVarInt,
    _EncodeVarInt,
)


def _decode_one(data):
    d = MqttPacketDecoder()
    packets = d.FeedBytes(data)
    if len(packets) != 1:
        raise AssertionError(f"expected exactly one packet, got {len(packets)}")
    return packets[0]


class TestVarInt(unittest.TestCase):

    def test_roundtrip(self):
        for value in [0, 1, 127, 128, 16383, 16384, 2097151, 2097152, 268435455]:
            encoded = _EncodeVarInt(value)
            (decoded, _) = _DecodeVarInt(encoded, 0)
            self.assertEqual(decoded, value, msg=f"value={value}")

    def test_partial_returns_none(self):
        # 0xFF means "continuation". With only that byte, decoder needs more.
        (value, _) = _DecodeVarInt(b"\xff", 0)
        self.assertIsNone(value)

    def test_overflow_raises(self):
        with self.assertRaises(MalformedPacketException):
            _DecodeVarInt(b"\xff\xff\xff\xff\x7f", 0)


class TestConnectRoundtrip(unittest.TestCase):

    def test_minimal(self):
        p = ConnectPacket(client_id="abc", clean_session=True, keep_alive=60)
        data = EncodePacket(p)
        decoded = _decode_one(data)
        self.assertEqual(decoded.client_id, "abc")
        self.assertTrue(decoded.clean_session)
        self.assertEqual(decoded.keep_alive, 60)
        self.assertEqual(decoded.protocol_name, "MQTT")
        self.assertEqual(decoded.protocol_level, 4)
        self.assertIsNone(decoded.username)
        self.assertIsNone(decoded.password)
        self.assertIsNone(decoded.will)

    def test_with_auth(self):
        p = ConnectPacket(client_id="x", username="u", password=b"\x01\x02\x03", keep_alive=30)
        decoded = _decode_one(EncodePacket(p))
        self.assertEqual(decoded.username, "u")
        self.assertEqual(decoded.password, b"\x01\x02\x03")

    def test_with_will(self):
        p = ConnectPacket(
            client_id="x",
            will=WillSpec(topic="bye", payload=b"goodbye", qos=1, retain=True),
        )
        decoded = _decode_one(EncodePacket(p))
        self.assertIsNotNone(decoded.will)
        self.assertEqual(decoded.will.topic, "bye")
        self.assertEqual(decoded.will.payload, b"goodbye")
        self.assertEqual(decoded.will.qos, 1)
        self.assertTrue(decoded.will.retain)

    def test_empty_client_id(self):
        p = ConnectPacket(client_id="", clean_session=True)
        decoded = _decode_one(EncodePacket(p))
        self.assertEqual(decoded.client_id, "")

    def test_reserved_bit_malformed(self):
        # Build a CONNECT manually with the reserved bit set.
        body = bytearray()
        body += b"\x00\x04MQTT"     # protocol name
        body.append(4)               # protocol level
        body.append(0x03)            # clean=1, reserved=1 (illegal)
        body += b"\x00\x3c"          # keepalive=60
        body += b"\x00\x00"          # empty client id
        packet = bytes([0x10]) + bytes([len(body)]) + bytes(body)
        d = MqttPacketDecoder()
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(packet)


class TestConnAckRoundtrip(unittest.TestCase):

    def test_accept(self):
        decoded = _decode_one(EncodePacket(ConnAckPacket(session_present=True, return_code=0)))
        self.assertTrue(decoded.session_present)
        self.assertEqual(decoded.return_code, 0)

    def test_refuse_protocol(self):
        decoded = _decode_one(EncodePacket(ConnAckPacket(session_present=False, return_code=1)))
        self.assertFalse(decoded.session_present)
        self.assertEqual(decoded.return_code, 1)

    def test_reserved_ack_flag_bits_rejected(self):
        # Bits 7..1 of the ack flags byte must be 0.
        packet = bytes([0x20, 0x02, 0x02, 0x00])  # CONNACK, len=2, ack_flags=0x02 (illegal), rc=0
        d = MqttPacketDecoder()
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(packet)


class TestPublishRoundtrip(unittest.TestCase):

    def test_qos0(self):
        p = PublishPacket(topic="a/b", payload=b"hello", qos=0)
        decoded = _decode_one(EncodePacket(p))
        self.assertEqual(decoded.topic, "a/b")
        self.assertEqual(decoded.payload, b"hello")
        self.assertEqual(decoded.qos, 0)
        self.assertFalse(decoded.retain)
        self.assertFalse(decoded.dup)
        self.assertIsNone(decoded.packet_id)

    def test_qos1_requires_packet_id(self):
        with self.assertRaises(ValueError):
            EncodePacket(PublishPacket(topic="x", payload=b"", qos=1))

    def test_qos2_with_retain_and_dup(self):
        p = PublishPacket(topic="x", payload=b"d", qos=2, retain=True, dup=True, packet_id=42)
        decoded = _decode_one(EncodePacket(p))
        self.assertEqual(decoded.qos, 2)
        self.assertTrue(decoded.retain)
        self.assertTrue(decoded.dup)
        self.assertEqual(decoded.packet_id, 42)

    def test_qos0_with_dup_rejected_on_encode(self):
        with self.assertRaises(ValueError):
            EncodePacket(PublishPacket(topic="x", payload=b"", qos=0, dup=True))

    def test_qos3_on_wire_is_malformed(self):
        # Fixed header byte: type=3, flags=0b0110 (qos=3).
        packet = bytes([0x36, 0x05]) + b"\x00\x01a" + b"\x00\x01"  # topic "a", pid=1
        d = MqttPacketDecoder()
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(packet)

    def test_publish_topic_with_wildcard_is_malformed(self):
        body = b"\x00\x03a/+" + b""
        packet = bytes([0x30, len(body)]) + body
        d = MqttPacketDecoder()
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(packet)

    def test_zero_packet_id_is_malformed(self):
        # PUBLISH QoS=1 with packet id 0.
        body = b"\x00\x01a" + b"\x00\x00"
        packet = bytes([0x32, len(body)]) + body
        d = MqttPacketDecoder()
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(packet)

    def test_empty_payload_ok(self):
        decoded = _decode_one(EncodePacket(PublishPacket(topic="t", payload=b"", qos=0)))
        self.assertEqual(decoded.payload, b"")


class TestSimpleAcks(unittest.TestCase):

    def test_puback_roundtrip(self):
        self.assertEqual(_decode_one(EncodePacket(PubAckPacket(packet_id=10))).packet_id, 10)

    def test_pubrec_roundtrip(self):
        self.assertEqual(_decode_one(EncodePacket(PubRecPacket(packet_id=11))).packet_id, 11)

    def test_pubrel_has_flag_bit(self):
        data = EncodePacket(PubRelPacket(packet_id=12))
        # Fixed header byte must have flags=0x02.
        self.assertEqual(data[0] & 0x0F, 0x02)
        self.assertEqual(_decode_one(data).packet_id, 12)

    def test_pubcomp_roundtrip(self):
        self.assertEqual(_decode_one(EncodePacket(PubCompPacket(packet_id=13))).packet_id, 13)

    def test_pubrel_missing_flag_bit_rejected(self):
        # PUBREL with flags=0 should be malformed.
        packet = bytes([0x60, 0x02, 0x00, 0x01])
        d = MqttPacketDecoder()
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(packet)


class TestSubscribeRoundtrip(unittest.TestCase):

    def test_one_filter(self):
        p = SubscribePacket(packet_id=5, subscriptions=[("a/+", 1)])
        decoded = _decode_one(EncodePacket(p))
        self.assertEqual(decoded.packet_id, 5)
        self.assertEqual(decoded.subscriptions, [("a/+", 1)])

    def test_multiple_filters(self):
        p = SubscribePacket(packet_id=6, subscriptions=[("a", 0), ("b/#", 2), ("$SYS/+", 1)])
        decoded = _decode_one(EncodePacket(p))
        self.assertEqual(decoded.subscriptions, [("a", 0), ("b/#", 2), ("$SYS/+", 1)])

    def test_zero_filters_rejected_on_encode(self):
        with self.assertRaises(ValueError):
            EncodePacket(SubscribePacket(packet_id=1, subscriptions=[]))

    def test_subscribe_missing_flag_bit_rejected(self):
        # SUBSCRIBE without the required 0x02 flag.
        body = b"\x00\x01" + b"\x00\x01a" + b"\x00"
        packet = bytes([0x80, len(body)]) + body
        d = MqttPacketDecoder()
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(packet)

    def test_subscribe_reserved_qos_bits_rejected(self):
        # Bits 7..2 of QoS byte must be 0.
        body = b"\x00\x01" + b"\x00\x01a" + b"\x04"  # qos byte = 0b00000100
        packet = bytes([0x82, len(body)]) + body
        d = MqttPacketDecoder()
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(packet)


class TestSubAckRoundtrip(unittest.TestCase):

    def test_roundtrip(self):
        p = SubAckPacket(packet_id=5, return_codes=[0x00, 0x01, 0x02, 0x80])
        decoded = _decode_one(EncodePacket(p))
        self.assertEqual(decoded.return_codes, [0x00, 0x01, 0x02, 0x80])

    def test_bad_return_code_rejected_on_decode(self):
        # SUBACK with rc=0x03 (not valid for 3.1.1).
        body = b"\x00\x01" + b"\x03"
        packet = bytes([0x90, len(body)]) + body
        d = MqttPacketDecoder()
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(packet)


class TestUnsubscribeRoundtrip(unittest.TestCase):

    def test_roundtrip(self):
        p = UnsubscribePacket(packet_id=7, filters=["a", "b/#"])
        decoded = _decode_one(EncodePacket(p))
        self.assertEqual(decoded.filters, ["a", "b/#"])

    def test_unsuback_roundtrip(self):
        decoded = _decode_one(EncodePacket(UnsubAckPacket(packet_id=8)))
        self.assertEqual(decoded.packet_id, 8)


class TestEmptyPackets(unittest.TestCase):

    def test_pingreq(self):
        data = EncodePacket(PingReqPacket())
        self.assertEqual(data, bytes([0xC0, 0x00]))
        self.assertIsInstance(_decode_one(data), PingReqPacket)

    def test_pingresp(self):
        data = EncodePacket(PingRespPacket())
        self.assertEqual(data, bytes([0xD0, 0x00]))
        self.assertIsInstance(_decode_one(data), PingRespPacket)

    def test_disconnect(self):
        data = EncodePacket(DisconnectPacket())
        self.assertEqual(data, bytes([0xE0, 0x00]))
        self.assertIsInstance(_decode_one(data), DisconnectPacket)


class TestStreamingDecoder(unittest.TestCase):

    def test_two_packets_in_one_feed(self):
        data = EncodePacket(PingReqPacket()) + EncodePacket(PingReqPacket())
        d = MqttPacketDecoder()
        packets = d.FeedBytes(data)
        self.assertEqual(len(packets), 2)

    def test_partial_then_complete(self):
        data = EncodePacket(PublishPacket(topic="t", payload=b"hello world", qos=0))
        d = MqttPacketDecoder()
        # Feed one byte at a time - decoder must hold buffer and emit once whole.
        results = []
        for i in range(len(data)):
            results.extend(d.FeedBytes(data[i:i + 1]))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].payload, b"hello world")

    def test_partial_varint(self):
        # Build a large publish so the remaining length needs 2 varint bytes.
        payload = b"x" * 200
        data = EncodePacket(PublishPacket(topic="t", payload=payload, qos=0))
        d = MqttPacketDecoder()
        # Feed first 2 bytes - the second byte of the varint is missing.
        self.assertEqual(d.FeedBytes(data[:2]), [])
        # Feed remaining bytes.
        out = d.FeedBytes(data[2:])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].payload, payload)

    def test_unknown_packet_type_rejected(self):
        # Type 0 is reserved.
        d = MqttPacketDecoder()
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(bytes([0x00, 0x00]))

    def test_pingreq_with_body_rejected(self):
        d = MqttPacketDecoder()
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(bytes([0xC0, 0x01, 0x00]))

    def test_max_packet_size_enforced(self):
        d = MqttPacketDecoder(max_packet_size=10)
        # PUBLISH with remaining-length > 10.
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(bytes([0x30, 0x14]))  # remaining length=20


class TestUtf8(unittest.TestCase):

    def test_unicode_topic(self):
        p = PublishPacket(topic="t/é/x", payload=b"x", qos=0)
        decoded = _decode_one(EncodePacket(p))
        self.assertEqual(decoded.topic, "t/é/x")

    def test_null_char_in_string_rejected(self):
        # Forge a CONNECT with a null in the client id.
        body = bytearray()
        body += b"\x00\x04MQTT"
        body.append(4)
        body.append(0x02)
        body += b"\x00\x3c"
        # client id "a\x00b"
        body += b"\x00\x03a\x00b"
        packet = bytes([0x10, len(body)]) + bytes(body)
        d = MqttPacketDecoder()
        with self.assertRaises(MalformedPacketException):
            d.FeedBytes(packet)


if __name__ == "__main__":
    unittest.main()
