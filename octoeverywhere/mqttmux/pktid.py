import threading
from typing import Set

from .types import MAX_PACKET_IDENTIFIER, ProtocolError


# Allocates 16-bit MQTT packet identifiers (1..65535) for one direction of
# one virtual client's session. MQTT 3.1.1 §2.3.1: packet identifiers are
# scoped to a single direction (client->server and server->client are
# independent) and must not be 0.
#
# Thread-safe: callers may allocate/free from any thread.
class PacketIdAllocator:

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_use: Set[int] = set()
        self._next_hint = 1


    # Returns the next free packet identifier and marks it in-use.
    # Raises ProtocolError if all 65535 slots are exhausted.
    def Allocate(self) -> int:
        with self._lock:
            if len(self._in_use) >= MAX_PACKET_IDENTIFIER:
                raise ProtocolError("Packet ID space exhausted")
            # Linear probe from the hint; wraps around. Worst case O(65535)
            # but the in-flight cap enforced by the mux (default 50) keeps
            # this very fast in practice.
            candidate = self._next_hint
            for _ in range(MAX_PACKET_IDENTIFIER):
                if candidate not in self._in_use:
                    self._in_use.add(candidate)
                    self._next_hint = candidate + 1
                    if self._next_hint > MAX_PACKET_IDENTIFIER:
                        self._next_hint = 1
                    return candidate
                candidate += 1
                if candidate > MAX_PACKET_IDENTIFIER:
                    candidate = 1
            # Should be unreachable given the size check above.
            raise ProtocolError("Packet ID space exhausted")


    # Releases a previously allocated packet identifier so it can be reused.
    # Idempotent: freeing an id that was never allocated is a no-op (this can
    # happen if a peer sends a spurious ACK we never expected).
    def Free(self, packet_id: int) -> None:
        with self._lock:
            self._in_use.discard(packet_id)


    # Marks an externally-supplied packet identifier as in-use. Used by the
    # wire client when accepting an incoming PUBLISH with QoS > 0 - the peer
    # chose the id; we track it so we don't reuse it for an outgoing message
    # while their QoS handshake is still in flight.
    #
    # Returns True if the id was not in use; False if there's a collision
    # (which is a protocol error the caller should surface).
    def Reserve(self, packet_id: int) -> bool:
        if packet_id < 1 or packet_id > MAX_PACKET_IDENTIFIER:
            return False
        with self._lock:
            if packet_id in self._in_use:
                return False
            self._in_use.add(packet_id)
            return True


    def IsInUse(self, packet_id: int) -> bool:
        with self._lock:
            return packet_id in self._in_use


    def InFlightCount(self) -> int:
        with self._lock:
            return len(self._in_use)


    def Clear(self) -> None:
        with self._lock:
            self._in_use.clear()
            self._next_hint = 1
