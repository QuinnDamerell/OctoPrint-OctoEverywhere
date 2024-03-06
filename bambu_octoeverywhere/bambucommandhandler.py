from octoeverywhere.commandhandler import CommandResponse

from .bambuclient import BambuClient

# This class implements the Platform Command Handler Interface
class BambuCommandHandler:

    def __init__(self, logger) -> None:
        self.Logger = logger


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
    def GetCurrentJobStatus(self):
        # Try to get the current state.
        bambuState = BambuClient.Get().GetState()

        # If the state is None, we are disconnected.
        if bambuState is None:
            # Returning None will be a "connection lost" state.
            return None

        # Map the state
        # TODO - Add "error" if possible
        # Possible states: https://github.com/greghesp/ha-bambulab/blob/e72e343acd3279c9bccba510f94bf0e291fe5aaa/custom_components/bambu_lab/pybambu/const.py#L83C1-L83C21
        state = "idle"
        if bambuState.gcode_state is not None:
            gcodeState = bambuState.gcode_state
            if gcodeState == "IDLE" or gcodeState == "INIT" or gcodeState == "OFFLINE" or gcodeState == "UNKNOWN":
                state = "idle"
            elif gcodeState == "RUNNING" or gcodeState == "SLICING":
                # Only check stg_cur in the known printing state, because sometimes it doesn't get reset to idle when transitioning to an error.
                stg = bambuState.stg_cur
                # Here's a full list: https://github.com/davglass/bambu-cli/blob/398c24057c71fc6bcc5dbd818bdcacc20833f61c/lib/const.js#L104
                # stg==255 is used as a kind of intenum unknown state when the print is first starting and finishing.
                # We can't really use it because it can happen at different points in time and it's not clear what the real state is.
                if stg == 2 or stg == 7:
                    state = "warmingup"
                elif stg == 14:
                    state = "cleaningnozzle"
                elif stg == 1:
                    state = "autobedlevel"
                else:
                    state = "printing"
            elif gcodeState == "PAUSE":
                state = "paused"
            elif gcodeState == "FINISH":
                state = "complete"
            elif gcodeState == "FAILED":
                state = "cancelled"
            elif gcodeState == "PREPARE":
                state = "warmingup"
            else:
                self.Logger.warn(f"Unknown gcode_state state in print state: {gcodeState}")

        # TODO - If in an error state, set some context as to why.
        errorStr_CanBeNone = None

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

        # Get duration and filename.
        durationSec = 0
        fileName = ""
        if bambuState.gcode_file is not None:
            fileName = bambuState.gcode_file
        #if "gcode_file" in res:
            #durationSec = res["gcode_file"]

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
            "Error": errorStr_CanBeNone,
            "CurrentPrint":
            {
                "Progress" : progress,
                "DurationSec" : durationSec,
                "TimeLeftSec" : timeLeftSec,
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
