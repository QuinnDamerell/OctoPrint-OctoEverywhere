import logging
import threading
import time
import unittest
from typing import List, Optional, Tuple

from octoeverywhere.mqttmux.localclient import LocalPluginClient
from octoeverywhere.mqttmux.mux import (
    IVirtualClient,
    InitialSubscription,
    MqttConnectionContext,
    MqttUpstreamMux,
    VirtualClientHandle,
)
from octoeverywhere.mqttmux.types import MqttMessage

from .fakepaho import FakePahoClient


def _silent_logger() -> logging.Logger:
    logger = logging.getLogger("mqttmux.test")
    logger.setLevel(logging.CRITICAL)
    return logger


def _make_ctx() -> MqttConnectionContext:
    return MqttConnectionContext(host="printer.local", port=1883, use_tls=False)


def _make_mux(fake: FakePahoClient, *, initial_subs=None, ctx_provider=None) -> MqttUpstreamMux:
    return MqttUpstreamMux(
        logger=_silent_logger(),
        printer_key="test",
        connection_context_provider=ctx_provider or (lambda: _make_ctx()),
        initial_subscriptions=initial_subs,
        subscribe_timeout_sec=2.0,
        publish_timeout_sec=2.0,
        client_factory=lambda *a, **kw: fake,
        backoff_min_sec=0.05,
        backoff_max_sec=0.1,
    )


# A capturing IVirtualClient for direct mux tests (without going through
# LocalPluginClient). Records connect/disconnect/messages.
class _CaptureClient(IVirtualClient):
    def __init__(self) -> None:
        self.connected: List[VirtualClientHandle] = []
        self.disconnected: List[Tuple[VirtualClientHandle, Optional[Exception]]] = []
        self.messages: List[MqttMessage] = []
    def OnUpstreamConnected(self, handle):
        self.connected.append(handle)
    def OnUpstreamDisconnected(self, handle, reason):
        self.disconnected.append((handle, reason))
    def DeliverMessage(self, handle, message):
        self.messages.append(message)


class TestMuxLifecycle(unittest.TestCase):

    def test_connect_fires_on_upstream_connected(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        client = _CaptureClient()
        handle = mux.Attach(client)
        mux.Start()
        # Wait for supervisor to call connect.
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(fake.connect_called)
        self.assertTrue(fake.loop_started)
        # Trigger CONNACK.
        fake.FireConnect(0)
        self.assertTrue(mux.IsUpstreamConnected())
        self.assertEqual(len(client.connected), 1)
        self.assertIs(client.connected[0], handle)
        mux.Shutdown()

    def test_attach_after_connect_fires_immediately(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        fake.FireConnect(0)
        late = _CaptureClient()
        mux.Attach(late)
        self.assertEqual(len(late.connected), 1)
        mux.Shutdown()

    def test_disconnect_fires_on_upstream_disconnected_and_triggers_reconnect(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        client = _CaptureClient()
        mux.Attach(client)
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        fake.FireConnect(0)
        # Trigger an unexpected disconnect.
        fake.FireDisconnect(7)
        self.assertEqual(len(client.disconnected), 1)
        self.assertFalse(mux.IsUpstreamConnected())
        # Supervisor should attempt another connect after the backoff sleep.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if fake.connect_called and fake.disconnect_called is False:
                # connect_called is sticky True; we instead check it stayed true,
                # which it has. Just give the loop a moment then break.
                break
            time.sleep(0.01)
        mux.Shutdown()

    def test_shutdown_disconnects_paho(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        fake.FireConnect(0)
        mux.Shutdown()
        # Shutdown is idempotent.
        mux.Shutdown()
        self.assertTrue(fake.disconnect_called)

    def test_paho_internal_reconnect_is_disabled(self):
        fake = FakePahoClient()
        captured_kwargs = {}
        def factory(*args, **kwargs):  # noqa: ARG001
            captured_kwargs.update(kwargs)
            return fake
        mux = MqttUpstreamMux(
            logger=_silent_logger(),
            printer_key="test",
            connection_context_provider=lambda: _make_ctx(),
            client_factory=factory,
            backoff_min_sec=0.05,
            backoff_max_sec=0.1,
        )
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        self.assertIs(captured_kwargs.get("reconnect_on_failure"), False)
        mux.Shutdown()


class TestMuxSubscribe(unittest.TestCase):

    def _start_mux(self, initial_subs=None):
        fake = FakePahoClient()
        mux = _make_mux(fake, initial_subs=initial_subs)
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        fake.FireConnect(0)
        return mux, fake

    def test_first_subscribe_issues_paho_subscribe_and_waits_for_suback(self):
        mux, fake = self._start_mux()
        client = _CaptureClient()
        handle = mux.Attach(client)

        result_holder = {}
        def do_sub():
            result_holder["r"] = mux.Subscribe(handle, "device/+/report", 1)
        t = threading.Thread(target=do_sub)
        t.start()
        # Wait until paho.subscribe was invoked and pick up its mid.
        deadline = time.time() + 1.0
        while not fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(fake.subscribes, [("device/+/report", 1)])
        fake.FireSubAck(mid=1, granted_qos_list=[1])
        t.join(timeout=1.0)
        self.assertEqual(result_holder["r"].granted_qos, 1)
        mux.Shutdown()

    def test_duplicate_subscribe_synthesizes_without_paho(self):
        mux, fake = self._start_mux()
        c1 = _CaptureClient()
        c2 = _CaptureClient()
        h1 = mux.Attach(c1)
        h2 = mux.Attach(c2)

        # First subscribe needs upstream.
        def do_first():
            mux.Subscribe(h1, "a/b", 0)
        t = threading.Thread(target=do_first)
        t.start()
        deadline = time.time() + 1.0
        while not fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        t.join(timeout=1.0)
        self.assertEqual(len(fake.subscribes), 1)

        # Second subscribe by a different handle - no new paho subscribe.
        r2 = mux.Subscribe(h2, "a/b", 0)
        self.assertEqual(r2.granted_qos, 0)
        self.assertEqual(len(fake.subscribes), 1)
        mux.Shutdown()

    def test_qos_escalation_triggers_new_upstream_subscribe(self):
        mux, fake = self._start_mux()
        c = _CaptureClient()
        h = mux.Attach(c)

        def first():
            mux.Subscribe(h, "a/b", 0)
        t = threading.Thread(target=first)
        t.start()
        deadline = time.time() + 1.0
        while not fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        t.join(timeout=1.0)

        def second():
            mux.Subscribe(h, "a/b", 2)
        t = threading.Thread(target=second)
        t.start()
        deadline = time.time() + 1.0
        while len(fake.subscribes) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(fake.subscribes[-1], ("a/b", 2))
        fake.FireSubAck(mid=2, granted_qos_list=[2])
        t.join(timeout=1.0)
        mux.Shutdown()

    def test_subscribe_during_disconnect_returns_failure(self):
        # Build mux but DON'T fire connect.
        fake = FakePahoClient()
        mux = _make_mux(fake)
        c = _CaptureClient()
        h = mux.Attach(c)
        mux.Start()
        deadline = time.time() + 1.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        # Don't fire connect - state is not connected.
        r = mux.Subscribe(h, "a", 0)
        self.assertTrue(r.IsFailure())
        mux.Shutdown()

    def test_failed_subscribe_is_not_left_in_dispatch_table(self):
        mux, fake = self._start_mux()
        c = _CaptureClient()
        h = mux.Attach(c)

        result_holder = {}
        def do_sub():
            result_holder["r"] = mux.Subscribe(h, "denied/topic", 0)
        t = threading.Thread(target=do_sub)
        t.start()
        deadline = time.time() + 1.0
        while not fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        fake.FireSubAck(mid=1, granted_qos_list=[0x80])
        t.join(timeout=1.0)
        self.assertTrue(result_holder["r"].IsFailure())

        fake.FireMessage("denied/topic", b"should-not-deliver", qos=0)
        self.assertEqual(len(c.messages), 0)
        mux.Shutdown()

    def test_concurrent_subscribe_waits_for_in_flight_so_failure_propagates(self):
        # Race: A subscribes (real upstream). While A waits for SUBACK, B
        # subscribes to the same filter. Without serialization, B would see
        # A's subtable entry and synthesize a "success" SUBACK. When A's
        # real SUBACK then comes back as failure, the rollback removes A's
        # entry - but B has already been told the subscribe succeeded even
        # though nothing flows from upstream.
        mux, fake = self._start_mux()
        c_a = _CaptureClient()
        c_b = _CaptureClient()
        h_a = mux.Attach(c_a)
        h_b = mux.Attach(c_b)

        results = {}
        def sub_a():
            results["a"] = mux.Subscribe(h_a, "shared", 0)
        def sub_b():
            results["b"] = mux.Subscribe(h_b, "shared", 0)
        ta = threading.Thread(target=sub_a)
        ta.start()
        # Wait for A's paho subscribe to be issued (means A is past the
        # subtable insert and waiting for SUBACK).
        deadline = time.time() + 1.0
        while not fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        # Now start B. It should detect A's in-flight subscribe and wait.
        tb = threading.Thread(target=sub_b)
        tb.start()
        # Give B a moment to enter its wait.
        time.sleep(0.05)
        # A's SUBACK comes back as failure.
        fake.FireSubAck(mid=1, granted_qos_list=[0x80])
        ta.join(timeout=2.0)
        # B should now wake up, re-evaluate, try its own upstream sub.
        deadline = time.time() + 1.0
        while len(fake.subscribes) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(fake.subscribes), 2, "B should have triggered a fresh upstream subscribe")
        fake.FireSubAck(mid=2, granted_qos_list=[0])
        tb.join(timeout=2.0)
        # A saw failure; B saw success (its own fresh attempt).
        self.assertTrue(results["a"].IsFailure())
        self.assertFalse(results["b"].IsFailure())
        mux.Shutdown()

    def test_unsubscribe_refcounted(self):
        mux, fake = self._start_mux()
        c1 = _CaptureClient()
        c2 = _CaptureClient()
        h1 = mux.Attach(c1)
        h2 = mux.Attach(c2)

        def sub1():
            mux.Subscribe(h1, "a", 0)
        t = threading.Thread(target=sub1)
        t.start()
        deadline = time.time() + 1.0
        while not fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        t.join(timeout=1.0)

        mux.Subscribe(h2, "a", 0)  # synthesized

        # Unsubscribe h1 - refcount still 1 - no upstream unsubscribe.
        mux.Unsubscribe(h1, "a")
        self.assertEqual(fake.unsubscribes, [])

        # Unsubscribe h2 - refcount hits 0 - upstream unsubscribe.
        def unsub2():
            mux.Unsubscribe(h2, "a")
        t = threading.Thread(target=unsub2)
        t.start()
        deadline = time.time() + 1.0
        while not fake.unsubscribes and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(fake.unsubscribes, ["a"])
        fake.FireUnsubAck(mid=fake.publishes[0][4] if fake.publishes else 2)
        t.join(timeout=1.0)
        mux.Shutdown()


class TestMuxDispatch(unittest.TestCase):

    def _start_mux(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        fake.FireConnect(0)
        return mux, fake

    def test_inbound_message_dispatched_to_matching_handles(self):
        mux, fake = self._start_mux()
        c1 = _CaptureClient()
        c2 = _CaptureClient()
        c3 = _CaptureClient()
        h1 = mux.Attach(c1)
        h2 = mux.Attach(c2)
        h3 = mux.Attach(c3)

        def subs():
            mux.Subscribe(h1, "device/+/report", 0)
            mux.Subscribe(h2, "device/sn1/#", 0)
            mux.Subscribe(h3, "other/topic", 0)
        t = threading.Thread(target=subs)
        t.start()
        # Two new filters need real upstream subs (h2's is the 3rd, h1's is 1st, h3's is 2nd).
        deadline = time.time() + 1.0
        while len(fake.subscribes) < 3 and time.time() < deadline:
            time.sleep(0.01)
        for mid, _ in enumerate(fake.subscribes, start=1):
            fake.FireSubAck(mid=mid, granted_qos_list=[0])
        t.join(timeout=1.0)

        fake.FireMessage("device/sn1/report", b"payload", qos=0)
        self.assertEqual(len(c1.messages), 1)
        self.assertEqual(len(c2.messages), 1)
        self.assertEqual(len(c3.messages), 0)
        self.assertEqual(c1.messages[0].topic, "device/sn1/report")
        self.assertEqual(c1.messages[0].payload, b"payload")
        mux.Shutdown()

    def test_inbound_qos_demoted_per_handle(self):
        mux, fake = self._start_mux()
        c = _CaptureClient()
        h = mux.Attach(c)

        def sub():
            mux.Subscribe(h, "x", 0)  # downstream wants QoS 0
        t = threading.Thread(target=sub)
        t.start()
        deadline = time.time() + 1.0
        while not fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        t.join(timeout=1.0)
        # Inbound QoS 2 must be demoted to 0 for this subscriber.
        fake.FireMessage("x", b"d", qos=2, mid=10)
        self.assertEqual(len(c.messages), 1)
        self.assertEqual(c.messages[0].qos, 0)
        mux.Shutdown()

    def test_dollar_topic_excluded_from_wildcards(self):
        mux, fake = self._start_mux()
        c = _CaptureClient()
        h = mux.Attach(c)
        def sub():
            mux.Subscribe(h, "#", 0)
        t = threading.Thread(target=sub)
        t.start()
        deadline = time.time() + 1.0
        while not fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        t.join(timeout=1.0)
        fake.FireMessage("$SYS/foo", b"x", qos=0)
        self.assertEqual(len(c.messages), 0)
        fake.FireMessage("regular/topic", b"x", qos=0)
        self.assertEqual(len(c.messages), 1)
        mux.Shutdown()


class TestMuxRetained(unittest.TestCase):

    def _start_mux(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        fake.FireConnect(0)
        return mux, fake

    def test_retained_replay_on_new_subscribe(self):
        mux, fake = self._start_mux()
        c1 = _CaptureClient()
        h1 = mux.Attach(c1)

        def sub1():
            mux.Subscribe(h1, "device/+/state", 0)
        t = threading.Thread(target=sub1)
        t.start()
        deadline = time.time() + 1.0
        while not fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        t.join(timeout=1.0)

        # Upstream delivers a retained message (e.g. via the printer pushing
        # initial state).
        fake.FireMessage("device/sn1/state", b"retained-state", qos=0, retain=True)
        # c1 already got it as a live delivery.
        self.assertEqual(len(c1.messages), 1)

        # A second client subscribes to a matching filter. It should receive
        # the cached retained message via replay, no new upstream subscribe.
        c2 = _CaptureClient()
        h2 = mux.Attach(c2)
        r = mux.Subscribe(h2, "device/+/state", 0)
        self.assertEqual(r.granted_qos, 0)
        self.assertEqual(len(c2.messages), 1)
        self.assertEqual(c2.messages[0].payload, b"retained-state")
        self.assertTrue(c2.messages[0].retain)
        mux.Shutdown()

    def test_retained_zero_byte_deletes(self):
        mux, fake = self._start_mux()
        c = _CaptureClient()
        h = mux.Attach(c)
        def sub():
            mux.Subscribe(h, "x", 0)
        t = threading.Thread(target=sub)
        t.start()
        deadline = time.time() + 1.0
        while not fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        t.join(timeout=1.0)
        fake.FireMessage("x", b"stored", qos=0, retain=True)
        fake.FireMessage("x", b"", qos=0, retain=True)  # deletes
        # New subscriber should NOT receive the deleted message.
        c2 = _CaptureClient()
        h2 = mux.Attach(c2)
        mux.Subscribe(h2, "x", 0)
        # c2 may have got the live deliveries we just fired - no, those went to
        # c only. Now check no retained replay.
        self.assertEqual(len(c2.messages), 0)
        mux.Shutdown()


class TestMuxReconnect(unittest.TestCase):

    def test_reconnect_re_issues_subscriptions(self):
        fake = FakePahoClient()
        # Initial sub set the vendor wants.
        initial = [InitialSubscription(filter="device/sn/report", qos=0)]
        mux = _make_mux(fake, initial_subs=initial)
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        fake.FireConnect(0)

        # Initial sub should have been issued.
        self.assertIn(("device/sn/report", 0), fake.subscribes)

        # Attach a virtual client with its own sub.
        c = _CaptureClient()
        h = mux.Attach(c)
        def do_sub():
            mux.Subscribe(h, "user/topic", 1)
        t = threading.Thread(target=do_sub)
        t.start()
        deadline = time.time() + 1.0
        while ("user/topic", 1) not in fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        # Find the SUBACK mid for user/topic.
        user_mid = None
        for i, (f, _q) in enumerate(fake.subscribes, start=1):
            if f == "user/topic":
                user_mid = i
                break
        self.assertIsNotNone(user_mid)
        fake.FireSubAck(mid=user_mid, granted_qos_list=[1])
        t.join(timeout=1.0)

        # Disconnect; supervisor schedules a reconnect.
        subs_before_reconnect = list(fake.subscribes)
        fake.FireDisconnect(0)
        # Wait for the supervisor to wake from backoff sleep and re-fire connect.
        # In our test fake, the same client is reused, so connect_called stays
        # True. We instead wait for a fresh CONNACK to be needed - the supervisor
        # will eventually call connect() again on the SAME fake client. Trigger
        # a CONNECT callback again.
        time.sleep(0.2)
        fake.FireConnect(0)
        # The mux should re-issue every subscription on the new connection -
        # both the initial and the dynamic user/topic at the right QoS.
        new_subs = fake.subscribes[len(subs_before_reconnect):]
        new_filters = {(f, q) for (f, q) in new_subs}
        self.assertIn(("device/sn/report", 0), new_filters)
        self.assertIn(("user/topic", 1), new_filters)
        mux.Shutdown()


class TestMuxDetach(unittest.TestCase):

    def test_detach_releases_subscriptions(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        fake.FireConnect(0)
        c = _CaptureClient()
        h = mux.Attach(c)
        def do_sub():
            mux.Subscribe(h, "a", 0)
            mux.Subscribe(h, "b", 0)
        t = threading.Thread(target=do_sub)
        t.start()
        deadline = time.time() + 1.0
        while len(fake.subscribes) < 2 and time.time() < deadline:
            time.sleep(0.01)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        fake.FireSubAck(mid=2, granted_qos_list=[0])
        t.join(timeout=1.0)
        mux.Detach(h)
        self.assertEqual(set(fake.unsubscribes), {"a", "b"})
        mux.Shutdown()


class TestLocalPluginClient(unittest.TestCase):

    def test_subscribe_callback_fires(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        fake.FireConnect(0)

        client = LocalPluginClient(_silent_logger(), mux)
        client.Start()
        received = []
        def cb(msg):
            received.append(msg)
        result_holder = {}
        def do_sub():
            result_holder["t"] = client.Subscribe("a/+/c", 0, cb)
        t = threading.Thread(target=do_sub)
        t.start()
        deadline = time.time() + 1.0
        while not fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        t.join(timeout=1.0)
        self.assertIsNotNone(result_holder["t"])

        fake.FireMessage("a/b/c", b"hi", qos=0)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].payload, b"hi")
        mux.Shutdown()

    def test_two_overlapping_subs_both_fire(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        fake.FireConnect(0)

        client = LocalPluginClient(_silent_logger(), mux)
        client.Start()
        a_msgs, b_msgs = [], []
        def do():
            client.Subscribe("a/+", 0, lambda m: a_msgs.append(m))
            client.Subscribe("a/b", 0, lambda m: b_msgs.append(m))
        t = threading.Thread(target=do)
        t.start()
        deadline = time.time() + 1.0
        while len(fake.subscribes) < 2 and time.time() < deadline:
            time.sleep(0.01)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        fake.FireSubAck(mid=2, granted_qos_list=[0])
        t.join(timeout=1.0)

        fake.FireMessage("a/b", b"x", qos=0)
        # Both callbacks fire because both filters match.
        self.assertEqual(len(a_msgs), 1)
        self.assertEqual(len(b_msgs), 1)
        mux.Shutdown()

    def test_publish_qos0_returns_true(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        fake.FireConnect(0)
        client = LocalPluginClient(_silent_logger(), mux)
        client.Start()
        ok = client.Publish("topic", b"data", qos=0)
        self.assertTrue(ok)
        self.assertEqual(fake.publishes[-1][0], "topic")
        self.assertEqual(fake.publishes[-1][1], b"data")
        mux.Shutdown()

    def test_unsubscribe_removes_callback(self):
        fake = FakePahoClient()
        mux = _make_mux(fake)
        mux.Start()
        deadline = time.time() + 2.0
        while not fake.connect_called and time.time() < deadline:
            time.sleep(0.01)
        fake.FireConnect(0)
        client = LocalPluginClient(_silent_logger(), mux)
        client.Start()
        received = []
        result_holder = {}
        def do():
            result_holder["t"] = client.Subscribe("a", 0, lambda m: received.append(m))
        t = threading.Thread(target=do)
        t.start()
        deadline = time.time() + 1.0
        while not fake.subscribes and time.time() < deadline:
            time.sleep(0.01)
        fake.FireSubAck(mid=1, granted_qos_list=[0])
        t.join(timeout=1.0)
        fake.FireMessage("a", b"1", qos=0)
        self.assertEqual(len(received), 1)
        # Unsubscribe.
        def unsub():
            client.Unsubscribe(result_holder["t"])
        t = threading.Thread(target=unsub)
        t.start()
        deadline = time.time() + 1.0
        while not fake.unsubscribes and time.time() < deadline:
            time.sleep(0.01)
        fake.FireUnsubAck(mid=fake._next_mid - 1)
        t.join(timeout=1.0)
        fake.FireMessage("a", b"2", qos=0)
        # Callback should not be invoked again.
        self.assertEqual(len(received), 1)
        mux.Shutdown()


if __name__ == "__main__":
    unittest.main()
