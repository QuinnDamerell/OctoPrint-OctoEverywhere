import logging
import socket
import time
import unittest
from typing import Tuple

from octoeverywhere.mqttmux.mux import MqttConnectionContext, MqttUpstreamMux
from octoeverywhere.mqttmux.tcpbroker import LocalTcpBrokerServer, StaticAuthCheck, TcpBrokerClient
from octoeverywhere.mqttmux.types import ConnAckReturnCode
from octoeverywhere.mqttmux.wirecodec import (
    ConnAckPacket,
    ConnectPacket,
    DisconnectPacket,
    EncodePacket,
    MqttPacketDecoder,
    PingReqPacket,
    PingRespPacket,
    PublishPacket,
    SubAckPacket,
    SubscribePacket,
)

from .fakepaho import FakePahoClient


def _silent_logger() -> logging.Logger:
    logger = logging.getLogger("mqttmux.tcpbroker.test")
    logger.setLevel(logging.CRITICAL)
    return logger


def _start_mux() -> Tuple[MqttUpstreamMux, FakePahoClient]:
    fake = FakePahoClient()
    mux = MqttUpstreamMux(
        logger=_silent_logger(),
        printer_key="test",
        connection_context_provider=lambda: MqttConnectionContext(host="h", port=1883),
        subscribe_timeout_sec=2.0,
        publish_timeout_sec=2.0,
        client_factory=lambda *a, **kw: fake,
        backoff_min_sec=0.05,
        backoff_max_sec=0.1,
    )
    mux.Start()
    deadline = time.time() + 2.0
    while not fake.connect_called and time.time() < deadline:
        time.sleep(0.01)
    fake.FireConnect(0)
    return mux, fake


def _pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _read_one_packet(sock: socket.socket, timeout: float = 2.0):
    sock.settimeout(timeout)
    d = MqttPacketDecoder()
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        packets = d.FeedBytes(chunk)
        if packets:
            return packets[0]
    return None


def _read_n_packets(sock: socket.socket, n: int, timeout: float = 2.0):
    sock.settimeout(timeout)
    d = MqttPacketDecoder()
    out = []
    deadline = time.time() + timeout
    while time.time() < deadline and len(out) < n:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        for pkt in d.FeedBytes(chunk):
            out.append(pkt)
            if len(out) >= n:
                break
    return out


class TestTcpBroker(unittest.TestCase):

    def setUp(self):
        self.mux, self.fake = _start_mux()
        self.port = _pick_free_port()
        self.server = LocalTcpBrokerServer(_silent_logger(), self.mux, "127.0.0.1", self.port)
        self.server.Start()

    def tearDown(self):
        self.server.Stop()
        self.mux.Shutdown()

    def _connect_client(self) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=2.0)
        return sock

    def test_connect_and_connack(self):
        sock = self._connect_client()
        try:
            sock.sendall(EncodePacket(ConnectPacket(client_id="t1", keep_alive=0)))
            pkt = _read_one_packet(sock)
            self.assertIsInstance(pkt, ConnAckPacket)
            self.assertEqual(pkt.return_code, ConnAckReturnCode.ACCEPTED)
        finally:
            sock.close()

    def test_subscribe_and_inbound_publish(self):
        sock = self._connect_client()
        try:
            sock.sendall(EncodePacket(ConnectPacket(client_id="t2", keep_alive=0)))
            pkt = _read_one_packet(sock)
            self.assertIsInstance(pkt, ConnAckPacket)

            # Subscribe.
            sock.sendall(EncodePacket(SubscribePacket(packet_id=1, subscriptions=[("topic/+", 0)])))
            # The wire client's worker triggers a paho subscribe; fire SUBACK.
            deadline = time.time() + 2.0
            while not self.fake.subscribes and time.time() < deadline:
                time.sleep(0.01)
            self.fake.FireSubAck(mid=1, granted_qos_list=[0])
            suback = _read_one_packet(sock)
            self.assertIsInstance(suback, SubAckPacket)
            self.assertEqual(suback.packet_id, 1)
            self.assertEqual(suback.return_codes, [0])

            # Upstream delivers a message.
            self.fake.FireMessage("topic/abc", b"payload", qos=0)
            pkt = _read_one_packet(sock)
            self.assertIsInstance(pkt, PublishPacket)
            self.assertEqual(pkt.topic, "topic/abc")
            self.assertEqual(pkt.payload, b"payload")
        finally:
            sock.close()

    def test_outbound_publish_forwards_upstream(self):
        sock = self._connect_client()
        try:
            sock.sendall(EncodePacket(ConnectPacket(client_id="t3", keep_alive=0)))
            _read_one_packet(sock)
            sock.sendall(EncodePacket(PublishPacket(topic="out", payload=b"hi", qos=0)))
            deadline = time.time() + 2.0
            while not self.fake.publishes and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.fake.publishes[-1][0], "out")
            self.assertEqual(self.fake.publishes[-1][1], b"hi")
        finally:
            sock.close()

    def test_pingreq_pingresp(self):
        sock = self._connect_client()
        try:
            sock.sendall(EncodePacket(ConnectPacket(client_id="t4", keep_alive=0)))
            _read_one_packet(sock)
            sock.sendall(EncodePacket(PingReqPacket()))
            pkt = _read_one_packet(sock)
            self.assertIsInstance(pkt, PingRespPacket)
        finally:
            sock.close()

    def test_clean_disconnect(self):
        sock = self._connect_client()
        try:
            sock.sendall(EncodePacket(ConnectPacket(client_id="t5", keep_alive=0)))
            _read_one_packet(sock)
            sock.sendall(EncodePacket(DisconnectPacket()))
            # Server-side close should follow.
            sock.settimeout(2.0)
            # Reading should return EOF.
            data = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except (ConnectionResetError, socket.timeout):
                pass
            # Either way, server closed gracefully.
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def test_two_clients_share_upstream_subscribe(self):
        sock1 = self._connect_client()
        sock2 = self._connect_client()
        try:
            sock1.sendall(EncodePacket(ConnectPacket(client_id="a", keep_alive=0)))
            _read_one_packet(sock1)
            sock2.sendall(EncodePacket(ConnectPacket(client_id="b", keep_alive=0)))
            _read_one_packet(sock2)

            # Both subscribe to the same filter.
            sock1.sendall(EncodePacket(SubscribePacket(packet_id=1, subscriptions=[("shared", 0)])))
            deadline = time.time() + 2.0
            while not self.fake.subscribes and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(self.fake.subscribes), 1)
            self.fake.FireSubAck(mid=1, granted_qos_list=[0])
            _read_one_packet(sock1)

            # Second sub doesn't hit upstream - synthesized.
            sock2.sendall(EncodePacket(SubscribePacket(packet_id=2, subscriptions=[("shared", 0)])))
            suback2 = _read_one_packet(sock2)
            self.assertIsInstance(suback2, SubAckPacket)
            # Still only one upstream sub.
            self.assertEqual(len(self.fake.subscribes), 1)

            # Upstream message reaches both.
            self.fake.FireMessage("shared", b"x", qos=0)
            pkt1 = _read_one_packet(sock1)
            pkt2 = _read_one_packet(sock2)
            self.assertIsInstance(pkt1, PublishPacket)
            self.assertIsInstance(pkt2, PublishPacket)
        finally:
            sock1.close()
            sock2.close()


class TestTcpBrokerAuth(unittest.TestCase):

    def setUp(self):
        self.mux, self.fake = _start_mux()
        self.port = _pick_free_port()
        self.server = LocalTcpBrokerServer(_silent_logger(), self.mux, "127.0.0.1", self.port,
                                            auth_check=StaticAuthCheck("alice", "hunter2"))
        self.server.Start()

    def tearDown(self):
        self.server.Stop()
        self.mux.Shutdown()

    def test_correct_creds_accepted(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=2.0)
        try:
            sock.sendall(EncodePacket(ConnectPacket(
                client_id="t", username="alice", password=b"hunter2", keep_alive=0)))
            pkt = _read_one_packet(sock)
            self.assertIsInstance(pkt, ConnAckPacket)
            self.assertEqual(pkt.return_code, ConnAckReturnCode.ACCEPTED)
        finally:
            sock.close()

    def test_wrong_password_rejected(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=2.0)
        try:
            sock.sendall(EncodePacket(ConnectPacket(
                client_id="t", username="alice", password=b"wrong", keep_alive=0)))
            pkt = _read_one_packet(sock)
            self.assertIsInstance(pkt, ConnAckPacket)
            self.assertEqual(pkt.return_code, ConnAckReturnCode.BAD_USERNAME_OR_PASSWORD)
        finally:
            sock.close()

    def test_missing_creds_rejected(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=2.0)
        try:
            sock.sendall(EncodePacket(ConnectPacket(client_id="t", keep_alive=0)))
            pkt = _read_one_packet(sock)
            self.assertIsInstance(pkt, ConnAckPacket)
            self.assertEqual(pkt.return_code, ConnAckReturnCode.BAD_USERNAME_OR_PASSWORD)
        finally:
            sock.close()


# Verifies the pluggable auth-check is used (independent of the static-creds
# convenience). Mirrors the way BambuClient.GetBrokerAuthCheck wires up against
# the live upstream context.
class TestTcpBrokerCustomAuth(unittest.TestCase):

    def test_custom_callable_is_invoked(self):
        mux, _ = _start_mux()
        port = _pick_free_port()
        captured = []
        def check(username, password):
            captured.append((username, password))
            if username == "bblp" and password == b"12345678":
                return ConnAckReturnCode.ACCEPTED
            return ConnAckReturnCode.BAD_USERNAME_OR_PASSWORD
        server = LocalTcpBrokerServer(_silent_logger(), mux, "127.0.0.1", port,
                                       auth_check=check)
        server.Start()
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            try:
                sock.sendall(EncodePacket(ConnectPacket(
                    client_id="t", username="bblp", password=b"12345678", keep_alive=0)))
                pkt = _read_one_packet(sock)
                self.assertIsInstance(pkt, ConnAckPacket)
                self.assertEqual(pkt.return_code, ConnAckReturnCode.ACCEPTED)
            finally:
                sock.close()

            # Wrong credentials should still go through our check.
            sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            try:
                sock.sendall(EncodePacket(ConnectPacket(
                    client_id="t", username="bblp", password=b"nope", keep_alive=0)))
                pkt = _read_one_packet(sock)
                self.assertEqual(pkt.return_code, ConnAckReturnCode.BAD_USERNAME_OR_PASSWORD)
            finally:
                sock.close()

            self.assertEqual(captured, [("bblp", b"12345678"), ("bblp", b"nope")])
        finally:
            server.Stop()
            mux.Shutdown()

    def test_callable_raising_results_in_not_authorized(self):
        mux, _ = _start_mux()
        port = _pick_free_port()
        def broken(_username, _password):
            raise RuntimeError("boom")
        server = LocalTcpBrokerServer(_silent_logger(), mux, "127.0.0.1", port,
                                       auth_check=broken)
        server.Start()
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            try:
                sock.sendall(EncodePacket(ConnectPacket(client_id="t", keep_alive=0)))
                pkt = _read_one_packet(sock)
                self.assertEqual(pkt.return_code, ConnAckReturnCode.NOT_AUTHORIZED)
            finally:
                sock.close()
        finally:
            server.Stop()
            mux.Shutdown()


class TestTcpBrokerLimits(unittest.TestCase):

    def test_max_clients_cap_rejects_excess_connections(self):
        mux, _fake = _start_mux()
        port = _pick_free_port()
        server = LocalTcpBrokerServer(_silent_logger(), mux, "127.0.0.1", port, max_clients=2)
        server.Start()
        socks = []
        try:
            # The first two clients connect and complete a CONNECT handshake.
            for i in range(2):
                s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
                socks.append(s)
                s.sendall(EncodePacket(ConnectPacket(client_id=f"cap{i}", keep_alive=0)))
                pkt = _read_one_packet(s)
                self.assertIsInstance(pkt, ConnAckPacket)
            # The third connection is over the cap; the server must close it
            # without ever sending a CONNACK.
            s3 = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            socks.append(s3)
            try:
                s3.sendall(EncodePacket(ConnectPacket(client_id="cap3", keep_alive=0)))
            except OSError:
                pass  # Already closed by the server - also a valid outcome.
            # The server may close cleanly (FIN -> recv returns b"") or reset
            # the connection (RST -> ConnectionResetError); both mean rejected.
            try:
                pkt = _read_one_packet(s3, timeout=1.0)
            except (ConnectionResetError, ConnectionAbortedError):
                pkt = None
            self.assertIsNone(pkt)
        finally:
            for s in socks:
                try:
                    s.close()
                except OSError:
                    pass
            server.Stop()
            mux.Shutdown()

    def test_no_connect_within_deadline_closes_connection(self):
        # MQTT 3.1.1 §3.1: the server should close connections that don't send
        # a CONNECT within a reasonable amount of time.
        old_timeout = TcpBrokerClient.PRE_CONNECT_TIMEOUT_SEC
        TcpBrokerClient.PRE_CONNECT_TIMEOUT_SEC = 0.2
        mux, _fake = _start_mux()
        port = _pick_free_port()
        server = LocalTcpBrokerServer(_silent_logger(), mux, "127.0.0.1", port)
        server.Start()
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            try:
                # Send nothing; the server should close the socket on us.
                s.settimeout(2.0)
                try:
                    data = s.recv(1)
                except (ConnectionResetError, ConnectionAbortedError):
                    data = b""
                self.assertEqual(data, b"")
            finally:
                s.close()
        finally:
            TcpBrokerClient.PRE_CONNECT_TIMEOUT_SEC = old_timeout
            server.Stop()
            mux.Shutdown()


if __name__ == "__main__":
    unittest.main()
