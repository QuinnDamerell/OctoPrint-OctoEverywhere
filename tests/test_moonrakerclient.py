import logging
import queue
import threading
import unittest

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position,protected-access
from moonraker_octoeverywhere.moonrakerclient import MoonrakerClient, MoonrakerCompat  # noqa: E402
from octoeverywhere.buffer import Buffer  # noqa: E402


class FakeMoonrakerCompatEvents:
    def __init__(self) -> None:
        self.Paused = []
        self.Errors = []
        self.Disconnects = []


    def OnPrintPaused(self, printStats=None, supplementalMessage=None) -> None:
        self.Paused.append((printStats, supplementalMessage))


    def OnPrintError(self, printStats=None) -> None:
        self.Errors.append(printStats)


    def OnPrintProgress(self, progress) -> None:
        return None


    def KlippyDisconnectedOrShutdown(self, platformErrorCode=None, error=None) -> None:
        self.Disconnects.append((platformErrorCode, error))


class FakeNotificationHandler:
    def __init__(self) -> None:
        self.Paused = []
        self.Errors = []


    def OnPaused(self, fileName=None, platformErrorCode=None, error=None) -> None:
        self.Paused.append((fileName, platformErrorCode, error))


    def OnError(self, error, platformErrorCode=None) -> None:
        self.Errors.append((error, platformErrorCode))


class TestMoonrakerClient(unittest.TestCase):
    def test_status_updates_route_pause_and_error_states(self) -> None:
        compat = FakeMoonrakerCompatEvents()
        client = MoonrakerClient.__new__(MoonrakerClient)
        client.MoonrakerCompat = compat

        pausedStats = {"state": "paused", "exception": {"message": "Filament stuck"}}
        client._OnWsNonResponseMessage({
            "method": "notify_status_update",
            "params": [
                {"print_stats": pausedStats},
                {"display_status": {"message": "Check filament path"}},
            ],
        })
        errorStats = {"state": "error", "message": "Printer stopped"}
        client._OnWsNonResponseMessage({
            "method": "notify_status_update",
            "params": [{"print_stats": errorStats}],
        })

        self.assertEqual(compat.Paused, [(pausedStats, "Check filament path")])
        self.assertEqual(compat.Errors, [errorStats])


    def test_shutdown_uses_cached_webhooks_state_message(self) -> None:
        compat = FakeMoonrakerCompatEvents()
        client = MoonrakerClient.__new__(MoonrakerClient)
        client.Logger = logging.getLogger("TestMoonrakerClient")
        client.MoonrakerCompat = compat
        client.LastWebhooksState = None
        client.LastWebhooksStateMessage = None
        client.JsonRpcIdLock = threading.Lock()
        client.JsonRpcWaitingContexts = {}
        client.NonResponseMsgQueue = queue.Queue()
        client.WebSocketDebugProfiler = None
        client._RestartWebsocket = lambda: None

        client._onWsData(None, Buffer(b'''{
            "jsonrpc": "2.0",
            "method": "notify_status_update",
            "params": [{
                "webhooks": {
                    "state": "shutdown",
                    "state_message": "MCU 'mcu' shutdown: Timer too close\\nOnce the underlying issue is corrected, restart."
                }
            }]
        }'''), None)
        client._onWsData(None, Buffer(b'''{
            "jsonrpc": "2.0",
            "method": "notify_klippy_shutdown"
        }'''), None)

        self.assertEqual(compat.Disconnects, [
            ("klippy_shutdown", "MCU 'mcu' shutdown: Timer too close"),
        ])


    def test_pause_and_error_use_full_u1_exception_details(self) -> None:
        fullStats = {
            "filename": "file.gcode",
            "state": "paused",
            "total_duration": 10.0,
            "print_duration": 9.0,
            "message": "",
            "exception": {
                "level": 2,
                "id": 525,
                "index": 1,
                "code": 7,
                "message": "Filament feed blocked",
            },
        }
        handler = FakeNotificationHandler()
        compat = MoonrakerCompat.__new__(MoonrakerCompat)
        compat.Logger = logging.getLogger("TestMoonrakerClient")
        compat.IsReadyToProcessNotifications = True
        compat.NotificationHandler = handler
        compat._GetCurrentPrintStats = lambda: fullStats

        compat.OnPrintPaused({"state": "paused"})
        fullStats["state"] = "error"
        fullStats["exception"]["level"] = 3
        compat.OnPrintError({"state": "error"})

        self.assertEqual(handler.Paused, [
            ("file.gcode", "0002-0525-0001-0007", "Filament feed blocked"),
        ])
        self.assertEqual(handler.Errors, [
            ("Filament feed blocked", "0003-0525-0001-0007"),
        ])


if __name__ == "__main__":
    unittest.main()
