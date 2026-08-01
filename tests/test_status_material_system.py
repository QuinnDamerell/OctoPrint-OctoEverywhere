import importlib
import logging
import os
import sys
import types
import unittest
from typing import Any, Dict, List, Union

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position
from bambu_octoeverywhere.bambumaterialsystem import BambuMaterialSystemBuilder  # noqa: E402
from bambu_octoeverywhere.bambumodels import BambuState  # noqa: E402
from moonraker_octoeverywhere.moonrakermaterialsystem import MoonrakerMaterialSystemBuilder  # noqa: E402
from octoeverywhere.commandhandler import CommandHandler  # noqa: E402
from octoeverywhere.interfaces import CommandResponse  # noqa: E402
from octoeverywhere.materialsystem import MaterialSystemHelper  # noqa: E402


def _ImportOctoPrintMaterialSystemBuilder() -> Any:
    packageName = "octoprint_octoeverywhere"
    if packageName not in sys.modules:
        package = types.ModuleType(packageName)
        package.__path__ = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), packageName)]
        sys.modules[packageName] = package
    return importlib.import_module(packageName + ".octoprintmaterialsystem").OctoPrintMaterialSystemBuilder


OctoPrintMaterialSystemBuilder = _ImportOctoPrintMaterialSystemBuilder()


class _StatusPlatform:
    def __init__(self, includeResult:bool=True) -> None:
        self.Calls:List[bool] = []
        self.IncludeResult = includeResult

    def GetCurrentJobStatus(self, includeMaterialSystem:bool=False) -> Union[int, None, Dict[str, Any]]:
        self.Calls.append(includeMaterialSystem)
        status:Dict[str, Any] = {"State": "idle", "CurrentPrint": {}}
        if self.IncludeResult:
            # Intentionally return this for both request variants. The common handler must enforce omission for lean
            # responses even if a platform implementation makes a mistake.
            status["MaterialSystem"] = {"Sources": []}
        return status

    def GetPlatformVersionStr(self) -> str:
        return "test"

    def GetSupportedFeatureFlags(self) -> int:
        return 0


class TestStatusMaterialSystemFlag(unittest.TestCase):
    def _Handler(self, platform:_StatusPlatform) -> CommandHandler:
        handler = CommandHandler(logging.getLogger("test"), None, platform, None)  # pyright: ignore[reportArgumentType]
        handler.ListWebcams = lambda includeUrls=True: CommandResponse.Success({"Webcams": [], "DefaultIndex": 0})  # type: ignore[method-assign]
        return handler

    def test_default_status_is_lean(self) -> None:
        platform = _StatusPlatform()
        response = self._Handler(platform).GetStatus()
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(platform.Calls, [False])
        self.assertNotIn("MaterialSystem", response.ResultDict["JobStatus"])  # type: ignore[index]

    def test_true_flag_is_forwarded(self) -> None:
        platform = _StatusPlatform()
        response = self._Handler(platform).ProcessCommand("status", {"IncludeMaterialSystem": True})
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(platform.Calls, [True])
        self.assertIn("MaterialSystem", response.ResultDict["JobStatus"])  # type: ignore[index]

    def test_get_query_string_boolean_is_supported(self) -> None:
        platform = _StatusPlatform()
        response = self._Handler(platform).ProcessCommand("status", {"includematerialsystem": "true"})
        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(platform.Calls, [True])

    def test_invalid_flag_is_rejected_before_platform_call(self) -> None:
        platform = _StatusPlatform()
        response = self._Handler(platform).GetStatus({"IncludeMaterialSystem": "yes"})
        self.assertEqual(response.StatusCode, 400)
        self.assertEqual(platform.Calls, [])

    def test_full_request_has_explicit_null_when_platform_cannot_report(self) -> None:
        platform = _StatusPlatform(includeResult=False)
        response = self._Handler(platform).GetStatus({"IncludeMaterialSystem": True})
        self.assertEqual(response.StatusCode, 200)
        self.assertIsNone(response.ResultDict["JobStatus"]["MaterialSystem"])  # type: ignore[index]


class TestBambuMaterialSystem(unittest.TestCase):
    @staticmethod
    def _Tray(position:int, materialType:str, color:str, remain:int, rfid:bool=True) -> Dict[str, Any]:
        uid = f"ABC{position}" if rfid else "0000000000000000"
        return {
            "id": str(position),
            "tray_id_name": f"Bambu {materialType}" if rfid else "",
            "tray_type": materialType,
            "tray_color": color,
            "remain": remain,
            "tag_uid": uid,
            "tray_uuid": uid,
            "tray_info_idx": f"GFL0{position}",
            "cali_idx": position,
            "k": 0.02,
            "n": 1.4,
        }

    def _State(self) -> BambuState:
        state = BambuState()
        state.OnUpdate({
            "nozzle_temper": 211.25,
            "nozzle_target_temper": 220,
            "nozzle_diameter": 0.4,
            "ams": {
                "version": 4,
                "tray_exist_bits": "d",  # Slots 0, 2, and 3 have filament; slot 1 is empty.
                "tray_is_bbl_bits": "5",
                "tray_now": "2",
                "tray_tar": "254",
                "ams": [{
                    "id": "0",
                    "humidity": "4",
                    "humidity_raw": "28",
                    "temp": "24.5",
                    "tray": [
                        self._Tray(0, "PLA", "FF6A13FF", 75),
                        self._Tray(1, "", "00000000", -1, False),
                        self._Tray(2, "PETG", "00AEEF80", 40),
                        self._Tray(3, "TPU", "111111FF", -1, False),
                    ]
                }]
            },
            "vt_tray": self._Tray(254, "PLA", "123456FF", 0, False),
        })
        return state

    def test_normalizes_ams_sources_units_tools_and_routes(self) -> None:
        materialSystem = BambuMaterialSystemBuilder.Build(self._State())

        self.assertEqual(len(materialSystem["Sources"]), 5)
        self.assertEqual(len(materialSystem["Tools"]), 1)
        self.assertEqual(materialSystem["Units"][0]["HumidityPercent"], 28.0)
        self.assertEqual(materialSystem["Units"][0]["PlatformDetails"]["HumidityLevel"], 4)

        slot0 = materialSystem["Sources"][0]
        slot1 = materialSystem["Sources"][1]
        external = materialSystem["Sources"][4]
        self.assertEqual(slot0["PrintMappingValue"], 0)
        self.assertEqual(slot0["Material"]["ColorHex"], "FF6A13")
        self.assertEqual(slot0["Material"]["RemainingPercent"], 75.0)
        self.assertFalse(slot0["IsEmpty"])
        self.assertTrue(slot1["IsEmpty"])
        self.assertEqual(external["PrintMappingValue"], -1)
        self.assertNotIn("RemainingPercent", external["Material"])

        routes = {(r["SourceId"], r["State"]) for r in materialSystem["Routes"]}
        self.assertIn((materialSystem["Sources"][2]["Id"], "loaded"), routes)
        self.assertIn((external["Id"], "loading"), routes)

    def test_partial_ams_update_preserves_full_topology(self) -> None:
        state = self._State()
        state.OnUpdate({"ams": {"tray_now": "1"}})
        materialSystem = BambuMaterialSystemBuilder.Build(state)
        self.assertEqual(len(materialSystem["Sources"]), 5)
        loaded = [r for r in materialSystem["Routes"] if r.get("State") == "loaded"]
        self.assertEqual(loaded[0]["SourceId"], materialSystem["Sources"][1]["Id"])

    def test_external_tray_identity_is_not_its_print_mapping(self) -> None:
        materialSystem = BambuMaterialSystemBuilder.Build(self._State())
        external = materialSystem["Sources"][-1]
        self.assertEqual(external["Id"], "bambu-external-spool")
        self.assertEqual(external["PlatformDetails"]["TrayId"], 254)
        self.assertEqual(external["PrintMappingValue"], -1)

    def test_h2_dual_nozzles_and_external_slots_do_not_guess_routing(self) -> None:
        state = BambuState()
        state.OnUpdate({
            "extruder": {
                "state": 2,
                "info": [
                    {"id": 0, "stat": 197376, "temp": (220 << 16) | 220},
                    {"id": 1, "stat": 196608, "temp": (88 << 16) | 159},
                ]
            },
            "vir_slot": [
                self._Tray(254, "PLA", "FFFFFFFF", -1, False),
                self._Tray(255, "PETG", "000000FF", -1, False),
            ],
            # H2 reports vt_tray as well, but vir_slot is the authoritative two-spool collection.
            "vt_tray": self._Tray(255, "PETG", "000000FF", -1, False),
        })

        materialSystem = BambuMaterialSystemBuilder.Build(state)
        self.assertTrue(materialSystem["Capabilities"]["SupportsMultiTool"])
        self.assertEqual(len(materialSystem["Tools"]), 2)
        self.assertEqual(materialSystem["Tools"][0]["ActualCelsius"], 220.0)
        self.assertEqual(materialSystem["Tools"][0]["TargetCelsius"], 220.0)
        self.assertEqual(materialSystem["Tools"][1]["ActualCelsius"], 159.0)
        self.assertEqual(materialSystem["Tools"][1]["TargetCelsius"], 88.0)
        self.assertNotIn("IsActive", materialSystem["Tools"][0])
        self.assertEqual(len(materialSystem["Sources"]), 2)
        self.assertNotIn("PrintMappingValue", materialSystem["Sources"][0])
        self.assertEqual(materialSystem["Routes"], [])


class TestMultiToolMaterialSystems(unittest.TestCase):
    def test_moonraker_reports_each_tool_temperature_and_active_tool(self) -> None:
        extruders = ["extruder", "extruder1", "extruder2", "extruder3"]
        status:Dict[str, Any] = {
            "extruder": {"temperature": 25, "target": 0, "status": "PARKED", "switch_count": 1985},
            "extruder1": {"temperature": 196, "target": 200, "status": "ACTIVATE", "switch_count": 1703},
            "extruder2": {"temperature": 25, "target": 0, "status": "PARKED", "error_count": 3},
            "extruder3": {"temperature": 25, "target": 0, "status": "PARKED"},
        }
        materialSystem = MoonrakerMaterialSystemBuilder.Build(status, extruders, "extruder1")
        self.assertEqual(len(materialSystem["Tools"]), 4)
        self.assertEqual(len(materialSystem["Sources"]), 4)
        self.assertEqual(len(materialSystem["Routes"]), 4)
        self.assertFalse(materialSystem["Tools"][0]["IsActive"])
        self.assertTrue(materialSystem["Tools"][1]["IsActive"])
        self.assertEqual(materialSystem["Tools"][1]["ActualCelsius"], 196.0)
        self.assertEqual(materialSystem["Tools"][2]["PlatformDetails"]["ErrorCount"], 3)

    def test_octoprint_reports_all_tools_without_guessing_active(self) -> None:
        materialSystem = OctoPrintMaterialSystemBuilder.Build({
            "tool0": {"actual": 25, "target": 0},
            "tool1": {"actual": 205, "target": 210},
            "bed": {"actual": 60, "target": 60},
        })
        self.assertEqual(len(materialSystem["Tools"]), 2)
        self.assertEqual(materialSystem["Tools"][1]["ActualCelsius"], 205.0)
        self.assertNotIn("IsActive", materialSystem["Tools"][0])
        self.assertNotIn("IsActive", materialSystem["Tools"][1])

    def test_snapmaker_u1_adds_rfid_feed_mapping_and_entangle_context(self) -> None:
        extruders = ["extruder", "extruder1", "extruder2", "extruder3"]
        status:Dict[str, Any] = {
            name: {
                "temperature": 25 + index,
                "target": 0,
                "nozzle_diameter": 0.4,
                "real_extruder_stats": "ACTIVATE" if index == 2 else "PARKED",
                "switch_count": 100 + index,
                "last_maintenance_count": 80,
            }
            for index, name in enumerate(extruders)
        }
        status.update({
            "print_task_config": {
                "filament_vendor": ["Snapmaker", "NONE", "Generic", "NONE"],
                "filament_type": ["PLA", "NONE", "PETG", "NONE"],
                "filament_sub_type": ["Basic", "NONE", "HF", "NONE"],
                "filament_color_rgba": ["FF6600FF", "FFFFFFFF", "112233FF", "FFFFFFFF"],
                "filament_exist": [True, False, True, False],
                "filament_official": [True, False, False, False],
                "filament_edit": [False, True, True, True],
                "filament_soft": [False, False, False, False],
                "filament_sku": [1234, 0, 0, 0],
                "extruder_map_table": [2, 0, 1, 3],
                "extruders_used": [True, False, True, False],
                "extruders_replenished": [1, 0, 3, 2],
                "auto_replenish_filament": True,
                "replenish_ignore_color": False,
                "filament_entangle_detect": True,
                "filament_entangle_sen": "high",
            },
            "filament_detect": {
                "info": [{"VENDOR": "Snapmaker", "MAIN_TYPE": "PLA", "SUB_TYPE": "Basic", "RGB_1": 0xFF6600, "CARD_UID": 99}]
            },
            "filament_feed left": {
                "extruder0": {"module_exist": True, "filament_detected": True, "disable_auto": False, "channel_state": 7},
                "extruder1": {"module_exist": True, "filament_detected": False, "disable_auto": False, "channel_state": 0},
            },
            "filament_feed right": {
                "extruder2": {"module_exist": True, "filament_detected": True, "disable_auto": False, "channel_state": 7},
                "extruder3": {"module_exist": True, "filament_detected": False, "disable_auto": True, "channel_state": 0},
            },
            "filament_entangle_detect e2_filament": {"detect_factor": 1.25},
            "filament_motion_sensor e2_filament": {"enabled": True, "filament_detected": True},
        })

        materialSystem = MoonrakerMaterialSystemBuilder.Build(status, extruders, "extruder2")
        self.assertEqual(materialSystem["PlatformDetails"]["Platform"], "SnapmakerU1")
        self.assertEqual(materialSystem["PlatformDetails"]["LogicalToPhysicalToolMap"], [2, 0, 1, 3])
        self.assertTrue(materialSystem["PlatformDetails"]["EntangleDetectionEnabled"])
        self.assertEqual(materialSystem["Sources"][0]["Material"]["Manufacturer"], "Snapmaker")
        self.assertEqual(materialSystem["Sources"][0]["Material"]["ColorHex"], "FF6600")
        self.assertFalse(materialSystem["Sources"][0]["IsEmpty"])
        self.assertTrue(materialSystem["Sources"][1]["IsEmpty"])
        self.assertNotIn("Material", materialSystem["Sources"][1])
        self.assertEqual(materialSystem["Sources"][0]["PlatformDetails"]["FeedState"], 7)
        self.assertEqual(materialSystem["Tools"][2]["PlatformDetails"]["EntangleDetectFactor"], 1.25)
        self.assertEqual(materialSystem["Tools"][2]["PlatformDetails"]["LastMaintenanceCount"], 80)

    def test_u1_optional_objects_are_only_discovered_when_present(self) -> None:
        objects = [
            "extruder", "print_task_config", "filament_detect", "filament_feed left",
            "filament_entangle_detect e0_filament", "filament_motion_sensor e0_filament", "unrelated",
        ]
        self.assertEqual(
            MoonrakerMaterialSystemBuilder.GetOptionalQueryObjectNames(objects),
            [
                "filament_detect", "filament_entangle_detect e0_filament", "filament_feed left",
                "filament_motion_sensor e0_filament", "print_task_config",
            ]
        )

    def test_shared_contract_caps_untrusted_topology(self) -> None:
        tools = [MaterialSystemHelper.BuildTool(i, f"Tool {i}", i == 0, 20, 0) for i in range(30)]
        materialSystem = MaterialSystemHelper.BuildOneToOne(tools)
        self.assertEqual(len(materialSystem["Tools"]), MaterialSystemHelper.c_MaxTools)
        self.assertEqual(len(materialSystem["Sources"]), MaterialSystemHelper.c_MaxTools)


if __name__ == "__main__":
    unittest.main()
