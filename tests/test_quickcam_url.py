# ruff: noqa: E402
import logging
import unittest

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

from octoeverywhere.Webcam.quickcam import QuickCamManager


# QuickCam only calls into the platform helper once a capture thread is running, which these tests never do.
class FakeWebcamPlatformHelper:

    def OnQuickCamStreamStart(self, url:str) -> None:
        raise AssertionError("OnQuickCamStreamStart should not be called by these tests.")


    def OnQuickCamStreamStall(self, url:str) -> None:
        raise AssertionError("OnQuickCamStreamStall should not be called by these tests.")


    def ShouldQuickCamKeepRunning(self) -> bool:
        return False


class TestQuickCamUrlNormalization(unittest.TestCase):

    def setUp(self) -> None:
        logger = logging.getLogger("TestQuickCamUrlNormalization")
        logger.addHandler(logging.NullHandler())
        self.Manager = QuickCamManager(logger, FakeWebcamPlatformHelper()) #pyright: ignore[reportArgumentType]


    def _Normalize(self, url:str) -> str:
        return self.Manager._NormalizeQuickCamUrl(url) #pylint: disable=protected-access


    # This is the bug that caused the webcam to 401 - the auth token was being stripped from the URL we connect to.
    def test_auth_params_are_kept(self) -> None:
        self.assertEqual(self._Normalize("http://192.168.1.5/webcam/?action=stream&token=abc123"),
                         "http://192.168.1.5/webcam/?action=stream&token=abc123")
        self.assertEqual(self._Normalize("http://192.168.1.5/webcam/?action=stream&apikey=abc123"),
                         "http://192.168.1.5/webcam/?action=stream&apikey=abc123")
        self.assertEqual(self._Normalize("jmpeg://192.168.1.5/stream?action=stream&token=abc123"),
                         "jmpeg://192.168.1.5/stream?action=stream&token=abc123")
        self.assertEqual(self._Normalize("http://192.168.1.5/cgi-bin/api.cgi?cmd=Video&channel=0&user=me&password=secret"),
                         "http://192.168.1.5/cgi-bin/api.cgi?cmd=Video&channel=0&user=me&password=secret")


    # The reason this function exists in the first place - unique params per request would prevent stream sharing.
    def test_cache_busting_params_are_stripped(self) -> None:
        self.assertEqual(self._Normalize("http://192.168.1.5/webcam/?action=stream&t=1699999999&cachebust=xyz"),
                         "http://192.168.1.5/webcam/?action=stream")
        self.assertEqual(self._Normalize("http://192.168.1.5/webcam/?_=1699999999&rand=55&nocache=1&action=snapshot"),
                         "http://192.168.1.5/webcam/?action=snapshot")


    # This is the URL built by WebcamHelper.DetectWebRTCStreamUrlAndTranslate for the Snapmaker U1.
    def test_fps_param_is_kept(self) -> None:
        self.assertEqual(self._Normalize("http://192.168.1.5/webcam/stream.mjpg?fps=10"),
                         "http://192.168.1.5/webcam/stream.mjpg?fps=10")


    def test_param_names_are_matched_case_insensitively(self) -> None:
        self.assertEqual(self._Normalize("http://192.168.1.5/webcam/?ACTION=stream&Token=abc123"),
                         "http://192.168.1.5/webcam/?ACTION=stream&Token=abc123")


    def test_urls_without_params_are_untouched(self) -> None:
        self.assertEqual(self._Normalize("http://192.168.1.5/webcam/"), "http://192.168.1.5/webcam/")
        self.assertEqual(self._Normalize("rtsp://user:pass@192.168.1.5:554/stream1"), "rtsp://user:pass@192.168.1.5:554/stream1")
        # An empty query string still hits the parse path, but there's nothing to keep.
        self.assertEqual(self._Normalize("http://192.168.1.5/webcam/?"), "http://192.168.1.5/webcam/")


    def test_blank_values_and_fragments_are_preserved(self) -> None:
        self.assertEqual(self._Normalize("http://192.168.1.5/webcam/?action=stream&token="),
                         "http://192.168.1.5/webcam/?action=stream&token=")
        self.assertEqual(self._Normalize("http://192.168.1.5/webcam/?action=stream&t=123#frag"),
                         "http://192.168.1.5/webcam/?action=stream#frag")


    # Tokens are often base64ish, so they must survive the parse and re-encode round trip.
    def test_encoded_values_round_trip(self) -> None:
        # The value decodes to 'a+b/c=' and must be re-encoded so the server sees the same value.
        normalized = self._Normalize("http://192.168.1.5/webcam/?action=stream&token=a%2Bb%2Fc%3D")
        self.assertEqual(self._Normalize(normalized), normalized)
        from urllib.parse import parse_qs, urlsplit #pylint: disable=import-outside-toplevel
        self.assertEqual(parse_qs(urlsplit(normalized).query)["token"], ["a+b/c="])


class TestQuickCamInstanceSharing(unittest.TestCase):

    def setUp(self) -> None:
        logger = logging.getLogger("TestQuickCamInstanceSharing")
        logger.addHandler(logging.NullHandler())
        self.Manager = QuickCamManager(logger, FakeWebcamPlatformHelper()) #pyright: ignore[reportArgumentType]


    def _GetOrCreate(self, url:str):
        return self.Manager._GetOrCreate(url) #pylint: disable=protected-access


    def test_cache_busted_urls_share_one_instance(self) -> None:
        first = self._GetOrCreate("http://192.168.1.5/webcam/?action=stream&t=1")
        second = self._GetOrCreate("http://192.168.1.5/webcam/?action=stream&t=2")
        self.assertIs(first, second)


    # The QuickCam must connect with the full URL, including the auth token.
    def test_the_instance_url_keeps_the_token(self) -> None:
        url = "http://192.168.1.5/webcam/?action=stream&token=abc123&t=1"
        qc = self._GetOrCreate(url)
        self.assertEqual(qc.Url, "http://192.168.1.5/webcam/?action=stream&token=abc123")


    def test_different_tokens_do_not_share_an_instance(self) -> None:
        first = self._GetOrCreate("http://192.168.1.5/webcam/?action=stream&token=abc")
        second = self._GetOrCreate("http://192.168.1.5/webcam/?action=stream&token=xyz")
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
