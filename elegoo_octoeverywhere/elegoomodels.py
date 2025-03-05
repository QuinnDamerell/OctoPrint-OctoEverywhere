import time
import logging
from typing import Union

from octoeverywhere.sentry import Sentry

# Keeps track of the current printer state locally, so we don't have to query the printer for it constantly.
class PrinterState:

    # There are the common printer status, these are also what we return to the service, so they MUST MATCH!
    PRINT_STATUS_NONE       = None
    PRINT_STATUS_IDLE      = "idle"
    PRINT_STATUS_WARMINGUP = "warmingup"
    PRINT_STATUS_PRINTING  = "printing"
    PRINT_STATUS_PAUSED    = "paused"
    PRINT_STATUS_RESUMING  = "resuming"
    PRINT_STATUS_COMPLETE  = "complete"
    PRINT_STATUS_CANCELLED = "cancelled"
    PRINT_STATUS_ERROR     = "error"


    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger
        self.MostRecentPrintInfo = MostRecentPrintInfo()
        # We only parse out what we currently use.
        # We try to use names like the vars in the json.
        self.CurrentStatus:int = None
        self.PrintInfoStatus:int = None
        self.CurrentLayer:int = None
        self.TotalLayer:int = None
        self.FileName:str = None
        # The duration of the print thus far
        self.DurationSec:int = None
        self.TotalPrintTimeEstSec:int = None
        # Expressed 100.0 -> 0.0
        self.Progress:float = None
        self.TaskId:str = None
        self.HotendActual:float = None
        self.HotendTarget:float = None
        self.BedActual:float = None
        self.BedTarget:float = None
        self.ChamberActual:float = None
        self.ChamberTarget:float = None


    # Called when there's a new print message from the printer.
    def OnUpdate(self, state:dict) -> None:
        # Each update contains all of the data, so we will either get the new value or we will clear it.
        currentStateArray = state.get("CurrentStatus", None)
        if currentStateArray is None or len(currentStateArray) == 0:
            self.CurrentStatus = None
        else:
            self.CurrentStatus = int(currentStateArray[0])

        # Get all of the values from print info.
        printInfo = state.get("PrintInfo", {})
        self.PrintInfoStatus = self._GetIntOrNone(printInfo, "Status")
        self.FileName = self._GetStrOrNone(printInfo, "Filename") # empty string becomes None
        self.TaskId =  self._GetStrOrNone(printInfo, "TaskId") # empty string becomes None
        self.CurrentLayer = self._GetIntOrNone(printInfo, "CurrentLayer")
        self.TotalLayer = self._GetIntOrNone(printInfo, "TotalLayer")
        self.DurationSec = self._GetIntOrNone(printInfo, "CurrentTicks")
        self.TotalPrintTimeEstSec = self._GetIntOrNone(printInfo, "TotalTicks")
        self.Progress = self._GetFloatOrNone(printInfo, "Progress")
        self.HotendActual = self._GetFloatOrNone(state, "TempOfNozzle")
        self.HotendTarget = self._GetFloatOrNone(state, "TempTargetNozzle")
        self.BedActual = self._GetFloatOrNone(state, "TempOfHotbed")
        self.BedTarget = self._GetFloatOrNone(state, "TempTargetHotbed")
        self.ChamberActual = self._GetFloatOrNone(state, "TempOfBox")
        self.ChamberTarget = self._GetFloatOrNone(state, "TempTargetBox")

        # Update the last print info
        self.MostRecentPrintInfo.Update(self)


    def _GetIntOrNone(self, d:dict, key:str) -> int:
        v = d.get(key, None)
        if v is None:
            return None
        return int(v)


    def _GetFloatOrNone(self, d:dict, key:str) -> int:
        v = d.get(key, None)
        if v is None:
            return None
        return int(v)


    def _GetStrOrNone(self, d:dict, key:str) -> str:
        v = d.get(key, None)
        if v is None or len(v) == 0:
            return None
        return str(v)


    # Returns cached info about the most recent print job (or the current job)
    def GetMostRecentPrintInfo(self) -> "MostRecentPrintInfo":
        return self.MostRecentPrintInfo


    # This is the one place the printer status is determined.
    # The reason we have it here is because there are lots of places that need to know,
    # and the status can be tricky.
    # The substring (it not None) will be shown the user as a sub state, so make it formatted correctly.
    #
    # Returns the main status with must be one of the string bellow, and an optional sub status.
    #   None      - The state is unknown or disconnected.
    #   idle      - The printer is ready to print
    #   warmingup - The printer is "printing" but in the warmup state.
    #   printing  - Active Print
    #   paused    - A print is paused
    #   resuming  - A print is being resumed
    #   complete  - A print is done
    #   cancelled - Similar to complete, where the job is still loaded, but it stopped printed because it was canceled.
    #   error     - The printer is in an error state.
    def GetCurrentStatus(self) -> Union[str, str]:
        if self.CurrentStatus is None or self.PrintInfoStatus is None:
            return None, None
        # We mostly just use the print state, which is what the frontend does.
        # We only use the current status for a few things the print state doesn't include.
        if self.CurrentStatus == 9:
            return PrinterState.PRINT_STATUS_IDLE, "Homing"
        # This is taken from the Elegoo frontend
        # return t.STATUS_LOADING = [0, 1, 15, 16, 18, 19, 20, 21],
        # t.STATUS_WAIT = [0],
        # t.STATUS_STOPPED = [8, 14],
        # t.STATUS_STOPPING = [1],
        # t.STATUS_COMPLETE = [9],
        # t.STATUS_SUSPENDING = [5],
        # t.STATUS_SUSPENDED = [6],
        # t.STATUS_PRINTING = [13],
        # t.STATUS_FILE_DETECTION = [10],
        # t.STATUS_RECOVERY = [12],
        #
        # There are some hints in this /assets/i18n/network-en.json file as well, it's a translation file with strings of the states.
        s = self.PrintInfoStatus
        if s == 0:
            return PrinterState.PRINT_STATUS_IDLE, None
        if s == 8 or s == 14:
            return PrinterState.PRINT_STATUS_CANCELLED, None
        if s == 1:
            # This is fired during warmup and cancelling, so we can't report canceling or we will
            # fire incorrect notifications and show the incorrect state.
            return PrinterState.PRINT_STATUS_WARMINGUP, None
        if s == 9:
            return PrinterState.PRINT_STATUS_COMPLETE, None
        if s == 5:
            return PrinterState.PRINT_STATUS_PAUSED, "Pausing"
        if s == 6:
            return PrinterState.PRINT_STATUS_PAUSED, None
        if s == 13:
            return PrinterState.PRINT_STATUS_PRINTING, None
        if s == 20:
            return PrinterState.PRINT_STATUS_WARMINGUP, "Bed Leveling"
        if s == 7:
            # TODO - I'm not sure exactly what this state is, we saw it during the print cancel.
            # But if it's a non printing state, the notification manager will think the printer went idle nad started a print again.
            return PrinterState.PRINT_STATUS_PRINTING, None
        if s in [0, 1, 15, 16, 18, 19, 21]:
            return PrinterState.PRINT_STATUS_WARMINGUP, None
        # We don't know what STATUS_FILE_DETECTION and STATUS_RECOVERY are, so we just ignore them.
        # Recovery might be a power loss recover
        # We use a warning here, since we want to add these, as they might send false notifications.
        self.Logger.warning(f"Unknown Elegoo print currentStatus: {self.CurrentStatus} ps: {self.PrintInfoStatus}")
        return PrinterState.PRINT_STATUS_IDLE, None


    # Since there's a lot to consider to figure out if a print is running, this one function acts as common logic across the plugin.
    def IsPrinting(self, includePausedAsPrinting:bool) -> bool:
        (status, _) = self.GetCurrentStatus()
        return PrinterState.IsPrintingState(status, includePausedAsPrinting)


    # We use this common method since "is this a printing state?" is complicated and we can to keep all of the logic common in the plugin
    @staticmethod
    def IsPrintingState(status:str, includePausedAsPrinting:bool) -> bool:
        if status is None:
            return False
        if status == PrinterState.PRINT_STATUS_PRINTING or status == PrinterState.PRINT_STATUS_RESUMING:
            return True
        if includePausedAsPrinting:
            if PrinterState.IsPausedState(status):
                return True
        return PrinterState.IsPrepareOrSlicingState(status)


    # We use this common method to keep all of the logic common in the plugin
    def IsPrepareOrSlicing(self) -> bool:
        (status, _) = self.GetCurrentStatus()
        return PrinterState.IsPrepareOrSlicingState(status)


    # We use this common method to keep all of the logic common in the plugin
    @staticmethod
    def IsPrepareOrSlicingState(status:str) -> bool:
        if status is None:
            return False
        return status == PrinterState.PRINT_STATUS_WARMINGUP


    # This one function acts as common logic across the plugin.
    def IsPaused(self) -> bool:
        (status, _) = self.GetCurrentStatus()
        return PrinterState.IsPausedState(status)


    # This one function acts as common logic across the plugin.
    @staticmethod
    def IsPausedState(status:str) -> bool:
        if status is None:
            return False
        return status == PrinterState.PRINT_STATUS_PAUSED


    # Returns a time reaming in seconds.
    # Returns null if the time is unknown.
    def GetTimeRemainingSec(self) -> int:
        return PrinterState.GetTimeRemainingSecStatic(self.DurationSec, self.TotalPrintTimeEstSec)


    @staticmethod
    def GetTimeRemainingSecStatic(durationSec:int, totalPrintTimeSec:int) -> int:
        if durationSec is None or totalPrintTimeSec is None:
            return None
        # Compute the time based on when the value last updated.
        return int(totalPrintTimeSec - durationSec)


    # If there is a file name, this returns it without the extension.
    def GetFileNameWithNoExtension(self):
        return PrinterState.GetFileNameWithNoExtensionStatic(self.FileName)


    # If there is a file name, this returns it without the extension.
    # For Elegoo, we also do more cleanup, since the Elegoo slicer has as common file name pattern.
    s_FixedUpFileNameCache = {}
    @staticmethod
    def GetFileNameWithNoExtensionStatic(fileName:str):
        if fileName is None:
            return None
        # We cache the fixed up file names, since we don't want to do this all of the time.
        fn = PrinterState.s_FixedUpFileNameCache.get(fileName, None)
        if fn is not None:
            return fn

        # All of the formatting below is best effort.
        # Example of the filename from the Elegoo slicer "ECC_0.4_elegoo_cube_PLA0.2_28m26s.gcode"
        fileNameLower = fileName.lower()
        try:
            # We do some cleanup on the file name, since the Elegoo slicer has a common pattern.
            # Try to find the nozzle size, which the file name is after.
            # It should look like "0.4_" or "0.6_", etc.
            startOfName = fileNameLower.find("0.")
            if startOfName != -1 and startOfName + 3 < len(fileNameLower):
                # Move past the 0.
                startOfName += 2
                if (str(fileNameLower[startOfName])).isdigit() and fileNameLower[startOfName + 1] == "_":
                    startOfName += 2
                    fileNameLower = fileNameLower[startOfName:]

            # Try to remove the info after the file name added by the slicer.
            # Try to find the layer height, which again should be like "0.2_" or "0.3_", etc.
            endOfName = fileNameLower.find("0.")
            if endOfName != -1 and endOfName + 3 < len(fileNameLower):
                # Move past the 0.
                endOfName += 2
                if (str(fileNameLower[endOfName])).isdigit() and fileNameLower[endOfName + 1] == "_":
                    # If we found it, find the _ before it, and trim
                    pos = fileNameLower.rfind("_", 0, endOfName)
                    if pos != -1:
                        fileNameLower = fileNameLower[:pos]

            # Remove the extension if there is one.
            pos = fileNameLower.rfind(".")
            if pos != -1:
                fileNameLower = fileNameLower[:pos]

            # Remove the underscores, since they are common in the file names.
            fileNameLower = fileNameLower.replace("_", " ")

            # Capitalize the string to clean it up
            fileNameLower = fileNameLower.title()
        except Exception as e:
            Sentry.Exception("Error in GetFileNameWithNoExtensionStatic", e)

        # Set the result into the cache and return it.
        PrinterState.s_FixedUpFileNameCache[fileName] = fileNameLower
        return fileNameLower


    # Returns a unique string for this print.
    # This string should be as unique as possible, but always the same for the same print.
    # If there is no active print, this should return None!
    # See details in NotificationHandler._RecoverOrRestForNewPrint
    def GetPrintCookie(self) -> str:
        # If there is no task id or file name, we shouldn't make a cookie.
        if self.TaskId is None or len(self.TaskId) == 0 or self.FileName is None or len(self.FileName) == 0:
            return None
        # It looks like task id is unique for each print, so we use that, plus the file name for debugging.
        # Don't use the formatted file name, since it can have spaces.
        return f"{self.TaskId}-{self.FileName}"


    # # If the printer is in an error state, this tries to return the type, if known.
    # # If the printer is not in an error state, None is returned.
    # def GetPrinterError(self) -> BambuPrintErrors:
    #     # If there is a printer error, this is not 0
    #     if self.print_error is None or self.print_error == 0:
    #         return None

    #     # Oddly there are some errors that aren't errors? And the printer might sit in them while printing.
    #     # We ignore these. We also use the direct int values, so we don't have to build the hex string all of the time.
    #     # These error codes are in https://e.bambulab.com/query.php?lang=en, but have empty strings.
    #     # Hex: 05008030, 03008012, 0500C011
    #     if self.print_error == 83918896 or self.print_error == 50364434 or self.print_error == 83935249:
    #         return None

    #     # This state is when the user is loading filament, and the printer is asking them to push it in.
    #     # This isn't an error.
    #     if self.print_error == 134184967:
    #         return None

    #     # There's a full list of errors here, we only care about some of them
    #     # https://e.bambulab.com/query.php?lang=en
    #     # We format the error into a hex the same way the are on the page, to make it easier.
    #     # NOTE SOME ERRORS HAVE MULTIPLE VALUES, SO GET THEM ALL!
    #     # They have different values for the different AMS slots
    #     h = hex(self.print_error)[2:].rjust(8, '0')
    #     errorMap = {
    #         "07008011": BambuPrintErrors.FilamentRunOut,
    #         "07018011": BambuPrintErrors.FilamentRunOut,
    #         "07028011": BambuPrintErrors.FilamentRunOut,
    #         "07038011": BambuPrintErrors.FilamentRunOut,
    #         "07FF8011": BambuPrintErrors.FilamentRunOut,
    #     }
    #     return errorMap.get(h, BambuPrintErrors.Unknown)


# Tracks the printer attributes
class PrinterAttributes:

    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger
        self.HasLoggedPrinterVersion = False
        # We only parse out what we currently use.
        self.MainboardId:str = None


    # Called when there's a new print message from the printer.
    def OnUpdate(self, msg:dict) -> None:
        self.MainboardId = msg.get("MainboardID", None)
        # if self.HasLoggedPrinterVersion is False:
        #     self.HasLoggedPrinterVersion = True
        #     self.Logger.info(f"Printer Version: {self.PrinterName}, CPU: {self.Cpu}, Project: {self.ProjectName} Hardware: {self.HardwareVersion}, Software: {self.SoftwareVersion}, Serial: {self.SerialNumber}")


# Captures the info for the most recent print job (or current job), since it's cleared really quickly after completion or a failure.
class MostRecentPrintInfo:

    def __init__(self) -> None:
        self.LastUpdateTimeSec:int = None
        self.FileName:str = None
        self.TaskId:str = None
        self.DurationSec:int = None
        self.TotalPrintTimeEstSec:int = None
        self.Progress:float = None
        self.CurrentLayer:int = None
        self.TotalLayer:int = None


    def Update(self, pState:"PrinterState") -> None:
        # This is tricky, because for some of the values 0 is a valid value but it's also the default when there's no print info.
        # So, we will determine if there's valid print info, and then grab the vars if so.
        # Right now we only check these, because the other values might come from teh slicer, and might not exist if a different slicer is used.
        hasPrintInfo = (pState.FileName is not None and len(pState.FileName) > 0
                        and pState.TaskId is not None and len(pState.TaskId) > 0)
        if hasPrintInfo is False:
            return

        # We know we have valid print info, so we will update the values.
        self.LastUpdateTimeSec = time.time()
        self.FileName = pState.FileName
        self.TaskId = pState.TaskId
        # This for example is tricky, because the "no print info" value is 0, but 0 is also a valid value at the start of the print.
        self.DurationSec = pState.DurationSec
        self.TotalPrintTimeEstSec = pState.TotalPrintTimeEstSec
        self.Progress = pState.Progress
        # Same here, 0 is a valid value.
        self.CurrentLayer = pState.CurrentLayer
        self.TotalLayer = pState.TotalLayer


    def GetFileNameWithNoExtension(self):
        return PrinterState.GetFileNameWithNoExtensionStatic(self.FileName)


    # Returns a time reaming in seconds.
    # Returns null if the time is unknown.
    def GetTimeRemainingSec(self) -> int:
        return PrinterState.GetTimeRemainingSecStatic(self.DurationSec, self.TotalPrintTimeEstSec)
