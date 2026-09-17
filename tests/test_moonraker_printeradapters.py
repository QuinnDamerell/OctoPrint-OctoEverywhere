import unittest

from moonraker_octoeverywhere.printeradapters import (
    MoonrakerPrinterAdapterFactory, SnapmakerU1PrinterAdapter, StandardMoonrakerPrinterAdapter)
from moonraker_octoeverywhere.printerstatemapping import PrinterStateMapping


class TestMoonrakerPrinterAdapters(unittest.TestCase):
    def test_u1_commands_require_a_combination_of_vendor_objects(self) -> None:
        for objects in [[], ["extruder", "machine_state_manager"], ["print_task_config"], ["filament_detect"]]:
            with self.subTest(objects=objects):
                adapter = MoonrakerPrinterAdapterFactory.GetForObjects(objects)
                self.assertIsInstance(adapter, StandardMoonrakerPrinterAdapter)
                self.assertNotIsInstance(adapter, SnapmakerU1PrinterAdapter)
                self.assertIsNone(adapter.GetPhysicalToolSelectionCommand(0))
        for marker in ["machine_state_manager", "filament_detect"]:
            adapter = MoonrakerPrinterAdapterFactory.GetForObjects(["extruder", "print_task_config", marker])
            self.assertIsInstance(adapter, SnapmakerU1PrinterAdapter)
            self.assertEqual(adapter.GetPhysicalToolSelectionCommand(0), "T0 A0")
            self.assertEqual(adapter.GetPhysicalToolSelectionCommand(3), "T3 A0")


    def test_u1_tool_selection_rejects_invalid_indexes(self) -> None:
        adapter = SnapmakerU1PrinterAdapter()
        for index in [-1, True, "0", 1.5]:
            with self.subTest(index=index):
                self.assertIsNone(adapter.GetPhysicalToolSelectionCommand(index))


    def test_error_recognition_does_not_enable_u1_commands_on_generic_printer(self) -> None:
        errorAdapter = MoonrakerPrinterAdapterFactory.GetForError({"coded": "0002-0525-0003-0011"})
        commandAdapter = MoonrakerPrinterAdapterFactory.GetForObjects(["extruder", "extruder1"])
        self.assertIsInstance(errorAdapter, SnapmakerU1PrinterAdapter)
        self.assertIsNone(commandAdapter.GetPhysicalToolSelectionCommand(0))


    def test_u1_exception_parts_accept_numeric_strings_and_preserve_tool_index(self) -> None:
        error = {"level": "2", "id": "525", "index": "3", "code": "11"}
        adapter = MoonrakerPrinterAdapterFactory.GetForError(error)
        self.assertIsInstance(adapter, SnapmakerU1PrinterAdapter)
        self.assertEqual(adapter.GetErrorCode(error), "0002-0525-0003-0011")


    def test_malformed_vendor_fields_do_not_fabricate_composite_code(self) -> None:
        for index in [None, True, -1, 1.5, "not-an-index"]:
            error = {"level": 2, "id": 525, "index": index, "code": 11}
            with self.subTest(index=index):
                adapter = MoonrakerPrinterAdapterFactory.GetForError(error)
                self.assertNotIsInstance(adapter, SnapmakerU1PrinterAdapter)
                self.assertEqual(adapter.GetErrorCode(error), "11")


    def test_cleared_u1_codes_are_not_faults(self) -> None:
        adapter = SnapmakerU1PrinterAdapter()
        self.assertIsNone(adapter.GetErrorCode({"level": 0, "id": 0, "index": 0, "code": 0}))
        self.assertIsNone(adapter.GetErrorCode({"coded": "0000-0000-0000-0000"}))


    def test_standard_adapter_preserves_vendor_codes_without_inventing_u1_states(self) -> None:
        adapter = StandardMoonrakerPrinterAdapter()
        self.assertEqual(adapter.GetErrorCode({"error_code": "CUSTOM-ERROR"}), "CUSTOM-ERROR")
        self.assertEqual(adapter.GetStatusQueryObjects(), [])
        self.assertIsNone(adapter.GetPrinterSubState({"machine_state_manager": {"main_state": 7, "action_code": 1}}))


    def test_u1_adapter_declares_and_reads_its_status_object(self) -> None:
        adapter = SnapmakerU1PrinterAdapter()
        self.assertEqual(adapter.GetStatusQueryObjects(), ["machine_state_manager"])
        self.assertEqual(adapter.GetPrinterSubState({
            "print_stats": {"state": "printing"},
            "machine_state_manager": {"main_state": 7, "action_code": 1},
        }), "Homing")
        self.assertIsNone(adapter.GetPrinterSubState({"print_stats": {"state": "printing"}}))
        self.assertIsNone(adapter.GetPrinterSubState({"machine_state_manager": None}))


    def test_u1_action_is_more_specific_than_main_state(self) -> None:
        adapter = SnapmakerU1PrinterAdapter()
        self.assertEqual(adapter.GetMachineStateManagerSubState({"main_state": 7, "action_code": 1}), "Homing")
        self.assertEqual(adapter.GetMachineStateManagerSubState({"main_state": "ABNORMAL"}), "Abnormal State")
        self.assertEqual(adapter.GetMachineStateManagerSubState({"action_code": "PRINT_RESUMING"}), "Resuming Print")
        self.assertIsNone(adapter.GetMachineStateManagerSubState({"main_state": "PRINTING", "action_code": 0}))
        self.assertIsNone(adapter.GetMachineStateManagerSubState({"main_state": True, "action_code": True}))


    def test_mapping_helper_preserves_compatibility_and_accepts_other_adapter(self) -> None:
        status = {"action_code": 1}
        self.assertEqual(PrinterStateMapping.GetMachineStateManagerSubState(status), "Homing")
        self.assertIsNone(PrinterStateMapping.GetMachineStateManagerSubState(status, StandardMoonrakerPrinterAdapter()))


if __name__ == "__main__":
    unittest.main()
