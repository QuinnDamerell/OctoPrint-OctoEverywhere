import unittest

from octoeverywhere.octohttprequest import OctoHttpRequest


class TestOctoHttpRequest(unittest.TestCase):

    def test_parse_out_path_handles_absolute_url_query_slashes(self) -> None:
        self.assertEqual(OctoHttpRequest.ParseOutPath("https://example.com?next=/foo"), "/")
        self.assertEqual(OctoHttpRequest.ParseOutPath("https://example.com?q=a/b"), "/")


    def test_parse_out_path_removes_query_and_fragment(self) -> None:
        self.assertEqual(OctoHttpRequest.ParseOutPath("https://example.com/path/to/page?next=/foo#section"), "/path/to/page")
        self.assertEqual(OctoHttpRequest.ParseOutPath("/relative/path?next=/foo#section"), "/relative/path")


    def test_parse_out_path_normalizes_an_empty_absolute_path(self) -> None:
        self.assertEqual(OctoHttpRequest.ParseOutPath("https://example.com"), "/")
        self.assertEqual(OctoHttpRequest.ParseOutPath("https://example.com?query=value"), "/")


if __name__ == "__main__":
    unittest.main()
