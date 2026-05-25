from .types import MAX_TOPIC_BYTES


# MQTT 3.1.1 topic/filter matching per §4.7. The rules are subtle so the
# implementation mirrors the spec wording rather than collapsing for cleverness:
#
#   * A filter consists of `/`-delimited levels.
#   * `+` matches exactly one level (any value).
#   * `#` matches zero or more remaining levels and may only appear as the
#     last level.
#   * Filters starting with `$` are normal; filters not starting with `$` do
#     NOT match topics that begin with `$` (so `$SYS/...` is not exposed via
#     wildcard subs). The check is on the very first level only.
#   * Topic names cannot be zero-length, contain `+` or `#`, or contain U+0000.


class TopicMatcher:

    # Returns True if `topic` matches `filter_`.
    # Caller is responsible for having validated both via Validate*().
    @staticmethod
    def Matches(filter_: str, topic: str) -> bool:
        if len(topic) == 0 or len(filter_) == 0:
            return False

        # `$`-topic rule: a wildcard filter whose first level is `+` or `#`
        # cannot match a topic whose first level starts with `$`.
        if topic[0] == "$":
            first_filter_level = filter_.split("/", 1)[0]
            if first_filter_level in ("+", "#"):
                return False

        # Fast path: identical strings always match (and avoids splitting).
        if filter_ == topic:
            return True

        f_parts = filter_.split("/")
        t_parts = topic.split("/")
        fi = 0
        ti = 0
        while fi < len(f_parts):
            f = f_parts[fi]
            if f == "#":
                # `#` matches zero or more remaining topic levels. Validate
                # already ensured this is the last filter level.
                return True
            if ti >= len(t_parts):
                # Filter has more levels than topic. Special case: `foo/+`
                # against `foo` does not match - `+` requires exactly one
                # level. The only way a shorter topic matches is if the
                # remaining filter is exactly `#`, handled above.
                return False
            if f == "+":
                # `+` matches any single level (including an empty one).
                pass
            elif f != t_parts[ti]:
                return False
            fi += 1
            ti += 1
        # All filter levels consumed; topic must also be fully consumed.
        return ti == len(t_parts)


    # Validates a topic filter (used in SUBSCRIBE/UNSUBSCRIBE). Returns True if
    # the filter is well-formed per MQTT 3.1.1 §4.7.
    @staticmethod
    def ValidateFilter(filter_: str) -> bool:
        if filter_ is None:
            return False
        if len(filter_) == 0:
            return False
        # UTF-8 byte length cap.
        try:
            byte_len = len(filter_.encode("utf-8"))
        except UnicodeEncodeError:
            return False
        if byte_len > MAX_TOPIC_BYTES:
            return False
        if "\x00" in filter_:
            return False

        parts = filter_.split("/")
        last_idx = len(parts) - 1
        for i, part in enumerate(parts):
            if "#" in part:
                # `#` must be alone in its level and must be the final level.
                if part != "#" or i != last_idx:
                    return False
            if "+" in part:
                # `+` must occupy the entire level.
                if part != "+":
                    return False
        return True


    # Validates a topic name (used in PUBLISH).
    @staticmethod
    def ValidateTopicName(topic: str) -> bool:
        if topic is None:
            return False
        if len(topic) == 0:
            return False
        try:
            byte_len = len(topic.encode("utf-8"))
        except UnicodeEncodeError:
            return False
        if byte_len > MAX_TOPIC_BYTES:
            return False
        if "\x00" in topic:
            return False
        if "+" in topic or "#" in topic:
            return False
        return True
