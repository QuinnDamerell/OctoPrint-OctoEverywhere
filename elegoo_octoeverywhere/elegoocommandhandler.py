from octoeverywhere.commandhandler import CommandResponse
# from octoeverywhere.printinfo import PrintInfoManager

from .elegooclient import ElegooClient
# from .bambumodels import BambuPrintErrors

# This class implements the Platform Command Handler Interface
class ElegooCommandHandler:

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
        state = ElegooClient.Get().GetStatus()
        result = state.GetResult()

        # If we get None or an error, we aren't connected.
        if state.HasError() or result is None:
            # Returning None will be a "connection lost" state.
            return None

        # Map the state
        state = "idle"
        errorStr_CanBeNone = None

        # This is a special state, if set, the string will be shown direct to the user.
        # The idea is that this is a more specific state than the state string.
        subState_CanBeNone = None

        printInfo = result.get("PrintInfo", None)
        if printInfo is not None:
            statusCode = printInfo.get("Status", None)
            # Map the states to our common states
            if statusCode == 0:
                state = "idle"
            elif statusCode == 1 or statusCode == 16 or statusCode == 20:
                state = "warmingup"
                # Resuming has the same state "Preparing".
                # So fif there's any progress, consider it resuming.
                progress = float(printInfo.get("Progress", 0))
                if progress > 0:
                    state = "resuming"
            elif statusCode == 13:
                state = "printing"
            elif statusCode == 5 or statusCode == 6:
                state = "paused"
            else:
                self.Logger.warn("Unknown Elegoo print status code: "+str(statusCode))

            # Here's an example of state strings
            # state = "idle"
            # if "status" in res and "print_stats" in res["status"] and "state" in res["status"]["print_stats"]:
            #     # https://moonraker.readthedocs.io/en/latest/printer_objects/#print_stats
            #     mrState = res["status"]["print_stats"]["state"]
            #     if mrState == "standby":
            #         state = "idle"
            #     elif mrState == "printing":
            #         # This is a special case, we consider "warmingup" a subset of printing.
            #         if MoonrakerClient.Get().GetMoonrakerCompat().CheckIfPrinterIsWarmingUp_WithPrintStats(result):
            #             state = "warmingup"
            #         else:
            #             state = "printing"
            #     elif mrState == "paused":
            #         state = "paused"
            #     elif mrState == "complete":
            #         state = "complete"
            #     elif mrState == "cancelled":
            #         state = "cancelled"
            #     elif mrState == "error":
            #         state = "error"
            #     else:
            #         self.Logger.warn("Unknown mrState returned from print_stats: "+str(mrState))
            # else:
            #     self.Logger.warn("MoonrakerCommandHandler failed to find the print_stats.status")



        # Get current layer info
        # None = The platform doesn't provide it.
        # 0 = The platform provider it, but there's no info yet.
        # # = The values
        currentLayerInt = None
        totalLayersInt = None
        if printInfo is not None:
            currentLayerInt = int(printInfo.get("CurrentLayer", 0))
            totalLayersInt = int(printInfo.get("TotalLayer", 0))

        # Get the filename.
        fileName = None
        if printInfo is not None:
            fileName = printInfo.get("Filename", "")

        # Get the time so far
        durationSec = 0
        if printInfo is not None:
            durationSec = int(printInfo.get("CurrentTicks", 0))

        timeLeftSec = 0# bambuState.GetContinuousTimeRemainingSec()
        if printInfo is not None:
            totalTimeSec = int(printInfo.get("TotalTicks", 0))
            timeLeftSec = int(totalTimeSec - durationSec)

        # This platform doesn't support filament usage.
        filamentUsageMm = 0

        # Get the progress, as a float 100.0-0.0
        # On Elegoo, the progress is always a whole number.
        progress = 0.0
        if printInfo is not None:
            progress = float(printInfo.get("Progress", 0))
        # if bambuState.mc_percent is not None:
        #     progress = float(bambuState.mc_percent)



        # Get the current temps if possible.
        hotendActual = result.get("TempOfNozzle", 0.0)
        hotendTarget = result.get("TempTargetNozzle", 0.0)
        bedActual = result.get("TempOfHotbed", 0.0)
        bedTarget = result.get("TempTargetHotbed", 0.0)

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
        return "Elegoo-CentauriCarbon"
        # version = BambuClient.Get().GetVersion()
        # if version is None:
        #     return "0.0.0"
        # return f"{version.SoftwareVersion}-{version.PrinterName}"


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the pause and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecutePause(self, smartPause, suppressNotificationBool, disableHotendBool, disableBedBool, zLiftMm, retractFilamentMm, showSmartPausePopup) -> CommandResponse:
        return CommandResponse.Success(None)

        # if BambuClient.Get().SendPause():
        #     return CommandResponse.Success(None)
        # else:
        #     return CommandResponse.Error(400, "Failed to send command to printer.")


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the resume and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteResume(self) -> CommandResponse:
        return CommandResponse.Success(None)

        # if BambuClient.Get().SendResume():
        #     return CommandResponse.Success(None)
        # else:
        #     return CommandResponse.Error(400, "Failed to send command to printer.")


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the cancel and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteCancel(self) -> CommandResponse:
        return CommandResponse.Success(None)

        # if BambuClient.Get().SendCancel():
        #     return CommandResponse.Success(None)
        # else:
        #     return CommandResponse.Error(400, "Failed to send command to printer.")
