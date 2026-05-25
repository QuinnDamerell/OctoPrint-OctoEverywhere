import unittest

from octoeverywhere.mqttmux.topicmatch import TopicMatcher


class TestTopicMatcher(unittest.TestCase):

    def test_exact_match(self):
        self.assertTrue(TopicMatcher.Matches("foo/bar", "foo/bar"))
        self.assertFalse(TopicMatcher.Matches("foo/bar", "foo/baz"))
        self.assertFalse(TopicMatcher.Matches("foo/bar", "foo/bar/baz"))
        self.assertFalse(TopicMatcher.Matches("foo/bar/baz", "foo/bar"))

    def test_single_level_wildcard(self):
        self.assertTrue(TopicMatcher.Matches("foo/+", "foo/bar"))
        self.assertTrue(TopicMatcher.Matches("foo/+", "foo/baz"))
        self.assertFalse(TopicMatcher.Matches("foo/+", "foo/bar/baz"))
        self.assertFalse(TopicMatcher.Matches("foo/+", "foo"))
        self.assertTrue(TopicMatcher.Matches("+/+", "foo/bar"))
        self.assertTrue(TopicMatcher.Matches("foo/+/baz", "foo/bar/baz"))
        self.assertFalse(TopicMatcher.Matches("foo/+/baz", "foo/baz"))
        # + matches empty levels (e.g. "foo//bar" has an empty middle level).
        self.assertTrue(TopicMatcher.Matches("foo/+/bar", "foo//bar"))

    def test_multi_level_wildcard(self):
        self.assertTrue(TopicMatcher.Matches("#", "foo"))
        self.assertTrue(TopicMatcher.Matches("#", "foo/bar/baz"))
        self.assertTrue(TopicMatcher.Matches("foo/#", "foo/bar"))
        self.assertTrue(TopicMatcher.Matches("foo/#", "foo/bar/baz"))
        # # matches zero levels per spec (§4.7.1.2).
        self.assertTrue(TopicMatcher.Matches("foo/#", "foo"))
        self.assertFalse(TopicMatcher.Matches("foo/#", "bar"))

    def test_dollar_prefix_rules(self):
        # Wildcard filters do not match $-prefixed topics (§4.7.2).
        self.assertFalse(TopicMatcher.Matches("#", "$SYS/foo"))
        self.assertFalse(TopicMatcher.Matches("+/foo", "$SYS/foo"))
        self.assertFalse(TopicMatcher.Matches("+", "$SYS"))
        # Explicit $-filters do match.
        self.assertTrue(TopicMatcher.Matches("$SYS/#", "$SYS/foo"))
        self.assertTrue(TopicMatcher.Matches("$SYS/+", "$SYS/foo"))
        self.assertTrue(TopicMatcher.Matches("$SYS/foo", "$SYS/foo"))

    def test_empty_inputs(self):
        self.assertFalse(TopicMatcher.Matches("", "foo"))
        self.assertFalse(TopicMatcher.Matches("foo", ""))
        self.assertFalse(TopicMatcher.Matches("", ""))

    def test_validate_filter(self):
        self.assertTrue(TopicMatcher.ValidateFilter("foo"))
        self.assertTrue(TopicMatcher.ValidateFilter("foo/bar"))
        self.assertTrue(TopicMatcher.ValidateFilter("foo/+/bar"))
        self.assertTrue(TopicMatcher.ValidateFilter("foo/#"))
        self.assertTrue(TopicMatcher.ValidateFilter("#"))
        self.assertTrue(TopicMatcher.ValidateFilter("+"))
        self.assertTrue(TopicMatcher.ValidateFilter("$SYS/#"))
        self.assertFalse(TopicMatcher.ValidateFilter(""))
        self.assertFalse(TopicMatcher.ValidateFilter("foo/#/bar"))      # # not at end
        self.assertFalse(TopicMatcher.ValidateFilter("foo/bar#"))       # # not alone in level
        self.assertFalse(TopicMatcher.ValidateFilter("foo/bar+/baz"))   # + not alone in level
        self.assertFalse(TopicMatcher.ValidateFilter("foo\x00bar"))     # null char
        self.assertFalse(TopicMatcher.ValidateFilter(None))  # type: ignore[arg-type]

    def test_validate_topic_name(self):
        self.assertTrue(TopicMatcher.ValidateTopicName("foo"))
        self.assertTrue(TopicMatcher.ValidateTopicName("foo/bar"))
        self.assertTrue(TopicMatcher.ValidateTopicName("$SYS/foo"))
        self.assertFalse(TopicMatcher.ValidateTopicName(""))
        self.assertFalse(TopicMatcher.ValidateTopicName("foo/+/bar"))
        self.assertFalse(TopicMatcher.ValidateTopicName("foo/#"))
        self.assertFalse(TopicMatcher.ValidateTopicName("foo\x00bar"))
        self.assertFalse(TopicMatcher.ValidateTopicName(None))  # type: ignore[arg-type]

    def test_validate_filter_oversize(self):
        self.assertFalse(TopicMatcher.ValidateFilter("a" * 65536))
        self.assertTrue(TopicMatcher.ValidateFilter("a" * 65535))


if __name__ == "__main__":
    unittest.main()
