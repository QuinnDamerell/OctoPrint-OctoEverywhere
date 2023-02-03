from octoeverywhere.sentry import Sentry

# Implements a common interface shared by OctoPrint and Moonraker.
class PrinterStateObject:

    def __init__(self, logger, octoPrintPrinterObject):
        self.Logger = logger
        self.OctoPrintPrinterObject = octoPrintPrinterObject
        self.NotificationHandler = None


    # Sets the notification handler for use. This is required.
    def SetNotificationHandler(self, handler):
        self.NotificationHandler = handler


    # ! Interface Function ! The entire interface must change if the function is changed.
    # This function will get the estimated time remaining for the current print.
    # Returns -1 if the estimate is unknown.
    def GetPrintTimeRemainingEstimateInSeconds(self):
        # Try to get the progress object from the current data. This is at least set by things like PrintTimeGenius and is more accurate.
        try:
            currentData = self.OctoPrintPrinterObject.get_current_data()
            if "progress" in currentData:
                if "printTimeLeft" in currentData["progress"]:
                    # When the print is just starting, the printTimeLeft will be None.
                    printTimeLeftSec = currentData["progress"]["printTimeLeft"]
                    if printTimeLeftSec is not None:
                        printTimeLeft = int(float(currentData["progress"]["printTimeLeft"]))
                        return printTimeLeft
        except Exception as e:
            Sentry.Exception("Failed to find progress object in printer current data.", e)

        # If that fails, try to use the default OctoPrint estimate.
        try:
            jobData = self.OctoPrintPrinterObject.get_current_job()
            if "estimatedPrintTime" in jobData:

                # When the print is first starting and there is no known time, this can be none.
                # In that case, return -1, unknown.
                if jobData["estimatedPrintTime"] is None:
                    return -1

                printTimeEstSec = int(jobData["estimatedPrintTime"])
                # Compute how long this print has been running and subtract
                # Sanity check the duration isn't longer than the ETA.
                currentDurationSec = int(self.NotificationHandler.GetCurrentDurationSecFloat())
                if currentDurationSec > printTimeEstSec:
                    return 0
                return printTimeEstSec - currentDurationSec
        except Exception as e:
            Sentry.Exception("Failed to find time estimate from OctoPrint. ", e)

        # We failed.
        return -1


    # ! Interface Function ! The entire interface must change if the function is changed.
    # Returns the current zoffset if known, otherwise -1.
    def GetCurrentZOffset(self):
        # Try to get the current value from the data.
        try:
            # We have seen in client logs sometimes this value doesn't exist,
            # and sometime it does, but it's just None.
            currentData = self.OctoPrintPrinterObject.get_current_data()
            if "currentZ" in currentData and currentData["currentZ"] is not None:
                currentZ = float(currentData["currentZ"])
                return currentZ
        except Exception as e:
            Sentry.Exception("Failed to find current z offset.", e)

        # Failed to find it.
        return -1


    # ! Interface Function ! The entire interface must change if the function is changed.
    # Returns True if the printing timers (notifications and gadget) should be running, which is only the printing state. (not even paused)
    # False if the printer state is anything else, which means they should stop.
    def ShouldPrintingTimersBeRunning(self):
        # Get the current state
        # States can be found here:
        # https://docs.octoprint.org/en/master/modules/printer.html#octoprint.printer.PrinterInterface.get_state_id
        # Note! The docs seem to be missing some states at the moment, like STATE_RESUMING, which can be found in comm.py
        state = self.OctoPrintPrinterObject.get_state_id()

        # Return if the state is printing or not.
        if state == "PRINTING" or state == "RESUMING" or state == "FINISHING":
            return True

        self.Logger.warn("ShouldPrintingTimersBeRunning is not in a printing state: "+str(state))
        return False


    # ! Interface Function ! The entire interface must change if the function is changed.
    # If called while the print state is "Printing", returns True if the print is currently in the warm-up phase. Otherwise False
    def IsPrintWarmingUp(self):
        # Using the current state, if the print time is None or 0, the print hasn't started because the system is warming up..
        # Using the get_current_data in this way is the same way the /api/job uses it.
        if self.OctoPrintPrinterObject is None:
            self.Logger.warn("IsPrintWarmingUp doesn't have a OctoPrint printer object.")
            return False

        # Get the current data.
        currentData = self.OctoPrintPrinterObject.get_current_data()
        if currentData is not None:
            progress = currentData["progress"]
            if progress is not None:
                printTime = progress["printTime"]
                if printTime is None or int(printTime) == 0:
                    return True

        # We aren't warming up.
        return False
