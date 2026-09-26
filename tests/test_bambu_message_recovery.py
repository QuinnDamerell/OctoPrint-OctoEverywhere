import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

from bambu_octoeverywhere.bambuclient import BambuClient  # noqa: E402
from octoeverywhere.sentry import Sentry  # noqa: E402


class TestBambuMessageRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self.Client = BambuClient.__new__(BambuClient)
        self.Client.Logger = logging.getLogger("TestBambuMessageRecovery")
        self.Client.LastMalformedMessageReconnectSec = 0.0
        self.Client.State = None
        self.Client.HasDoneFirstFullStateSync = False
        self.Client.StateTranslator = Mock()
        self.Client._mux = Mock()  # pylint: disable=protected-access
        self.Client._HandlePendingCommandResponse = Mock()  # pylint: disable=protected-access

    def test_invalid_json_recovers_without_reconnect_loop_and_next_update_works(self) -> None:
        # pylint: disable=protected-access
        with patch.object(Sentry, "OnException") as report, patch("bambu_octoeverywhere.bambuclient.time.monotonic", return_value=100.0):
            for _ in range(2):
                self.Client._OnReportMessage(SimpleNamespace(payload=b'{"print":', topic="report"))
            self.Client._mux.ForceReconnect.assert_called_once()
            self.assertEqual(report.call_count, 2)
            self.assertNotIn('{"print":', report.call_args[0][0])
            self.Client._OnReportMessage(SimpleNamespace(payload=b'{"print":{"mc_percent":12}}', topic="report"))
        self.assertEqual(self.Client.State.mc_percent, 12)
        self.Client.StateTranslator.OnMqttMessage.assert_called_once()

    def test_invalid_utf8_also_recovers(self) -> None:
        # pylint: disable=protected-access
        with patch.object(Sentry, "OnException"):
            self.Client._OnReportMessage(SimpleNamespace(payload=b'\xff', topic="report"))
        self.Client._mux.ForceReconnect.assert_called_once()

    def test_disconnected_full_sync_reconnects_without_sentry(self) -> None:
        # pylint: disable=protected-access
        for results in ([False], [True, False]):
            with self.subTest(results=results), patch.object(self.Client, "_Publish", side_effect=results), patch.object(Sentry, "OnException") as report:
                self.Client._mux.reset_mock()
                self.Client._DoFullStateSync()
                self.Client._mux.ForceReconnect.assert_called_once()
                report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
