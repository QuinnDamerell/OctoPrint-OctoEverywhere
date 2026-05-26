import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

from .types import (
    MAX_PACKET_IDENTIFIER,
    MAX_REMAINING_LENGTH,
    MAX_TOPIC_BYTES,
    MalformedPacketException,
    PacketType,
    ProtocolLevel,
    WillSpec,
)


# Hand-rolled MQTT 3.1.1 wire codec.
#
# Design notes:
#   * Decode is streaming via MqttPacketDecoder.FeedBytes() so it tolerates
#     partial frames from TCP / WebSocket.
#   * Every encode/decode function takes a `protocol_level` parameter so v5
#     support can be added later without restructuring. Today, any
#     protocol_level != 4 in a code path that depends on it raises
#     NotImplementedError - which can only happen if the wire client accepts
#     a v5 CONNECT, and it currently does not.
#   * Packet dataclasses carry optional v5 fields (properties, etc.); the 3.1.1
#     encoder ignores them, the v5 encoder (future) would emit them.
#   * Validation is split into two layers. The codec rejects bytes that violate
#     framing or reserved-bit constraints (these are MalformedPacketException
#     and the connection must be closed). The wire client layer enforces
#     higher-level rules (e.g. unknown packet id, invalid subscribe filter).


# =============================================================================
# Primitive type helpers
# =============================================================================

# Variable-byte integer (MQTT 3.1.1 §2.2.3). Encodes 0..268_435_455 in 1-4 bytes.
def _EncodeVarInt(value: int) -> bytes:
    if value < 0 or value > MAX_REMAINING_LENGTH:
        raise ValueError(f"VarInt out of range: {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value > 0:
            byte |= 0x80
            out.append(byte)
        else:
            out.append(byte)
            break
    return bytes(out)


# Returns (value, new_offset). Raises MalformedPacketException on overflow.
# Returns (None, offset) when more bytes are needed.
def _DecodeVarInt(buf: "Union[bytes, bytearray]", offset: int) -> Tuple[Optional[int], int]:
    value = 0
    multiplier = 1
    pos = offset
    while True:
        if pos >= len(buf):
            return (None, offset)
        byte = buf[pos]
        pos += 1
        value += (byte & 0x7F) * multiplier
        if (byte & 0x80) == 0:
            return (value, pos)
        multiplier *= 128
        if multiplier > 128 * 128 * 128:
            # Already consumed 4 continuation bytes worth. A 5th byte indicates
            # malformed input per §2.2.3.
            raise MalformedPacketException("VarInt exceeds 4 bytes")


# 2-byte big-endian unsigned integer.
def _EncodeUint16(value: int) -> bytes:
    if value < 0 or value > 0xFFFF:
        raise ValueError(f"Uint16 out of range: {value}")
    return struct.pack(">H", value)


def _DecodeUint16(buf: bytes, offset: int) -> Tuple[int, int]:
    if offset + 2 > len(buf):
        raise MalformedPacketException("Truncated uint16")
    return (struct.unpack_from(">H", buf, offset)[0], offset + 2)


# UTF-8 string (MQTT 3.1.1 §1.5.3): 2-byte length prefix + UTF-8 bytes.
# The spec forbids U+0000 and unmatched surrogates. We do a basic check;
# stricter validation (control char ranges) can be layered above.
def _EncodeString(value: str) -> bytes:
    if "\x00" in value:
        raise ValueError("UTF-8 string must not contain U+0000")
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_TOPIC_BYTES:
        raise ValueError(f"UTF-8 string exceeds {MAX_TOPIC_BYTES} bytes")
    return _EncodeUint16(len(encoded)) + encoded


def _DecodeString(buf: bytes, offset: int) -> Tuple[str, int]:
    length, pos = _DecodeUint16(buf, offset)
    if pos + length > len(buf):
        raise MalformedPacketException("Truncated UTF-8 string")
    try:
        value = buf[pos:pos + length].decode("utf-8")
    except UnicodeDecodeError as e:
        raise MalformedPacketException(f"Invalid UTF-8: {e}") from e
    if "\x00" in value:
        raise MalformedPacketException("UTF-8 string contains U+0000")
    return (value, pos + length)


# Binary data: 2-byte length prefix + raw bytes (used for password and will
# message in CONNECT payload).
def _EncodeBinary(value: bytes) -> bytes:
    if len(value) > 0xFFFF:
        raise ValueError(f"Binary data exceeds {0xFFFF} bytes")
    return _EncodeUint16(len(value)) + value


def _DecodeBinary(buf: bytes, offset: int) -> Tuple[bytes, int]:
    length, pos = _DecodeUint16(buf, offset)
    if pos + length > len(buf):
        raise MalformedPacketException("Truncated binary data")
    return (bytes(buf[pos:pos + length]), pos + length)


# =============================================================================
# Packet dataclasses
# =============================================================================
# Base class is only for type hints; we never instantiate it directly.

@dataclass
class MqttPacket:
    pass


@dataclass
class ConnectPacket(MqttPacket):
    protocol_name: str = "MQTT"
    protocol_level: int = ProtocolLevel.MQTT_3_1_1
    client_id: str = ""
    clean_session: bool = True
    keep_alive: int = 0
    username: Optional[str] = None
    password: Optional[bytes] = None
    will: Optional[WillSpec] = None
    # v5-only:
    properties: Optional[Dict[int, "object"]] = None


@dataclass
class ConnAckPacket(MqttPacket):
    session_present: bool = False
    return_code: int = 0  # 3.1.1 return code or v5 reason code in the same byte
    properties: Optional[Dict[int, "object"]] = None  # v5 only


@dataclass
class PublishPacket(MqttPacket):
    topic: str = ""
    payload: bytes = b""
    qos: int = 0
    retain: bool = False
    dup: bool = False
    packet_id: Optional[int] = None  # required iff qos > 0
    properties: Optional[Dict[int, "object"]] = None  # v5 only


@dataclass
class PubAckPacket(MqttPacket):
    packet_id: int = 0
    # v5 only:
    reason_code: int = 0
    properties: Optional[Dict[int, "object"]] = None


@dataclass
class PubRecPacket(MqttPacket):
    packet_id: int = 0
    reason_code: int = 0
    properties: Optional[Dict[int, "object"]] = None


@dataclass
class PubRelPacket(MqttPacket):
    packet_id: int = 0
    reason_code: int = 0
    properties: Optional[Dict[int, "object"]] = None


@dataclass
class PubCompPacket(MqttPacket):
    packet_id: int = 0
    reason_code: int = 0
    properties: Optional[Dict[int, "object"]] = None


@dataclass
class SubscribePacket(MqttPacket):
    packet_id: int = 0
    # Each subscription: (filter, requested_qos). v5 carries extra flags
    # (no_local, retain_as_published, retain_handling); the codec accepts a
    # richer tuple shape via the subscriptions_v5 field but 3.1.1 only uses
    # `subscriptions`.
    subscriptions: List[Tuple[str, int]] = field(default_factory=list)
    properties: Optional[Dict[int, "object"]] = None  # v5 only


@dataclass
class SubAckPacket(MqttPacket):
    packet_id: int = 0
    return_codes: List[int] = field(default_factory=list)
    properties: Optional[Dict[int, "object"]] = None  # v5 only


@dataclass
class UnsubscribePacket(MqttPacket):
    packet_id: int = 0
    filters: List[str] = field(default_factory=list)
    properties: Optional[Dict[int, "object"]] = None  # v5 only


@dataclass
class UnsubAckPacket(MqttPacket):
    packet_id: int = 0
    reason_codes: List[int] = field(default_factory=list)  # v5 only
    properties: Optional[Dict[int, "object"]] = None  # v5 only


@dataclass
class PingReqPacket(MqttPacket):
    pass


@dataclass
class PingRespPacket(MqttPacket):
    pass


@dataclass
class DisconnectPacket(MqttPacket):
    reason_code: int = 0  # v5 only
    properties: Optional[Dict[int, "object"]] = None  # v5 only


# =============================================================================
# Encoder
# =============================================================================

# Encodes a packet to its wire bytes. protocol_level controls the encoding;
# only MQTT_3_1_1 is implemented today.
def EncodePacket(packet: MqttPacket, protocol_level: int = ProtocolLevel.MQTT_3_1_1) -> bytes:
    if protocol_level != ProtocolLevel.MQTT_3_1_1:
        raise NotImplementedError(f"protocol_level {protocol_level} not yet supported by encoder")
    if isinstance(packet, ConnectPacket):
        return _EncodeConnect(packet)
    if isinstance(packet, ConnAckPacket):
        return _EncodeConnAck(packet)
    if isinstance(packet, PublishPacket):
        return _EncodePublish(packet)
    if isinstance(packet, PubAckPacket):
        return _EncodeAckSimple(PacketType.PUBACK, packet.packet_id)
    if isinstance(packet, PubRecPacket):
        return _EncodeAckSimple(PacketType.PUBREC, packet.packet_id)
    if isinstance(packet, PubRelPacket):
        return _EncodeAckSimple(PacketType.PUBREL, packet.packet_id, flags=0x02)
    if isinstance(packet, PubCompPacket):
        return _EncodeAckSimple(PacketType.PUBCOMP, packet.packet_id)
    if isinstance(packet, SubscribePacket):
        return _EncodeSubscribe(packet)
    if isinstance(packet, SubAckPacket):
        return _EncodeSubAck(packet)
    if isinstance(packet, UnsubscribePacket):
        return _EncodeUnsubscribe(packet)
    if isinstance(packet, UnsubAckPacket):
        return _EncodeUnsubAck(packet)
    if isinstance(packet, PingReqPacket):
        return bytes([PacketType.PINGREQ << 4, 0])
    if isinstance(packet, PingRespPacket):
        return bytes([PacketType.PINGRESP << 4, 0])
    if isinstance(packet, DisconnectPacket):
        return bytes([PacketType.DISCONNECT << 4, 0])
    raise TypeError(f"Unknown packet type: {type(packet).__name__}")


def _FixedHeader(packet_type: int, flags: int, remaining_length: int) -> bytes:
    return bytes([(packet_type << 4) | (flags & 0x0F)]) + _EncodeVarInt(remaining_length)


def _EncodeConnect(p: ConnectPacket) -> bytes:
    if p.protocol_level != ProtocolLevel.MQTT_3_1_1:
        # We construct CONNECT packets ourselves only when acting as a client
        # to the upstream printer, which is always 3.1.1 today.
        raise NotImplementedError("CONNECT encoder only supports MQTT 3.1.1")
    body = bytearray()
    body += _EncodeString(p.protocol_name)
    body += bytes([p.protocol_level])
    flags = 0
    if p.clean_session:
        flags |= 0x02
    if p.will is not None:
        flags |= 0x04
        flags |= (p.will.qos & 0x03) << 3
        if p.will.retain:
            flags |= 0x20
    if p.password is not None:
        flags |= 0x40
    if p.username is not None:
        flags |= 0x80
    body.append(flags)
    body += _EncodeUint16(p.keep_alive)
    body += _EncodeString(p.client_id)
    if p.will is not None:
        body += _EncodeString(p.will.topic)
        body += _EncodeBinary(p.will.payload)
    if p.username is not None:
        body += _EncodeString(p.username)
    if p.password is not None:
        body += _EncodeBinary(p.password)
    return _FixedHeader(PacketType.CONNECT, 0, len(body)) + bytes(body)


def _EncodeConnAck(p: ConnAckPacket) -> bytes:
    body = bytes([0x01 if p.session_present else 0x00, p.return_code & 0xFF])
    return _FixedHeader(PacketType.CONNACK, 0, len(body)) + body


def _EncodePublish(p: PublishPacket) -> bytes:
    if p.qos < 0 or p.qos > 2:
        raise ValueError(f"Invalid PUBLISH QoS: {p.qos}")
    if p.qos == 0 and p.dup:
        # §3.3.1.1 - DUP must be 0 for QoS 0.
        raise ValueError("DUP must be 0 for QoS 0 PUBLISH")
    if p.qos > 0 and p.packet_id is None:
        raise ValueError("PUBLISH with QoS > 0 requires packet_id")
    if p.qos > 0 and p.packet_id == 0:
        raise ValueError("packet_id must not be 0")
    flags = 0
    if p.dup:
        flags |= 0x08
    flags |= (p.qos & 0x03) << 1
    if p.retain:
        flags |= 0x01
    body = bytearray()
    body += _EncodeString(p.topic)
    if p.qos > 0:
        assert p.packet_id is not None
        body += _EncodeUint16(p.packet_id)
    body += p.payload
    return _FixedHeader(PacketType.PUBLISH, flags, len(body)) + bytes(body)


def _EncodeAckSimple(packet_type: int, packet_id: int, flags: int = 0) -> bytes:
    body = _EncodeUint16(packet_id)
    return _FixedHeader(packet_type, flags, len(body)) + body


def _EncodeSubscribe(p: SubscribePacket) -> bytes:
    if len(p.subscriptions) == 0:
        raise ValueError("SUBSCRIBE requires at least one subscription")
    body = bytearray()
    body += _EncodeUint16(p.packet_id)
    for filter_, qos in p.subscriptions:
        if qos < 0 or qos > 2:
            raise ValueError(f"Invalid SUBSCRIBE QoS: {qos}")
        body += _EncodeString(filter_)
        body.append(qos & 0x03)
    return _FixedHeader(PacketType.SUBSCRIBE, 0x02, len(body)) + bytes(body)


def _EncodeSubAck(p: SubAckPacket) -> bytes:
    if len(p.return_codes) == 0:
        raise ValueError("SUBACK requires at least one return code")
    body = bytearray()
    body += _EncodeUint16(p.packet_id)
    for rc in p.return_codes:
        body.append(rc & 0xFF)
    return _FixedHeader(PacketType.SUBACK, 0, len(body)) + bytes(body)


def _EncodeUnsubscribe(p: UnsubscribePacket) -> bytes:
    if len(p.filters) == 0:
        raise ValueError("UNSUBSCRIBE requires at least one filter")
    body = bytearray()
    body += _EncodeUint16(p.packet_id)
    for f in p.filters:
        body += _EncodeString(f)
    return _FixedHeader(PacketType.UNSUBSCRIBE, 0x02, len(body)) + bytes(body)


def _EncodeUnsubAck(p: UnsubAckPacket) -> bytes:
    body = _EncodeUint16(p.packet_id)
    return _FixedHeader(PacketType.UNSUBACK, 0, len(body)) + body


# =============================================================================
# Decoder
# =============================================================================
#
# Streaming decoder. FeedBytes appends to an internal buffer and returns all
# fully-decodable packets. Caller is the wire client which is also responsible
# for enforcing higher-level invariants (auth, packet ID matching, etc).

class MqttPacketDecoder:

    def __init__(self, max_packet_size: int = MAX_REMAINING_LENGTH, protocol_level: int = ProtocolLevel.MQTT_3_1_1) -> None:
        if max_packet_size > MAX_REMAINING_LENGTH:
            max_packet_size = MAX_REMAINING_LENGTH
        self._buffer = bytearray()
        self._max_packet_size = max_packet_size
        self._protocol_level = protocol_level


    # Setter used by the wire client when CONNECT establishes the version.
    def SetProtocolLevel(self, protocol_level: int) -> None:
        self._protocol_level = protocol_level


    # Append `data` to the internal buffer and decode as many full packets as
    # are present. Returns the decoded packets in order.
    #
    # Raises MalformedPacketException on protocol-level violations; caller
    # must close the connection. The internal buffer is preserved across
    # FeedBytes calls so a partial packet at the end is fine.
    def FeedBytes(self, data: bytes) -> List[MqttPacket]:
        if len(data) > 0:
            self._buffer.extend(data)
        out: List[MqttPacket] = []
        while True:
            packet, consumed = self._TryParseOne()
            if packet is None:
                break
            del self._buffer[:consumed]
            out.append(packet)
        return out


    def Reset(self) -> None:
        del self._buffer[:]


    # Try to parse one packet starting at buffer offset 0. Returns
    # (packet, bytes_consumed) on success; (None, 0) when more bytes are
    # needed. Raises MalformedPacketException if the framing is invalid.
    def _TryParseOne(self) -> Tuple[Optional[MqttPacket], int]:
        if len(self._buffer) < 1:
            return (None, 0)
        first = self._buffer[0]
        packet_type = (first >> 4) & 0x0F
        flags = first & 0x0F

        # Try to decode remaining length starting at offset 1.
        remaining_length, header_end = _DecodeVarInt(self._buffer, 1)
        if remaining_length is None:
            return (None, 0)

        if remaining_length > self._max_packet_size:
            raise MalformedPacketException(
                f"Packet length {remaining_length} exceeds max {self._max_packet_size}")

        total = header_end + remaining_length
        if len(self._buffer) < total:
            return (None, 0)

        body = bytes(self._buffer[header_end:total])

        if packet_type == PacketType.CONNECT:
            return (_DecodeConnect(flags, body), total)
        if packet_type == PacketType.CONNACK:
            return (_DecodeConnAck(flags, body, self._protocol_level), total)
        if packet_type == PacketType.PUBLISH:
            return (_DecodePublish(flags, body, self._protocol_level), total)
        if packet_type == PacketType.PUBACK:
            return (_DecodeSimpleAck(flags, body, PacketType.PUBACK, self._protocol_level, PubAckPacket), total)
        if packet_type == PacketType.PUBREC:
            return (_DecodeSimpleAck(flags, body, PacketType.PUBREC, self._protocol_level, PubRecPacket), total)
        if packet_type == PacketType.PUBREL:
            return (_DecodeSimpleAck(flags, body, PacketType.PUBREL, self._protocol_level, PubRelPacket), total)
        if packet_type == PacketType.PUBCOMP:
            return (_DecodeSimpleAck(flags, body, PacketType.PUBCOMP, self._protocol_level, PubCompPacket), total)
        if packet_type == PacketType.SUBSCRIBE:
            return (_DecodeSubscribe(flags, body, self._protocol_level), total)
        if packet_type == PacketType.SUBACK:
            return (_DecodeSubAck(flags, body, self._protocol_level), total)
        if packet_type == PacketType.UNSUBSCRIBE:
            return (_DecodeUnsubscribe(flags, body, self._protocol_level), total)
        if packet_type == PacketType.UNSUBACK:
            return (_DecodeUnsubAck(flags, body, self._protocol_level), total)
        if packet_type == PacketType.PINGREQ:
            if flags != 0 or len(body) != 0:
                raise MalformedPacketException("PINGREQ malformed")
            return (PingReqPacket(), total)
        if packet_type == PacketType.PINGRESP:
            if flags != 0 or len(body) != 0:
                raise MalformedPacketException("PINGRESP malformed")
            return (PingRespPacket(), total)
        if packet_type == PacketType.DISCONNECT:
            return (_DecodeDisconnect(flags, body, self._protocol_level), total)
        raise MalformedPacketException(f"Unknown or reserved packet type: {packet_type}")


# --- per-type decoders ---

def _RequireFlags(actual: int, expected: int, type_name: str) -> None:
    if actual != expected:
        raise MalformedPacketException(f"{type_name} fixed header flags must be {expected:#x}, got {actual:#x}")


def _DecodeConnect(flags: int, body: bytes) -> ConnectPacket:
    _RequireFlags(flags, 0, "CONNECT")
    pos = 0
    protocol_name, pos = _DecodeString(body, pos)
    if pos >= len(body):
        raise MalformedPacketException("CONNECT truncated at protocol level")
    protocol_level = body[pos]
    pos += 1
    if pos >= len(body):
        raise MalformedPacketException("CONNECT truncated at connect flags")
    connect_flags = body[pos]
    pos += 1
    # §3.1.2.3: bit 0 of connect flags is reserved and must be 0.
    if connect_flags & 0x01:
        raise MalformedPacketException("CONNECT reserved flag bit must be 0")
    clean_session = bool(connect_flags & 0x02)
    will_flag = bool(connect_flags & 0x04)
    will_qos = (connect_flags >> 3) & 0x03
    will_retain = bool(connect_flags & 0x20)
    password_flag = bool(connect_flags & 0x40)
    username_flag = bool(connect_flags & 0x80)
    if not will_flag:
        if will_qos != 0:
            raise MalformedPacketException("CONNECT will-qos must be 0 when will-flag is 0")
        if will_retain:
            raise MalformedPacketException("CONNECT will-retain must be 0 when will-flag is 0")
    elif will_qos == 3:
        raise MalformedPacketException("CONNECT will-qos must be 0, 1, or 2")
    # §3.1.2.9: password flag must not be set if username flag is 0 (3.1.1).
    # v5 relaxes this; check only for 3.1.1.
    if protocol_level == ProtocolLevel.MQTT_3_1_1 and password_flag and not username_flag:
        raise MalformedPacketException("CONNECT password flag set without username flag")
    keep_alive, pos = _DecodeUint16(body, pos)
    if protocol_level != ProtocolLevel.MQTT_3_1_1:
        # The wire client rejects unsupported protocol levels with CONNACK
        # 0x01. Return the version information without trying to parse the
        # protocol-specific payload; MQTT 5, for example, inserts a property
        # block here before the client id.
        return ConnectPacket(
            protocol_name=protocol_name,
            protocol_level=protocol_level,
            client_id="",
            clean_session=clean_session,
            keep_alive=keep_alive,
        )
    # MQTT 3.1.1 payload starts here.
    client_id, pos = _DecodeString(body, pos)
    will: Optional[WillSpec] = None
    if will_flag:
        will_topic, pos = _DecodeString(body, pos)
        will_payload, pos = _DecodeBinary(body, pos)
        will = WillSpec(topic=will_topic, payload=will_payload, qos=will_qos, retain=will_retain)
    username: Optional[str] = None
    if username_flag:
        username, pos = _DecodeString(body, pos)
    password: Optional[bytes] = None
    if password_flag:
        password, pos = _DecodeBinary(body, pos)
    if pos != len(body):
        raise MalformedPacketException(f"CONNECT trailing {len(body) - pos} bytes")
    return ConnectPacket(
        protocol_name=protocol_name,
        protocol_level=protocol_level,
        client_id=client_id,
        clean_session=clean_session,
        keep_alive=keep_alive,
        username=username,
        password=password,
        will=will,
    )


def _DecodeConnAck(flags: int, body: bytes, protocol_level: int) -> ConnAckPacket:
    _RequireFlags(flags, 0, "CONNACK")
    if len(body) < 2:
        raise MalformedPacketException("CONNACK truncated")
    if protocol_level != ProtocolLevel.MQTT_3_1_1:
        raise NotImplementedError("CONNACK decoder only supports 3.1.1 today")
    ack_flags = body[0]
    if ack_flags & 0xFE:
        # §3.2.2.1: bits 7-1 reserved and must be 0.
        raise MalformedPacketException("CONNACK reserved ack flags must be 0")
    if len(body) != 2:
        raise MalformedPacketException(f"CONNACK length {len(body)} != 2 for 3.1.1")
    return ConnAckPacket(session_present=bool(ack_flags & 0x01), return_code=body[1])


def _DecodePublish(flags: int, body: bytes, protocol_level: int) -> PublishPacket:
    dup = bool(flags & 0x08)
    qos = (flags >> 1) & 0x03
    retain = bool(flags & 0x01)
    if qos == 3:
        raise MalformedPacketException("PUBLISH QoS=3 is invalid")
    if qos == 0 and dup:
        raise MalformedPacketException("PUBLISH DUP must be 0 for QoS 0")
    pos = 0
    topic, pos = _DecodeString(body, pos)
    if "+" in topic or "#" in topic:
        raise MalformedPacketException("PUBLISH topic must not contain wildcards")
    packet_id: Optional[int] = None
    if qos > 0:
        packet_id, pos = _DecodeUint16(body, pos)
        if packet_id == 0:
            raise MalformedPacketException("PUBLISH packet identifier must not be 0")
    if protocol_level != ProtocolLevel.MQTT_3_1_1:
        raise NotImplementedError("PUBLISH decoder only supports 3.1.1 today")
    payload = body[pos:]
    return PublishPacket(topic=topic, payload=payload, qos=qos, retain=retain, dup=dup, packet_id=packet_id)


def _DecodeSimpleAck(flags: int, body: bytes, packet_type: int, protocol_level: int, cls):  # type: ignore[no-untyped-def]
    expected_flags = 0x02 if packet_type == PacketType.PUBREL else 0
    _RequireFlags(flags, expected_flags, PacketType(packet_type).name)
    if protocol_level == ProtocolLevel.MQTT_3_1_1:
        # 3.1.1: exactly 2 bytes - packet id only.
        if len(body) != 2:
            raise MalformedPacketException(f"{PacketType(packet_type).name} length {len(body)} != 2 for 3.1.1")
        packet_id, _ = _DecodeUint16(body, 0)
        if packet_id == 0:
            raise MalformedPacketException(f"{PacketType(packet_type).name} packet identifier must not be 0")
        return cls(packet_id=packet_id)
    raise NotImplementedError(f"{PacketType(packet_type).name} decoder only supports 3.1.1 today")


def _DecodeSubscribe(flags: int, body: bytes, protocol_level: int) -> SubscribePacket:
    _RequireFlags(flags, 0x02, "SUBSCRIBE")
    if protocol_level != ProtocolLevel.MQTT_3_1_1:
        raise NotImplementedError("SUBSCRIBE decoder only supports 3.1.1 today")
    packet_id, pos = _DecodeUint16(body, 0)
    if packet_id == 0:
        raise MalformedPacketException("SUBSCRIBE packet identifier must not be 0")
    subs: List[Tuple[str, int]] = []
    while pos < len(body):
        filter_, pos = _DecodeString(body, pos)
        if pos >= len(body):
            raise MalformedPacketException("SUBSCRIBE truncated at requested QoS")
        qos = body[pos]
        pos += 1
        # §3.8.3: bits 7-2 of requested-QoS byte are reserved and must be 0.
        if qos & 0xFC:
            raise MalformedPacketException("SUBSCRIBE requested QoS reserved bits must be 0")
        if qos > 2:
            raise MalformedPacketException(f"SUBSCRIBE invalid requested QoS: {qos}")
        subs.append((filter_, qos))
    if len(subs) == 0:
        # §3.8.3: payload must contain at least one filter.
        raise MalformedPacketException("SUBSCRIBE payload must contain at least one filter")
    return SubscribePacket(packet_id=packet_id, subscriptions=subs)


def _DecodeSubAck(flags: int, body: bytes, protocol_level: int) -> SubAckPacket:
    _RequireFlags(flags, 0, "SUBACK")
    if protocol_level != ProtocolLevel.MQTT_3_1_1:
        raise NotImplementedError("SUBACK decoder only supports 3.1.1 today")
    packet_id, pos = _DecodeUint16(body, 0)
    if packet_id == 0:
        raise MalformedPacketException("SUBACK packet identifier must not be 0")
    return_codes = list(body[pos:])
    if len(return_codes) == 0:
        raise MalformedPacketException("SUBACK payload must contain at least one return code")
    for rc in return_codes:
        if rc not in (0x00, 0x01, 0x02, 0x80):
            raise MalformedPacketException(f"SUBACK invalid return code: {rc:#x}")
    return SubAckPacket(packet_id=packet_id, return_codes=return_codes)


def _DecodeUnsubscribe(flags: int, body: bytes, protocol_level: int) -> UnsubscribePacket:
    _RequireFlags(flags, 0x02, "UNSUBSCRIBE")
    if protocol_level != ProtocolLevel.MQTT_3_1_1:
        raise NotImplementedError("UNSUBSCRIBE decoder only supports 3.1.1 today")
    packet_id, pos = _DecodeUint16(body, 0)
    if packet_id == 0:
        raise MalformedPacketException("UNSUBSCRIBE packet identifier must not be 0")
    filters: List[str] = []
    while pos < len(body):
        f, pos = _DecodeString(body, pos)
        filters.append(f)
    if len(filters) == 0:
        raise MalformedPacketException("UNSUBSCRIBE payload must contain at least one filter")
    return UnsubscribePacket(packet_id=packet_id, filters=filters)


def _DecodeUnsubAck(flags: int, body: bytes, protocol_level: int) -> UnsubAckPacket:
    _RequireFlags(flags, 0, "UNSUBACK")
    if protocol_level == ProtocolLevel.MQTT_3_1_1:
        # 3.1.1: exactly 2 bytes - packet id only.
        if len(body) != 2:
            raise MalformedPacketException(f"UNSUBACK length {len(body)} != 2 for 3.1.1")
        packet_id, _ = _DecodeUint16(body, 0)
        if packet_id == 0:
            raise MalformedPacketException("UNSUBACK packet identifier must not be 0")
        return UnsubAckPacket(packet_id=packet_id)
    raise NotImplementedError("UNSUBACK decoder only supports 3.1.1 today")


def _DecodeDisconnect(flags: int, body: bytes, protocol_level: int) -> DisconnectPacket:
    _RequireFlags(flags, 0, "DISCONNECT")
    if protocol_level == ProtocolLevel.MQTT_3_1_1:
        if len(body) != 0:
            raise MalformedPacketException(f"DISCONNECT length {len(body)} != 0 for 3.1.1")
        return DisconnectPacket()
    raise NotImplementedError("DISCONNECT decoder only supports 3.1.1 today")


# Sanity utility exposed for tests / debug.
def IsValidPacketIdentifier(packet_id: int) -> bool:
    return 1 <= packet_id <= MAX_PACKET_IDENTIFIER
