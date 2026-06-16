import logging
import tempfile
import unittest

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

from octoeverywhere.notificationshandler import NotificationsHandler  # noqa: E402
from octoeverywhere.printinfo import PrintInfoManager  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
