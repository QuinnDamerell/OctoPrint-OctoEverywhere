import unittest

from octoeverywhere.mqttmux.subtable import SubscriptionTable


class TestSubscriptionTable(unittest.TestCase):

    def test_first_subscribe_needs_upstream(self):
        t = SubscriptionTable()
        out = t.Subscribe(handle_id=1, filter_="device/+/report", qos=0)
        self.assertTrue(out.needs_upstream_subscribe)
        self.assertEqual(out.upstream_qos, 0)
        self.assertIsNone(out.synthesized_granted_qos)

    def test_duplicate_subscribe_synthesizes_suback(self):
        t = SubscriptionTable()
        t.Subscribe(handle_id=1, filter_="a/b", qos=1)
        out = t.Subscribe(handle_id=2, filter_="a/b", qos=1)
        self.assertFalse(out.needs_upstream_subscribe)
        self.assertEqual(out.synthesized_granted_qos, 1)

    def test_qos_escalation_requires_upstream(self):
        t = SubscriptionTable()
        t.Subscribe(handle_id=1, filter_="a", qos=0)
        out = t.Subscribe(handle_id=2, filter_="a", qos=2)
        self.assertTrue(out.needs_upstream_subscribe)
        self.assertEqual(out.upstream_qos, 2)
        # Subsequent subs at <= the new max are immediate.
        out2 = t.Subscribe(handle_id=3, filter_="a", qos=1)
        self.assertFalse(out2.needs_upstream_subscribe)
        self.assertEqual(out2.synthesized_granted_qos, 1)

    def test_unsubscribe_refcount(self):
        t = SubscriptionTable()
        t.Subscribe(handle_id=1, filter_="a", qos=0)
        t.Subscribe(handle_id=2, filter_="a", qos=0)
        out = t.Unsubscribe(handle_id=1, filter_="a")
        self.assertTrue(out.removed_any)
        self.assertFalse(out.needs_upstream_unsubscribe)
        out = t.Unsubscribe(handle_id=2, filter_="a")
        self.assertTrue(out.needs_upstream_unsubscribe)

    def test_unsubscribe_unknown_is_noop(self):
        t = SubscriptionTable()
        out = t.Unsubscribe(handle_id=1, filter_="never")
        self.assertFalse(out.needs_upstream_unsubscribe)
        self.assertFalse(out.removed_any)

    def test_detach_handle(self):
        t = SubscriptionTable()
        t.Subscribe(handle_id=1, filter_="a", qos=0)
        t.Subscribe(handle_id=1, filter_="b", qos=0)
        t.Subscribe(handle_id=2, filter_="a", qos=0)
        now_empty = t.DetachHandle(handle_id=1)
        # "a" still has handle 2; "b" had only handle 1.
        self.assertEqual(now_empty, ["b"])
        self.assertEqual(t.FilterCount(), 1)

    def test_dispatch_max_qos_per_handle(self):
        # Per MQTT 3.1.1 §3.3.5: when multiple subs of one client match, the
        # client receives ONE PUBLISH at the max matching QoS.
        t = SubscriptionTable()
        t.Subscribe(handle_id=1, filter_="a/b", qos=0)
        t.Subscribe(handle_id=1, filter_="a/+", qos=2)
        t.Subscribe(handle_id=2, filter_="#", qos=1)
        results = t.GetMatchingSubscribers("a/b")
        per_handle = {r.handle_id: r for r in results}
        self.assertEqual(per_handle[1].qos, 2)
        self.assertEqual(per_handle[2].qos, 1)
        self.assertEqual(len(results), 2)

    def test_dispatch_wildcards_match(self):
        t = SubscriptionTable()
        t.Subscribe(handle_id=1, filter_="device/+/report", qos=0)
        t.Subscribe(handle_id=2, filter_="device/#", qos=0)
        results = t.GetMatchingSubscribers("device/abc/report")
        self.assertEqual({r.handle_id for r in results}, {1, 2})
        # No match.
        self.assertEqual(t.GetMatchingSubscribers("other"), [])

    def test_dispatch_dollar_topic_excluded_from_wildcards(self):
        t = SubscriptionTable()
        t.Subscribe(handle_id=1, filter_="#", qos=0)
        t.Subscribe(handle_id=2, filter_="$SYS/#", qos=0)
        results = t.GetMatchingSubscribers("$SYS/uptime")
        self.assertEqual({r.handle_id for r in results}, {2})

    def test_snapshot_filters(self):
        t = SubscriptionTable()
        t.Subscribe(handle_id=1, filter_="a", qos=0)
        t.Subscribe(handle_id=1, filter_="b", qos=2)
        snap = dict(t.SnapshotFilters())
        self.assertEqual(snap, {"a": 0, "b": 2})

    def test_update_granted_qos(self):
        t = SubscriptionTable()
        t.Subscribe(handle_id=1, filter_="a", qos=2)
        # Broker grants only QoS 1.
        t.UpdateGrantedQos("a", 1)
        out = t.Subscribe(handle_id=2, filter_="a", qos=2)
        # New subscriber requested QoS 2; we don't have it upstream, so we
        # escalate (not synthesize).
        self.assertTrue(out.needs_upstream_subscribe)

    def test_subscription_identifiers_aggregated(self):
        # v5-shaped: even though we don't speak v5, the table aggregates IDs.
        t = SubscriptionTable()
        t.Subscribe(handle_id=1, filter_="a/+", qos=0, subscription_identifier=10)
        t.Subscribe(handle_id=1, filter_="a/b", qos=0, subscription_identifier=20)
        t.Subscribe(handle_id=2, filter_="a/b", qos=0)  # no id
        results = t.GetMatchingSubscribers("a/b")
        per_handle = {r.handle_id: r for r in results}
        self.assertEqual(sorted(per_handle[1].subscription_identifiers), [10, 20])
        self.assertEqual(per_handle[2].subscription_identifiers, [])


if __name__ == "__main__":
    unittest.main()
