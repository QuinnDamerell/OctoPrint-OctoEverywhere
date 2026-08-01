import unittest
from typing import Any, Dict, List

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position,protected-access
from moonraker_octoeverywhere.moonrakercommandhandler import MoonrakerCommandHandler  # noqa: E402


class TestActiveExtruderSelection(unittest.TestCase):
    def _Status(self, activeExtruder:Any) -> Dict[str, Any]:
        return {"toolhead": {"extruder": activeExtruder}}

    c_ToolChangerExtruders:List[str] = ["extruder", "extruder1", "extruder2", "extruder3"]

    # The bug this covers: on a tool changing printer the object named "extruder" can be a parked tool sitting at
    # room temp while a different extruder is active, so reading "extruder" reported a temp for the wrong tool.
    def test_uses_the_active_extruder_not_the_first_one(self) -> None:
        name = MoonrakerCommandHandler.GetActiveExtruderObjectName(self._Status("extruder1"), self.c_ToolChangerExtruders)
        self.assertEqual(name, "extruder1")

    def test_single_extruder_printer(self) -> None:
        name = MoonrakerCommandHandler.GetActiveExtruderObjectName(self._Status("extruder"), ["extruder"])
        self.assertEqual(name, "extruder")

    # If the printer doesn't report a toolhead we must still report the main extruder rather than nothing.
    def test_missing_toolhead_falls_back(self) -> None:
        name = MoonrakerCommandHandler.GetActiveExtruderObjectName({}, self.c_ToolChangerExtruders)
        self.assertEqual(name, "extruder")

    def test_missing_or_invalid_active_name_falls_back(self) -> None:
        for value in (None, "", 5):
            name = MoonrakerCommandHandler.GetActiveExtruderObjectName(self._Status(value), self.c_ToolChangerExtruders)
            self.assertEqual(name, "extruder", f"'{value}' should fall back")

    def test_empty_known_list_falls_back_to_default(self) -> None:
        name = MoonrakerCommandHandler.GetActiveExtruderObjectName({}, [])
        self.assertEqual(name, "extruder")

    # The printer is the authority on which extruder is active, so a name we didn't expect is still used.
    def test_unknown_active_extruder_is_still_used(self) -> None:
        name = MoonrakerCommandHandler.GetActiveExtruderObjectName(self._Status("extruder7"), self.c_ToolChangerExtruders)
        self.assertEqual(name, "extruder7")


class TestExtruderDiscovery(unittest.TestCase):
    c_ToolChangerObjects = ["webhooks", "toolhead", "extruder2", "extruder", "extruder1", "heater_bed", "extruder_stepper e1"]

    def test_finds_and_orders_extruders(self) -> None:
        names = MoonrakerCommandHandler.GetExtruderObjectNames(self.c_ToolChangerObjects)
        self.assertEqual(names, ["extruder", "extruder1", "extruder2"])

    def test_single_extruder_printer(self) -> None:
        names = MoonrakerCommandHandler.GetExtruderObjectNames(["webhooks", "extruder", "heater_bed"])
        self.assertEqual(names, ["extruder"])

    # If we can't read the object list we must still query the extruder every Klipper printer has.
    def test_unknown_object_list_falls_back(self) -> None:
        self.assertEqual(MoonrakerCommandHandler.GetExtruderObjectNames(None), ["extruder"])
        self.assertEqual(MoonrakerCommandHandler.GetExtruderObjectNames([]), ["extruder"])


class TestChamberDiscovery(unittest.TestCase):
    # Only a heater_generic reports a target, so it wins over a plain sensor.
    def test_prefers_heater_over_sensor(self) -> None:
        objects = ["temperature_sensor chamber", "heater_generic chamber"]
        self.assertEqual(MoonrakerCommandHandler.GetChamberObjectName(objects), "heater_generic chamber")

    def test_finds_sensor_only_chamber(self) -> None:
        objects = ["webhooks", "temperature_sensor chamber"]
        self.assertEqual(MoonrakerCommandHandler.GetChamberObjectName(objects), "temperature_sensor chamber")

    # Printers name this differently. The Snapmaker U1 calls its chamber sensor "cavity".
    def test_finds_alternate_names(self) -> None:
        self.assertEqual(MoonrakerCommandHandler.GetChamberObjectName(["temperature_sensor cavity"]), "temperature_sensor cavity")
        self.assertEqual(MoonrakerCommandHandler.GetChamberObjectName(["temperature_sensor Enclosure"]), "temperature_sensor Enclosure")

    def test_no_chamber(self) -> None:
        self.assertIsNone(MoonrakerCommandHandler.GetChamberObjectName(["webhooks", "extruder", "heater_bed"]))
        self.assertIsNone(MoonrakerCommandHandler.GetChamberObjectName(None))

    # A sensor that isn't a chamber must not be picked up as one.
    def test_ignores_unrelated_sensors(self) -> None:
        self.assertIsNone(MoonrakerCommandHandler.GetChamberObjectName(["temperature_sensor mcu", "temperature_sensor raspberry_pi"]))


class TestExtruderObjectNameMatching(unittest.TestCase):
    def test_matches_extruder_objects(self) -> None:
        for name in ("extruder", "extruder1", "extruder2", "extruder10"):
            self.assertIsNotNone(MoonrakerCommandHandler.c_ExtruderObjectNameRegex.match(name), name)

    # These must not be picked up as extruders, or we'd query the wrong objects for temps.
    def test_does_not_match_other_objects(self) -> None:
        for name in ("extruder_stepper", "extruder_stepper my_stepper", "heater_bed", "toolhead", "myextruder", "extruder "):
            self.assertIsNone(MoonrakerCommandHandler.c_ExtruderObjectNameRegex.match(name), name)


if __name__ == "__main__":
    unittest.main()
