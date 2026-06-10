import logging
import platform
import socket
import threading
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

from .mux import MqttUpstreamMux
from .types import ConnAckReturnCode
from .wireclient import WireVirtualClient
from .wirecodec import ConnectPacket

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
# Listens on a configurable bind address and port and accepts standard MQTT
# 3.1.1 TCP clients. Each accepted connection becomes a TcpBrokerClient (a
# WireVirtualClient subclass) attached to the shared MqttUpstreamMux, so the
# printer's MQTT broker sees a single upstream connection regardless of how
# many local clients are connected.
#
# Opt-in via config (Config.MqttLocalBrokerEnabled). Default off to avoid
# binding port 1883 in environments that already run mosquitto or similar.
#
# Per the user's choice in plan §7, this exposes "full access" to all topics
# (no ACL filtering today). Optional CONNECT-time username/password auth is
# supported.
class LocalTcpBrokerServer:

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
                 max_clients: int = 25) -> None:
        self._logger = logger
        self._mux = mux
        self._bind = bind
        self._port = port
        self._auth_check = auth_check
        self._backlog = backlog
        self._max_clients = max_clients
        self._listener: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
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

        try:
            server = LocalTcpBrokerServer(logger, mux, bind, int(port), auth_check=auth_check)
            server.Start()
            return server
        except Exception as e:
            logger.error("LocalTcpBrokerServer failed to start on %s:%s - %s", bind, port, e)
            return None


    def Start(self) -> None:
        if self._listener is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if platform.system() == "Windows":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined] #pylint: disable=no-member
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._bind, self._port))
            sock.listen(self._backlog)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise
        # Short accept-wait so Stop() can break in promptly.
        sock.settimeout(0.5)
        self._listener = sock
        self._accept_thread = threading.Thread(
            target=self._AcceptLoop, name=f"mqttmux-tcpbroker-accept[{self._bind}:{self._port}]",
            daemon=True,
        )
        self._accept_thread.start()
        self._logger.info("LocalTcpBrokerServer listening on %s:%s", self._bind, self._port)


    def Stop(self) -> None:
        self._stopped.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
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


    def _AcceptLoop(self) -> None:
        while not self._stopped.is_set():
            listener = self._listener
            if listener is None:
                return
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
            self._SpawnClient(client_sock, addr)


    def _SpawnClient(self, client_sock: socket.socket, addr: _PeerAddress) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        # Enforce the connection cap before spending any more resources.
        with self._clients_lock:
            at_capacity = len(self._clients) >= self._max_clients
        if at_capacity:
            self._logger.warning("LocalTcpBrokerServer rejecting %s: at max client count (%d)",
                                 peer, self._max_clients)
            try:
                client_sock.close()
            except Exception:
                pass
            return
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
        client = TcpBrokerClient(self._logger, self._mux, peer, client_sock,
                                  auth_check=self._auth_check,
                                  on_finished=self._OnClientFinished)
        with self._clients_lock:
            self._clients.append(client)
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

    def __init__(self, logger: logging.Logger, mux: MqttUpstreamMux, peer_label: str,
                 client_sock: socket.socket,
                 auth_check: Optional[AuthCheck] = None,
                 on_finished: Optional[_OnFinishedCallback] = None) -> None:
        super().__init__(logger, mux, peer_label)
        self._socket = client_sock
        self._sock_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._auth_check = auth_check
        self._on_finished = on_finished


    def Start(self) -> None:
        if self._reader_thread is not None:
            return
        self._reader_thread = threading.Thread(
            target=self._ReaderLoop, name=f"mqttmux-tcpbroker-read[{self._peer_label}]",
            daemon=True,
        )
        self._reader_thread.start()


    def _ReaderLoop(self) -> None:
        relaxed_timeout = False
        try:
            while not self._closed:
                try:
                    data = self._socket.recv(4096)
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
                # Once the CONNECT lands (dispatched on this same thread inside
                # FeedBytes), relax the recv timeout to the long fallback.
                if not relaxed_timeout and self._connected:
                    relaxed_timeout = True
                    try:
                        self._socket.settimeout(TcpBrokerClient.FALLBACK_RECV_TIMEOUT_SEC)
                    except Exception as e:
                        self._logger.debug("TcpBrokerClient[%s] settimeout raised: %s", self._peer_label, e)
        finally:
            self.OnPeerClosed()
            if self._on_finished is not None:
                try:
                    self._on_finished(self)
                except Exception as e:
                    self._logger.debug("TcpBrokerClient[%s] on_finished raised: %s",
                                       self._peer_label, e)


    def OnPeerClosed(self) -> None:
        was_closed = self._closed
        super().OnPeerClosed()
        if not was_closed:
            self._CloseTransport()


    def _SendBytes(self, data: bytes) -> None:
        with self._sock_lock:
            try:
                self._socket.sendall(data)
            except (ConnectionError, OSError) as e:
                self._logger.debug("TcpBrokerClient[%s] sendall raised: %s", self._peer_label, e)
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
