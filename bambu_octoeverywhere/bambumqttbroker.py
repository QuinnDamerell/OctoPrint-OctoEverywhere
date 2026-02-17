import logging
import socket
import struct
import threading
from typing import Dict, FrozenSet, List, Optional

from octoeverywhere.sentry import Sentry


# MQTT 3.1.1 packet type constants (upper nibble of the fixed header byte).
class _PacketType:
    CONNECT     = 0x10
    CONNACK     = 0x20
    PUBLISH     = 0x30
    PUBACK      = 0x40
    SUBSCRIBE   = 0x80
    SUBACK      = 0x90
    UNSUBSCRIBE = 0xA0
    UNSUBACK    = 0xB0
    PINGREQ     = 0xC0
    PINGRESP    = 0xD0
    DISCONNECT  = 0xE0


# Maximum MQTT remaining-length we accept (1 MB).  The theoretical MQTT max is
# 256 MB, but Bambu printer messages are small JSON.  Capping this prevents a
# malicious or broken client from forcing a huge allocation.
_MAX_PACKET_SIZE = 1 * 1024 * 1024

# Maximum number of simultaneous downstream client connections.
_MAX_CLIENTS = 20


def _encode_remaining_length(length: int) -> bytes:
    """Encode an integer as MQTT variable-length remaining-length bytes."""
    result = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length > 0:
            byte |= 0x80
        result.append(byte)
        if length == 0:
            break
    return bytes(result)


def _encode_utf8_str(s: str) -> bytes:
    """Encode a string as an MQTT UTF-8 string (2-byte big-endian length prefix + UTF-8 bytes)."""
    encoded = s.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def _build_publish_packet(topic: str, payload: bytes) -> bytes:
    """Pre-build a complete QoS-0 PUBLISH packet (fixed header + topic + payload)."""
    topic_bytes = _encode_utf8_str(topic)
    body = topic_bytes + payload
    return bytes([_PacketType.PUBLISH]) + _encode_remaining_length(len(body)) + body


def _topic_matches(subscription: str, topic: str) -> bool:
    """
    Return True if *topic* matches *subscription*, respecting MQTT wildcards.
      + matches exactly one topic level.
      # matches zero or more topic levels and must be the last character.
    """
    if subscription == topic:
        return True
    sub_parts = subscription.split("/")
    top_parts = topic.split("/")
    for i, sub_part in enumerate(sub_parts):
        if sub_part == "#":
            return True  # matches the rest
        if i >= len(top_parts):
            return False
        if sub_part != "+" and sub_part != top_parts[i]:
            return False
    return len(sub_parts) == len(top_parts)


# Exceptions that are expected during normal operation (client disappears,
# network hiccup, etc.).  These should be logged quietly, NOT sent to Sentry.
_EXPECTED_DISCONNECT_ERRORS = (
    socket.timeout,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    OSError,
)


class _BrokerClient:
    """
    Handles the MQTT session for a single connected client.
    Each instance runs on its own daemon thread spawned by BambuMqttBroker.
    """

    def __init__(self, broker: "BambuMqttBroker", sock: socket.socket, addr: tuple) -> None:
        self._broker = broker
        self._sock = sock
        self._addr = addr
        self.ClientId: str = f"<unknown>@{addr}"
        # Subscriptions is replaced atomically (frozenset) so DeliverMessage can
        # read it safely from the upstream thread without a lock.
        self.Subscriptions: FrozenSet[str] = frozenset()
        self._send_lock = threading.Lock()
        self._is_closed = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def Run(self) -> None:
        """Read and dispatch MQTT packets until the connection closes."""
        try:
            while True:
                header = self._recv_exact(1)
                if not header:
                    break
                packet_type = header[0] & 0xF0
                flags       = header[0] & 0x0F

                remaining_length = self._read_remaining_length()
                if remaining_length is None:
                    break

                # Guard against oversized packets.
                if remaining_length > _MAX_PACKET_SIZE:
                    self._broker.Logger.warning("MQTT Broker: client '%s' sent oversized packet (%d bytes), disconnecting.", self.ClientId, remaining_length)
                    break

                body = self._recv_exact(remaining_length) if remaining_length > 0 else b""
                if body is None:
                    break

                if   packet_type == _PacketType.CONNECT:     self._handle_connect(body)
                elif packet_type == _PacketType.PUBLISH:      self._handle_publish(body, flags)
                elif packet_type == _PacketType.SUBSCRIBE:    self._handle_subscribe(body)
                elif packet_type == _PacketType.UNSUBSCRIBE:  self._handle_unsubscribe(body)
                elif packet_type == _PacketType.PINGREQ:      self._send_packet(_PacketType.PINGRESP, b"")
                elif packet_type == _PacketType.DISCONNECT:   break
                # Silently ignore other packet types (PUBACK, PUBREC, etc.).

        except _EXPECTED_DISCONNECT_ERRORS:
            # Client gone — completely normal, not worth logging.
            pass
        except Exception as e:
            if not self._is_closed:
                Sentry.OnException(f"MQTT Broker: unexpected error from client {self.ClientId}", e)
        finally:
            self._cleanup()

    # ------------------------------------------------------------------
    # Packet handlers
    # ------------------------------------------------------------------

    def _handle_connect(self, body: bytes) -> None:
        """Parse CONNECT, accept unconditionally, send CONNACK."""
        offset = 0
        # Protocol Name
        _proto_name, offset = self._read_utf8_str(body, offset)
        # Protocol Level
        offset += 1
        # Connect Flags (not used — we skip auth, will, etc.)
        offset += 1
        # Keep Alive (seconds) — use to set a generous socket timeout
        keep_alive = struct.unpack_from(">H", body, offset)[0]; offset += 2
        if keep_alive > 0:
            try:
                self._sock.settimeout(keep_alive * 1.5 + 10)
            except Exception:
                pass

        # Client ID
        client_id, offset = self._read_utf8_str(body, offset)
        self.ClientId = client_id if client_id else f"auto_{id(self)}@{self._addr}"

        # We intentionally skip auth (username/password) — this is a local relay.
        # The upstream Bambu connection already enforces credentials.

        # CONNACK must be sent BEFORE we register the client, otherwise
        # OnUpstreamMessage could deliver a PUBLISH before the client receives CONNACK.
        self._send_packet(_PacketType.CONNACK, bytes([0x00, 0x00]))
        self._broker._on_client_connect(self)
        self._broker.Logger.info("MQTT Broker: client '%s' connected from %s", self.ClientId, self._addr)

    def _handle_subscribe(self, body: bytes) -> None:
        """Parse SUBSCRIBE, record subscriptions, send SUBACK."""
        offset = 0
        packet_id = struct.unpack_from(">H", body, offset)[0]; offset += 2

        return_codes: List[int] = []
        new_subs = set(self.Subscriptions)
        while offset < len(body):
            topic, offset = self._read_utf8_str(body, offset)
            _qos = body[offset]; offset += 1
            new_subs.add(topic)
            return_codes.append(0x00)  # grant QoS 0
            self._broker.Logger.debug("MQTT Broker: '%s' subscribed to '%s'", self.ClientId, topic)
        new_topics = frozenset(new_subs) - self.Subscriptions
        self.Subscriptions = frozenset(new_subs)

        payload = struct.pack(">H", packet_id) + bytes(return_codes)
        self._send_packet(_PacketType.SUBACK, payload)

        # Forward new subscriptions upstream so the printer sends us these topics.
        for topic in new_topics:
            self._broker._subscribe_upstream(topic)

    def _handle_unsubscribe(self, body: bytes) -> None:
        """Parse UNSUBSCRIBE, remove subscriptions, send UNSUBACK."""
        offset = 0
        packet_id = struct.unpack_from(">H", body, offset)[0]; offset += 2

        new_subs = set(self.Subscriptions)
        while offset < len(body):
            topic, offset = self._read_utf8_str(body, offset)
            new_subs.discard(topic)
            self._broker.Logger.debug("MQTT Broker: '%s' unsubscribed from '%s'", self.ClientId, topic)
        removed_topics = self.Subscriptions - frozenset(new_subs)
        self.Subscriptions = frozenset(new_subs)

        self._send_packet(_PacketType.UNSUBACK, struct.pack(">H", packet_id))

        # Unsubscribe upstream for topics no longer needed by any client.
        for topic in removed_topics:
            self._broker._unsubscribe_upstream(topic)

    def _handle_publish(self, body: bytes, flags: int) -> None:
        """Parse PUBLISH from the downstream client and forward to the upstream printer."""
        qos    = (flags >> 1) & 0x03
        offset = 0
        topic, offset = self._read_utf8_str(body, offset)

        packet_id: Optional[int] = None
        if qos > 0:
            packet_id = struct.unpack_from(">H", body, offset)[0]; offset += 2

        payload = body[offset:]

        self._broker.Logger.debug("MQTT Broker: '%s' published %d bytes to '%s'", self.ClientId, len(payload), topic)
        self._broker._forward_to_upstream(topic, payload)

        # Acknowledge QoS 1 publishes.
        if qos == 1 and packet_id is not None:
            self._send_packet(_PacketType.PUBACK, struct.pack(">H", packet_id))

    # ------------------------------------------------------------------
    # Outgoing delivery
    # ------------------------------------------------------------------

    def DeliverPublishPacket(self, packet: bytes, topic: str) -> bool:
        """
        Send a pre-built PUBLISH packet if this client has a matching subscription.
        Returns False if the client is closed or the send fails.
        """
        if self._is_closed:
            return False
        if not any(_topic_matches(sub, topic) for sub in self.Subscriptions):
            return False
        try:
            with self._send_lock:
                self._sock.sendall(packet)
            return True
        except Exception as e:
            self._broker.Logger.debug("MQTT Broker: error delivering to '%s': %s", self.ClientId, e)
            self._cleanup()
            return False

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _send_packet(self, packet_type: int, payload: bytes) -> None:
        fixed = bytes([packet_type]) + _encode_remaining_length(len(payload))
        with self._send_lock:
            self._sock.sendall(fixed + payload)

    def _recv_exact(self, n: int) -> Optional[bytes]:
        """Read exactly *n* bytes, or return None on EOF/error."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def _read_remaining_length(self) -> Optional[int]:
        """Decode the MQTT variable-length remaining-length field."""
        multiplier = 1
        value = 0
        for _ in range(4):
            b = self._recv_exact(1)
            if b is None:
                return None
            byte = b[0]
            value += (byte & 0x7F) * multiplier
            multiplier *= 128
            if (byte & 0x80) == 0:
                return value
        return None  # Malformed — too many bytes

    @staticmethod
    def _read_utf8_str(data: bytes, offset: int):
        """Read a length-prefixed UTF-8 string. Returns (string, new_offset)."""
        length = struct.unpack_from(">H", data, offset)[0]
        offset += 2
        s = data[offset:offset + length].decode("utf-8", errors="replace")
        return s, offset + length

    def _cleanup(self) -> None:
        if self._is_closed:
            return
        self._is_closed = True
        self._broker._on_client_disconnect(self)
        try:
            self._sock.close()
        except Exception:
            pass
        self._broker.Logger.info("MQTT Broker: client '%s' disconnected.", self.ClientId)

    def ForceClose(self) -> None:
        """Forcibly close this client (e.g. when another client reuses the same client ID)."""
        try:
            self._sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public broker class
# ---------------------------------------------------------------------------

class BambuMqttBroker:
    """
    Minimal MQTT 3.1.1 broker that relays messages between multiple downstream
    clients and the single upstream connection to a Bambu Lab printer.

    Architecture:
      ┌──────────────────────────────────────┐
      │  BambuClient (upstream paho-mqtt)    │  ← one connection to the printer
      │  • calls OnUpstreamMessage() here    │
      │  • receives PublishRaw() calls here  │
      └────────────────┬─────────────────────┘
                       │
               BambuMqttBroker (this class)
                       │  listens on 0.0.0.0:<port>
          ┌────────────┴────────────┐
     _BrokerClient            _BrokerClient   …  (one thread each)
     (Bambu Studio)           (custom app)

    Usage:
      broker = BambuMqttBroker(logger, port)
      broker.Start()
      BambuClient.Get().AddBrokerMessageListener(broker.OnUpstreamMessage)
    """

    DefaultPort = 1883

    def __init__(self, logger: logging.Logger, port: int) -> None:
        self.Logger = logger
        self._port = port
        self._clients: Dict[str, _BrokerClient] = {}  # client_id → client
        self._clients_lock = threading.Lock()
        self._server_sock: Optional[socket.socket] = None
        self._is_running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def Start(self) -> None:
        """Start the broker on a daemon thread. Returns immediately."""
        self._is_running = True
        t = threading.Thread(target=self._server_thread, daemon=True, name="BambuMqttBroker")
        t.start()

    def Stop(self) -> None:
        self._is_running = False
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Upstream → downstream relay
    # ------------------------------------------------------------------

    def OnUpstreamMessage(self, topic: str, payload: bytes) -> None:
        """
        Called by BambuClient whenever a raw message arrives from the printer.
        Forwards the message to every downstream client that has a matching subscription.
        """
        with self._clients_lock:
            clients = list(self._clients.values())

        if not clients:
            return

        # Pre-build the PUBLISH packet once and send the same bytes to every client.
        packet = _build_publish_packet(topic, payload)

        for client in clients:
            try:
                client.DeliverPublishPacket(packet, topic)
            except Exception as e:
                self.Logger.error("MQTT Broker: error delivering to '%s': %s", client.ClientId, e)

    # ------------------------------------------------------------------
    # Upstream subscription management
    # ------------------------------------------------------------------

    def _get_all_downstream_subscriptions(self) -> FrozenSet[str]:
        """Return the union of all connected clients' subscriptions."""
        with self._clients_lock:
            clients = list(self._clients.values())
        result: set = set()
        for c in clients:
            result.update(c.Subscriptions)
        return frozenset(result)

    def _subscribe_upstream(self, topic: str) -> None:
        """Subscribe to a topic on the upstream printer connection (if not already subscribed by BambuClient itself)."""
        try:
            from .bambuclient import BambuClient  # avoid circular import at module level
            bc = BambuClient.Get()
            if bc.Client is not None and bc.Client.is_connected():
                bc.Client.subscribe(topic)
                self.Logger.debug("MQTT Broker: subscribed upstream to '%s'", topic)
        except Exception as e:
            self.Logger.warning("MQTT Broker: failed to subscribe upstream to '%s': %s", topic, e)

    def _unsubscribe_upstream(self, topic: str) -> None:
        """Unsubscribe from a topic upstream, but only if no downstream client still needs it."""
        # Check if any other client still has this subscription.
        all_subs = self._get_all_downstream_subscriptions()
        if topic in all_subs:
            return  # Still needed by another client
        try:
            from .bambuclient import BambuClient  # avoid circular import at module level
            bc = BambuClient.Get()
            # Don't unsubscribe from the report topic — BambuClient owns that subscription.
            if "/report" in topic:
                return
            if bc.Client is not None and bc.Client.is_connected():
                bc.Client.unsubscribe(topic)
                self.Logger.debug("MQTT Broker: unsubscribed upstream from '%s'", topic)
        except Exception as e:
            self.Logger.warning("MQTT Broker: failed to unsubscribe upstream from '%s': %s", topic, e)

    def OnUpstreamReconnect(self) -> None:
        """
        Called by BambuClient when a new upstream connection is established.
        Re-subscribes all topics that downstream clients need.
        """
        all_subs = self._get_all_downstream_subscriptions()
        if not all_subs:
            return
        self.Logger.info("MQTT Broker: upstream reconnected, re-subscribing %d downstream topic(s).", len(all_subs))
        for topic in all_subs:
            self._subscribe_upstream(topic)

    # ------------------------------------------------------------------
    # Downstream → upstream relay
    # ------------------------------------------------------------------

    def _forward_to_upstream(self, topic: str, payload: bytes) -> None:
        """Forward a PUBLISH from a downstream client to the upstream printer."""
        try:
            from .bambuclient import BambuClient  # avoid circular import at module level
            if not BambuClient.Get().PublishRaw(topic, payload):
                self.Logger.warning("MQTT Broker: upstream not connected, dropped publish to '%s'", topic)
        except Exception as e:
            Sentry.OnException("MQTT Broker: error forwarding publish to upstream", e)

    # ------------------------------------------------------------------
    # Client lifecycle callbacks (called by _BrokerClient)
    # ------------------------------------------------------------------

    def _on_client_connect(self, client: _BrokerClient) -> None:
        with self._clients_lock:
            existing = self._clients.get(client.ClientId)
            if existing is not None:
                self.Logger.info("MQTT Broker: replacing existing session for client '%s'", client.ClientId)
                existing.ForceClose()
            self._clients[client.ClientId] = client

    def _on_client_disconnect(self, client: _BrokerClient) -> None:
        with self._clients_lock:
            # Only remove if it's still the same instance (not replaced by a re-connect).
            if self._clients.get(client.ClientId) is client:
                del self._clients[client.ClientId]
        # Unsubscribe upstream for any topics this client was the sole subscriber of.
        # This runs after the client is removed from _clients, so _get_all_downstream_subscriptions
        # will no longer include this client's subscriptions.
        for topic in client.Subscriptions:
            self._unsubscribe_upstream(topic)

    # ------------------------------------------------------------------
    # Accept loop
    # ------------------------------------------------------------------

    def _server_thread(self) -> None:
        try:
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_sock.bind(("0.0.0.0", self._port))
            self._server_sock.listen(10)
            self.Logger.info("MQTT Broker: listening on 0.0.0.0:%d", self._port)

            while self._is_running:
                try:
                    sock, addr = self._server_sock.accept()

                    # Enforce connection limit to prevent resource exhaustion.
                    with self._clients_lock:
                        if len(self._clients) >= _MAX_CLIENTS:
                            self.Logger.warning("MQTT Broker: rejecting connection from %s, max clients (%d) reached.", addr, _MAX_CLIENTS)
                            try:
                                sock.close()
                            except Exception:
                                pass
                            continue

                    # Default timeout before CONNECT is received; overridden per-client after CONNECT.
                    sock.settimeout(30)
                    broker_client = _BrokerClient(self, sock, addr)
                    t = threading.Thread(
                        target=broker_client.Run,
                        daemon=True,
                        name=f"BambuMqttBrokerClient-{addr}",
                    )
                    t.start()
                except OSError:
                    if self._is_running:
                        raise
        except Exception as e:
            if self._is_running:
                Sentry.OnException("MQTT Broker: server thread error", e)
