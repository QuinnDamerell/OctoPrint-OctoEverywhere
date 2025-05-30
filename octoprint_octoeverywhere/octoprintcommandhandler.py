import logging
from typing import Any, Dict, Union

from octoprint import __version__
from octoprint.printer import PrinterInterface

from octoeverywhere.sentry import Sentry
from octoeverywhere.commandhandler import CommandHandler, CommandResponse
from octoeverywhere.interfaces import IPlatformCommandHandler, IOctoPrintPlugin

from .smartpause import SmartPause
from .printerstateobject import PrinterStateObject

# This class implements the Platform Command Handler Interface
class OctoPrintCommandHandler(IPlatformCommandHandler):

    def __init__(self, logger:logging.Logger, octoPrintPrinterObject:PrinterInterface, printerStateObject:PrinterStateObject, mainPluginImpl:IOctoPrintPlugin) -> None:
        self.Logger = logger
        self.OctoPrintPrinterObject = octoPrintPrinterObject
        self.PrinterStateObject = printerStateObject
        self.MainPluginImpl = mainPluginImpl


    # A helper for checking if things exist in dicts.
    def _Exists(self, dictObj:Dict[str, Any], key:str):
        return key in dictObj and dictObj[key] is not None


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
    def GetCurrentJobStatus(self) -> Union[int, None, Dict[str, Any]]:
        try:
            # Get the date from the octoprint printer object.
            currentData:dict[str, Any] = self.OctoPrintPrinterObject.get_current_data() #pyright: ignore[reportUnknownMemberType] octoprint has no typing

            # Get progress
            progress = 0.0
            if self._Exists(currentData, "progress") and self._Exists(currentData["progress"], "completion"):
                progress = float(currentData["progress"]["completion"])

            # Get the current print time
            durationSec = 0
            if self._Exists(currentData, "progress") and self._Exists(currentData["progress"], "printTime"):
                durationSec = int(currentData["progress"]["printTime"])

            # Get the current print time
            # Use the common function for this.
            # This will return -1 if it fails, which is fine because our service expects it.
            timeLeftSec = int(self.PrinterStateObject.GetPrintTimeRemainingEstimateInSeconds())

            # Get the file name. This only exists when a file is loaded or printing.
            fileName = ""
            if self._Exists(currentData, "job") and self._Exists(currentData["job"], "file") and self._Exists(currentData["job"]["file"], "display"):
                fileName = currentData["job"]["file"]["display"]

            # Get the estimated total filament used.
            estTotalFilamentUsageMm = 0
            if self._Exists(currentData, "job") and self._Exists(currentData["job"], "filament") and self._Exists(currentData["job"]["filament"], "tool0") and self._Exists(currentData["job"]["filament"]["tool0"], "length"):
                estTotalFilamentUsageMm = int(currentData["job"]["filament"]["tool0"]["length"])

            # Get the error, if there is one.
            # This is shown to the user directly, so it must be short (think of a dashboard status) and formatted well.
            # TODO - Since OctoPrint can give back all kinds of strings for this, we don't set it, since we can't show it to the user.
            errorStr_CanBeNone = None
            # if self._Exists(currentData, "state") and self._Exists(currentData["state"], "error"):
            #     errorStr_CanBeNone = currentData["state"]["error"]

            # Map the state to our common states.
            # We us this get_state_id to get a more explicit state, over what's in get_current_data above.
            # For example, the printing state string in get_current_data can change if printing from an SD card
            # Possible Values - https://github.com/OctoPrint/OctoPrint/blob/260a1aef11432c421246019e25b6b744abbaed60/src/octoprint/util/comm.py#L432
            # There are a lot of values here. We only include some of them and then consider the rest "idle"
            opStateStr = self.OctoPrintPrinterObject.get_state_id() #pyright: ignore[reportUnknownMemberType] octoprint has no typing
            state = "idle"
            if opStateStr == "PRINTING" or opStateStr == "STARTING" or opStateStr == "FINISHING":
                # Special cases for printing
                if self.PrinterStateObject.IsPrintWarmingUp():
                    state = "warmingup"
                else:
                    state = "printing"
            elif opStateStr == "PAUSED" or opStateStr == "PAUSING":
                state = "paused"
            elif opStateStr == "RESUMING":
                state = "resuming"
            elif opStateStr == "ERROR" or opStateStr == "CLOSED_WITH_ERROR":
                state = "error"
            elif opStateStr == "OPERATIONAL":
                # When a print is complete, the progress will stay at 100% until it's cleared.
                if progress > 99.999:
                    state = "complete"
                else:
                    # Otherwise, we are just idle.
                    state = "idle"
            # Note that OctoPrint doesn't have a cancelled state. When a job it canceled it goes directly back to the "fresh loaded job" idle state.

            # Get the current temps if possible.
            # Note there will be no objects in the dic if the printer isn't connected or in other cases.
            currentTemps = self.OctoPrintPrinterObject.get_current_temperatures() #pyright: ignore[reportUnknownMemberType] octoprint has no typing
            hotendActual = 0.0
            hotendTarget = 0.0
            bedTarget = 0.0
            bedActual = 0.0
            if self._Exists(currentTemps, "tool0"):
                tool0 = currentTemps["tool0"]
                if self._Exists(tool0, "actual"):
                    hotendActual = round(float(tool0["actual"]), 2)
                if self._Exists(tool0, "target"):
                    hotendTarget = round(float(tool0["target"]), 2)
            if self._Exists(currentTemps, "bed"):
                bed = currentTemps["bed"]
                if self._Exists(bed, "actual"):
                    bedActual = round(float(bed["actual"]), 2)
                if self._Exists(bed, "target"):
                    bedTarget = round(float(bed["target"]), 2)

            # Build the object and return.
            return {
                "State": state,
                "Error": errorStr_CanBeNone,
                "CurrentPrint":
                {
                    "Progress" : progress,
                    "DurationSec" : durationSec,
                    # In some system buggy cases, the time left can be super high and won't fit into a int32, so we cap it.
                    "TimeLeftSec" : min(timeLeftSec, 2147483600),
                    "FileName" : fileName,
                    "EstTotalFilUsedMm" : estTotalFilamentUsageMm,
                    "CurrentLayer": None, # OctoPrint doesn't provide these.
                    "TotalLayers": None,  # OctoPrint doesn't provide these.
                    "Temps": {
                        "BedActual": bedActual,
                        "BedTarget": bedTarget,
                        "HotendActual": hotendActual,
                        "HotendTarget": hotendTarget,
                    }
                }
            }

        except Exception as e:
            Sentry.OnExceptionNoSend("GetCurrentJobStatus failed to get job status", e)
        return None


    # !! Platform Command Handler Interface Function !!
    # This must return the platform version as a string.
    def GetPlatformVersionStr(self) -> str:
        try:
            versionStr = str(__version__)
            if versionStr is None or len(versionStr) == 0:
                return "Unknown"
            return versionStr
        except Exception as e:
            Sentry.OnExceptionNoSend("GetPlatformVersionStr failed to get OctoPrint version", e)
        return "Unknown"


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the pause and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecutePause(self, smartPause:bool, suppressNotificationBool:bool, disableHotendBool:bool, disableBedBool:bool, zLiftMm:int, retractFilamentMm:int, showSmartPausePopup:bool) -> CommandResponse:
        # Ensure we are printing, if not, respond with the common error.
        if self.OctoPrintPrinterObject.is_printing() is False: #pyright: ignore[reportUnknownMemberType] octoprint has no typing
            self.Logger.info("ExecutePause is not doing anything because theres' no print in progress..")
            return CommandResponse.Error(CommandHandler.c_CommandError_InvalidPrinterState, "Printer state is not printing.")

        # If we aren't using smart pause, just pause now.
        if smartPause is False:
            try:
                # Set the suppression if desired.
                if suppressNotificationBool:
                    SmartPause.Get().SetLastPauseNotificationSuppressionTimeNow()

                # Do the pause.
                self.OctoPrintPrinterObject.pause_print() #pyright: ignore[reportUnknownMemberType] octoprint has no typing

                # Return success.
                return CommandResponse.Success(None)

            except Exception as e:
                Sentry.OnException("Pause command failed to execute.", e)
                return CommandResponse.Error(500, "Failed to pause")

        # Otherwise, do the smart pause.
        try:
            # If this doesn't throw it's successful
            SmartPause.Get().DoSmartPause(disableHotendBool, disableBedBool, zLiftMm, retractFilamentMm, suppressNotificationBool)
        except Exception as e:
            Sentry.OnException("Failed to ExecutePause, SmartPause error.", e)
            return CommandResponse.Error(500, "Failed to pause")

        # On success, if we did a smart pause, send a notification to tell the user.
        if self.MainPluginImpl is not None:
            if showSmartPausePopup and (disableBedBool or disableHotendBool or zLiftMm > 0 or retractFilamentMm > 0):
                self.MainPluginImpl.ShowSmartPausePopUpOnPortalLoad()
        else:
            self.Logger.error("ExecutePause was called with smart pause, but the main plugin is None. This should never happen.")

        # Success!
        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the resume and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteResume(self) -> CommandResponse:
        # Ensure we are paused, if not, respond with the common error.
        if self.OctoPrintPrinterObject.is_paused() is False and self.OctoPrintPrinterObject.is_pausing() is False: #pyright: ignore[reportUnknownMemberType] octoprint has no typing
            self.Logger.info("ExecuteResume is not doing anything because the printer isn't paused..")
            return CommandResponse.Error(CommandHandler.c_CommandError_InvalidPrinterState, "Printer state is not paused.")

        # Do the resume.
        self.OctoPrintPrinterObject.resume_print() #pyright: ignore[reportUnknownMemberType] octoprint has no typing

        # Return success.
        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the cancel and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteCancel(self) -> CommandResponse:
        # Ensure we are paused, if not, respond with the common error.
        state = self.OctoPrintPrinterObject.get_state_id() #pyright: ignore[reportUnknownMemberType] octoprint has no typing
        if state != "PRINTING" and state != "RESUMING" and state != "FINISHING" and state != "STARTING" and state != "PAUSED" and state != "PAUSING":
            self.Logger.info("ExecuteCancel is not doing anything because the printer printing.")
            return CommandResponse.Error(CommandHandler.c_CommandError_InvalidPrinterState, "Printer state is not printing.")

        # Do the cancel.
        self.OctoPrintPrinterObject.cancel_print() #pyright: ignore[reportUnknownMemberType] octoprint has no typing

        # Return success.
        return CommandResponse.Success(None)
