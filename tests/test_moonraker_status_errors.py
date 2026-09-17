import logging
import unittest
from typing import Any, Dict, Optional, cast
from unittest.mock import Mock, patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position
from moonraker_octoeverywhere.jsonrpcresponse import JsonRpcResponse  # noqa: E402
from moonraker_octoeverywhere.moonrakercommandhandler import (  # noqa: E402
    FileMetadataCache, LightManager, MoonrakerClient, MoonrakerCommandHandler,
)
from octoeverywhere.commandhandler import CommandHandler  # noqa: E402


class TestMoonrakerStatusErrors(unittest.TestCase):
    def setUp(self) -> None:
        self.Client = Mock()
        self.Client.GetPrinterObjectList.return_value = [
            "print_stats", "gcode_move", "virtual_sdcard", "toolhead", "heater_bed", "extruder", "webhooks",
        ]
        self.Client.IsDisconnectDueToAuth.return_value = False
        compat = self.Client.GetMoonrakerCompat.return_value
        compat.CheckIfPrinterIsWarmingUp_WithPrintStats.return_value = False
        compat.GetCurrentLayerInfo.return_value = (4, 20)
        compat.GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsVirtualSdCardAndGcodeMoveResult.return_value = 600
        lights = Mock()
        lights.GetLightObjectNames.return_value = {}
        lights.GetLightStatus.return_value = []
        metadata = Mock()
        metadata.GetEstimatedFilamentUsageMm.return_value = 1234
        for target, replacement in ((MoonrakerClient, self.Client), (LightManager, lights), (FileMetadataCache, metadata)):
            patcher = patch.object(target, "Get", return_value=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.Handler = MoonrakerCommandHandler(logging.getLogger("TestMoonrakerStatusErrors"), Mock())


    @staticmethod
    def _QueryResponse(printStats:Dict[str, Any], webhooks:Optional[Dict[str, Any]]=None) -> JsonRpcResponse:
        return JsonRpcResponse.FromSuccess({"status": {
            "print_stats": {"filename": "sample.gcode", "print_duration": 120, **printStats},
            "webhooks": {"state": "ready", "state_message": "Printer is ready"} if webhooks is None else webhooks,
            "toolhead": {"extruder": "extruder"},
            "virtual_sdcard": {"progress": 0.25},
            "extruder": {"temperature": 210, "target": 210},
            "heater_bed": {"temperature": 60, "target": 60},
        }})


    def _GetStatus(self) -> Dict[str, Any]:
        status = self.Handler.GetCurrentJobStatus()
        self.assertIsInstance(status, dict)
        return cast(Dict[str, Any], status)


    def _EnableU1(self) -> None:
        self.Client.GetPrinterObjectList.return_value += ["print_task_config", "machine_state_manager"]


    def test_standard_pause_and_error_include_message_without_fabricated_vendor_code(self) -> None:
        for state in ("paused", "error"):
            with self.subTest(state=state):
                self.Client.SendJsonRpcRequest.return_value = self._QueryResponse({"state": state, "message": "Filament sensor triggered"})
                status = self._GetStatus()
                self.assertEqual(status["State"], state)
                self.assertEqual(status["Error"], "Filament sensor triggered")
                self.assertIsNone(status["PlatformErrorCode"])
                self.assertEqual(status["CurrentPrint"]["FileName"], "sample.gcode")
                self.assertEqual(status["CurrentPrint"]["Progress"], 25)
        requestedObjects = self.Client.SendJsonRpcRequest.call_args[0][1]["objects"]
        self.assertIn("print_stats", requestedObjects)
        self.assertIn("webhooks", requestedObjects)


    def test_u1_structured_exception_keeps_clean_reason_and_complete_code(self) -> None:
        self._EnableU1()
        self.Client.SendJsonRpcRequest.return_value = self._QueryResponse({
            "state": "paused",
            "message": '{"coded":"0002-0525-0003-0011","msg":"Raw exception message"}',
            "exception": {"level": 2, "id": 525, "index": 3, "code": 11, "message": "Filament is stuck"},
        })
        status = self._GetStatus()
        self.assertEqual(status["State"], "paused")
        self.assertEqual(status["Error"], "Filament is stuck")
        self.assertEqual(status["PlatformErrorCode"], "0002-0525-0003-0011")


    def test_other_printer_structured_error_is_supported(self) -> None:
        self.Client.SendJsonRpcRequest.return_value = self._QueryResponse({
            "state": "paused", "error": {"error_code": "FS-001", "reason": "Filament sensor triggered"},
        })
        status = self._GetStatus()
        self.assertEqual(status["Error"], "Filament sensor triggered")
        self.assertEqual(status["PlatformErrorCode"], "FS-001")


    def test_empty_pause_reason_does_not_become_an_error(self) -> None:
        self.Client.SendJsonRpcRequest.return_value = self._QueryResponse({"state": "paused", "message": " ", "exception": {}})
        status = self._GetStatus()
        self.assertEqual(status["State"], "paused")
        self.assertIsNone(status["Error"])
        self.assertIsNone(status["PlatformErrorCode"])


    def test_healthy_states_ignore_stale_error_text_and_vendor_code(self) -> None:
        self._EnableU1()
        for printerState, expectedState in (("standby", "idle"), ("printing", "printing"), ("complete", "complete"), ("cancelled", "cancelled")):
            with self.subTest(state=printerState):
                self.Client.SendJsonRpcRequest.return_value = self._QueryResponse({
                    "state": printerState, "message": "Old error",
                    "exception": {"level": 2, "id": 525, "index": 3, "code": 11, "message": "Old exception"},
                })
                status = self._GetStatus()
                self.assertEqual(status["State"], expectedState)
                self.assertIsNone(status["Error"])
                self.assertIsNone(status["PlatformErrorCode"])


    def test_warmup_ignores_stale_error(self) -> None:
        self.Client.GetMoonrakerCompat.return_value.CheckIfPrinterIsWarmingUp_WithPrintStats.return_value = True
        self.Client.SendJsonRpcRequest.return_value = self._QueryResponse({"state": "printing", "message": "Old error"})
        status = self._GetStatus()
        self.assertEqual(status["State"], "warmingup")
        self.assertIsNone(status["Error"])
        self.assertIsNone(status["PlatformErrorCode"])


    def test_webhooks_fault_overrides_stale_printing_state(self) -> None:
        for state in ("error", "shutdown"):
            with self.subTest(state=state):
                self.Client.SendJsonRpcRequest.return_value = self._QueryResponse(
                    {"state": "printing", "message": "Old print error"},
                    {"state": state, "state_message": "MCU shutdown: ADC out of range\nThis generally occurs when a heater exceeds its limit."})
                status = self._GetStatus()
                self.assertEqual(status["State"], "error")
                self.assertEqual(status["Error"], "MCU shutdown: ADC out of range")
                self.assertIsNone(status["PlatformErrorCode"])


    def test_u1_shutdown_json_prefix_is_decoded_before_trailing_text(self) -> None:
        self._EnableU1()
        self.Client.SendJsonRpcRequest.return_value = self._QueryResponse(
            {"state": "paused", "message": "Old pause reason"},
            {"state": "shutdown", "state_message":
             '{"coded":"0003-0522-0000-0002","msg":"Heater is not heating"}\n'
             "MCU 'toolhead' shutdown: Timeout\nOnce the underlying issue is corrected, restart."})
        status = self._GetStatus()
        self.assertEqual(status["State"], "error")
        self.assertEqual(status["Error"], "Heater is not heating")
        self.assertEqual(status["PlatformErrorCode"], "0003-0522-0000-0002")


    def test_recovery_does_not_reuse_previous_shutdown_reason(self) -> None:
        self._EnableU1()
        oldMessage = '{"coded":"0003-0522-0000-0002","msg":"Heater stopped"}'
        self.Client.SendJsonRpcRequest.side_effect = [
            self._QueryResponse({"state": "printing"}, {"state": "shutdown", "state_message": oldMessage}),
            self._QueryResponse({"state": "printing", "message": oldMessage}, {"state": "ready", "state_message": oldMessage}),
        ]
        self.assertEqual(self._GetStatus()["Error"], "Heater stopped")
        recovered = self._GetStatus()
        self.assertEqual(recovered["State"], "printing")
        self.assertIsNone(recovered["Error"])
        self.assertIsNone(recovered["PlatformErrorCode"])


    def test_printer_info_preserves_fault_when_object_queries_are_unavailable(self) -> None:
        for state in ("error", "shutdown"):
            with self.subTest(state=state):
                self.Client.SendJsonRpcRequest.reset_mock()
                self.Client.SendJsonRpcRequest.side_effect = [
                    JsonRpcResponse.FromError(503, "Klippy is not ready"),
                    JsonRpcResponse.FromSuccess({"state": state, "state_message": "Unable to open MCU serial port"}),
                ]
                status = self._GetStatus()
                self.assertEqual(status["State"], "error")
                self.assertEqual(status["Error"], "Unable to open MCU serial port")
                self.assertIsNone(status["PlatformErrorCode"])
                self.assertIsNone(status["CurrentPrint"])
                self.assertEqual([call[0][0] for call in self.Client.SendJsonRpcRequest.call_args_list], ["printer.objects.query", "printer.info"])


    def test_printer_info_decodes_u1_error_without_object_discovery(self) -> None:
        self.Client.GetPrinterObjectList.return_value = None
        self.Client.SendJsonRpcRequest.side_effect = [
            JsonRpcResponse.FromError(503, "Klippy is not ready"),
            JsonRpcResponse.FromSuccess({"state": "shutdown", "state_message":
                '{"coded":"0003-0522-0000-0002","msg":"Heater stopped"}\nMCU shutdown: Timeout'}),
        ]
        status = self._GetStatus()
        self.assertEqual(status["Error"], "Heater stopped")
        self.assertEqual(status["PlatformErrorCode"], "0003-0522-0000-0002")
        self.assertIsNone(status["CurrentPrint"])


    def test_missing_or_healthy_printer_info_does_not_invent_fault(self) -> None:
        for infoResponse in (
            JsonRpcResponse.FromError(503, "Printer unavailable"),
            JsonRpcResponse.FromSuccess({"state": "ready", "state_message": "Old shutdown message"}),
            JsonRpcResponse.FromSuccess({"state": "startup"}),
            JsonRpcResponse.FromSuccess({}),
            JsonRpcResponse.FromSuccess(None),
            JsonRpcResponse.FromSuccess("Unavailable"),
        ):
            with self.subTest(info=infoResponse.Result, error=infoResponse.ErrorCode):
                self.Client.SendJsonRpcRequest.side_effect = [JsonRpcResponse.FromError(503, "Klippy is not ready"), infoResponse]
                self.assertIsNone(self.Handler.GetCurrentJobStatus())


    def test_query_auth_errors_skip_printer_info_and_preserve_lost_auth_state(self) -> None:
        for response in (
            JsonRpcResponse.FromError(JsonRpcResponse.MR_401_UNAUTHORIZED),
            JsonRpcResponse.FromError(401, "Unauthorized"),
            JsonRpcResponse.FromError(403, "Forbidden"),
        ):
            with self.subTest(code=response.ErrorCode):
                self.Client.SendJsonRpcRequest.reset_mock()
                self.Client.SendJsonRpcRequest.return_value = response
                self.assertEqual(self.Handler.GetCurrentJobStatus(), CommandHandler.c_CommandError_LostAuth)
                self.assertEqual(self.Client.SendJsonRpcRequest.call_count, 1)


    def test_printer_info_auth_error_preserves_lost_auth_state(self) -> None:
        for response in (JsonRpcResponse.FromError(JsonRpcResponse.MR_401_UNAUTHORIZED), JsonRpcResponse.FromError(403, "Forbidden")):
            with self.subTest(code=response.ErrorCode):
                self.Client.SendJsonRpcRequest.side_effect = [JsonRpcResponse.FromError(503, "Klippy is not ready"), response]
                self.assertEqual(self.Handler.GetCurrentJobStatus(), CommandHandler.c_CommandError_LostAuth)


    def test_auth_disconnect_is_not_misreported_as_offline(self) -> None:
        self.Client.IsDisconnectDueToAuth.return_value = True
        self.Client.SendJsonRpcRequest.return_value = JsonRpcResponse.FromError(JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED)
        self.assertEqual(self.Handler.GetCurrentJobStatus(), CommandHandler.c_CommandError_LostAuth)
        self.assertEqual(self.Client.SendJsonRpcRequest.call_count, 1)


    def test_disconnected_websocket_skips_printer_info(self) -> None:
        self.Client.SendJsonRpcRequest.return_value = JsonRpcResponse.FromError(JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED)
        self.assertIsNone(self.Handler.GetCurrentJobStatus())
        self.assertEqual(self.Client.SendJsonRpcRequest.call_count, 1)


if __name__ == "__main__":
    unittest.main()
