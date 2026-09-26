import logging
import unittest
from unittest.mock import Mock, patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

from bambu_octoeverywhere.bambumodels import BambuPrintErrors, BambuPrinters, BambuState, BambuVersion  # noqa: E402
from bambu_octoeverywhere.bambustatetranslater import BambuStateTranslator  # noqa: E402
from octoeverywhere.printinfo import PrintInfoManager  # noqa: E402
from octoeverywhere.sentry import Sentry  # noqa: E402


class TestBambuModels(unittest.TestCase):
    def test_hex_error_codes_are_uppercase_for_lookup_and_notifications(self) -> None:
        state = BambuState()
        state.print_error = 0x07FF8011

        self.assertEqual(state.GetPrinterErrorType(), BambuPrintErrors.FilamentRunOut)
        self.assertNotEqual(state.GetDetailedPrinterErrorStr(), "Error")

        translator = BambuStateTranslator(logging.getLogger("TestBambuModels"))
        self.assertEqual(translator._GetBambuPlatformErrorCode(state), "07FF8011")

    def test_print_ids_accept_numbers_and_preserve_zero(self) -> None:
        for projectId, taskId in ((0, 123), ("0", "123")):
            state = BambuState()
            state.OnUpdate({"project_id": projectId, "task_id": taskId, "subtask_name": "part.3mf"})
            self.assertEqual(state.GetPrintCookie(), "0-123-part")
            state.OnUpdate({"mc_percent": 20})
            self.assertEqual(state.GetPrintCookie(), "0-123-part")

    def test_missing_or_invalid_print_ids_wait_for_metadata(self) -> None:
        for value in (None, "", False, [], {}):
            state = BambuState()
            state.OnUpdate({"project_id": value, "task_id": 123, "subtask_name": "part.3mf"})
            self.assertIsNone(state.GetPrintCookie())

    def test_partial_version_messages_dont_report_unknown_models(self) -> None:
        version = BambuVersion(logging.getLogger("TestBambuModels"))
        with patch.object(Sentry, "LogInfo") as report:
            version.OnUpdate({"module": []})
            version.OnUpdate({"module": [{"name": "esp32", "hw_ver": "AP04"}]})
            report.assert_not_called()
            version.OnUpdate({"module": [{"name": "esp32", "project_name": "C12"}]})
            self.assertEqual(version.PrinterName, BambuPrinters.P1S)

    def test_known_model_survives_partial_updates(self) -> None:
        version = BambuVersion(logging.getLogger("TestBambuModels"))
        version.OnUpdate({"module": [{"product_name": "Bambu Lab H2D"}]})
        with patch.object(Sentry, "LogInfo") as report:
            version.OnUpdate({"module": [{"name": "ap", "hw_ver": "AP03"}]})
            self.assertEqual(version.PrinterName, BambuPrinters.H2D)
            report.assert_not_called()

    def test_complete_unknown_model_is_reported_once(self) -> None:
        version = BambuVersion(logging.getLogger("TestBambuModels"))
        with patch.object(Sentry, "LogInfo") as report:
            for _ in range(3):
                version.OnUpdate({"module": [{"name": "ap", "hw_ver": "AP03"}]})
            self.assertEqual(version.PrinterName, BambuPrinters.Unknown)
            report.assert_called_once()


class TestBambuDeferredPrintStart(unittest.TestCase):
    def setUp(self) -> None:
        self.Translator = BambuStateTranslator(logging.getLogger("TestBambuDeferredPrintStart"))
        self.Notifications = Mock()
        self.Notifications.IsTrackingPrint.return_value = False
        self.Translator.SetNotificationHandler(self.Notifications)
        self.State = BambuState()
        self.PrintInfo = Mock()
        self.PrintInfo.GetPrintInfo.return_value = None
        managerPatch = patch.object(PrintInfoManager, "Get", return_value=self.PrintInfo)
        managerPatch.start()
        self.addCleanup(managerPatch.stop)

    def _Update(self, update, firstSync=False) -> None:
        self.State.OnUpdate(update)
        self.Translator.OnMqttMessage({"print": update}, self.State, firstSync)

    def _SetPrintIds(self) -> None:
        self._Update({"project_id": 0, "task_id": 123, "subtask_name": "part.3mf"})

    def test_start_waits_for_ids_without_another_state_change(self) -> None:
        self._Update({"gcode_state": "IDLE"}, True)
        self._Update({"gcode_state": "RUNNING"})
        self.Notifications.OnStarted.assert_not_called()
        self._SetPrintIds()
        self._Update({"mc_percent": 10})
        self.Notifications.OnStarted.assert_called_once_with("0-123-part", "part")

    def test_initial_sync_restores_when_ids_arrive_later(self) -> None:
        self._Update({"gcode_state": "RUNNING"}, True)
        self.Notifications.OnRestorePrintIfNeeded.assert_not_called()
        self._SetPrintIds()
        self.Notifications.OnRestorePrintIfNeeded.assert_called_once_with(True, False, "0-123-part")
        self.Notifications.OnStarted.assert_not_called()

    def test_delayed_full_sync_after_partial_idle_does_not_start_a_new_print(self) -> None:
        self._Update({"gcode_state": "IDLE"})
        self._Update({"gcode_state": "RUNNING"}, True)
        self._SetPrintIds()
        self.Notifications.OnRestorePrintIfNeeded.assert_called_once_with(True, False, "0-123-part")
        self.Notifications.OnStarted.assert_not_called()

    def test_print_ending_before_metadata_does_not_start_later(self) -> None:
        self._Update({"gcode_state": "IDLE"}, True)
        self._Update({"gcode_state": "RUNNING"})
        self._Update({"gcode_state": "FINISH"})
        self._SetPrintIds()
        self.Notifications.OnStarted.assert_not_called()
        self.Notifications.OnDone.assert_not_called()

    def test_print_pausing_before_metadata_gets_context_before_pause(self) -> None:
        self._Update({"gcode_state": "IDLE"}, True)
        self._Update({"gcode_state": "RUNNING"})
        self._Update({"gcode_state": "PAUSE"})
        self._SetPrintIds()
        calls = [call[0] for call in self.Notifications.method_calls]
        self.assertLess(calls.index("OnStarted"), calls.index("OnPaused"))
        self.Notifications.OnStarted.assert_called_once()

    def test_print_without_ids_is_tracked_after_the_wait_timeout(self) -> None:
        clock = [100.0]
        with patch("bambu_octoeverywhere.bambustatetranslater.time.monotonic", side_effect=lambda: clock[0]):
            self._Update({"gcode_state": "IDLE"}, True)
            self._Update({"gcode_state": "RUNNING", "subtask_name": "part.3mf"})
            clock[0] += BambuStateTranslator.c_MaxPrintInfoWaitSec - 1
            self._Update({"mc_percent": 5})
            self.Notifications.OnStarted.assert_not_called()

            # Once the wait is over, the print is tracked with the info we have.
            clock[0] += 2
            self._Update({"mc_percent": 6})
            self.Notifications.OnStarted.assert_called_once_with("none-none-part", "part")

            # The cookie stays the same for the rest of the print, even if the ids arrive later.
            self._SetPrintIds()
            self.assertEqual(self.State.GetPrintCookie(), "none-none-part")
            self._Update({"gcode_state": "FINISH"})
            self.Notifications.OnDone.assert_called_once()
            self.assertEqual(self.State.GetPrintCookie(), "0-123-part")


    def test_paused_initial_sync_restores_without_start_notification(self) -> None:
        self._Update({"gcode_state": "PAUSE"}, True)
        self._SetPrintIds()
        self.Notifications.OnRestorePrintIfNeeded.assert_called_once_with(False, True, "0-123-part")
        self.Notifications.OnStarted.assert_not_called()


if __name__ == "__main__":
    unittest.main()
