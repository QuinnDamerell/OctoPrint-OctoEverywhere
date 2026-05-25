import unittest

from octoeverywhere.mqttmux.pktid import PacketIdAllocator
from octoeverywhere.mqttmux.types import ProtocolError


class TestPacketIdAllocator(unittest.TestCase):

    def test_allocate_starts_at_one_and_increments(self):
        a = PacketIdAllocator()
        self.assertEqual(a.Allocate(), 1)
        self.assertEqual(a.Allocate(), 2)
        self.assertEqual(a.Allocate(), 3)

    def test_free_releases_id(self):
        a = PacketIdAllocator()
        ids = [a.Allocate() for _ in range(5)]
        a.Free(ids[2])
        self.assertFalse(a.IsInUse(ids[2]))
        self.assertEqual(a.InFlightCount(), 4)
        # The allocator walks forward to minimize id-reuse ambiguity, so the
        # freed slot only becomes a candidate after a full sweep. That's
        # intentional - we don't assert it's reused immediately.

    def test_free_is_idempotent(self):
        a = PacketIdAllocator()
        a.Free(42)  # never allocated
        a.Free(42)  # double-free is fine
        self.assertFalse(a.IsInUse(42))

    def test_reserve_marks_in_use(self):
        a = PacketIdAllocator()
        self.assertTrue(a.Reserve(100))
        self.assertTrue(a.IsInUse(100))
        # Re-reserving the same id fails (collision).
        self.assertFalse(a.Reserve(100))
        # Allocate must not produce a reserved id.
        for _ in range(200):
            x = a.Allocate()
            self.assertNotEqual(x, 100)

    def test_reserve_rejects_invalid_ids(self):
        a = PacketIdAllocator()
        self.assertFalse(a.Reserve(0))         # id 0 forbidden
        self.assertFalse(a.Reserve(-1))
        self.assertFalse(a.Reserve(70000))

    def test_in_flight_count(self):
        a = PacketIdAllocator()
        self.assertEqual(a.InFlightCount(), 0)
        a.Allocate()
        a.Allocate()
        self.assertEqual(a.InFlightCount(), 2)

    def test_clear(self):
        a = PacketIdAllocator()
        for _ in range(10):
            a.Allocate()
        a.Clear()
        self.assertEqual(a.InFlightCount(), 0)
        self.assertEqual(a.Allocate(), 1)

    def test_exhaustion(self):
        a = PacketIdAllocator()
        # Reserve everything.
        for i in range(1, 65536):
            self.assertTrue(a.Reserve(i))
        with self.assertRaises(ProtocolError):
            a.Allocate()

    def test_wrap_around(self):
        a = PacketIdAllocator()
        # Reserve a span at the top, then allocate enough to wrap.
        for i in range(65500, 65536):
            a.Reserve(i)
        # Allocate 65500 ids - should give 1..65499 in order (none of 65500-65535
        # since they're reserved).
        seen = []
        for _ in range(65499):
            seen.append(a.Allocate())
        self.assertEqual(min(seen), 1)
        self.assertEqual(max(seen), 65499)
        # All allocated ids are distinct.
        self.assertEqual(len(set(seen)), len(seen))


if __name__ == "__main__":
    unittest.main()
