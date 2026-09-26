import logging
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

from octoeverywhere.notificationshandler import NotificationsHandler  # noqa: E402
from octoeverywhere.printinfo import PrintInfoManager  # noqa: E402
from octoeverywhere.buffer import Buffer  # noqa: E402
from octoeverywhere.httpresult import HttpResult  # noqa: E402


class FakeImage:
    width = 1200
    height = 1200

    def resize(self, size):
        self.width, self.height = size
        return self


    def transpose(self, operation):
        return self


    def save(self, buffer, **kwargs):
        buffer.write(b"processed")


    def close(self):
        pass


class FakePrinterState:
    def GetPrintTimeRemainingEstimateInSeconds(self) -> int:
        return 123


    def GetCurrentLayerInfo(self):
        return (2, 10)


    def GetCurrentZOffsetMm(self) -> int:
        return -1


    def ShouldPrintingTimersBeRunning(self) -> bool:
        return False


class FakeBedCooldownWatcher:
    def Start(self) -> None:
        return None


    def Stop(self) -> None:
        return None


class TestNotificationsHandler(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("TestNotificationsHandler")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        PrintInfoManager.Init(self.logger, self.tmp.name)


    def _MakeHandler(self) -> NotificationsHandler:
        handler = NotificationsHandler(self.logger, FakePrinterState())
        handler.BedCooldownWatcher = FakeBedCooldownWatcher()
        handler.StopTimers = lambda: None
        handler.GetNotificationSnapshot = lambda *args, **kwargs: None
        return handler


    def test_notification_methods_include_error_info(self) -> None:
        handler = self._MakeHandler()
        sent = []
        handler._updateCurrentFileName = lambda fileName: None
        handler._sendEvent = lambda event, args=None, progressOverwriteFloat=None, useFinalSnapSnapshot=False: sent.append((event, args or {})) or True

        handler.OnPaused("file.gcode", platformErrorCode=2502, error="Paused by printer")
        handler.OnError("Printer error", platformErrorCode="machine_status=14")
        handler.OnFailed("file.gcode", None, "cancelled", platformErrorCode="STOPPED", error="Stopped")
        handler.OnFilamentChange(platformErrorCode="07008011", error="Filament run out")
        handler._clearSpammyEventContexts()
        handler.OnUserInteractionNeeded(platformErrorCode="action:paused", error="Paused for user")

        self.assertEqual(sent[0], ("paused", {
            "Error": "Paused by printer",
            "PlatformErrorCode": "2502",
        }))
        self.assertEqual(sent[1], ("error", {
            "Error": "Printer error",
            "PlatformErrorCode": "machine_status=14",
        }))
        self.assertEqual(sent[2], ("failed", {
            "Reason": "cancelled",
            "Error": "Stopped",
            "PlatformErrorCode": "STOPPED",
        }))
        self.assertEqual(sent[3], ("filamentchange", {
            "Error": "Filament run out",
            "PlatformErrorCode": "07008011",
        }))
        self.assertEqual(sent[4], ("userinteractionneeded", {
            "Error": "Paused for user",
            "PlatformErrorCode": "action:paused",
        }))


    def test_notification_methods_omit_error_when_no_platform_message(self) -> None:
        handler = self._MakeHandler()
        sent = []
        handler._updateCurrentFileName = lambda fileName: None
        handler._sendEvent = lambda event, args=None, progressOverwriteFloat=None, useFinalSnapSnapshot=False: sent.append((event, args or {})) or True

        handler.OnPaused("file.gcode", platformErrorCode=2502)
        handler.OnFailed("file.gcode", None, "cancelled", platformErrorCode="STOPPED")
        handler.OnError(None, platformErrorCode="machine_status=14")

        self.assertEqual(sent[0], ("paused", {
            "PlatformErrorCode": "2502",
        }))
        self.assertEqual(sent[1], ("failed", {
            "Reason": "cancelled",
            "PlatformErrorCode": "STOPPED",
        }))
        self.assertEqual(sent[2], ("error", {
            "PlatformErrorCode": "machine_status=14",
        }))


    def test_common_event_args_preserve_error_info_in_rest_body(self) -> None:
        handler = self._MakeHandler()
        handler.SetPrinterId("printer-1")
        handler.SetOctoKey("octo-key")

        args, files = handler.BuildCommonEventArgs("error", {
            "Error": "Printer error",
            "PlatformErrorCode": "07008011",
        })

        self.assertIsNotNone(args)
        self.assertIsNotNone(files)
        self.assertEqual(args["PrinterId"], "printer-1")
        self.assertEqual(args["OctoKey"], "octo-key")
        self.assertEqual(args["Event"], "error")
        self.assertEqual(args["Error"], "Printer error")
        self.assertEqual(args["PlatformErrorCode"], "07008011")


    def test_resume_recovers_missing_context_before_notification(self) -> None:
        handler = self._MakeHandler()
        handler.PrintCookie = "active-print"
        handler.StartPrintTimers = Mock()
        sent = []
        handler._sendEvent = lambda *args, **kwargs: sent.append(handler.GetPrintId())

        handler.OnResume("file.gcode")

        self.assertEqual(len(sent), 1)
        self.assertIsNotNone(sent[0])
        self.assertEqual(handler.GetPrintInfo().GetFileName(), "file.gcode")
        self.assertTrue(handler.HasSendFirstLayerDoneMessage)
        self.assertTrue(handler.RestorePrintProgressPercentage)
        # The restore starts the timers with the restored print time, so the resume shouldn't restart them.
        handler.StartPrintTimers.assert_called_once()


    def test_resume_preserves_existing_print_identity(self) -> None:
        handler = self._MakeHandler()
        handler.PrintCookie = "active-print"
        PrintInfoManager.Get().CreateNewPrintInfo(handler.PrintCookie, "original-id")
        handler.StartPrintTimers = Mock()
        handler._sendEvent = Mock()
        handler.OnResume("file.gcode")
        self.assertEqual(handler.GetPrintId(), "original-id")


    def test_restore_without_cookie_does_not_start_timers(self) -> None:
        handler = self._MakeHandler()
        handler.StartPrintTimers = Mock()
        handler._sendEvent = Mock()
        handler.OnRestorePrintIfNeeded(True, False, "")
        handler.StartPrintTimers.assert_not_called()
        handler._sendEvent.assert_not_called()
        self.assertIsNone(handler.GetPrintInfo())


    def test_resume_without_cookie_still_notifies_and_starts_timers(self) -> None:
        # OctoPrint can connect to a printer that's already printing, so there's no print cookie.
        # The resume should still be sent and Gadget should still watch the rest of the print.
        handler = self._MakeHandler()
        handler.StartPrintTimers = Mock()
        handler._sendEvent = Mock()
        handler.OnResume("file.gcode")
        handler._sendEvent.assert_called_once_with("resume")
        handler.StartPrintTimers.assert_called_once_with(False, None)
        self.assertIsNone(handler.GetPrintInfo())


    def _GetSnapshot(self, handler, imageModule, flipH=False, data=b"original"):
        webcam = Mock()
        webcam.GetWebcamFlipH.return_value = flipH
        webcam.GetWebcamFlipV.return_value = False
        webcam.GetWebcamRotation.return_value = 0
        webcam.GetSnapshot.return_value = HttpResult(200, {}, "http://camera/snapshot", False, fullBodyBuffer=Buffer(data))
        with patch("octoeverywhere.notificationshandler.WebcamHelper.Get", return_value=webcam), \
             patch("octoeverywhere.notificationshandler.Image", imageModule), \
             patch("octoeverywhere.notificationshandler.Sentry.OnException") as report:
            snapshot = NotificationsHandler.GetNotificationSnapshot(handler)
            report.assert_not_called()
            return snapshot


    def test_resize_does_not_require_unused_flip_apis(self) -> None:
        imageModule = types.SimpleNamespace(open=lambda data: FakeImage())
        self.assertEqual(self._GetSnapshot(self._MakeHandler(), imageModule).Get(), b"processed")


    def test_missing_flip_api_preserves_original_snapshot_and_warns_once(self) -> None:
        handler = self._MakeHandler()
        imageModule = types.SimpleNamespace(open=lambda data: FakeImage())
        with patch.object(handler.Logger, "warning") as warning:
            self.assertEqual(self._GetSnapshot(handler, imageModule, True).Get(), b"original")
            self.assertEqual(self._GetSnapshot(handler, imageModule, True).Get(), b"original")
            warning.assert_called_once()


    def test_both_pillow_flip_namespaces_are_supported(self) -> None:
        for modern in (False, True):
            with self.subTest(modern=modern):
                constants = types.SimpleNamespace(FLIP_LEFT_RIGHT=0, FLIP_TOP_BOTTOM=1)
                imageModule = types.SimpleNamespace(open=lambda data: FakeImage())
                if modern:
                    imageModule.Transpose = constants
                else:
                    imageModule.FLIP_LEFT_RIGHT = constants.FLIP_LEFT_RIGHT
                    imageModule.FLIP_TOP_BOTTOM = constants.FLIP_TOP_BOTTOM
                self.assertEqual(self._GetSnapshot(self._MakeHandler(), imageModule, True).Get(), b"processed")


    def test_optional_image_fallback_keeps_snapshot_size_limit(self) -> None:
        handler = self._MakeHandler()
        with patch.object(NotificationsHandler, "MaxSnapshotFileSizeBytes", 4):
            self.assertIsNone(self._GetSnapshot(handler, types.SimpleNamespace(), True))


    def test_missing_image_import_preserves_original_snapshot(self) -> None:
        self.assertEqual(self._GetSnapshot(self._MakeHandler(), None).Get(), b"original")


if __name__ == "__main__":
    unittest.main()
