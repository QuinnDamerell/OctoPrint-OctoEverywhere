import threading
from collections import OrderedDict
from typing import List, Optional

from .topicmatch import TopicMatcher
from .types import MqttMessage, _CloneMessage


# LRU cache of upstream retained PUBLISH messages keyed by exact topic name.
#
# MQTT 3.1.1 §3.3.1.3:
#   * A retained PUBLISH replaces any previous retained PUBLISH on the same
#     topic.
#   * A retained PUBLISH with a zero-length payload deletes any retained
#     message on the topic and is NOT stored.
#   * When a new subscription matches an existing retained PUBLISH, the broker
#     must send it to the subscriber with the RETAIN flag set.
#
# This cache lives in the mux because the upstream broker only sends each
# retained message ONCE - on the very first matching upstream subscribe. Later
# subscribes by other downstream clients have to be served from our cache.
#
# Bounded to avoid OOM if a chatty printer ever publishes thousands of distinct
# retained topics. Default 1024 matches the plan; the mux passes the config.
class RetainedCache:

    def __init__(self, max_entries: int = 1024) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._lock = threading.Lock()
        # OrderedDict for O(1) LRU update via move_to_end.
        self._entries: "OrderedDict[str, MqttMessage]" = OrderedDict()
        self._max_entries = max_entries


    # Process an inbound retained PUBLISH from upstream.
    # Returns True if the cache state changed, False otherwise (e.g. a delete
    # against a topic that wasn't cached).
    def OnRetainedPublish(self, message: MqttMessage) -> bool:
        if not message.retain:
            # Caller should only pass retain=True messages, but we guard
            # defensively rather than crashing - this code runs on paho's loop
            # thread and an unexpected raise here would tear down the conn.
            return False
        with self._lock:
            if len(message.payload) == 0:
                # Spec: zero-byte retained payload deletes any cached entry
                # and is not itself stored.
                return self._entries.pop(message.topic, None) is not None
            return self._StoreLocked(message)


    # Process a live PUBLISH that may be a retained update delivered to an
    # already-existing upstream subscription. MQTT brokers clear RETAIN for
    # those live deliveries, so we can only update topics we already know are
    # retained.
    def OnLivePublishForCachedTopic(self, message: MqttMessage) -> bool:
        if message.retain:
            return self.OnRetainedPublish(message)
        with self._lock:
            if message.topic not in self._entries:
                return False
            if len(message.payload) == 0:
                return self._entries.pop(message.topic, None) is not None
            retained_copy = MqttMessage(
                topic=message.topic,
                payload=message.payload,
                qos=message.qos,
                retain=True,
                properties=message.properties,
                packet_id=None,
            )
            return self._StoreLocked(retained_copy)


    # Returns cloned copies of all currently-cached retained messages whose
    # topic matches the given filter.
    def GetMatching(self, filter_: str) -> List[MqttMessage]:
        with self._lock:
            return [_CloneMessage(m) for topic, m in self._entries.items() if TopicMatcher.Matches(filter_, topic)]


    # Returns a cloned copy of the cached retained message for a single exact
    # topic, or None.
    def Get(self, topic: str) -> Optional[MqttMessage]:
        with self._lock:
            m = self._entries.get(topic, None)
            return _CloneMessage(m) if m is not None else None


    # Wipes the cache entirely. Called on upstream reconnect; the printer's
    # retained state is push-based and what we cached may no longer reflect
    # reality.
    def Clear(self) -> None:
        with self._lock:
            self._entries.clear()


    def Size(self) -> int:
        with self._lock:
            return len(self._entries)


    def _StoreLocked(self, message: MqttMessage) -> bool:
        self._entries[message.topic] = message
        self._entries.move_to_end(message.topic)
        # Enforce LRU bound.
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return True
