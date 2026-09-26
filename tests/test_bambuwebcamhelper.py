import logging
import unittest
from unittest.mock import Mock, patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

from bambu_octoeverywhere.bambuclient import BambuClient  # noqa: E402
from bambu_octoeverywhere.bambumodels import BambuState  # noqa: E402
from bambu_octoeverywhere.bambuwebcamhelper import BambuWebcamHelper  # noqa: E402
from linux_host.config import Config  # noqa: E402


class TestBambuWebcamHelper(unittest.TestCase):
    def _GetUrl(self, advertised, host="printer.local", accessCode="12345678") -> str:
        config = Mock()
        values = {Config.BambuAccessToken: accessCode, Config.CompanionKeyIpOrHostname: host}
        config.GetStr.side_effect = lambda section, key, default: values[key]
        state = BambuState()
        state.rtsp_url = advertised
        client = Mock()
        client.GetState.return_value = state
        helper = BambuWebcamHelper(logging.getLogger("TestBambuWebcamHelper"), config)
        with patch.object(BambuClient, "Get", return_value=client):
            return helper._GetStreamingUrl()  # pylint: disable=protected-access

    def test_rtsp_uses_connected_host_and_preserves_path_and_port(self) -> None:
        for host in ("0.0.0.0", "192.0.2.1", "old-printer.local"):
            self.assertEqual(self._GetUrl(f"rtsps://{host}:322/streaming/live/1?quality=high"),
                             "rtsps://bblp:12345678@printer.local:322/streaming/live/1?quality=high")

    def test_rtsp_replaces_advertised_credentials(self) -> None:
        self.assertEqual(self._GetUrl("rtsp://old:secret@printer.local:554/live"),
                         "rtsp://bblp:12345678@printer.local:554/live")

    def test_ipv6_and_access_code_are_encoded(self) -> None:
        self.assertEqual(self._GetUrl("rtsps://[::]:322/live", "2001:db8::1", "a@b:c"),
                         "rtsps://bblp:a%40b%3Ac@[2001:db8::1]:322/live")

    def test_invalid_advertised_url_uses_default(self) -> None:
        for url in ("invalid", "rtsps://printer.local:bad/live", "http://printer.local/live"):
            self.assertEqual(self._GetUrl(url), "rtsps://bblp:12345678@printer.local:322/streaming/live/1")

    def test_websocket_camera_still_uses_its_port(self) -> None:
        self.assertEqual(self._GetUrl(None), "ws://bblp:12345678@printer.local:6000")


if __name__ == "__main__":
    unittest.main()
