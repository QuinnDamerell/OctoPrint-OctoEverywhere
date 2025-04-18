import logging
from typing import Any, Dict, Optional, Tuple

from octoprint.printer import PrinterInterface

from octoeverywhere.sentry import Sentry
from octoeverywhere.notificationshandler import NotificationsHandler
from octoeverywhere.interfaces import IPrinterStateReporter


# Implements a common interface shared by OctoPrint and Moonraker.
class PrinterStateObject(IPrinterStateReporter):

    def __init__(self, logger:logging.Logger, octoPrintPrinterObject:PrinterInterface):
        self.Logger = logger
        self.OctoPrintPrinterObject = octoPrintPrinterObject
        self.NotificationHandler:Optional[NotificationsHandler] = None


    # Sets the notification handler for use. This is required.
    def SetNotificationHandler(self, handler:NotificationsHandler) -> None:
        self.NotificationHandler = handler


    # ! Interface Function ! The entire interface must change if the function is changed.
    # This function will get the estimated time remaining for the current print.
    # Returns -1 if the estimate is unknown.
    def GetPrintTimeRemainingEstimateInSeconds(self) -> int:
        # Try to get the progress object from the current data. This is at least set by things like PrintTimeGenius and is more accurate.
        try:
            currentData = self.OctoPrintPrinterObject.get_current_data() #pyright: ignore[reportUnknownMemberType]
            if "progress" in currentData:
                if "printTimeLeft" in currentData["progress"]:
                    # When the print is just starting, the printTimeLeft will be None.
                    printTimeLeftSec = currentData["progress"]["printTimeLeft"]
                    if printTimeLeftSec is not None:
                        printTimeLeft = int(float(currentData["progress"]["printTimeLeft"]))
                        return printTimeLeft
        except Exception as e:
            Sentry.OnException("Failed to find progress object in printer current data.", e)

        # If that fails, try to use the default OctoPrint estimate.
        try:
            jobData = self.OctoPrintPrinterObject.get_current_job() #pyright: ignore[reportUnknownMemberType]
            if "estimatedPrintTime" in jobData:

                # When the print is first starting and there is no known time, this can be none.
                # In that case, return -1, unknown.
                if jobData["estimatedPrintTime"] is None:
                    return -1

                printTimeEstSec = int(jobData["estimatedPrintTime"])
                # Compute how long this print has been running and subtract
                # Sanity check the duration isn't longer than the ETA.
                currentDurationSec = 0
                if self.NotificationHandler is not None:
                    currentDurationSec = int(self.NotificationHandler.GetCurrentDurationSecFloat())
                else:
                    self.Logger.error("Notification handler is None, can't get current duration.")
                if currentDurationSec > printTimeEstSec:
                    return 0
                return printTimeEstSec - currentDurationSec
        except Exception as e:
            Sentry.OnException("Failed to find time estimate from OctoPrint. ", e)

        # We failed.
        return -1


    # ! Interface Function ! The entire interface must change if the function is changed.
    # If the printer is warming up, this value would be -1. The First Layer Notification logic depends upon this!
    # Returns the current zoffset if known, otherwise -1.
    def GetCurrentZOffsetMm(self) -> int:
        # Try to get the current value from the data.
        try:
            # Don't get the current zoffset until the print is running, since the tool could be at any
            # height before the print starts.
            if self.IsPrintWarmingUp():
                return -1

            # We have seen in client logs sometimes this value doesn't exist,
            # and sometime it does, but it's just None.
            currentData = self.OctoPrintPrinterObject.get_current_data() #pyright: ignore[reportUnknownMemberType]
            if "currentZ" in currentData and currentData["currentZ"] is not None:
                currentZ = int(currentData["currentZ"])
                return currentZ
        except Exception as e:
            Sentry.OnException("Failed to find current z offset.", e)

        # Failed to find it.
        return -1


    # Returns:
    #     (None, None) if the platform doesn't support layer info.
    #     (0,0) if the current layer is unknown.
    #     (currentLayer(int), totalLayers(int)) if the values are known.
    def GetCurrentLayerInfo(self) -> Tuple[Optional[int], Optional[int]]:
        # OctoPrint doesn't compute or track the layer height right now.
        return (None, None)


    # ! Interface Function ! The entire interface must change if the function is changed.
    # Returns True if the printing timers (notifications and gadget) should be running, which is only the printing state. (not even paused)
    # False if the printer state is anything else, which means they should stop.
    def ShouldPrintingTimersBeRunning(self) -> bool:
        # Get the current state
        # States can be found here:
        # https://docs.octoprint.org/en/master/modules/printer.html#octoprint.printer.PrinterInterface.get_state_id
        # Note! The docs seem to be missing some states at the moment, like STATE_RESUMING, which can be found in comm.py
        state = self.OctoPrintPrinterObject.get_state_id() #pyright: ignore[reportUnknownMemberType]

        # Return if the state is printing or not.
        if state == "PRINTING" or state == "RESUMING" or state == "FINISHING" or state == "STARTING":
            return True

        self.Logger.warning("ShouldPrintingTimersBeRunning is not in a printing state: "+str(state))
        return False


    # ! Interface Function ! The entire interface must change if the function is changed.
    # If called while the print state is "Printing", returns True if the print is currently in the warm-up phase. Otherwise False
    def IsPrintWarmingUp(self) -> bool:
        # Using the current state, if the print time is None or 0, the print hasn't started because the system is warming up..
        # Using the get_current_data in this way is the same way the /api/job uses it.
        currentData = self.OctoPrintPrinterObject.get_current_data() #pyright: ignore[reportUnknownMemberType]
        if currentData is not None:
            progress = currentData["progress"]
            if progress is not None:
                printTime = progress["printTime"]
                if printTime is None or int(printTime) == 0:
                    return True

        # We aren't warming up.
        return False


    # ! Interface Function ! The entire interface must change if the function is changed.
    # Returns the current hotend temp and bed temp as a float in celsius if they are available, otherwise None.
    def GetTemps(self) -> Tuple[Optional[float], Optional[float]]:
        # Get the current temps if possible.
        # Note there will be no objects in the dic if the printer isn't connected or in other cases.
        currentTemps = self.OctoPrintPrinterObject.get_current_temperatures() #pyright: ignore[reportUnknownMemberType]
        hotendActual = None
        bedActual = None
        if self._Exists(currentTemps, "tool0"):
            tool0 = currentTemps["tool0"]
            if self._Exists(tool0, "actual"):
                hotendActual = round(float(tool0["actual"]), 2)
        if self._Exists(currentTemps, "bed"):
            bed = currentTemps["bed"]
            if self._Exists(bed, "actual"):
                bedActual = round(float(bed["actual"]), 2)
        return (hotendActual, bedActual)


    # A helper for checking if things exist in dicts.
    def _Exists(self, dictObj:Dict[str, Any], key:str) -> bool:
        return key in dictObj and dictObj[key] is not None
