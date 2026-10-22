import json
import logging
import threading
import unittest
from unittest.mock import MagicMock, call, patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position,wrong-import-order,protected-access
from elegoo_octoeverywhere.elegooclient import ElegooClient, MsgWaitingContext  # noqa: E402
from elegoo_octoeverywhere.elegoofilemanager import ElegooFileManager  # noqa: E402
from elegoo_octoeverywhere.elegoostatetranslater import ElegooStateTranslator  # noqa: E402
from octoeverywhere.buffer import Buffer  # noqa: E402
from octoeverywhere.interfaces import WebSocketOpCode  # noqa: E402
from octoeverywhere.notificationshandler import NotificationsHandler  # noqa: E402


class TestElegooNotificationRecovery(unittest.TestCase):
    def test_corrupt_file_list_preserves_print_notification_transitions(self) -> None:
        logger = logging.getLogger("TestElegooNotificationRecovery")
        translator = ElegooStateTranslator(logger)
        notifications = MagicMock(spec=NotificationsHandler)
        notifications.IsTrackingPrint.return_value = True
        translator.SetNotificationHandler(notifications)

        # Use the actual message parser, state model, and translator without starting
        # the client's connection worker or sending notifications to the service.
        client = ElegooClient.__new__(ElegooClient)
        client.Logger = logger
        client.StateTranslator = translator
        client.WebsocketMux = MagicMock()
        client.RequestLock = threading.Lock()
        client.RequestPendingContexts = {}
        client._CleanupStateOnDisconnect()
        client.WebSocketConnected = True
        client.WebSocketConnectFinalized = True
        client.LastConnectionFailedDueToTooManyClients = False
        websocket = MagicMock()
        # Model the real close callback so a regression also resets the translator's
        # state and loses transitions, rather than merely recording a mock call.
        websocket.Close.side_effect = lambda: client._OnWsClose(websocket)

        def sendStatus(status:int, duration:int, progress:int) -> None:
            activePrint = status != 0
            message = {
                "Topic": "sdcp/status/printer",
                "Status": {
                    "CurrentStatus": [0],
                    "PrintInfo": {
                        "Status": status,
                        "Filename": "cube.gcode" if activePrint else "",
                        "TaskId": "print-task" if activePrint else "",
                        "CurrentTicks": duration,
                        "TotalTicks": 600,
                        "Progress": progress,
                    },
                },
            }
            client._OnWsData(websocket, Buffer(json.dumps(message).encode("utf-8")), WebSocketOpCode.TEXT)

        requestId = "f" * 32
        fileList = json.dumps({
            "Topic": "sdcp/response/printer",
            "Data": {
                "Cmd": 258,
                "RequestID": requestId,
                "Data": {"FileList": [{"name": "/local/cube.gcode"}, {"name": "/local/BAD_BYTES.gcode"}]},
            },
        }).encode("utf-8").replace(b"BAD_BYTES", b"\x8b\x9d")
        fileManager = MagicMock()
        fileManager.GetFileInfoFromState.return_value = None

        with patch.object(ElegooFileManager, "Get", return_value=fileManager), \
             patch("elegoo_octoeverywhere.elegoostatetranslater.DelayedCallback.Create") as delayedCallback, \
             patch("elegoo_octoeverywhere.elegooclient.Sentry.OnException") as report:
            sendStatus(0, 0, 0)
            # The corrupt entry must not prevent later start, pause, resume, or done
            # messages from being compared against the preceding printer state.
            for status, duration, progress in [(13, 60, 10), (6, 120, 20), (13, 180, 30), (9, 600, 100)]:
                client.RequestPendingContexts[requestId] = MsgWaitingContext(requestId)
                client._OnWsData(websocket, Buffer(fileList), WebSocketOpCode.TEXT)
                sendStatus(status, duration, progress)

            notifications.OnStarted.assert_called_once_with(
                "print-task-cube.gcode", "Cube", fileSizeKBytes=0, totalFilamentWeightMg=0)
            notifications.OnPaused.assert_called_once_with(
                "Cube", platformErrorCode="CurrentStatus=0;PrintInfoStatus=6", error=None)
            notifications.OnResume.assert_called_once_with("Cube")
            notifications.OnDone.assert_called_once_with("Cube", "600")
            notifications.OnRestorePrintIfNeeded.assert_called_once_with(False, False, None)
            notifications.OnPrintProgress.assert_any_call(None, 20.0)
            transitions = [event for event in notifications.method_calls
                           if event[0] in ("OnStarted", "OnPaused", "OnResume", "OnDone")]
            self.assertEqual(transitions, [
                call.OnStarted("print-task-cube.gcode", "Cube", fileSizeKBytes=0, totalFilamentWeightMg=0),
                call.OnPaused("Cube", platformErrorCode="CurrentStatus=0;PrintInfoStatus=6", error=None),
                call.OnResume("Cube"),
                call.OnDone("Cube", "600"),
            ])
            websocket.Close.assert_not_called()
            delayedCallback.assert_not_called()
            report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
