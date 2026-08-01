import importlib
import logging
import os
import sys
import types
import unittest
from typing import Any, Dict

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()


def _ImportCommandHandler() -> Any:
    # The octoprint_octoeverywhere package __init__ pulls in flask, requests, and OctoPrint's plugin API, none of which
    # are needed by the command handler. Registering the package with just a __path__ lets the submodule import normally
    # without running that __init__, so this test doesn't need stubs for OctoPrint's entire plugin surface.
    packageName = "octoprint_octoeverywhere"
    if packageName not in sys.modules:
        package = types.ModuleType(packageName)
        package.__path__ = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), packageName)]
        sys.modules[packageName] = package
    return importlib.import_module(packageName + ".octoprintcommandhandler").OctoPrintCommandHandler


OctoPrintCommandHandler = _ImportCommandHandler()


class _FakePrinterObject:
    def __init__(self, stateId:str) -> None:
        self._StateId = stateId

    def get_state_id(self) -> str:
        return self._StateId

    def get_current_data(self) -> Dict[str, Any]:
        return {
            "progress": {"completion": 0.0, "printTime": 0},
            "job": {"file": {"display": ""}},
        }

    def get_current_temperatures(self) -> Dict[str, Any]:
        return {}


class _FakePrinterStateObject:
    def GetPrintTimeRemainingEstimateInSeconds(self) -> int:
        return -1

    def IsPrintWarmingUp(self) -> bool:
        return False


class TestOctoPrintJobStatusState(unittest.TestCase):
    def _GetStatus(self, stateId:str) -> Any:
        handler = OctoPrintCommandHandler(
            logging.getLogger("test"),
            _FakePrinterObject(stateId), #pyright: ignore[reportArgumentType]
            _FakePrinterStateObject(), #pyright: ignore[reportArgumentType]
            None, #pyright: ignore[reportArgumentType]
        )
        return handler.GetCurrentJobStatus()

    # When OctoPrint is running but has no serial connection, there is no printer to report on.
    # Returning None is the documented way to signal "printer not connected".
    # These used to fall through to "idle", which made a disconnected printer look ready to print.
    def test_not_connected_states_report_not_connected(self) -> None:
        for stateId in ("OFFLINE", "CLOSED", "OFFLINE_AFTER_ERROR", "DETECT_SERIAL", "DETECT_BAUDRATE", "CONNECTING", "UNKNOWN"):
            self.assertIsNone(self._GetStatus(stateId), f"'{stateId}' should report not connected")

    def test_operational_is_idle(self) -> None:
        status = self._GetStatus("OPERATIONAL")
        self.assertIsInstance(status, dict)
        self.assertEqual(status["State"], "idle")

    def test_printing_is_printing(self) -> None:
        status = self._GetStatus("PRINTING")
        self.assertIsInstance(status, dict)
        self.assertEqual(status["State"], "printing")

    def test_paused_is_paused(self) -> None:
        status = self._GetStatus("PAUSED")
        self.assertIsInstance(status, dict)
        self.assertEqual(status["State"], "paused")

    def test_error_states_are_error(self) -> None:
        for stateId in ("ERROR", "CLOSED_WITH_ERROR"):
            status = self._GetStatus(stateId)
            self.assertIsInstance(status, dict, f"'{stateId}' should return a status dict")
            self.assertEqual(status["State"], "error")


if __name__ == "__main__":
    unittest.main()
