from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional


# MQTT protocol levels we know about. Today we only implement 3.1.1; the codec
# and wire client keep `protocol_level` as a parameter so MQTT 5 can be added
# without restructuring.
class ProtocolLevel(IntEnum):
    MQTT_3_1 = 3      # legacy "MQIsdp", we reject
    MQTT_3_1_1 = 4    # our implemented version
    MQTT_5 = 5        # future support


class QoS(IntEnum):
    AT_MOST_ONCE = 0
    AT_LEAST_ONCE = 1
    EXACTLY_ONCE = 2


# MQTT control packet types per MQTT 3.1.1 §2.2.1.
class PacketType(IntEnum):
    CONNECT = 1
    CONNACK = 2
    PUBLISH = 3
    PUBACK = 4
    PUBREC = 5
    PUBREL = 6
    PUBCOMP = 7
    SUBSCRIBE = 8
    SUBACK = 9
    UNSUBSCRIBE = 10
    UNSUBACK = 11
    PINGREQ = 12
    PINGRESP = 13
    DISCONNECT = 14
    AUTH = 15  # MQTT 5 only


# CONNACK return codes for MQTT 3.1.1 (§3.2.2.3). v5 uses a different code space;
# the wire encoder handles the mapping when v5 is added.
class ConnAckReturnCode(IntEnum):
    ACCEPTED = 0
    UNACCEPTABLE_PROTOCOL_VERSION = 1
    IDENTIFIER_REJECTED = 2
    SERVER_UNAVAILABLE = 3
    BAD_USERNAME_OR_PASSWORD = 4
    NOT_AUTHORIZED = 5


# SUBACK return codes for MQTT 3.1.1 (§3.9.3).
class SubAckReturnCode(IntEnum):
    GRANTED_QOS_0 = 0x00
    GRANTED_QOS_1 = 0x01
    GRANTED_QOS_2 = 0x02
    FAILURE = 0x80


# Spec-rooted exceptions raised by the codec/wire layer.
class MqttException(Exception):
    pass


class MalformedPacketException(MqttException):
    # The bytes on the wire don't conform to MQTT framing. Connection must be
    # closed per MQTT 3.1.1 §4.8 without further response.
    pass


class ProtocolError(MqttException):
    # Bytes were well-framed but violated protocol semantics (e.g. PUBACK packet
    # identifier we never assigned, SUBSCRIBE with no filters).
    pass


class UnsupportedProtocolVersion(MqttException):
    # CONNECT carried a protocol level we don't speak. Caller should reply with
    # CONNACK 0x01 and then close.
    pass


# An MQTT application message moving through the mux. Payload is always bytes
# on the wire; helpers that accept str do the encode at the boundary.
@dataclass
class MqttMessage:
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    # MQTT 5 fields - left as None for 3.1.1. The codec ignores these when
    # encoding a 3.1.1 packet. Adding v5 means populating them.
    properties: Optional[Dict[int, Any]] = None
    # Internal: the packet identifier the upstream side used (if any). Not
    # meaningful for QoS 0. Used by the mux to correlate PUBACK/PUBREC.
    packet_id: Optional[int] = None


# A subscription request from a downstream client. `qos` is the maximum QoS the
# client wants to receive. The mux may upgrade upstream to a higher QoS if some
# other downstream client also wants more.
@dataclass
class SubscriptionFilter:
    filter: str
    qos: int = 0
    # MQTT 5 only - subscription identifier, attached to matching PUBLISHes.
    subscription_identifier: Optional[int] = None
    # MQTT 5 only - no-local, retain-as-published, retain-handling. Defaults
    # match 3.1.1 behaviour. The codec ignores these when encoding 3.1.1.
    no_local: bool = False
    retain_as_published: bool = False
    retain_handling: int = 0


# A downstream client's Last-Will-and-Testament. The mux never forwards this
# upstream; it is broadcast only to other downstream subscribers on abnormal
# disconnect (see plan §7 / wireclient.py).
@dataclass
class WillSpec:
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False
    # MQTT 5 will-properties (e.g. will-delay-interval). None for 3.1.1.
    properties: Optional[Dict[int, Any]] = None


# Outcome of a downstream CONNECT processed by a wire virtual client.
@dataclass
class ConnectResult:
    success: bool
    return_code: int                  # 3.1.1 ConnAckReturnCode value
    session_present: bool = False
    assigned_client_id: Optional[str] = None
    # MQTT 5 only - CONNACK reason code + properties. Unused for 3.1.1.
    reason_code: Optional[int] = None
    properties: Optional[Dict[int, Any]] = None


# A subscription token issued by LocalPluginClient.Subscribe(). Opaque to
# callers; the only thing they can do with it is hand it back to Unsubscribe().
@dataclass(frozen=True)
class SubToken:
    handle_id: int
    filter: str
    sequence: int                     # disambiguates multiple subs to the same filter


# Type alias for the callback signature used by LocalPluginClient subscribers.
MessageCallback = Callable[[MqttMessage], None]
ConnectionCallback = Callable[[], None]


# Per-subscriber dispatch record produced by SubscriptionTable.GetMatchingSubscribers.
# A v5 client may match multiple of its own subs with different subscription
# identifiers, so we carry a list rather than a single value.
@dataclass
class MatchResult:
    handle_id: int
    qos: int
    callback: Optional[MessageCallback]
    subscription_identifiers: List[int] = field(default_factory=list)


# Spec-defined wire constants the codec and wire client both reference.
MAX_PACKET_IDENTIFIER = 0xFFFF
MAX_TOPIC_BYTES = 0xFFFF
MAX_REMAINING_LENGTH = 268_435_455  # 256 MB - MQTT 3.1.1 §2.2.3 limit on VBI
DEFAULT_RECEIVE_MAXIMUM = 65535     # MQTT 5 default; we don't expose this to 3.1.1
