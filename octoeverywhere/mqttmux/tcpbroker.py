import logging
import os
import platform
import select
import socket
import ssl
import threading
import time
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

from .mux import MqttUpstreamMux
from .types import ConnAckReturnCode
from .wireclient import WireVirtualClient
from .wirecodec import ConnectPacket
from .tlscert import CreateServerTlsContext

if TYPE_CHECKING:
    from linux_host.config import Config as _Config


# Type alias: socket.accept returns (sock, address) where address is a tuple
# whose shape depends on the address family. For AF_INET it's (host, port).
_PeerAddress = Tuple[str, int]
_OnFinishedCallback = Callable[["TcpBrokerClient"], None]


# Pluggable auth check for the local broker. Takes the username and password
# fields as they appear in an MQTT CONNECT packet (password is the raw bytes
# from the wire, NOT a decoded string - the spec carries it as binary data
# §3.1.3.5). Returns a ConnAckReturnCode value:
#   * ACCEPTED                  - auth ok
#   * BAD_USERNAME_OR_PASSWORD  - reject
#   * NOT_AUTHORIZED            - reject (used when we can't verify, e.g. the
#                                 upstream context isn't yet known)
#
# When None, the broker accepts all CONNECTs (anonymous).
AuthCheck = Callable[[Optional[str], Optional[bytes]], int]


# Build an AuthCheck that compares against fixed username/password strings.
# Returns None if both expected values are None (i.e. no auth configured).
def StaticAuthCheck(expected_username: Optional[str],
                    expected_password: Optional[str]) -> Optional[AuthCheck]:
    if expected_username is None and expected_password is None:
        return None
    expected_pw_bytes = (expected_password or "").encode("utf-8")
    def _check(username: Optional[str], password: Optional[bytes]) -> int:
        if username != expected_username:
            return ConnAckReturnCode.BAD_USERNAME_OR_PASSWORD
        if password != expected_pw_bytes:
            return ConnAckReturnCode.BAD_USERNAME_OR_PASSWORD
        return ConnAckReturnCode.ACCEPTED
    return _check


# Local TCP MQTT broker.
#
# Listens on a configurable bind address and port and accepts plaintext or
# TLS MQTT clients. Each accepted connection becomes a TcpBrokerClient (a
# WireVirtualClient subclass) attached to the shared MqttUpstreamMux, so the
# printer's MQTT broker sees a single upstream connection regardless of how
# many local clients are connected.
#
# Enabled by default via config (Config.MqttLocalBrokerEnabled). The primary
# port defaults to 1883; an additional TLS-only listener defaults to 8883.
#
# All topics are relayed without ACL filtering. Optional CONNECT-time
# username/password authentication is supported.
class LocalTcpBrokerServer:

    # How often tls_refresh is re-run while the server is up. Auto-generated
    # certificates renew 30 days before expiry, so twice a day is plenty, and
    # it also picks up replaced custom certificates without a restart.
    TLS_REFRESH_INTERVAL_SEC = 12 * 60 * 60

    # backlog defaults to 32 - well over what a single printer's worth of
    # clients should ever need but not so high as to invite SYN flood antics
    # on an exposed instance.
    #
    # max_clients bounds the number of concurrent connections. Each connection
    # costs a reader thread (plus a keepalive/control thread once connected),
    # so on low memory devices we must not let an open port turn into
    # unbounded thread growth. Connections over the cap are closed on accept.
    def __init__(self, logger: logging.Logger, mux: MqttUpstreamMux,
                 bind: str, port: int,
                 auth_check: Optional[AuthCheck] = None,
                 backlog: int = 32,
                 max_clients: int = 25,
                 tls_context: Optional[ssl.SSLContext] = None,
                 tls_port: int = 0,
                 tls_refresh: Optional[Callable[[], ssl.SSLContext]] = None) -> None:
        self._logger = logger
        self._mux = mux
        self._bind = bind
        self._port = port
        self._auth_check = auth_check
        self._backlog = backlog
        self._max_clients = max_clients
        self._tls_context = tls_context
        self._tls_port = tls_port
        self._tls_refresh = tls_refresh
        self._listener: Optional[socket.socket] = None
        self._tls_listener: Optional[socket.socket] = None
        self._clients_lock = threading.Lock()
        self._clients: List["TcpBrokerClient"] = []
        self._stopped = threading.Event()


    # Convenience for the vendor host: check config and start iff enabled.
    # Returns the started server or None if disabled / failed.
    #
    # upstream_auth_check: callable produced by the vendor client that verifies
    # connection-time credentials against whatever the upstream printer is
    # currently using (Bambu: 'bblp' + access code; Elegoo: 'elegoo' + access
    # code). When config.MqttLocalBrokerRequireUpstreamAuth is true (default)
    # AND this callable is provided, it becomes the broker's auth check. When
    # false, the static MqttLocalBrokerUsername / MqttLocalBrokerPassword are
    # used instead (or anonymous if both are unset).
    @staticmethod
    def MaybeStartFromConfig(logger: logging.Logger, config: "_Config", mux: MqttUpstreamMux,
                              upstream_auth_check: Optional[AuthCheck] = None) -> Optional["LocalTcpBrokerServer"]:
        # Lazy import to avoid an octoeverywhere -> linux_host -> octoeverywhere
        # cycle at module load.
        from linux_host.config import Config  # pylint: disable=import-outside-toplevel

        # Setup the local TCP broker if enabled in config.
        if not config.GetBool(Config.SectionMqtt, Config.MqttLocalBrokerEnabled, True):
            return None
        bind = config.GetStr(Config.SectionMqtt, Config.MqttLocalBrokerBind, "0.0.0.0")
        port = config.GetInt(Config.SectionMqtt, Config.MqttLocalBrokerPort, 1883)
        if bind is None:
            bind = "0.0.0.0"
        if port is None:
            port = 1883
        tls_port = config.GetInt(Config.SectionMqtt, Config.MqttLocalBrokerTlsPort, 8883)
        if tls_port is None:
            tls_port = 8883
        cert_path = config.GetStr(Config.SectionMqtt, Config.MqttLocalBrokerTlsCertPath, None, keepInConfigIfNone=True)
        key_path = config.GetStr(Config.SectionMqtt, Config.MqttLocalBrokerTlsKeyPath, None, keepInConfigIfNone=True)
        if not 1 <= port <= 65535 or not 0 <= tls_port <= 65535:
            logger.error("LocalTcpBrokerServer: port must be 1-65535 and tls_port must be 0-65535 (0 disables the additional listener)")
            return None

        # Setup auth.
        # Always read all of the config options so they always get set to defaults in the config file.
        require_upstream = config.GetBool(Config.SectionMqtt, Config.MqttLocalBrokerRequireUpstreamAuth, True)
        static_user = config.GetStr(Config.SectionMqtt, Config.MqttLocalBrokerUsername, None, keepInConfigIfNone=True)
        static_pass = config.GetStr(Config.SectionMqtt, Config.MqttLocalBrokerPassword, None, keepInConfigIfNone=True)
        auth_check: Optional[AuthCheck] = None
        if require_upstream:
            if upstream_auth_check is None:
                raise ValueError("LocalTcpBrokerServer: upstream_auth_check is required when MqttLocalBrokerRequireUpstreamAuth is true")
            auth_check = upstream_auth_check
            logger.info("LocalTcpBrokerServer: requiring upstream credentials for downstream CONNECTs")
        else:
            auth_check = StaticAuthCheck(static_user, static_pass)
            if auth_check is None:
                logger.warning("LocalTcpBrokerServer: NO auth configured - the broker on %s:%s will accept any client",
                               bind, port)
            else:
                logger.info("LocalTcpBrokerServer: using static credentials from config")

        storage_dir = os.path.dirname(config.OeConfigFilePath)
        def tls_refresh() -> ssl.SSLContext:
            return CreateServerTlsContext(logger, storage_dir, cert_path=cert_path, key_path=key_path, bind=bind)

        tls_context = None
        try:
            tls_context = tls_refresh()
        except Exception as e:
            # Preserve existing plaintext clients if certificate provisioning fails.
            # TLS clients are explicitly rejected, never downgraded to plaintext.
            logger.error("LocalTcpBrokerServer: TLS setup failed; TLS connections will be rejected. "
                         "Plaintext MQTT remains available on %s:%s: %s", bind, port, e)

        try:
            server = LocalTcpBrokerServer(logger, mux, bind, int(port), auth_check=auth_check,
                                          tls_context=tls_context, tls_port=tls_port, tls_refresh=tls_refresh)
            server.Start()
            return server
        except Exception as e:
            logger.error("LocalTcpBrokerServer failed to start on %s:%s - %s", bind, port, e)
            return None


    def Start(self) -> None:
        if self._listener is not None:
            return
        self._stopped.clear()
        self._listener = self._StartListener(self._port, tls_only=False)
        # Keep the TLS port listening even if certificate setup failed so clients
        # are rejected with an actionable log instead of silently failing to connect.
        if self._tls_port != 0 and self._tls_port != self._port:
            try:
                self._tls_listener = self._StartListener(self._tls_port, tls_only=True)
            except Exception as e:
                mode = "Plaintext and TLS" if self._tls_context is not None else "Plaintext MQTT"
                self._logger.warning("LocalTcpBrokerServer: additional TLS listener on %s:%s unavailable: %s. "
                                     "%s remains available on port %s. Check for another broker or plugin instance.",
                                     self._bind, self._tls_port, e, mode, self._port)
        if self._tls_refresh is not None:
            threading.Thread(target=self._TlsRefreshLoop, name="mqttmux-tcpbroker-tls-refresh", daemon=True).start()


    # A long-running relay must renew its automatic certificate before expiry.
    # New connections pick up the refreshed context; established sessions are
    # unaffected.
    def _TlsRefreshLoop(self) -> None:
        while not self._stopped.wait(self.TLS_REFRESH_INTERVAL_SEC):
            refresh = self._tls_refresh
            if refresh is None:
                return
            try:
                self._tls_context = refresh()
            except Exception as e:
                self._logger.warning("LocalTcpBrokerServer: TLS certificate refresh failed; keeping the current certificate: %s", e)


    def _StartListener(self, port: int, tls_only: bool) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if platform.system() == "Windows":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined] #pylint: disable=no-member
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._bind, port))
            sock.listen(self._backlog)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise
        # Short accept-wait so Stop() can break in promptly.
        sock.settimeout(0.5)
        accept_thread = threading.Thread(
            target=self._AcceptLoop, args=(sock, tls_only), name=f"mqttmux-tcpbroker-accept[{self._bind}:{port}]",
            daemon=True,
        )
        accept_thread.start()
        mode = "TLS only" if tls_only else "plaintext and TLS" if self._tls_context is not None else "plaintext only"
        if tls_only and self._tls_context is None:
            mode = "TLS unavailable; connections will be rejected"
        self._logger.info("LocalTcpBrokerServer listening on %s:%s (%s)", self._bind, sock.getsockname()[1], mode)
        return sock


    def Stop(self) -> None:
        self._stopped.set()
        listeners = (self._listener, self._tls_listener)
        self._listener = None
        self._tls_listener = None
        for listener in listeners:
            if listener is None:
                continue
            try:
                # On Linux, close() alone leaves the port listening until the
                # accept thread's pending wait returns; shutdown releases it now.
                try:
                    listener.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                listener.close()
            except Exception as e:
                self._logger.debug("LocalTcpBrokerServer close listener raised: %s", e)
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            try:
                c.OnPeerClosed()
            except Exception as e:
                self._logger.debug("LocalTcpBrokerServer client close raised: %s", e)


    def _AcceptLoop(self, listener: socket.socket, tls_only: bool) -> None:
        while not self._stopped.is_set():
            try:
                client_sock, addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                # Listener closed under us; exit.
                return
            except Exception as e:
                self._logger.warning("LocalTcpBrokerServer accept raised: %s", e)
                continue
            self._SpawnClient(client_sock, addr, tls_only)


    def _SpawnClient(self, client_sock: socket.socket, addr: _PeerAddress, tls_only: bool = False) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        # Admission is atomic across both listeners. Pending handshakes count
        # toward the cap, and shutdown must not miss a newly accepted client.
        with self._clients_lock:
            if self._stopped.is_set() or len(self._clients) >= self._max_clients:
                self._logger.info("LocalTcpBrokerServer rejecting %s: stopped or at max client count (%d)",
                                  peer, self._max_clients)
                client_sock.close()
                return
            client = TcpBrokerClient(self._logger, self._mux, peer, client_sock,
                                     auth_check=self._auth_check, on_finished=self._OnClientFinished,
                                     tls_context=self._tls_context, tls_only=tls_only)
            self._clients.append(client)
        try:
            # MQTT acks (CONNACK, PUBACK, PINGRESP...) are tiny request/response
            # packets; disable Nagle so they aren't delayed behind unacked data.
            client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Until the CONNECT lands, use a short recv timeout so an idle peer
            # can't hold a reader thread open (MQTT 3.1.1 §3.1: the server
            # SHOULD close the connection if no CONNECT arrives in a reasonable
            # time). The client bumps this to the long fallback after CONNECT.
            client_sock.settimeout(TcpBrokerClient.PRE_CONNECT_TIMEOUT_SEC)
        except Exception as e:
            self._logger.debug("LocalTcpBrokerServer socket setup raised: %s", e)
        client.Start()
        self._logger.info("LocalTcpBrokerServer accepted %s", peer)


    def _OnClientFinished(self, client: "TcpBrokerClient") -> None:
        with self._clients_lock:
            try:
                self._clients.remove(client)
            except ValueError:
                pass


# One per accepted TCP connection. Owns its socket, a reader thread that
# pumps recv() into the wire decoder, and writes outbound bytes via a
# send-side lock.
class TcpBrokerClient(WireVirtualClient):

    # Recv timeout used until the MQTT CONNECT lands. Per MQTT 3.1.1 §3.1 the
    # server SHOULD close connections that don't send a CONNECT in a
    # reasonable amount of time.
    PRE_CONNECT_TIMEOUT_SEC = 30.0

    # Fallback recv timeout (set once the session is connected) for clients
    # that connect with MQTT keepalive=0 (disabled). Without this, a peer that
    # disappears without a FIN would leave the reader thread blocked on
    # recv() forever.
    FALLBACK_RECV_TIMEOUT_SEC = 300.0
    # Matches the plaintext path, where sendall shares the socket's fallback timeout.
    TLS_SEND_TIMEOUT_SEC = FALLBACK_RECV_TIMEOUT_SEC

    def __init__(self, logger: logging.Logger, mux: MqttUpstreamMux, peer_label: str,
                 client_sock: socket.socket,
                 auth_check: Optional[AuthCheck] = None,
                 on_finished: Optional[_OnFinishedCallback] = None,
                 tls_context: Optional[ssl.SSLContext] = None,
                 tls_only: bool = False) -> None:
        super().__init__(logger, mux, peer_label)
        self._socket = client_sock
        self._sock_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._auth_check = auth_check
        self._on_finished = on_finished
        self._tls_context = tls_context
        self._tls_only = tls_only
        self._is_tls = False
        self._connect_deadline = time.monotonic() + self.PRE_CONNECT_TIMEOUT_SEC


    def Start(self) -> None:
        if self._reader_thread is not None:
            return
        self._reader_thread = threading.Thread(
            target=self._ReaderLoop, name=f"mqttmux-tcpbroker-read[{self._peer_label}]",
            daemon=True,
        )
        self._reader_thread.start()


    def _ReaderLoop(self) -> None:
        try:
            if not self._PrepareTransport():
                return
            while not self._closed:
                try:
                    deadline = (time.monotonic() + self.FALLBACK_RECV_TIMEOUT_SEC
                                if self._connected else self._connect_deadline)
                    data = self._ReceiveBytes(4096, deadline)
                except socket.timeout:
                    if self._closed:
                        return
                    # If the peer hasn't completed CONNECT yet, close. Per MQTT
                    # 3.1.1 §3.1 the server should drop connections that don't
                    # send a CONNECT in a reasonable amount of time.
                    if not self._connected:
                        self._logger.info("TcpBrokerClient[%s] no CONNECT within %.0fs; closing",
                                          self._peer_label, TcpBrokerClient.PRE_CONNECT_TIMEOUT_SEC)
                        return
                    # For clients with MQTT keepalive > 0, the keepalive
                    # watchdog in WireVirtualClient handles liveness. The
                    # socket timeout is a fallback for keepalive=0 peers
                    # that disappear without a FIN.
                    if self._keep_alive_sec == 0:
                        self._logger.info("TcpBrokerClient[%s] recv timeout (keepalive disabled); closing",
                                          self._peer_label)
                        return
                    continue
                except (ConnectionError, OSError) as e:
                    self._logger.debug("TcpBrokerClient[%s] recv raised: %s", self._peer_label, e)
                    return
                if not data:
                    return
                self.FeedBytes(data)
        except socket.timeout:
            self._logger.info("TcpBrokerClient[%s] timed out waiting for TLS handshake or MQTT CONNECT; closing", self._peer_label)
        except ssl.SSLError as e:
            self._logger.warning("TcpBrokerClient[%s] TLS handshake failed: %s. "
                                 "The client may need to trust the broker certificate.", self._peer_label, e)
        except OSError as e:
            self._logger.debug("TcpBrokerClient[%s] transport closed: %s", self._peer_label, e)
        finally:
            self.OnPeerClosed()
            if self._on_finished is not None:
                try:
                    self._on_finished(self)
                except Exception as e:
                    self._logger.debug("TcpBrokerClient[%s] on_finished raised: %s",
                                       self._peer_label, e)


    # Sniff without consuming bytes, so OpenSSL sees the complete ClientHello,
    # including when it arrives in fragments. Only the client worker blocks.
    def _PrepareTransport(self) -> bool:
        if self._tls_only and self._tls_context is None:
            self._logger.warning("TcpBrokerClient[%s] TLS connection rejected because broker TLS is unavailable; "
                                 "check the TLS setup error at startup", self._peer_label)
            return False
        self._SetConnectTimeout()
        first = self._socket.recv(1, socket.MSG_PEEK)
        if not first:
            return False
        if first != b"\x16":
            if self._tls_only:
                self._logger.warning("TcpBrokerClient[%s] plaintext MQTT sent to TLS-only listener; enable TLS in the client", self._peer_label)
                return False
        else:
            if self._tls_context is None:
                self._logger.warning("TcpBrokerClient[%s] TLS connection requested but broker TLS is unavailable; check the TLS setup error at startup", self._peer_label)
                return False
            # Publish the wrapped socket before handshaking so Stop can interrupt
            # the handshake. No MQTT traffic can write to it until this returns.
            with self._sock_lock:
                if self._closed:
                    return False
                tls_socket = self._tls_context.wrap_socket(self._socket, server_side=True, do_handshake_on_connect=False)
                self._socket = tls_socket
                self._is_tls = True
            self._SetConnectTimeout()
            tls_socket.do_handshake()
        if not self._is_tls:
            # Plain sockets support a concurrent recv and send, so they keep
            # simple blocking I/O with a timeout.
            return True
        # SSL objects cannot be read and written concurrently. Nonblocking I/O
        # lets us serialize SSL calls while waiting for readiness outside the
        # lock, so an idle reader never blocks upstream publishes or shutdown.
        with self._sock_lock:
            if self._closed:
                return False
            self._socket.setblocking(False)
        return True


    def _SetConnectTimeout(self) -> None:
        remaining = self._connect_deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("TLS/MQTT connection deadline expired")
        self._socket.settimeout(remaining)


    def _WaitForIo(self, writing: bool, deadline: float) -> None:
        if self._closed:
            raise ConnectionAbortedError("MQTT transport closed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("MQTT transport deadline expired")
        # The other I/O thread can consume a TLS control record and change
        # SSL readiness without leaving the TCP socket readable. Recheck
        # periodically, also bounding how long shutdown waits.
        wait_sec = min(remaining, 0.25)
        try:
            if hasattr(select, "poll"):
                # poll has no FD_SETSIZE limit; select() raises for fds >= 1024.
                poller = select.poll()
                poller.register(self._socket, select.POLLOUT if writing else select.POLLIN)
                ready = len(poller.poll(wait_sec * 1000)) > 0
            else:
                readable, writable, _ = select.select([] if writing else [self._socket],
                                                       [self._socket] if writing else [], [], wait_sec)
                ready = bool(readable or writable)
        except ValueError as e:
            # Another thread may close the socket just before the wait starts.
            raise ConnectionAbortedError("MQTT transport closed") from e
        if not ready and time.monotonic() >= deadline:
            raise socket.timeout("MQTT transport deadline expired")


    def _ReceiveBytes(self, size: int, deadline: float) -> bytes:
        if not self._is_tls:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("MQTT receive deadline expired")
            self._socket.settimeout(remaining)
            return self._socket.recv(size)
        while not self._closed:
            if time.monotonic() >= deadline:
                raise socket.timeout("MQTT receive deadline expired")
            with self._sock_lock:
                try:
                    # Try SSL_read before select: OpenSSL may already have
                    # decrypted bytes buffered even when the TCP socket is idle.
                    return self._socket.recv(size)
                except (ssl.SSLWantReadError, BlockingIOError):
                    writing = False
                except ssl.SSLWantWriteError:
                    writing = True
            self._WaitForIo(writing, deadline)
        return b""


    def OnPeerClosed(self) -> None:
        was_closed = self._closed
        super().OnPeerClosed()
        if not was_closed:
            self._CloseTransport()


    def _SendBytes(self, data: bytes) -> None:
        # WireVirtualClient's send lock preserves packet ordering; this lock
        # protects individual SSL operations against the concurrent reader.
        deadline = time.monotonic() + self.TLS_SEND_TIMEOUT_SEC
        offset = 0
        view = memoryview(data)
        try:
            if not self._is_tls:
                # Uses the socket timeout the reader maintains. No lock needed:
                # plain sockets allow a concurrent recv, and Close shouldn't wait on it.
                self._socket.sendall(data)
                return
            while offset < len(data):
                if self._closed:
                    raise ConnectionAbortedError("MQTT transport closed")
                if time.monotonic() >= deadline:
                    raise socket.timeout("MQTT send deadline expired")
                writing = True
                with self._sock_lock:
                    try:
                        sent = self._socket.send(view[offset:])
                        if sent == 0:
                            raise ConnectionError("MQTT transport closed during send")
                        offset += sent
                        continue
                    except ssl.SSLWantReadError:
                        writing = False
                    except (ssl.SSLWantWriteError, BlockingIOError):
                        pass
                self._WaitForIo(writing, deadline)
        except OSError as e:
            self._logger.debug("TcpBrokerClient[%s] send raised: %s", self._peer_label, e)
            raise


    def _CloseTransport(self) -> None:
        with self._sock_lock:
            try:
                # Shutdown then close so the recv loop unblocks promptly.
                try:
                    self._socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._socket.close()
            except Exception as e:
                self._logger.debug("TcpBrokerClient[%s] close raised: %s", self._peer_label, e)


    # Optional auth gate configured at the broker level. None means accept
    # every CONNECT regardless of credentials (anonymous broker).
    def _CheckAuth(self, pkt: ConnectPacket) -> int:
        if self._auth_check is None:
            return ConnAckReturnCode.ACCEPTED
        try:
            return self._auth_check(pkt.username, pkt.password)
        except Exception as e:
            self._logger.error("TcpBrokerClient[%s] auth check raised: %s", self._peer_label, e)
            return ConnAckReturnCode.NOT_AUTHORIZED
