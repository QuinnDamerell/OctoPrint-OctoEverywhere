from typing import Any, Dict, Tuple

from .types import MqttException


# MQTT 5 property identifiers (§2.2.2.2). Defined as named constants today so
# the codec can reference them in future v5 work. The encode/decode helpers
# raise on actual v5 input until properly implemented - this is a hard
# implementation gate, not silent acceptance.

PAYLOAD_FORMAT_INDICATOR = 0x01
MESSAGE_EXPIRY_INTERVAL = 0x02
CONTENT_TYPE = 0x03
RESPONSE_TOPIC = 0x08
CORRELATION_DATA = 0x09
SUBSCRIPTION_IDENTIFIER = 0x0B
SESSION_EXPIRY_INTERVAL = 0x11
ASSIGNED_CLIENT_IDENTIFIER = 0x12
SERVER_KEEP_ALIVE = 0x13
AUTHENTICATION_METHOD = 0x15
AUTHENTICATION_DATA = 0x16
REQUEST_PROBLEM_INFORMATION = 0x17
WILL_DELAY_INTERVAL = 0x18
REQUEST_RESPONSE_INFORMATION = 0x19
RESPONSE_INFORMATION = 0x1A
SERVER_REFERENCE = 0x1C
REASON_STRING = 0x1F
RECEIVE_MAXIMUM = 0x21
TOPIC_ALIAS_MAXIMUM = 0x22
TOPIC_ALIAS = 0x23
MAXIMUM_QOS = 0x24
RETAIN_AVAILABLE = 0x25
USER_PROPERTY = 0x26
MAXIMUM_PACKET_SIZE = 0x27
WILDCARD_SUBSCRIPTION_AVAILABLE = 0x28
SUBSCRIPTION_IDENTIFIER_AVAILABLE = 0x29
SHARED_SUBSCRIPTION_AVAILABLE = 0x2A


class PropertiesNotImplementedError(MqttException):
    pass


# Encode a property dictionary to wire bytes. Returns the property block
# including the leading variable-byte-integer length.
#
# 3.1.1 callers should never invoke this with non-empty input; the wire codec
# only emits the property block on v5 packets. When v5 is implemented, this
# becomes a real encoder; today it raises if anything but None/empty is given
# so a subtle v5 leak fails loud.
def EncodeProperties(props: Dict[int, Any]) -> bytes:
    if props is None or len(props) == 0:
        return b""
    raise PropertiesNotImplementedError(
        "MQTT 5 properties encoder not implemented; this is reachable only "
        "from MQTT 5 packets, which the mux currently rejects at CONNECT.")


# Decode a property block from buf starting at offset. Returns (props, new_offset).
# 3.1.1 callers should never invoke this; included for future v5 work and to
# make the call sites in wirecodec.py read naturally.
def DecodeProperties(buf: bytes, offset: int) -> Tuple[Dict[int, Any], int]:  # pragma: no cover - not yet used
    raise PropertiesNotImplementedError(
        "MQTT 5 properties decoder not implemented; this is reachable only "
        "from MQTT 5 packets, which the mux currently rejects at CONNECT.")
