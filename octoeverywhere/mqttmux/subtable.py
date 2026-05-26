import threading
from typing import Dict, List, Optional, Tuple

from .topicmatch import TopicMatcher
from .types import MatchResult, MessageCallback


# A single subscription record held by the table.
class _Subscriber:
    __slots__ = ("handle_id", "qos", "callback", "subscription_identifier")
    def __init__(self, handle_id: int, qos: int, callback: Optional[MessageCallback],
                 subscription_identifier: Optional[int]) -> None:
        self.handle_id = handle_id
        self.qos = qos
        self.callback = callback
        self.subscription_identifier = subscription_identifier


# One entry per distinct filter string in the mux. Refcounted; the upstream
# subscribe is held as long as any downstream client is subscribed.
class _FilterEntry:
    __slots__ = ("max_upstream_qos", "subscribers")
    def __init__(self) -> None:
        self.max_upstream_qos = 0
        # List rather than dict-by-handle: a single handle MAY add the same
        # filter twice (e.g. a vendor client adds it from two different code
        # paths). We treat those as independent subscriptions so refcounting
        # works without surprise; remove-by-token gives precise unsubscribe.
        self.subscribers: List[_Subscriber] = []


# Result of a Subscribe() call telling the caller what work is left to do
# upstream. The mux uses this to decide whether to issue a real paho subscribe.
class SubscribeOutcome:
    __slots__ = ("needs_upstream_subscribe", "upstream_qos", "synthesized_granted_qos")

    def __init__(self, needs_upstream_subscribe: bool, upstream_qos: int,
                 synthesized_granted_qos: Optional[int]) -> None:
        # True iff this Subscribe added a brand-new filter OR escalated the
        # required upstream QoS for an existing filter. The mux must call
        # paho.subscribe(filter, qos=upstream_qos) in either case.
        self.needs_upstream_subscribe = needs_upstream_subscribe
        # The QoS we now want upstream (always >= any per-subscriber requested
        # QoS for this filter).
        self.upstream_qos = upstream_qos
        # If the upstream subscription already existed at >= the requested
        # QoS, the table has a granted QoS to immediately synthesize a SUBACK
        # for this subscriber. None when the mux must wait for the real
        # upstream SUBACK to fire callbacks back.
        self.synthesized_granted_qos = synthesized_granted_qos


class UnsubscribeOutcome:
    __slots__ = ("needs_upstream_unsubscribe", "removed_any")

    def __init__(self, needs_upstream_unsubscribe: bool, removed_any: bool) -> None:
        self.needs_upstream_unsubscribe = needs_upstream_unsubscribe
        self.removed_any = removed_any


# Refcounted multiplexing subscription table.
#
# Thread-safe. All public methods take an internal lock and never invoke
# callbacks; the mux is responsible for releasing the lock and then dispatching.
class SubscriptionTable:

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: Dict[str, _FilterEntry] = {}


    # Adds a subscription for (handle_id, filter) at the given downstream QoS.
    # Returns a SubscribeOutcome describing what the mux must do upstream and
    # whether it can immediately synthesize a SUBACK.
    def Subscribe(self, handle_id: int, filter_: str, qos: int,
                  callback: Optional[MessageCallback] = None,
                  subscription_identifier: Optional[int] = None) -> SubscribeOutcome:
        with self._lock:
            entry = self._entries.get(filter_)
            sub = _Subscriber(handle_id, qos, callback, subscription_identifier)
            if entry is None:
                entry = _FilterEntry()
                entry.max_upstream_qos = qos
                entry.subscribers.append(sub)
                self._entries[filter_] = entry
                return SubscribeOutcome(
                    needs_upstream_subscribe=True,
                    upstream_qos=qos,
                    synthesized_granted_qos=None,
                )
            # Existing filter. Add subscriber.
            entry.subscribers.append(sub)
            if qos > entry.max_upstream_qos:
                # QoS escalation: must re-subscribe upstream at the higher QoS.
                # (Real brokers may grant lower than requested; the mux must
                # remember the actually-granted QoS once the SUBACK lands.)
                entry.max_upstream_qos = qos
                return SubscribeOutcome(
                    needs_upstream_subscribe=True,
                    upstream_qos=qos,
                    # Don't synthesize yet - we want the real SUBACK to confirm
                    # the new QoS first; this subscriber will be acked then.
                    synthesized_granted_qos=None,
                )
            # Already subscribed upstream at >= this QoS. Synthesize a SUBACK
            # locally using the previously-granted QoS (min of requested vs
            # already-granted).
            granted = min(qos, entry.max_upstream_qos)
            return SubscribeOutcome(
                needs_upstream_subscribe=False,
                upstream_qos=entry.max_upstream_qos,
                synthesized_granted_qos=granted,
            )


    # Removes one subscription for (handle_id, filter). Removes only the
    # most-recent matching subscriber (LIFO) if there are multiple.
    # Returns an UnsubscribeOutcome telling the mux whether to issue a real
    # upstream UNSUBSCRIBE (refcount hit zero).
    def Unsubscribe(self, handle_id: int, filter_: str) -> UnsubscribeOutcome:
        with self._lock:
            entry = self._entries.get(filter_)
            if entry is None:
                return UnsubscribeOutcome(False, False)
            # Find the most recent subscriber for this handle and remove it.
            removed = False
            for i in range(len(entry.subscribers) - 1, -1, -1):
                if entry.subscribers[i].handle_id == handle_id:
                    del entry.subscribers[i]
                    removed = True
                    break
            if not removed:
                return UnsubscribeOutcome(False, False)
            if len(entry.subscribers) == 0:
                del self._entries[filter_]
                return UnsubscribeOutcome(True, True)
            entry.max_upstream_qos = max(s.qos for s in entry.subscribers)
            return UnsubscribeOutcome(False, True)


    # Wholesale removal: drops every subscription owned by `handle_id`. Used
    # when a virtual client detaches. Returns the list of filters that now
    # need real upstream UNSUBSCRIBE (refcount hit zero).
    def DetachHandle(self, handle_id: int) -> List[str]:
        with self._lock:
            now_empty: List[str] = []
            for filter_, entry in list(self._entries.items()):
                entry.subscribers = [s for s in entry.subscribers if s.handle_id != handle_id]
                if len(entry.subscribers) == 0:
                    del self._entries[filter_]
                    now_empty.append(filter_)
            return now_empty


    # Snapshots all current filters and their max upstream QoS. Called by the
    # mux on upstream reconnect to re-issue every subscription in a batch.
    def SnapshotFilters(self) -> List[Tuple[str, int]]:
        with self._lock:
            return [(f, e.max_upstream_qos) for f, e in self._entries.items()]


    # Records the actually-granted upstream QoS after a real SUBACK arrives.
    # Some brokers may grant a lower QoS than requested; this lets us keep the
    # table accurate so future synthesized SUBACKs reflect reality.
    def UpdateGrantedQos(self, filter_: str, granted_qos: int) -> None:
        with self._lock:
            entry = self._entries.get(filter_)
            if entry is None:
                return
            entry.max_upstream_qos = min(entry.max_upstream_qos, granted_qos)


    # Returns the per-handle dispatch list for an inbound topic. Per MQTT
    # 3.1.1 §3.3.5, when multiple of a single client's subscriptions match
    # the same incoming PUBLISH, the broker delivers ONE PUBLISH at the max
    # matching QoS to that client.
    #
    # Subscription identifiers (v5) are aggregated across all of the client's
    # matching filters. For 3.1.1 today this list stays empty.
    def GetMatchingSubscribers(self, topic: str) -> List[MatchResult]:
        with self._lock:
            per_handle: Dict[int, MatchResult] = {}
            for filter_, entry in self._entries.items():
                if not TopicMatcher.Matches(filter_, topic):
                    continue
                for sub in entry.subscribers:
                    existing = per_handle.get(sub.handle_id)
                    if existing is None:
                        ids: List[int] = []
                        if sub.subscription_identifier is not None:
                            ids.append(sub.subscription_identifier)
                        per_handle[sub.handle_id] = MatchResult(
                            handle_id=sub.handle_id,
                            qos=sub.qos,
                            callback=sub.callback,
                            subscription_identifiers=ids,
                        )
                    else:
                        if sub.qos > existing.qos:
                            existing.qos = sub.qos
                            # Whichever subscriber has the highest QoS wins
                            # the callback slot - both are owned by the same
                            # handle anyway and the wire client doesn't read
                            # the callback field.
                            existing.callback = sub.callback
                        if sub.subscription_identifier is not None:
                            existing.subscription_identifiers.append(sub.subscription_identifier)
            return list(per_handle.values())


    # Clears the entire table. Used when the mux is torn down.
    def Clear(self) -> None:
        with self._lock:
            self._entries.clear()


    # Diagnostics: number of distinct upstream filters held.
    def FilterCount(self) -> int:
        with self._lock:
            return len(self._entries)
