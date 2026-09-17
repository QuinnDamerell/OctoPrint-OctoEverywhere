import logging
import re
import threading
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position
from moonraker_octoeverywhere.jsonrpcresponse import JsonRpcResponse  # noqa: E402
from moonraker_octoeverywhere.moonrakerclient import MoonrakerClient  # noqa: E402
from moonraker_octoeverywhere.moonrakercommandhandler import MoonrakerCommandHandler  # noqa: E402
from octoeverywhere.commandhandler import CommandHandler  # noqa: E402
from octoeverywhere.interfaces import CommandResponse  # noqa: E402


class _ControlPrinter:
    # Model the printer state that makes tool-control regressions dangerous: logical
    # tool remapping, a different active tool, and an existing absolute E mode.
    def __init__(self) -> None:
        self.Objects:Optional[List[str]] = ["toolhead", "gcode", "extruder", "extruder1", "heater_bed",
                                            "print_stats", "virtual_sdcard"]
        self.ActiveTool:Optional[str] = "extruder1"
        self.PrintState:Optional[str] = "standby"
        self.VirtualSdActive = False
        self.ConnectionGeneration:Optional[int] = 1
        self.DisconnectDueToAuth = False
        self.ExpectedGenerations:List[Optional[int]] = []
        self.ReconnectBeforePhase:Optional[str] = None
        self.Commands:Dict[str, Dict[str, Any]] = {"T0": {}, "T1": {}}
        self.SwitchTargets:Dict[str, Optional[str]] = {"T0": "extruder", "T1": "extruder1"}
        self.Calls:List[Tuple[str, Optional[Dict[str, Any]], Optional[float]]] = []
        self.QueryResults:List[JsonRpcResponse] = []
        self.Failures:Dict[str, JsonRpcResponse] = {}
        self.AbsoluteExtrusion = True
        self.Feedrate = 1800.0
        self.SavedState:Optional[Tuple[bool, float]] = None
        self.RestoreCount = 0
        self.Extrusions:List[Tuple[Optional[str], float]] = []
        self.MoveEntered:Optional[threading.Event] = None
        self.ReleaseMove:Optional[threading.Event] = None

    def GetPrinterObjectList(self) -> Optional[List[str]]:
        return self.Objects

    def IsDisconnectDueToAuth(self) -> bool:
        return self.DisconnectDueToAuth

    def GetConnectionGeneration(self) -> Optional[int]:
        return self.ConnectionGeneration

    def Status(self) -> Dict[str, Any]:
        return {
            "toolhead": {"extruder": self.ActiveTool},
            "gcode": {"commands": self.Commands},
            "print_stats": {"state": self.PrintState},
            "virtual_sdcard": {"is_active": self.VirtualSdActive},
        }

    def SendJsonRpcRequest(self, method:str, paramsDict:Optional[Dict[str, Any]]=None,
                           timeoutSec:Optional[float]=None, waitForResponse:bool=True,
                           expectedConnectionGeneration:Optional[int]=None) -> JsonRpcResponse:
        self.Calls.append((method, paramsDict, timeoutSec))
        self.ExpectedGenerations.append(expectedConnectionGeneration)
        script = paramsDict.get("script", "") if paramsDict is not None else ""
        phase = "query" if method == "printer.objects.query" else "select"
        if "G1 E" in script:
            phase = "move"
        elif script.startswith("SAVE_GCODE_STATE"):
            phase = "save"
        elif script.startswith("RESTORE_GCODE_STATE"):
            phase = "cleanup"
        if self.ReconnectBeforePhase == phase:
            self.ConnectionGeneration = (self.ConnectionGeneration or 0) + 1
            self.ReconnectBeforePhase = None
        if expectedConnectionGeneration is not None and expectedConnectionGeneration != self.ConnectionGeneration:
            return JsonRpcResponse.FromError(JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED)
        if method == "printer.objects.query":
            if self.QueryResults:
                return self.QueryResults.pop(0)
            status = self.Status()
            requested = paramsDict.get("objects", {}) if paramsDict is not None else {}
            queriedStatus = {}
            for objectName, fields in requested.items():
                if objectName in status:
                    queriedStatus[objectName] = status[objectName] if fields is None else {
                        field: status[objectName][field] for field in fields if field in status[objectName]
                    }
            return JsonRpcResponse.FromSuccess({"status": queriedStatus})
        if method != "printer.gcode.script" or paramsDict is None:
            raise AssertionError(f"Unexpected RPC: {method} {paramsDict}")
        toolMatch = re.fullmatch(r"(T\d+)( A0)?", script)
        if toolMatch is not None:
            if "select" in self.Failures:
                return self.Failures["select"]
            command = toolMatch.group(1)
            if toolMatch.group(2):
                index = int(command[1:])
                self.ActiveTool = "extruder" if index == 0 else f"extruder{index}"
            else:
                self.ActiveTool = self.SwitchTargets.get(command, self.ActiveTool)
            return JsonRpcResponse.FromSimpleSuccess("ok")
        for line in script.splitlines():
            if line.startswith("SAVE_GCODE_STATE"):
                if "save" in self.Failures:
                    return self.Failures["save"]
                # Klipper retains named states after restoration. A refreshed SAVE
                # inside the movement script deliberately replaces the earlier one.
                self.SavedState = (self.AbsoluteExtrusion, self.Feedrate)
            elif line == "M83":
                self.AbsoluteExtrusion = False
            elif line.startswith("G1 E"):
                self.Feedrate = float(line.split(" F", 1)[1])
                if self.MoveEntered is not None and self.ReleaseMove is not None:
                    self.MoveEntered.set()
                    if not self.ReleaseMove.wait(2.0):
                        raise AssertionError("Timed out waiting to release fake extrusion")
                if "move" in self.Failures:
                    return self.Failures["move"]
                if self.AbsoluteExtrusion:
                    raise AssertionError("Manual extrusion must use relative E mode")
                self.Extrusions.append((self.ActiveTool, float(line.split(" ")[1][1:])))
            elif line.startswith("RESTORE_GCODE_STATE"):
                if "restore" in self.Failures:
                    return self.Failures["restore"]
                if self.SavedState is None:
                    raise AssertionError("Restored G-code state without a successful save")
                self.AbsoluteExtrusion, self.Feedrate = self.SavedState
                self.RestoreCount += 1
            elif line.startswith(("SET_HEATER_TEMPERATURE ", "M104 ", "M140 ")):
                if "heat" in self.Failures:
                    return self.Failures["heat"]
            else:
                raise AssertionError(f"Unexpected G-code: {line}")
        return JsonRpcResponse.FromSimpleSuccess("ok")

    def Scripts(self) -> List[str]:
        return [params["script"] for method, params, _ in self.Calls
                if method == "printer.gcode.script" and params is not None]


class TestMoonrakerToolControls(unittest.TestCase):
    def setUp(self) -> None:
        self.Printer = _ControlPrinter()
        self.Handler = MoonrakerCommandHandler(logging.getLogger("TestMoonrakerToolControls"), None) #pyright: ignore[reportArgumentType]
        clientPatch = patch.object(MoonrakerClient, "Get", return_value=self.Printer)
        clientPatch.start()
        self.addCleanup(clientPatch.stop)

    def _UseU1(self) -> None:
        self.Printer.Objects = ["toolhead", "gcode", "extruder", "extruder1", "extruder2", "extruder3",
                                "print_task_config", "machine_state_manager", "print_stats", "virtual_sdcard"]
        # U1 native Tn commands are not macros and need not advertise help text.
        self.Printer.Commands = {}
        self.Printer.SwitchTargets["T0"] = "extruder2"

    def _AssertNoExtrusion(self, response:CommandResponse) -> None:
        self.assertNotEqual(response.StatusCode, 200)
        self.assertEqual(self.Printer.Extrusions, [])
        self.assertFalse(any("G1 E" in script for script in self.Printer.Scripts()))

    def test_u1_selects_physical_tool_zero_before_extrusion(self) -> None:
        self._UseU1()
        response = self.Handler.ExecuteExtrude(0, 5.0)
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(self.Printer.Extrusions, [("extruder", 5.0)])
        self.assertEqual(self.Printer.Scripts(), [
            "T0 A0",
            "SAVE_GCODE_STATE NAME=OCTOEVERYWHERE_EXTRUDE",
            "SAVE_GCODE_STATE NAME=OCTOEVERYWHERE_EXTRUDE\nM83\nG1 E5.0 F300\nRESTORE_GCODE_STATE NAME=OCTOEVERYWHERE_EXTRUDE",
        ])
        selectCall = next(call for call in self.Printer.Calls if call[1] == {"script": "T0 A0"})
        self.assertEqual(selectCall[2], 120.0)
        self.assertTrue(self.Printer.AbsoluteExtrusion)
        self.assertEqual(self.Printer.Feedrate, 1800.0)
        self.assertEqual(self.Printer.ExpectedGenerations, [1] * len(self.Printer.Calls))
        self.assertEqual(self.Printer.RestoreCount, 1)
        queries = [params["objects"] for method, params, _ in self.Printer.Calls
                   if method == "printer.objects.query" and params is not None]
        self.assertEqual(len(queries), 2)
        for objects in queries:
            self.assertIn("state", objects["print_stats"])
            self.assertIn("is_active", objects["virtual_sdcard"])

    def test_u1_higher_physical_tool_retracts(self) -> None:
        self._UseU1()
        response = self.Handler.ExecuteExtrude(3, -4.0)
        self.assertEqual(response.StatusCode, 200)
        self.assertIn("T3 A0", self.Printer.Scripts())
        self.assertEqual(self.Printer.Extrusions, [("extruder3", -4.0)])

    def test_generic_registered_t0_selects_the_requested_tool(self) -> None:
        response = self.Handler.ExecuteExtrude(0, 2.0)
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(self.Printer.Scripts()[0], "T0")
        self.assertEqual(self.Printer.Extrusions, [("extruder", 2.0)])

    def test_macro_object_without_registered_command_cannot_select_tool(self) -> None:
        self.Printer.Objects = ["toolhead", "gcode", "extruder", "extruder1", "gcode_macro T0"]
        self.Printer.Commands = {}
        response = self.Handler.ExecuteExtrude(0, 2.0)
        self._AssertNoExtrusion(response)
        self.assertEqual(self.Printer.Scripts(), [])

    def test_generic_logical_mapping_cannot_extrude_the_wrong_physical_tool(self) -> None:
        self.Printer.SwitchTargets["T0"] = "extruder1"
        response = self.Handler.ExecuteExtrude(0, 2.0)
        self._AssertNoExtrusion(response)
        self.assertEqual(self.Printer.Scripts(), ["T0"])

    def test_single_tool_printer_needs_no_t0_when_target_is_active(self) -> None:
        self.Printer.Objects = ["toolhead", "gcode", "extruder"]
        self.Printer.Commands = {}
        self.Printer.ActiveTool = "extruder"
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(self.Printer.Extrusions, [("extruder", 1.0)])
        self.assertFalse(any(script.startswith("T") for script in self.Printer.Scripts()))

    def test_single_tool_discovery_does_not_replace_missing_active_status(self) -> None:
        self.Printer.Objects = ["toolhead", "gcode", "extruder"]
        self.Printer.Commands = {}
        self.Printer.ActiveTool = None
        self._AssertNoExtrusion(self.Handler.ExecuteExtrude(0, 1.0))
        self.assertEqual(self.Printer.Scripts(), [])

    def test_other_active_tool_without_selector_is_rejected(self) -> None:
        self.Printer.Commands = {}
        self._AssertNoExtrusion(self.Handler.ExecuteExtrude(0, 1.0))
        self.assertEqual(self.Printer.Scripts(), [])

    def test_active_print_rejects_manual_extrusion_before_tool_selection(self) -> None:
        self._UseU1()
        self.Printer.PrintState = "printing"
        self._AssertNoExtrusion(self.Handler.ExecuteExtrude(0, 1.0))
        self.assertEqual(self.Printer.Scripts(), [])

    def test_active_virtual_sdcard_rejects_extrusion_with_idle_print_stats(self) -> None:
        self.Printer.VirtualSdActive = True
        self._AssertNoExtrusion(self.Handler.ExecuteExtrude(0, 1.0))
        self.assertEqual(self.Printer.Scripts(), [])

    def test_print_that_starts_during_tool_change_prevents_extrusion(self) -> None:
        for state, sdActive in (("printing", False), ("standby", True)):
            with self.subTest(state=state, sdActive=sdActive):
                self.Printer.Calls.clear()
                initialStatus = self.Printer.Status()
                verifiedStatus = self.Printer.Status()
                verifiedStatus["toolhead"]["extruder"] = "extruder"
                verifiedStatus["print_stats"]["state"] = state
                verifiedStatus["virtual_sdcard"]["is_active"] = sdActive
                self.Printer.QueryResults = [JsonRpcResponse.FromSuccess({"status": initialStatus}),
                                             JsonRpcResponse.FromSuccess({"status": verifiedStatus})]
                self._AssertNoExtrusion(self.Handler.ExecuteExtrude(0, 1.0))
                self.assertEqual(self.Printer.Scripts(), ["T0"])

    def test_configs_without_virtual_sdcard_can_extrude_when_print_state_is_unknown(self) -> None:
        self.Printer.Objects = ["toolhead", "gcode", "extruder"]
        self.Printer.Commands = {}
        self.Printer.ActiveTool = "extruder"
        self.Printer.QueryResults = [JsonRpcResponse.FromSuccess({"status": {
            "toolhead": {"extruder": "extruder"}, "gcode": {"commands": {}},
        }})]
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(self.Printer.Extrusions, [("extruder", 1.0)])

    def test_paused_print_can_extrude_when_virtual_sdcard_is_inactive(self) -> None:
        self.Printer.PrintState = "paused"
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(self.Printer.Extrusions, [("extruder", 1.0)])

    def test_sparse_discovery_does_not_renumber_tools(self) -> None:
        self.Printer.Objects = ["toolhead", "gcode", "extruder", "extruder3"]
        response = self.Handler.ExecuteExtrude(1, 1.0)
        self._AssertNoExtrusion(response)
        self.assertEqual(response.StatusCode, 400)
        self.assertEqual(self.Printer.Scripts(), [])

    def test_unknown_discovery_does_not_guess_tool_zero_exists(self) -> None:
        self.Printer.Objects = None
        self.Printer.ActiveTool = "extruder"
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self._AssertNoExtrusion(response)
        self.assertEqual(response.StatusCode, CommandHandler.c_CommandError_ExecutionFailure)
        self.assertEqual(self.Printer.Scripts(), [])

    def test_unknown_discovery_reports_disconnected_printer(self) -> None:
        self.Printer.Objects = None
        self.Printer.ConnectionGeneration = None
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self._AssertNoExtrusion(response)
        self.assertEqual(response.StatusCode, CommandHandler.c_CommandError_HostNotConnected)

    def test_unknown_discovery_preserves_authentication_error(self) -> None:
        self.Printer.Objects = None
        self.Printer.ConnectionGeneration = None
        self.Printer.DisconnectDueToAuth = True
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self._AssertNoExtrusion(response)
        self.assertEqual(response.StatusCode, CommandHandler.c_CommandError_LostAuth)

    def test_query_transport_failure_does_not_move(self) -> None:
        self.Printer.QueryResults = [JsonRpcResponse.FromError(JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED)]
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self._AssertNoExtrusion(response)
        self.assertEqual(response.StatusCode, CommandHandler.c_CommandError_HostNotConnected)

    def test_selection_failure_does_not_save_or_extrude(self) -> None:
        self.Printer.Failures["select"] = JsonRpcResponse.FromError(400, "Tool docking failed")
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self._AssertNoExtrusion(response)
        self.assertIn("Tool docking failed", response.ErrorStr or "")
        self.assertEqual(self.Printer.Scripts(), ["T0"])

    def test_selection_timeout_does_not_continue_motion(self) -> None:
        self.Printer.Failures["select"] = JsonRpcResponse.FromError(JsonRpcResponse.OE_ERROR_TIMEOUT)
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self._AssertNoExtrusion(response)
        self.assertEqual(response.StatusCode, CommandHandler.c_CommandError_ExecutionFailure)
        self.assertEqual(self.Printer.Scripts(), ["T0"])

    def test_verification_transport_failure_does_not_extrude(self) -> None:
        self.Printer.QueryResults = [
            JsonRpcResponse.FromSuccess({"status": {"toolhead": {"extruder": "extruder1"},
                                                    "gcode": {"commands": {"T0": {}}}}}),
            JsonRpcResponse.FromError(JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED),
        ]
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self._AssertNoExtrusion(response)
        self.assertEqual(self.Printer.Scripts(), ["T0"])

    def test_failed_save_never_changes_mode_or_attempts_restore(self) -> None:
        self.Printer.ActiveTool = "extruder"
        self.Printer.Failures["save"] = JsonRpcResponse.FromError(400, "Save failed")
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self._AssertNoExtrusion(response)
        self.assertTrue(self.Printer.AbsoluteExtrusion)
        self.assertFalse(any(script.startswith("RESTORE") for script in self.Printer.Scripts()))

    def test_cold_extrusion_error_restores_mode_and_preserves_original_failure(self) -> None:
        self.Printer.ActiveTool = "extruder"
        self.Printer.Failures["move"] = JsonRpcResponse.FromError(400, "Extrude below minimum temp")
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self.assertNotEqual(response.StatusCode, 200)
        self.assertIn("Extrude below minimum temp", response.ErrorStr or "")
        self.assertEqual(self.Printer.Extrusions, [])
        self.assertTrue(self.Printer.AbsoluteExtrusion)
        self.assertEqual(self.Printer.Feedrate, 1800.0)
        self.assertEqual(self.Printer.RestoreCount, 1)
        self.assertEqual(self.Printer.Scripts()[-1], "RESTORE_GCODE_STATE NAME=OCTOEVERYWHERE_EXTRUDE")

    def test_existing_relative_mode_is_preserved(self) -> None:
        self.Printer.AbsoluteExtrusion = False
        self.Printer.Feedrate = 900.0
        response = self.Handler.ExecuteExtrude(1, -1.0)
        self.assertEqual(response.StatusCode, 200)
        self.assertFalse(self.Printer.AbsoluteExtrusion)
        self.assertEqual(self.Printer.Feedrate, 900.0)

    def test_uncertain_extrusion_transport_error_does_not_replay_cleanup(self) -> None:
        for code in (JsonRpcResponse.OE_ERROR_TIMEOUT, JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED,
                     JsonRpcResponse.OE_ERROR_EXCEPTION):
            with self.subTest(code=code):
                self.Printer.Calls.clear()
                self.Printer.SavedState = None
                self.Printer.Failures["move"] = JsonRpcResponse.FromError(code, "Connection lost during extrusion")
                response = self.Handler.ExecuteExtrude(1, 1.0)
                self.assertNotEqual(response.StatusCode, 200)
                self.assertFalse(any(script.startswith("RESTORE") for script in self.Printer.Scripts()))

    def test_reconnect_between_save_and_movement_cannot_move_new_connection(self) -> None:
        self.Printer.ReconnectBeforePhase = "move"
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self.assertEqual(response.StatusCode, CommandHandler.c_CommandError_HostNotConnected)
        self.assertEqual(self.Printer.Extrusions, [])
        self.assertTrue(self.Printer.AbsoluteExtrusion)
        self.assertEqual(self.Printer.RestoreCount, 0)
        self.assertEqual(self.Printer.ConnectionGeneration, 2)
        self.assertEqual(self.Printer.ExpectedGenerations, [1] * len(self.Printer.Calls))
        self.assertFalse(any(script.startswith("RESTORE") for script in self.Printer.Scripts()))

    def test_reconnect_before_error_cleanup_cannot_restore_state_on_new_connection(self) -> None:
        self.Printer.Failures["move"] = JsonRpcResponse.FromError(400, "Extrude below minimum temp")
        self.Printer.ReconnectBeforePhase = "cleanup"
        response = self.Handler.ExecuteExtrude(0, 1.0)
        self.assertNotEqual(response.StatusCode, 200)
        self.assertIn("Extrude below minimum temp", response.ErrorStr or "")
        self.assertEqual(self.Printer.Extrusions, [])
        self.assertEqual(self.Printer.RestoreCount, 0)
        self.assertEqual(self.Printer.ConnectionGeneration, 2)
        self.assertEqual(self.Printer.ExpectedGenerations, [1] * len(self.Printer.Calls))

    def test_failed_move_remains_primary_error_when_restore_also_fails(self) -> None:
        self.Printer.Failures["move"] = JsonRpcResponse.FromError(400, "Extrude below minimum temp")
        self.Printer.Failures["restore"] = JsonRpcResponse.FromError(400, "Restore failed")
        response = self.Handler.ExecuteExtrude(1, 1.0)
        self.assertNotEqual(response.StatusCode, 200)
        self.assertIn("Extrude below minimum temp", response.ErrorStr or "")
        self.assertIn("may have", response.ErrorStr or "")
        self.assertEqual(self.Printer.Scripts()[-1], "RESTORE_GCODE_STATE NAME=OCTOEVERYWHERE_EXTRUDE")

    def test_restore_failure_reports_error_after_completed_extrusion(self) -> None:
        self.Printer.Failures["restore"] = JsonRpcResponse.FromError(400, "Restore failed")
        response = self.Handler.ExecuteExtrude(1, 1.0)
        self.assertNotEqual(response.StatusCode, 200)
        self.assertIn("Restore failed", response.ErrorStr or "")
        self.assertIn("may have", response.ErrorStr or "")
        self.assertEqual(self.Printer.Extrusions, [("extruder1", 1.0)])

    def test_concurrent_extrusions_do_not_interleave_saved_state(self) -> None:
        self.Printer.ActiveTool = "extruder"
        self.Printer.MoveEntered = threading.Event()
        self.Printer.ReleaseMove = threading.Event()
        secondStarted = threading.Event()
        responses:List[CommandResponse] = []
        errors:List[Exception] = []

        def extrude(second:bool) -> None:
            try:
                if second:
                    secondStarted.set()
                responses.append(self.Handler.ExecuteExtrude(0, 1.0))
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=extrude, args=(False,))
        second = threading.Thread(target=extrude, args=(True,))
        first.start()
        try:
            self.assertTrue(self.Printer.MoveEntered.wait(1.0))
            callsBeforeSecond = len(self.Printer.Calls)
            second.start()
            self.assertTrue(secondStarted.wait(1.0))
            second.join(0.05)
            self.assertEqual(len(self.Printer.Calls), callsBeforeSecond)
        finally:
            self.Printer.ReleaseMove.set()
            first.join(2.0)
            if second.ident is not None:
                second.join(2.0)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([response.StatusCode for response in responses], [200, 200])
        self.assertTrue(self.Printer.AbsoluteExtrusion)

    def test_explicit_temperature_targets_physical_heater_on_generic_and_u1(self) -> None:
        for isU1 in (False, True):
            with self.subTest(isU1=isU1):
                if isU1:
                    self._UseU1()
                self.Printer.Calls.clear()
                response = self.Handler.ExecuteSetTemp(None, None, 220.0, 0)
                self.assertEqual(response.StatusCode, 200)
                self.assertEqual(self.Printer.Scripts(), ["SET_HEATER_TEMPERATURE HEATER=extruder TARGET=220.0"])

    def test_sparse_temperature_target_uses_actual_number(self) -> None:
        self.Printer.Objects = ["toolhead", "gcode", "extruder", "extruder3"]
        response = self.Handler.ExecuteSetTemp(None, None, 210.0, 3)
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(self.Printer.Scripts(), ["SET_HEATER_TEMPERATURE HEATER=extruder3 TARGET=210.0"])

    def test_missing_temperature_target_rejects_entire_request_before_heating(self) -> None:
        self.Printer.Objects = ["toolhead", "extruder", "extruder3", "heater_bed"]
        response = self.Handler.ExecuteSetTemp(60.0, None, 210.0, 1)
        self.assertEqual(response.StatusCode, 400)
        self.assertEqual(self.Printer.Scripts(), [])

    def test_unknown_discovery_rejects_explicit_heater(self) -> None:
        self.Printer.Objects = None
        response = self.Handler.ExecuteSetTemp(None, None, 210.0, 0)
        self.assertEqual(response.StatusCode, CommandHandler.c_CommandError_ExecutionFailure)
        self.assertEqual(self.Printer.Scripts(), [])

    def test_unknown_heater_discovery_reports_disconnect_and_auth_separately(self) -> None:
        self.Printer.Objects = None
        self.Printer.ConnectionGeneration = None
        for authFailure, expectedCode in ((False, CommandHandler.c_CommandError_HostNotConnected),
                                          (True, CommandHandler.c_CommandError_LostAuth)):
            with self.subTest(authFailure=authFailure):
                self.Printer.DisconnectDueToAuth = authFailure
                response = self.Handler.ExecuteSetTemp(None, None, 210.0, 0)
                self.assertEqual(response.StatusCode, expectedCode)
                self.assertEqual(self.Printer.Scripts(), [])

    def test_omitted_tool_keeps_printer_active_tool_semantics(self) -> None:
        self._UseU1()
        self.Printer.Objects = None
        response = self.Handler.ExecuteSetTemp(None, None, 210.0, None)
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(self.Printer.Scripts(), ["M104 S210.0"])

    def test_zero_temperature_is_sent_to_physical_heater(self) -> None:
        response = self.Handler.ExecuteSetTemp(None, None, 0.0, 1)
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(self.Printer.Scripts(), ["SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=0.0"])

    def test_heater_rpc_failure_reaches_caller(self) -> None:
        self.Printer.Failures["heat"] = JsonRpcResponse.FromError(400, "Requested temperature out of range")
        response = self.Handler.ExecuteSetTemp(None, None, 210.0, 0)
        self.assertEqual(response.StatusCode, CommandHandler.c_CommandError_ExecutionFailure)
        self.assertIn("Requested temperature out of range", response.ErrorStr or "")


if __name__ == "__main__":
    unittest.main()
