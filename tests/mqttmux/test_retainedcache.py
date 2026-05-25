import unittest

from octoeverywhere.mqttmux.retainedcache import RetainedCache
from octoeverywhere.mqttmux.types import MqttMessage


def _msg(topic, payload=b"x", retain=True):
    return MqttMessage(topic=topic, payload=payload, qos=0, retain=retain)


class TestRetainedCache(unittest.TestCase):

    def test_stores_and_retrieves(self):
        c = RetainedCache()
        self.assertTrue(c.OnRetainedPublish(_msg("a/b", b"hello")))
        got = c.Get("a/b")
        self.assertIsNotNone(got)
        self.assertEqual(got.payload, b"hello")

    def test_overwrites_same_topic(self):
        c = RetainedCache()
        c.OnRetainedPublish(_msg("a", b"one"))
        c.OnRetainedPublish(_msg("a", b"two"))
        self.assertEqual(c.Get("a").payload, b"two")
        self.assertEqual(c.Size(), 1)

    def test_zero_byte_payload_deletes(self):
        c = RetainedCache()
        c.OnRetainedPublish(_msg("a", b"hello"))
        self.assertEqual(c.Size(), 1)
        # Empty payload deletes and is NOT stored.
        changed = c.OnRetainedPublish(_msg("a", b""))
        self.assertTrue(changed)
        self.assertEqual(c.Size(), 0)
        self.assertIsNone(c.Get("a"))
        # Deleting a non-existent topic returns False.
        self.assertFalse(c.OnRetainedPublish(_msg("nope", b"")))

    def test_ignores_non_retained(self):
        c = RetainedCache()
        self.assertFalse(c.OnRetainedPublish(_msg("a", b"x", retain=False)))
        self.assertEqual(c.Size(), 0)

    def test_matching_with_wildcards(self):
        c = RetainedCache()
        c.OnRetainedPublish(_msg("device/sn1/report"))
        c.OnRetainedPublish(_msg("device/sn2/report"))
        c.OnRetainedPublish(_msg("other/topic"))
        results = c.GetMatching("device/+/report")
        self.assertEqual({m.topic for m in results}, {"device/sn1/report", "device/sn2/report"})
        results = c.GetMatching("#")
        self.assertEqual(len(results), 3)
        # $-rule: # doesn't match $-prefixed cached topic.
        c.OnRetainedPublish(_msg("$SYS/uptime"))
        results = c.GetMatching("#")
        self.assertEqual(len(results), 3)
        results = c.GetMatching("$SYS/#")
        self.assertEqual(len(results), 1)

    def test_lru_eviction(self):
        c = RetainedCache(max_entries=3)
        c.OnRetainedPublish(_msg("a"))
        c.OnRetainedPublish(_msg("b"))
        c.OnRetainedPublish(_msg("c"))
        # Touch "a" to refresh its LRU position.
        c.OnRetainedPublish(_msg("a", b"refresh"))
        # Add a 4th: "b" should evict (least recently used).
        c.OnRetainedPublish(_msg("d"))
        self.assertEqual(c.Size(), 3)
        self.assertIsNone(c.Get("b"))
        self.assertIsNotNone(c.Get("a"))
        self.assertIsNotNone(c.Get("c"))
        self.assertIsNotNone(c.Get("d"))

    def test_clear(self):
        c = RetainedCache()
        c.OnRetainedPublish(_msg("a"))
        c.OnRetainedPublish(_msg("b"))
        c.Clear()
        self.assertEqual(c.Size(), 0)


if __name__ == "__main__":
    unittest.main()
