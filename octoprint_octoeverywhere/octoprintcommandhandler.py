from octoprint import __version__

from octoeverywhere.sentry import Sentry
from octoeverywhere.commandhandler import CommandHandler, CommandResponse

from .smartpause import SmartPause

# This class implements the Platform Command Handler Interface
class OctoPrintCommandHandler:

    def __init__(self, logger, octoPrintPrinterObject, printerStateObject, mainPluginImpl):
        self.Logger = logger
        self.OctoPrintPrinterObject = octoPrintPrinterObject
        self.PrinterStateObject = printerStateObject
        self.MainPluginImpl = mainPluginImpl


    # !! Platform Command Handler Interface Function !!
    #
    # This must return the common "JobStatus" dict or None on failure.
    # The format of this must stay consistent with OctoPrint and the service.
    # Returning None send back the NoHostConnected error, assuming that the plugin isn't connected to the host or the host isn't
    # connected to the pritner's firmware.
    #
    # See the JobStatusV2 class in the service for the object definition.
    #
    def GetCurrentJobStatus(self):
        try:
            # Get the date from the octoprint printer object.
            currentData = self.OctoPrintPrinterObject.get_current_data()

            # Get progress
            progress = 0.0
            if "completion" in currentData["progress"] and currentData["progress"]["completion"] is not None:
                progress = float(currentData["progress"]["completion"])

            # Get the current print time
            durationSec = 0
            if "printTime" in currentData["progress"] and currentData["progress"]["printTime"] is not None:
                durationSec = int(currentData["progress"]["printTime"])

            # Get the current print time
            # Use the common function for this.
            # This will return -1 if it fails, which is fine because our service expects it.
            timeLeftSec = int(self.PrinterStateObject.GetPrintTimeRemainingEstimateInSeconds())

            # Get the file name. This only exists when a file is loaded or printing.
            fileName = ""
            if "display" in currentData["job"]["file"]:
                fileName = currentData["job"]["file"]["display"]

            # Get the error, if there is one.
            errorStr_CanBeNone = None
            if "error" in currentData["state"] and currentData["state"]["error"] is not None:
                errorStr_CanBeNone = currentData["state"]["error"]

            # Map the state to our common states.
            # We us this get_state_id to get a more explicit state, over what's in get_current_data above.
            # For example, the printing state string in get_current_data can change if printing from an SD card
            # Possible Values - https://github.com/OctoPrint/OctoPrint/blob/260a1aef11432c421246019e25b6b744abbaed60/src/octoprint/util/comm.py#L432
            # There are a lot of values here. We only include some of them and then consider the rest "idle"
            opStateStr = self.OctoPrintPrinterObject.get_state_id()
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

            # Build the object and return.
            return {
                "State": state,
                "Error": errorStr_CanBeNone,
                "CurrentPrint":
                {
                    "Progress" : progress,
                    "DurationSec" : durationSec,
                    "TimeLeftSec" : timeLeftSec,
                    "FileName" : fileName
                }
            }

        except Exception as e:
            Sentry.ExceptionNoSend("GetCurrentJobStatus failed to get job status", e)
        return None


    # !! Platform Command Handler Interface Function !!
    # This must return the platform version as a string.
    def GetPlatformVersionStr(self):
        try:
            versionStr = str(__version__)
            if versionStr is None or len(versionStr) == 0:
                return "Unknown"
            return versionStr
        except Exception as e:
            Sentry.ExceptionNoSend("GetPlatformVersionStr failed to get OctoPrint version", e)
        return "Unknown"


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the pause and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecutePause(self, smartPause, suppressNotificationBool, disableHotendBool, disableBedBool, zLiftMm, retractFilamentMm, showSmartPausePopup):
        # Ensure we are printing, if not, respond with the common error.
        if self.OctoPrintPrinterObject.is_printing() is False:
            self.Logger.info("ExecutePause is not doing anything because theres' no print in progress..")
            return CommandResponse.Error(CommandHandler.c_CommandError_InvalidPrinterState, "Printer state is not printing.")

        # If we aren't using smart pause, just pause now.
        if smartPause is False:
            try:
                # Set the suppression if desired.
                if suppressNotificationBool:
                    SmartPause.Get().SetLastPauseNotificationSuppressionTimeNow()

                # Do the pause.
                self.OctoPrintPrinterObject.pause_print()

                # Return success.
                return CommandResponse.Success(None)

            except Exception as e:
                Sentry.Exception("Pause command failed to execute.", e)
                return CommandResponse.Error(500, "Failed to pause")

        # Otherwise, do the smart pause.
        try:
            # If this doesn't throw it's successful
            SmartPause.Get().DoSmartPause(disableHotendBool, disableBedBool, zLiftMm, retractFilamentMm, suppressNotificationBool)
        except Exception as e:
            Sentry.Exception("Failed to ExecutePause, SmartPause error.", e)
            return CommandResponse.Error(500, "Failed to pause")

        # On success, if we did a smart pause, send a notification to tell the user.
        if self.MainPluginImpl is not None and showSmartPausePopup and (disableBedBool or disableHotendBool or zLiftMm > 0 or retractFilamentMm > 0):
            self.MainPluginImpl.ShowSmartPausePopUpOnPortalLoad()

        # Success!
        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the resume and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteResume(self):
        # Ensure we are paused, if not, respond with the common error.
        if self.OctoPrintPrinterObject.is_paused() is False and self.OctoPrintPrinterObject.is_pausing() is False:
            self.Logger.info("ExecuteResume is not doing anything because the printer isn't paused..")
            return CommandResponse.Error(CommandHandler.c_CommandError_InvalidPrinterState, "Printer state is not paused.")

        # Do the resume.
        self.OctoPrintPrinterObject.resume_print()

        # Return success.
        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the cancel and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteCancel(self):
        # Ensure we are paused, if not, respond with the common error.
        state = self.OctoPrintPrinterObject.get_state_id()
        if state != "PRINTING" and state != "RESUMING" and state != "FINISHING" and state != "STARTING" and state != "PAUSED" and state != "PAUSING":
            self.Logger.info("ExecuteCancel is not doing anything because the printer printing.")
            return CommandResponse.Error(CommandHandler.c_CommandError_InvalidPrinterState, "Printer state is not printing.")

        # Do the cancel.
        self.OctoPrintPrinterObject.cancel_print()

        # Return success.
        return CommandResponse.Success(None)