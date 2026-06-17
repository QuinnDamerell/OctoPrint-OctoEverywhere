from typing import Any, Dict, Optional


class PrinterStateMapping:
    # UI-ready strings for Klipper forks that expose a machine_state_manager object.
    # This schema is currently known to work with the Snapmaker U1. Other printers
    # can add compatible mappings here without changing Moonraker command handling.
    U1MachineMainStateMap:Dict[int, str] = {
        2: "XYZ Offset Calibration",
        3: "Bed Leveling",
        4: "Flow Calibration",
        5: "Shaper Calibration",
        6: "Firmware Upgrade",
        7: "Abnormal State",
        8: "Screws Tilt Adjust",
        9: "Auto Loading Filament",
        10: "Auto Unloading Filament",
        11: "Manual Loading Filament",
        12: "Park Point Calibration",
        13: "Homing Origin Calibration",
    }

    U1MachineMainStateNameMap:Dict[str, str] = {
        "XYZ_OFFSET_CALIBRATE": "XYZ Offset Calibration",
        "BED_LEVELING": "Bed Leveling",
        "FLOW_CALIBRATION": "Flow Calibration",
        "SHAPER_CALIBRATE": "Shaper Calibration",
        "UPGRADING": "Firmware Upgrade",
        "ABNORMAL": "Abnormal State",
        "SCREWS_TILT_ADJUST": "Screws Tilt Adjust",
        "AUTO_LOAD": "Auto Loading Filament",
        "AUTO_UNLOAD": "Auto Unloading Filament",
        "MANUAL_LOAD": "Manual Loading Filament",
        "PARK_POINT_MANUAL_CALIBRATION": "Park Point Calibration",
        "HOMING_ORIGIN_CALIBRATION": "Homing Origin Calibration",
    }

    U1MachineActionCodeMap:Dict[int, str] = {
        1: "Homing",
        2: "Detecting Plate",
        3: "Preheating Chamber",
        128: "Restoring Print",
        130: "Resuming Print",
        131: "Replenishing Filament",
        132: "Checking Tool Change",
        133: "Auto Loading Filament",
        134: "Pre-extruding Filament",
        135: "Auto Unloading Filament",
        136: "Detecting Bed",
        192: "Cleaning Extruder",
        193: "Cleaning Extruder 1",
        194: "Cleaning Extruder 2",
        195: "Cleaning Extruder 3",
        196: "Probing Extruder XYZ Offset",
        197: "Probing Extruder 1 XYZ Offset",
        198: "Probing Extruder 2 XYZ Offset",
        199: "Probing Extruder 3 XYZ Offset",
        200: "Cleaning Nozzle",
        201: "Waiting For Nozzle Cooling",
        256: "Bed Leveling",
        257: "Bed Preheating",
        258: "Bed Prescanning",
        320: "Calibrating Extruder Flow",
        321: "Calibrating Extruder 1 Flow",
        322: "Calibrating Extruder 2 Flow",
        323: "Calibrating Extruder 3 Flow",
        384: "Shaper Calibration",
        512: "Resetting To Initial Position",
        513: "Probing Reference Points",
        514: "Manual Tuning",
        515: "Verifying Probe Adjustment",
        576: "Auto Loading Filament",
        640: "Auto Unloading Filament",
        704: "Manual Loading Filament",
        768: "Park Point Calibration",
        769: "Verifying Extruder Pick",
        770: "Verifying Extruder Park",
        832: "Homing Origin Calibration",
    }

    U1MachineActionCodeNameMap:Dict[str, str] = {
        "HOMING": "Homing",
        "DETECT_PLATE": "Detecting Plate",
        "PREHRAT_CHAMBER": "Preheating Chamber",
        "PREHEAT_CHAMBER": "Preheating Chamber",
        "PRINT_PL_RESTORE": "Restoring Print",
        "PRINT_RESUMING": "Resuming Print",
        "PRINT_REPLENISHING": "Replenishing Filament",
        "PRINT_SWITCH_CHECKING": "Checking Tool Change",
        "PRINT_AUTO_FEEDING": "Auto Loading Filament",
        "PRINT_PREEXTRUDING": "Pre-extruding Filament",
        "PRINT_AUTO_UNLOADING": "Auto Unloading Filament",
        "PRINT_BED_DETECTING": "Detecting Bed",
        "MANUAL_CLEAN_EXTRUDER": "Cleaning Extruder",
        "MANUAL_CLEAN_EXTRUDER1": "Cleaning Extruder 1",
        "MANUAL_CLEAN_EXTRUDER2": "Cleaning Extruder 2",
        "MANUAL_CLEAN_EXTRUDER3": "Cleaning Extruder 3",
        "EXTRUDER_XYZ_OFFSET_PROBE": "Probing Extruder XYZ Offset",
        "EXTRUDER1_XYZ_OFFSET_PROBE": "Probing Extruder 1 XYZ Offset",
        "EXTRUDER2_XYZ_OFFSET_PROBE": "Probing Extruder 2 XYZ Offset",
        "EXTRUDER3_XYZ_OFFSET_PROBE": "Probing Extruder 3 XYZ Offset",
        "AUTO_CLEAN_NOZZLE": "Cleaning Nozzle",
        "WAIT_NOZZLE_COOLING": "Waiting For Nozzle Cooling",
        "BED_LEVELING": "Bed Leveling",
        "BED_PREHEATING": "Bed Preheating",
        "BED_PRESCANNING": "Bed Prescanning",
        "EXTRUDER_FLOW_CALIBRATING": "Calibrating Extruder Flow",
        "EXTRUDER1_FLOW_CALIBRATING": "Calibrating Extruder 1 Flow",
        "EXTRUDER2_FLOW_CALIBRATING": "Calibrating Extruder 2 Flow",
        "EXTRUDER3_FLOW_CALIBRATING": "Calibrating Extruder 3 Flow",
        "SHAPER_CALIBRATING": "Shaper Calibration",
        "RESET_TO_INITIAL": "Resetting To Initial Position",
        "PROBE_REFERENCE_POINTS": "Probing Reference Points",
        "MANUAL_TUNING": "Manual Tuning",
        "PROBING_ADJUST_VERIFY": "Verifying Probe Adjustment",
        "AUTO_LOADING": "Auto Loading Filament",
        "AUTO_UNLOADING": "Auto Unloading Filament",
        "MANUAL_LOADING": "Manual Loading Filament",
        "PARK_POINT_MANUAL_CALIBRATING": "Park Point Calibration",
        "EXTRUDER_PICK_VERIFY": "Verifying Extruder Pick",
        "EXTRUDER_PARK_VERIFY": "Verifying Extruder Park",
        "HOMING_ORIGIN_CALIBRATING": "Homing Origin Calibration",
    }

    @staticmethod
    def GetMachineStateManagerSubState(machineStateManager:Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(machineStateManager, dict) or len(machineStateManager) == 0:
            return None

        actionCode = machineStateManager.get("action_code", None)
        # An active action is more specific than the main machine state.
        actionSubState = PrinterStateMapping._MapMachineStateManagerValue(
            actionCode,
            PrinterStateMapping.U1MachineActionCodeMap,
            PrinterStateMapping.U1MachineActionCodeNameMap
        )
        if actionSubState is not None:
            return actionSubState

        mainState = machineStateManager.get("main_state", None)
        return PrinterStateMapping._MapMachineStateManagerValue(
            mainState,
            PrinterStateMapping.U1MachineMainStateMap,
            PrinterStateMapping.U1MachineMainStateNameMap
        )


    @staticmethod
    def _MapMachineStateManagerValue(value:Any, valueMap:Dict[int, str], valueNameMap:Dict[str, str]) -> Optional[str]:
        if value is None:
            return None

        if isinstance(value, bool):
            return None

        if isinstance(value, int):
            return valueMap.get(value, None)

        # Some implementations may serialize enum names instead of numeric values.
        # Treat idle/printing as no substate, since the primary State already covers them.
        if isinstance(value, str):
            normalized = value.strip()
            if len(normalized) == 0:
                return None
            if normalized.isdigit():
                return valueMap.get(int(normalized), None)

            normalizedUpper = normalized.upper()
            if normalizedUpper in ["IDLE", "PRINTING"]:
                return None
            if normalizedUpper in valueNameMap:
                return valueNameMap[normalizedUpper]

            return normalizedUpper.replace("_", " ").title()

        return None
