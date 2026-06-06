import threading
from typing import Any, Callable, List, Optional, Tuple

from paho.mqtt.enums import MQTTErrorCode


# Minimal paho.mqtt.client.Client stand-in for unit tests of MqttUpstreamMux.
#
# The mux only calls a small set of paho methods: connect/disconnect, loop_start,
# loop_stop, subscribe, unsubscribe, publish, reconnect_delay_set, tls_*,
# ws_set_options, username_pw_set, on_* callback setters. We implement just
# those, plus a handful of test-side helpers (FireConnect, FireMessage, ...)
# to drive callbacks deterministically.


class FakeReasonCode:
    def __init__(self, value: int) -> None:
        self.value = value
        self.is_failure = value not in (0, 1, 2)

    def __str__(self) -> str:
        return str(self.value)


class FakePublishInfo:
    def __init__(self, mid: int, rc: int = MQTTErrorCode.MQTT_ERR_SUCCESS,
                 auto_complete: bool = True) -> None:
        self.mid = mid
        self.rc = rc
        self._published = threading.Event()
        if auto_complete:
            self._published.set()

    def wait_for_publish(self, timeout: Optional[float] = None) -> None:
        if self.rc != MQTTErrorCode.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"Message publish failed: {self.rc}")
        self._published.wait(timeout)

    def is_published(self) -> bool:
        return self._published.is_set()

    def Complete(self) -> None:
        self._published.set()


class FakePahoClient:

    def __init__(self, callback_api_version: Any = None,
                 client_id: str = "", transport: str = "tcp") -> None:
        self.client_id = client_id
        self.transport = transport
        self.connect_called = False
        self.loop_started = False
        self.loop_stopped = False
        self.disconnect_called = False
        self.tls_args: Optional[Tuple] = None
        self.tls_insecure = False
        self.ws_path: Optional[str] = None
        self.username: Optional[str] = None
        self.password: Optional[str] = None
        self.connect_args: Optional[Tuple[str, int, int]] = None
        # Track subscribe/unsubscribe/publish calls so tests can assert on them.
        self.subscribes: List[Tuple[str, int]] = []
        self.unsubscribes: List[str] = []
        self.publishes: List[Tuple[str, bytes, int, bool, int]] = []  # topic, payload, qos, retain, mid
        self.publish_infos: List[FakePublishInfo] = []
        self.publish_rc = MQTTErrorCode.MQTT_ERR_SUCCESS
        self.publish_auto_complete = True
        # Callbacks the mux installs.
        self.on_connect: Optional[Callable] = None
        self.on_disconnect: Optional[Callable] = None
        self.on_message: Optional[Callable] = None
        self.on_subscribe: Optional[Callable] = None
        self.on_unsubscribe: Optional[Callable] = None
        self.on_log: Optional[Callable] = None
        self._next_mid = 1
        self._mid_lock = threading.Lock()

    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> None:
        pass

    def tls_set(self, *args, **kwargs) -> None:  # noqa: ARG002
        self.tls_args = (args, kwargs)

    def tls_insecure_set(self, insecure: bool) -> None:
        self.tls_insecure = insecure

    def ws_set_options(self, path: Optional[str] = None) -> None:
        self.ws_path = path

    def username_pw_set(self, username: Optional[str], password: Optional[str]) -> None:
        self.username = username
        self.password = password

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connect_called = True
        self.connect_args = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def disconnect(self) -> None:
        self.disconnect_called = True

    def _NextMid(self) -> int:
        with self._mid_lock:
            mid = self._next_mid
            self._next_mid += 1
            return mid

    def subscribe(self, filter_: str, qos: int = 0) -> Tuple[int, int]:
        mid = self._NextMid()
        self.subscribes.append((filter_, qos))
        return (MQTTErrorCode.MQTT_ERR_SUCCESS, mid)

    def unsubscribe(self, filter_: str) -> Tuple[int, int]:
        mid = self._NextMid()
        self.unsubscribes.append(filter_)
        return (MQTTErrorCode.MQTT_ERR_SUCCESS, mid)

    def publish(self, topic: str, payload=None, qos: int = 0, retain: bool = False) -> FakePublishInfo:
        mid = self._NextMid()
        if isinstance(payload, str):
            payload_bytes = payload.encode("utf-8")
        elif payload is None:
            payload_bytes = b""
        else:
            payload_bytes = bytes(payload)
        self.publishes.append((topic, payload_bytes, qos, retain, mid))
        info = FakePublishInfo(mid=mid, rc=self.publish_rc, auto_complete=self.publish_auto_complete)
        self.publish_infos.append(info)
        return info

    # ----- test driver helpers -----

    def FireConnect(self, reason_value: int = 0) -> None:
        if self.on_connect is None:
            return
        self.on_connect(self, None, {}, FakeReasonCode(reason_value), None)

    def FireDisconnect(self, reason_value: int = 0) -> None:
        if self.on_disconnect is None:
            return
        self.on_disconnect(self, None, {}, FakeReasonCode(reason_value), None)

    def FireMessage(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False, mid: int = 0) -> None:
        if self.on_message is None:
            return
        msg = _FakeMqttMessage(topic=topic, payload=payload, qos=qos, retain=retain, mid=mid)
        self.on_message(self, None, msg)

    def FireSubAck(self, mid: int, granted_qos_list: List[int]) -> None:
        if self.on_subscribe is None:
            return
        rcs = [FakeReasonCode(q) for q in granted_qos_list]
        self.on_subscribe(self, None, mid, rcs, None)

    def FireUnsubAck(self, mid: int) -> None:
        if self.on_unsubscribe is None:
            return
        self.on_unsubscribe(self, None, mid, [], None)


class _FakeMqttMessage:
    def __init__(self, topic: str, payload: bytes, qos: int, retain: bool, mid: int) -> None:
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.retain = retain
        self.mid = mid
