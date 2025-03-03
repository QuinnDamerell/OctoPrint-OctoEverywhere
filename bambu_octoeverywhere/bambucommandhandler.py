from octoeverywhere.commandhandler import CommandResponse
from octoeverywhere.printinfo import PrintInfoManager

from .bambuclient import BambuClient
from .bambumodels import BambuPrintErrors

# This class implements the Platform Command Handler Interface
class BambuCommandHandler:

    def __init__(self, logger) -> None:
        self.Logger = logger


    # This map contains UI ready strings that map to a subset of sub-stages we can send which are more specific than the state.
    # These need to be UI ready, since they will be shown directly.
    # Some known stages are excluded, because we don't want to show them.
    # Here's a full list: https://github.com/davglass/bambu-cli/blob/398c24057c71fc6bcc5dbd818bdcacc20833f61c/lib/const.js#L104
    SubStageMap = {
        1:  "Auto Bed Leveling",
        2:  "Bed Preheating",
        3:  "Sweeping XY Mech Mode",
        4:  "Changing Filament",
        5:  "M400 Pause",
        6:  "Filament Runout",
        7:  "Heating Hotend",
        8:  "Calibrating Extrusion",
        9:  "Scanning Bed Surface",
        10: "Inspecting First Layer",
        11: "Identifying Build Plate",
        12: "Calibrating Micro Lidar",
        13: "Homing Toolhead",
        14: "Cleaning Nozzle",
        15: "Checking Temperature",
        16: "Paused By User",
        17: "Front Cover Falling",
        18: "Calibrating Micro Lidar",
        19: "Calibrating Extrusion Flow",
        20: "Nozzle Temperature Malfunction",
        21: "Bed Temperature Malfunction",
        22: "Filament Unloading",
        23: "Skip Step Pause",
        24: "Filament Loading",
        25: "Motor Noise Calibration",
        26: "AMS lost",
        27: "Low Speed Of Heat Break Fan",
        28: "Chamber Temperature Control Error",
        29: "Cooling Chamber",
        30: "Paused By Gcode",
        31: "Motor Noise Showoff",
        32: "Nozzle Filament Covered Detected Pause",
        33: "Cutter Error",
        34: "First Layer Error",
        35: "Nozzle Clogged"
    }


    # !! Platform Command Handler Interface Function !!
    #
    # This must return the common "JobStatus" dict or None on failure.
    # The format of this must stay consistent with OctoPrint and the service.
    # Returning None send back the NoHostConnected error, assuming that the plugin isn't connected to the host or the host isn't
    # connected to the printer's firmware.
    #
    # See the JobStatusV2 class in the service for the object definition.
    #
    # Returning None will result in the "Printer not connected" state.
    # Or one of the CommandHandler.c_CommandError_... ints can be returned, which will be sent as the result.
    #
    def GetCurrentJobStatus(self):
        # Try to get the current state.
        bambuState = BambuClient.Get().GetState()

        # If the state is None, we are disconnected.
        if bambuState is None:
            # Returning None will be a "connection lost" state.
            return None

        # Map the state
        # Possible states: https://github.com/greghesp/ha-bambulab/blob/e72e343acd3279c9bccba510f94bf0e291fe5aaa/custom_components/bambu_lab/pybambu/const.py#L83C1-L83C21
        state = "idle"
        errorStr_CanBeNone = None

        # Before checking the state, see if the print is in an error state.
        # This error state can be common among other states, like "IDLE" or "PAUSE"
        printError = bambuState.GetPrinterError()
        if printError is not None:
            # Always set the state to error.
            # If we can match a known state, return a good string that can be shown for the user.
            state = "error"
            if printError == BambuPrintErrors.FilamentRunOut:
                errorStr_CanBeNone = "Filament Run Out"
        # If we aren't in error, use the state
        elif bambuState.gcode_state is not None:
            gcodeState = bambuState.gcode_state
            if gcodeState == "IDLE" or gcodeState == "INIT" or gcodeState == "OFFLINE" or gcodeState == "UNKNOWN":
                state = "idle"
            elif gcodeState == "RUNNING" or gcodeState == "SLICING":
                # Only check stg_cur in the known printing state, because sometimes it doesn't get reset to idle when transitioning to an error.
                stg = bambuState.stg_cur
                if stg == 2 or stg == 7:
                    state = "warmingup"
                else:
                    # These are all a subset of printing states.
                    state = "printing"
            elif gcodeState == "PAUSE":
                state = "paused"
            elif gcodeState == "FINISH":
                # When the X1C first starts and does the first time user calibration, the state is FINISH
                # but there's really nothing done. This might happen after other calibrations, so if the total layers is 0, we are idle.
                if bambuState.total_layer_num is not None and bambuState.total_layer_num == 0:
                    state = "idle"
                else:
                    state = "complete"
            elif gcodeState == "FAILED":
                state = "cancelled"
            elif gcodeState == "PREPARE":
                state = "warmingup"
            else:
                self.Logger.warn(f"Unknown gcode_state state in print state: {gcodeState}")

        # If we have a mapped sub state, set it.
        subState_CanBeNone = None
        if bambuState.stg_cur is not None:
            if bambuState.stg_cur in BambuCommandHandler.SubStageMap:
                subState_CanBeNone = BambuCommandHandler.SubStageMap[bambuState.stg_cur]

        # Get current layer info
        # None = The platform doesn't provide it.
        # 0 = The platform provider it, but there's no info yet.
        # # = The values
        currentLayerInt = None
        totalLayersInt = None
        if bambuState.layer_num is not None:
            currentLayerInt = int(bambuState.layer_num)
        if bambuState.total_layer_num is not None:
            totalLayersInt = int(bambuState.total_layer_num)

        # Get the filename.
        fileName = bambuState.GetFileNameWithNoExtension()
        if fileName is None:
            fileName = ""

        # For Bambu, the printer doesn't report the duration or the print start time.
        # Thus we have to track it ourselves in our print info.
        # When the print is over, a final print duration is set, so this doesn't keep going from print start.
        durationSec = 0
        pi = PrintInfoManager.Get().GetPrintInfo(bambuState.GetPrintCookie())
        if pi is not None:
            durationSec = pi.GetPrintDurationSec()

        # If we have a file name, try to get the current filament usage.
        filamentUsageMm = 0
        # if fileName is not None and len(fileName) > 0:
        #     filamentUsageMm = FileMetadataCache.Get().GetEstimatedFilamentUsageMm(fileName)

        # Get the progress
        progress = 0.0
        if bambuState.mc_percent is not None:
            progress = float(bambuState.mc_percent)

        # We have special logic to handle the time left count down, since bambu only gives us minutes
        # and we want seconds. We can estimate it pretty well by counting down from the last time it changed.
        timeLeftSec = bambuState.GetContinuousTimeRemainingSec()
        if timeLeftSec is None:
            timeLeftSec = 0

        # Get the current temps if possible.
        hotendActual = 0.0
        hotendTarget = 0.0
        bedTarget = 0.0
        bedActual = 0.0
        if bambuState.nozzle_temper is not None:
            hotendActual = round(float(bambuState.nozzle_temper), 2)
        if bambuState.nozzle_target_temper is not None:
            hotendTarget = round(float(bambuState.nozzle_target_temper), 2)
        if bambuState.bed_temper is not None:
            bedActual = round(float(bambuState.bed_temper), 2)
        if bambuState.bed_target_temper is not None:
            bedTarget = round(float(bambuState.bed_target_temper), 2)

        # Build the object and return.
        return {
            "State": state,
            "SubState": subState_CanBeNone,
            "Error": errorStr_CanBeNone,
            "CurrentPrint":
            {
                "Progress" : progress,
                "DurationSec" : durationSec,
                # In some system buggy cases, the time left can be super high and won't fit into a int32, so we cap it.
                "TimeLeftSec" : min(timeLeftSec, 2147483600),
                "FileName" : fileName,
                "EstTotalFilUsedMm" : filamentUsageMm,
                "CurrentLayer": currentLayerInt,
                "TotalLayers": totalLayersInt,
                "Temps": {
                    "BedActual": bedActual,
                    "BedTarget": bedTarget,
                    "HotendActual": hotendActual,
                    "HotendTarget": hotendTarget,
                }
            }
        }


    # !! Platform Command Handler Interface Function !!
    # This must return the platform version as a string.
    def GetPlatformVersionStr(self):
        version = BambuClient.Get().GetVersion()
        if version is None:
            return "0.0.0"
        return f"{version.SoftwareVersion}-{version.PrinterName}"


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the pause and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecutePause(self, smartPause, suppressNotificationBool, disableHotendBool, disableBedBool, zLiftMm, retractFilamentMm, showSmartPausePopup) -> CommandResponse:
        if BambuClient.Get().SendPause():
            return CommandResponse.Success(None)
        else:
            return CommandResponse.Error(400, "Failed to send command to printer.")


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the resume and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteResume(self) -> CommandResponse:
        if BambuClient.Get().SendResume():
            return CommandResponse.Success(None)
        else:
            return CommandResponse.Error(400, "Failed to send command to printer.")


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the cancel and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteCancel(self) -> CommandResponse:
        if BambuClient.Get().SendCancel():
            return CommandResponse.Success(None)
        else:
            return CommandResponse.Error(400, "Failed to send command to printer.")
