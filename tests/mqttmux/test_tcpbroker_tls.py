import logging
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from docker_octoeverywhere.MQTTBootstrap import MQTTBootstrap
from linux_host.config import Config
from octoeverywhere.mqttmux.tcpbroker import LocalTcpBrokerServer, StaticAuthCheck, TcpBrokerClient
from octoeverywhere.mqttmux.tlscert import CreateServerTlsContext
from octoeverywhere.mqttmux.types import ConnAckReturnCode
from octoeverywhere.mqttmux.wirecodec import (
    ConnectPacket, EncodePacket, MqttPacketDecoder, PingReqPacket, PingRespPacket,
    PublishPacket, SubscribePacket, SubAckPacket,
)
from .test_tcpbroker import _pick_free_port, _read_one_packet, _start_mux


class TestTcpBrokerTls(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.storage = tempfile.TemporaryDirectory()
        cls.logger = logging.getLogger("mqttmux.tls.test")
        cls.tls_context = CreateServerTlsContext(cls.logger, cls.storage.name)

    @classmethod
    def tearDownClass(cls):
        cls.storage.cleanup()

    def setUp(self):
        self.mux, self.fake = _start_mux()
        self.addCleanup(self.mux.Shutdown)

    def _start(self, tls=True, **kwargs):
        server = LocalTcpBrokerServer(self.logger, self.mux, "127.0.0.1", 0,
                                      tls_context=self.tls_context if tls else None, **kwargs)
        server.Start()
        self.addCleanup(server.Stop)
        return server, server._listener.getsockname()[1]

    @staticmethod
    def _client_context():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _socket(self, port, tls=False):
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        self.addCleanup(sock.close)
        if tls:
            sock = self._client_context().wrap_socket(sock, server_hostname="localhost")
            self.addCleanup(sock.close)
        return sock

    def _connect(self, sock, client_id="test", password=None):
        sock.sendall(EncodePacket(ConnectPacket(client_id=client_id, keep_alive=0,
                                               username="bblp" if password is not None else None,
                                               password=password)))
        return _read_one_packet(sock)

    def _assert_closed(self, sock):
        sock.settimeout(2)
        try:
            self.assertEqual(sock.recv(4096), b"")
        except (ConnectionResetError, ConnectionAbortedError):
            pass

    def _wait(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(predicate())

    def test_primary_accepts_plaintext_and_tls_with_auth_and_ping(self):
        _, port = self._start(auth_check=StaticAuthCheck("bblp", "code"))
        for tls in (False, True):
            with self.subTest(tls=tls):
                sock = self._socket(port, tls=tls)
                self.assertEqual(self._connect(sock, str(tls), b"code").return_code, ConnAckReturnCode.ACCEPTED)
                sock.sendall(EncodePacket(PingReqPacket()))
                self.assertIsInstance(_read_one_packet(sock), PingRespPacket)
                bad = self._socket(port, tls=tls)
                self.assertEqual(self._connect(bad, "bad" + str(tls), b"wrong").return_code,
                                 ConnAckReturnCode.BAD_USERNAME_OR_PASSWORD)

    def test_tls_listener_rejects_plaintext(self):
        tls_port = _pick_free_port()
        _, _ = self._start(tls_port=tls_port)
        secure = self._socket(tls_port, tls=True)
        self.assertEqual(self._connect(secure).return_code, ConnAckReturnCode.ACCEPTED)
        plain = self._socket(tls_port)
        with self.assertLogs(self.logger, level="WARNING") as captured:
            plain.sendall(EncodePacket(ConnectPacket(client_id="plain")))
            self._assert_closed(plain)
        self.assertTrue(any("TLS-only" in message for message in captured.output))

    def test_tls_subscriptions_and_publish_reach_upstream(self):
        _, port = self._start()
        sock = self._socket(port, tls=True)
        self._connect(sock)
        sock.sendall(EncodePacket(SubscribePacket(packet_id=1, subscriptions=[("device/test/report", 0)])))
        self._wait(lambda: bool(self.fake.subscribes))
        self.fake.FireSubAck(mid=1, granted_qos_list=[0])
        self.assertIsInstance(_read_one_packet(sock), SubAckPacket)
        self.fake.FireMessage("device/test/report", b"status", qos=0)
        report = _read_one_packet(sock)
        self.assertIsInstance(report, PublishPacket)
        self.assertEqual(report.payload, b"status")
        sock.sendall(EncodePacket(PublishPacket(topic="device/test/request", payload=b"command", qos=0)))
        self._wait(lambda: bool(self.fake.publishes))
        self.assertEqual(self.fake.publishes[0][0:2], ("device/test/request", b"command"))

    def test_fragmented_client_hello_is_preserved(self):
        server, port = self._start()
        sock = self._socket(port)
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        client = self._client_context().wrap_bio(incoming, outgoing, server_hostname="localhost")
        with self.assertRaises(ssl.SSLWantReadError):
            client.do_handshake()
        hello = outgoing.read()
        sock.sendall(hello[:1])
        # Wait until the server is handshaking with only the first byte, then
        # deliver the remainder. This exercises sniffing across real fragments.
        self._wait(lambda: bool(server._clients) and isinstance(server._clients[0]._socket, ssl.SSLSocket))
        sock.sendall(hello[1:])
        while True:
            try:
                client.do_handshake()
                break
            except ssl.SSLWantReadError:
                pending = outgoing.read()
                if pending:
                    sock.sendall(pending)
                incoming.write(sock.recv(16384))
        pending = outgoing.read()
        if pending:
            sock.sendall(pending)
        client.write(EncodePacket(ConnectPacket(client_id="fragmented", keep_alive=0)))
        sock.sendall(outgoing.read())
        while True:
            try:
                response = client.read(16384)
                break
            except ssl.SSLWantReadError:
                incoming.write(sock.recv(16384))
        self.assertEqual(MqttPacketDecoder().FeedBytes(response)[0].return_code, ConnAckReturnCode.ACCEPTED)

    def test_stalled_tls_handshake_does_not_block_other_clients(self):
        with patch.object(TcpBrokerClient, "PRE_CONNECT_TIMEOUT_SEC", 0.5):
            _, port = self._start()
            stalled = self._socket(port)
            stalled.sendall(b"\x16")
            healthy = self._socket(port, tls=True)
            self.assertEqual(self._connect(healthy).return_code, ConnAckReturnCode.ACCEPTED)
            self._assert_closed(stalled)
            healthy.sendall(EncodePacket(PingReqPacket()))
            self.assertIsInstance(_read_one_packet(healthy), PingRespPacket)

    def test_pending_handshakes_count_toward_shared_listener_limit(self):
        tls_port = _pick_free_port()
        server, port = self._start(tls_port=tls_port, max_clients=1)
        stalled = self._socket(port)
        stalled.sendall(b"\x16")
        self._wait(lambda: len(server._clients) == 1)
        rejected = self._socket(tls_port)
        self._assert_closed(rejected)
        stalled.close()
        self._wait(lambda: not server._clients)
        healthy = self._socket(tls_port, tls=True)
        self.assertEqual(self._connect(healthy).return_code, ConnAckReturnCode.ACCEPTED)

    def test_stop_interrupts_tls_handshake_and_closes_both_listeners(self):
        tls_port = _pick_free_port()
        server, port = self._start(tls_port=tls_port)
        sock = self._socket(tls_port)
        sock.sendall(b"\x16")
        self._wait(lambda: bool(server._clients) and isinstance(server._clients[0]._socket, ssl.SSLSocket))
        reader = server._clients[0]._reader_thread
        server.Stop()
        reader.join(timeout=2)
        self.assertFalse(reader.is_alive())
        self._assert_closed(sock)
        for closed_port in (port, tls_port):
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", closed_port), timeout=0.2)

    def test_partial_mqtt_connect_has_one_total_deadline(self):
        with patch.object(TcpBrokerClient, "PRE_CONNECT_TIMEOUT_SEC", 0.4):
            _, port = self._start()
            sock = self._socket(port)
            start = time.monotonic()
            sock.sendall(b"\x10")
            time.sleep(0.25)
            sock.sendall(b"\x20")
            self._assert_closed(sock)
            self.assertLess(time.monotonic() - start, 0.6)

    def test_tls_request_without_context_has_actionable_error(self):
        _, port = self._start(tls=False)
        sock = self._socket(port)
        with self.assertLogs(self.logger, level="WARNING") as captured:
            sock.sendall(b"\x16\x03\x01\x00\x05")
            self._assert_closed(sock)
        self.assertTrue(any("TLS is unavailable" in message for message in captured.output))
        self.assertFalse(any("malformed packet" in message for message in captured.output))

    def test_occupied_tls_port_preserves_primary_listener(self):
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        self.addCleanup(occupied.close)
        with self.assertLogs(self.logger, level="WARNING") as captured:
            _, port = self._start(tls_port=occupied.getsockname()[1])
        self.assertTrue(any("additional TLS listener" in message for message in captured.output))
        for tls in (False, True):
            sock = self._socket(port, tls=tls)
            self.assertEqual(self._connect(sock, str(tls)).return_code, ConnAckReturnCode.ACCEPTED)

    def test_same_primary_and_tls_port_uses_single_mixed_listener(self):
        port = _pick_free_port()
        server = LocalTcpBrokerServer(self.logger, self.mux, "127.0.0.1", port,
                                      tls_context=self.tls_context, tls_port=port)
        self.addCleanup(server.Stop)
        server.Start()
        self.assertIsNone(server._tls_listener)
        for tls in (False, True):
            self.assertEqual(self._connect(self._socket(port, tls=tls), str(tls)).return_code,
                             ConnAckReturnCode.ACCEPTED)

    def test_config_tls_failures_reject_connections_and_preserve_plaintext(self):
        failures = [ValueError("bad cert"), RuntimeError("OpenSSL generation failed"),
                    subprocess.TimeoutExpired("openssl", 60)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                config = Config(directory)
                port, tls_port = _pick_free_port(), _pick_free_port()
                while tls_port == port:
                    tls_port = _pick_free_port()
                config.SetStr(Config.SectionMqtt, Config.MqttLocalBrokerBind, "127.0.0.1")
                config.SetInt(Config.SectionMqtt, Config.MqttLocalBrokerPort, port)
                config.SetInt(Config.SectionMqtt, Config.MqttLocalBrokerTlsPort, tls_port)
                config.SetBool(Config.SectionMqtt, Config.MqttLocalBrokerRequireUpstreamAuth, False)
                with patch("octoeverywhere.mqttmux.tcpbroker.CreateServerTlsContext", side_effect=failure):
                    with self.assertLogs(self.logger, level="ERROR") as startup:
                        server = LocalTcpBrokerServer.MaybeStartFromConfig(self.logger, config, self.mux)
                self.assertIsNotNone(server)
                self.addCleanup(server.Stop)
                self.assertTrue(any(str(failure) in message for message in startup.output))
                self.assertIsNotNone(server._tls_listener)
                with self.assertLogs(self.logger, level="WARNING") as connections:
                    # A TLS-only connection must close promptly, even before the
                    # client sends any bytes. The mixed port must reject TLS, too.
                    self._assert_closed(self._socket(tls_port))
                    for rejected_port in (port, tls_port):
                        with self.assertRaises((ssl.SSLError, ConnectionError)):
                            self._socket(rejected_port, tls=True)
                self.assertEqual(sum("TLS is unavailable" in message for message in connections.output), 3)
                self.assertFalse(any("malformed packet" in message for message in connections.output))
                self.assertEqual(self._connect(self._socket(port)).return_code, ConnAckReturnCode.ACCEPTED)
                server.Stop()

    def test_config_and_docker_tls_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(directory)
            config.SetBool(Config.SectionMqtt, Config.MqttLocalBrokerRequireUpstreamAuth, False)
            env = {"MQTT_RELAY_TLS_PORT": "0", "MQTT_RELAY_TLS_CERT_PATH": "/certs/server.crt",
                   "MQTT_RELAY_TLS_KEY_PATH": "/certs/server.key"}
            with patch.dict(os.environ, env, clear=True):
                MQTTBootstrap.Bootstrap(self.logger, config)
            with patch("octoeverywhere.mqttmux.tcpbroker.CreateServerTlsContext", return_value=self.tls_context) as create:
                with patch.object(LocalTcpBrokerServer, "Start"):
                    server = LocalTcpBrokerServer.MaybeStartFromConfig(self.logger, config, self.mux)
            self.assertEqual(server._tls_port, 0)
            self.assertIs(server._tls_context, self.tls_context)
            create.assert_called_once_with(self.logger, directory, cert_path="/certs/server.crt",
                                           key_path="/certs/server.key", bind="0.0.0.0")

    def test_blank_docker_tls_port_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(directory)
            config.SetInt(Config.SectionMqtt, Config.MqttLocalBrokerTlsPort, 9883)
            with patch.dict(os.environ, {"MQTT_RELAY_TLS_PORT": " "}, clear=True):
                MQTTBootstrap.Bootstrap(self.logger, config)
            self.assertEqual(config.GetInt(Config.SectionMqtt, Config.MqttLocalBrokerTlsPort, 8883), 9883)

    def test_tls_refresh_swaps_context_for_new_connections(self):
        refreshed = threading.Event()
        def refresh():
            refreshed.set()
            return self.tls_context
        with patch.object(LocalTcpBrokerServer, "TLS_REFRESH_INTERVAL_SEC", 0.05):
            server, port = self._start(tls=False, tls_refresh=refresh)
            self.assertTrue(refreshed.wait(2))
        self._wait(lambda: server._tls_context is self.tls_context)
        self.assertEqual(self._connect(self._socket(port, tls=True)).return_code, ConnAckReturnCode.ACCEPTED)

    def test_failed_tls_refresh_keeps_current_context(self):
        calls = []
        def refresh():
            calls.append(1)
            raise RuntimeError("openssl failed")
        with patch.object(LocalTcpBrokerServer, "TLS_REFRESH_INTERVAL_SEC", 0.05):
            server, port = self._start(tls_refresh=refresh)
            self._wait(lambda: len(calls) >= 2)
        self.assertIs(server._tls_context, self.tls_context)
        self.assertEqual(self._connect(self._socket(port, tls=True)).return_code, ConnAckReturnCode.ACCEPTED)

    def test_plaintext_clients_stay_blocking(self):
        server, port = self._start()
        self.assertEqual(self._connect(self._socket(port)).return_code, ConnAckReturnCode.ACCEPTED)
        self._wait(lambda: len(server._clients) == 1)
        client = server._clients[0]
        self.assertFalse(client._is_tls)
        self.assertIsNotNone(client._socket.gettimeout())

    def test_tls12_full_duplex_serializes_ssl_operations(self):
        self._exercise_full_duplex(ssl.TLSVersion.TLSv1_2)

    @unittest.skipUnless(getattr(ssl, "HAS_TLSv1_3", False), "TLS 1.3 is unavailable")
    def test_tls13_full_duplex_serializes_ssl_operations(self):
        self._exercise_full_duplex(ssl.TLSVersion.TLSv1_3)

    def _exercise_full_duplex(self, version):
        # OpenSSL forbids concurrent operations on one SSL object, even a read
        # and a write. Yield inside each operation to expose accidental overlap
        # while exercising real TLS framing and MQTT delivery in both directions.
        operation_lock = threading.Lock()
        concurrent_operations = []

        def audited(operation, *args, **kwargs):
            if not operation_lock.acquire(blocking=False):
                concurrent_operations.append(operation.__name__)
                raise AssertionError("Concurrent operations on one SSL socket")
            try:
                time.sleep(0.0005)
                return operation(*args, **kwargs)
            finally:
                operation_lock.release()

        class AuditedSslSocket(ssl.SSLSocket):
            def recv(self, *args, **kwargs):
                return audited(super().recv, *args, **kwargs)

            def send(self, *args, **kwargs):
                return audited(super().send, *args, **kwargs)

        context = CreateServerTlsContext(self.logger, self.storage.name)
        context.minimum_version = version
        context.maximum_version = version
        context.sslsocket_class = AuditedSslSocket
        server = LocalTcpBrokerServer(self.logger, self.mux, "127.0.0.1", 0, tls_context=context)
        self.addCleanup(server.Stop)
        server.Start()
        sock = self._socket(server._listener.getsockname()[1], tls=True)
        self.assertEqual(sock.version(), "TLSv1.2" if version == ssl.TLSVersion.TLSv1_2 else "TLSv1.3")
        self._connect(sock)
        topic = "device/duplex/report"
        sock.sendall(EncodePacket(SubscribePacket(packet_id=1, subscriptions=[(topic, 0)])))
        self._wait(lambda: bool(self.fake.subscribes))
        self.fake.FireSubAck(mid=1, granted_qos_list=[0])
        self.assertIsInstance(_read_one_packet(sock), SubAckPacket)

        count = 300
        expected = [str(sequence).encode("ascii") + b":" + b"x" * 256 for sequence in range(count)]
        stop = threading.Event()

        def publish_reports():
            for payload in expected:
                if stop.is_set():
                    return
                self.fake.FireMessage(topic, payload)

        publisher = threading.Thread(target=publish_reports, daemon=True)
        publisher.start()
        decoder = MqttPacketDecoder()
        reports = []
        pings_sent = pings_received = 0
        deadline = time.monotonic() + 10
        try:
            while len(reports) < count or pings_received < count:
                self.assertLess(time.monotonic(), deadline, "Duplex TLS delivery stalled")
                if pings_sent < count:
                    sock.sendall(EncodePacket(PingReqPacket()))
                    pings_sent += 1
                data = sock.recv(16384)
                self.assertTrue(data, "TLS socket closed during duplex traffic")
                for packet in decoder.FeedBytes(data):
                    if isinstance(packet, PublishPacket):
                        self.assertEqual(packet.topic, topic)
                        reports.append(packet.payload)
                    else:
                        self.assertIsInstance(packet, PingRespPacket)
                        pings_received += 1
        finally:
            stop.set()
            sock.close()
            publisher.join(timeout=2)
        self.assertFalse(publisher.is_alive())
        self.assertEqual(concurrent_operations, [])
        self.assertEqual(reports, expected)
        self.assertEqual(pings_received, count)


if __name__ == "__main__":
    unittest.main()
