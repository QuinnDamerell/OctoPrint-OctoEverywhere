import logging
import unittest
from typing import Any, Dict, List, Optional, Tuple

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position
from octoeverywhere.commandhandler import CommandHandler  # noqa: E402
from octoeverywhere.interfaces import CommandResponse  # noqa: E402


class _RecordingPlatformCommandHandler:
    # Records what SetTemp forwards, so the tests can tell "reached the printer" from "rejected by validation".
    def __init__(self) -> None:
        self.Calls:List[Tuple[Optional[float], Optional[float], Optional[float], Optional[int]]] = []

    def ExecuteSetTemp(self, bedC:Optional[float], chamberC:Optional[float], toolC:Optional[float], toolNumber:Optional[int]) -> CommandResponse:
        self.Calls.append((bedC, chamberC, toolC, toolNumber))
        return CommandResponse.Success(None)


class TestSetTempArgs(unittest.TestCase):
    def setUp(self) -> None:
        self.Platform = _RecordingPlatformCommandHandler()
        self.Handler = CommandHandler(logging.getLogger("test"), None, self.Platform, None) #pyright: ignore[reportArgumentType]

    def _SetTemp(self, args:Optional[Dict[str, Any]]) -> CommandResponse:
        return self.Handler.SetTemp(args)

    # A target of 0 is how a heater is turned off. It must reach the platform handler, not be read as "not specified".
    def test_zero_turns_a_heater_off(self) -> None:
        for key, expectedIndex in (("BedC", 0), ("ChamberC", 1), ("ToolC", 2)):
            platform = _RecordingPlatformCommandHandler()
            handler = CommandHandler(logging.getLogger("test"), None, platform, None) #pyright: ignore[reportArgumentType]

            response = handler.SetTemp({key: 0})

            self.assertEqual(response.StatusCode, 200, f"{key}=0 should be accepted")
            self.assertEqual(len(platform.Calls), 1, f"{key}=0 should reach the platform handler")
            self.assertEqual(platform.Calls[0][expectedIndex], 0)

    def test_all_zero_is_accepted(self) -> None:
        response = self._SetTemp({"BedC": 0, "ChamberC": 0, "ToolC": 0})
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(self.Platform.Calls, [(0, 0, 0, None)])

    def test_no_heater_is_rejected(self) -> None:
        response = self._SetTemp({})
        self.assertEqual(response.StatusCode, 400)
        self.assertEqual(len(self.Platform.Calls), 0)

    def test_normal_target_is_forwarded(self) -> None:
        response = self._SetTemp({"BedC": 60, "ToolC": 200, "ToolNumber": 1})
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(self.Platform.Calls, [(60, None, 200, 1)])

    def test_over_max_is_rejected(self) -> None:
        for args in ({"BedC": 76}, {"ChamberC": 76}, {"ToolC": 261}):
            response = self._SetTemp(args)
            self.assertEqual(response.StatusCode, 400, f"{args} should be rejected")
        self.assertEqual(len(self.Platform.Calls), 0)

    # Negative targets are never valid and previously passed straight through to the printer.
    def test_negative_is_rejected(self) -> None:
        for args in ({"BedC": -1}, {"ChamberC": -1}, {"ToolC": -1}, {"ToolNumber": -1, "ToolC": 200}):
            response = self._SetTemp(args)
            self.assertEqual(response.StatusCode, 400, f"{args} should be rejected")
        self.assertEqual(len(self.Platform.Calls), 0)

    # bool is a subclass of int in python, so True would otherwise be accepted as a temp of 1.
    def test_bool_is_rejected(self) -> None:
        for args in ({"BedC": True}, {"ToolC": False}, {"ToolC": 200, "ToolNumber": True}):
            response = self._SetTemp(args)
            self.assertEqual(response.StatusCode, 400, f"{args} should be rejected")
        self.assertEqual(len(self.Platform.Calls), 0)

    def test_non_numeric_is_rejected(self) -> None:
        response = self._SetTemp({"BedC": "hot"})
        self.assertEqual(response.StatusCode, 400)
        self.assertEqual(len(self.Platform.Calls), 0)

    def test_no_args_is_rejected(self) -> None:
        response = self._SetTemp(None)
        self.assertEqual(response.StatusCode, 400)
        self.assertEqual(len(self.Platform.Calls), 0)


if __name__ == "__main__":
    unittest.main()
