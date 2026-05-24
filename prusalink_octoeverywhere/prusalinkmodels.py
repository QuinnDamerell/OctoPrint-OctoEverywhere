import logging
import time
from typing import Any, Dict, Optional, Tuple, cast

from octoeverywhere.sentry import Sentry


class PrinterState:

    PRINT_STATUS_NONE       = None
    PRINT_STATUS_IDLE       = "idle"
    PRINT_STATUS_WARMINGUP  = "warmingup"
    PRINT_STATUS_PRINTING   = "printing"
    PRINT_STATUS_PAUSED     = "paused"
    PRINT_STATUS_RESUMING   = "resuming"
    PRINT_STATUS_COMPLETE   = "complete"
    PRINT_STATUS_CANCELLED  = "cancelled"
    PRINT_STATUS_ERROR      = "error"

    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger
        self.MostRecentPrintInfo = MostRecentPrintInfo()

        self.PrinterState:Optional[str] = None
        self.JobState:Optional[str] = None
        self.JobId:Optional[int] = None
        self.Progress:Optional[float] = None
        self.DurationSec:Optional[int] = None
        self.RemainingTimeSec:Optional[int] = None
        self.FileName:Optional[str] = None
        self.FileSizeBytes:Optional[int] = None
        self.EstFilamentUsedMm:Optional[int] = None
        self.EstFilamentWeightMg:Optional[int] = None
        self.TotalPrintTimeEstSec:Optional[int] = None
        self.HotendActual:Optional[float] = None
        self.HotendTarget:Optional[float] = None
        self.BedActual:Optional[float] = None
        self.BedTarget:Optional[float] = None
        self.AxisZ:Optional[float] = None
        self.CameraId:Optional[str] = None
        self.HasActiveCamera:Optional[bool] = None
        self.StatusMessage:Optional[str] = None
        self.ConnectMessage:Optional[str] = None


    def OnUpdate(self, status:Dict[str, Any], job:Optional[Dict[str, Any]], info:Optional[Dict[str, Any]]) -> None:
        printer = status.get("printer", {})
        if isinstance(printer, dict):
            printer = cast(Dict[str, Any], printer)
            self.PrinterState = self._GetStrOrNone(printer, "state", self.PrinterState)
            self.HotendActual = self._GetFloatOrNone(printer, "temp_nozzle", self.HotendActual)
            self.HotendTarget = self._GetFloatOrNone(printer, "target_nozzle", self.HotendTarget)
            self.BedActual = self._GetFloatOrNone(printer, "temp_bed", self.BedActual)
            self.BedTarget = self._GetFloatOrNone(printer, "target_bed", self.BedTarget)
            self.AxisZ = self._GetFloatOrNone(printer, "axis_z", self.AxisZ)

            statusPrinter = printer.get("status_printer", None)
            if isinstance(statusPrinter, dict):
                statusPrinter = cast(Dict[str, Any], statusPrinter)
                self.StatusMessage = self._GetStrOrNone(statusPrinter, "message", self.StatusMessage)
            statusConnect = printer.get("status_connect", None)
            if isinstance(statusConnect, dict):
                statusConnect = cast(Dict[str, Any], statusConnect)
                self.ConnectMessage = self._GetStrOrNone(statusConnect, "message", self.ConnectMessage)

        statusJob = status.get("job", None)
        if isinstance(statusJob, dict):
            self.JobId = self._GetIntOrNone(statusJob, "id", self.JobId)
            self.Progress = self._GetFloatOrNone(statusJob, "progress", self.Progress)
            self.DurationSec = self._GetIntOrNone(statusJob, "time_printing", self.DurationSec)
            self.RemainingTimeSec = self._GetIntOrNone(statusJob, "time_remaining", self.RemainingTimeSec)
        else:
            self.JobId = None
            self.Progress = None
            self.DurationSec = None
            self.RemainingTimeSec = None

        camera = status.get("camera", None)
        if isinstance(camera, dict):
            self.CameraId = self._GetStrOrNone(camera, "id", self.CameraId)

        if info is not None:
            self.HasActiveCamera = self._GetBoolOrNone(info, "active_camera", self.HasActiveCamera)

        if job is None:
            self.JobState = None
            self.FileName = None
            self.FileSizeBytes = None
            self.EstFilamentUsedMm = None
            self.EstFilamentWeightMg = None
            self.TotalPrintTimeEstSec = None
        else:
            self.JobId = self._GetIntOrNone(job, "id", self.JobId)
            self.JobState = self._GetStrOrNone(job, "state", self.JobState)
            self.Progress = self._GetFloatOrNone(job, "progress", self.Progress)
            self.DurationSec = self._GetIntOrNone(job, "time_printing", self.DurationSec)
            self.RemainingTimeSec = self._GetIntOrNone(job, "time_remaining", self.RemainingTimeSec)

            fileObj = job.get("file", None)
            if isinstance(fileObj, dict):
                fileObj = cast(Dict[str, Any], fileObj)
                self.FileName = self._GetStrOrNone(fileObj, "display_name", self.FileName)
                if self.FileName is None:
                    self.FileName = self._GetStrOrNone(fileObj, "name", self.FileName)
                self.FileSizeBytes = self._GetIntOrNone(fileObj, "size", self.FileSizeBytes)
                meta = fileObj.get("meta", None)
                if isinstance(meta, dict):
                    meta = cast(Dict[str, Any], meta)
                    self.TotalPrintTimeEstSec = self._GetIntOrNone(meta, "estimated_print_time", self.TotalPrintTimeEstSec)
                    if self.TotalPrintTimeEstSec is None:
                        self.TotalPrintTimeEstSec = self._GetIntOrNone(meta, "print_time", self.TotalPrintTimeEstSec)
                    filamentMm = self._GetFloatOrNone(meta, "filament used [mm]", None)
                    if filamentMm is not None:
                        self.EstFilamentUsedMm = int(filamentMm)
                    filamentG = self._GetFloatOrNone(meta, "filament used [g]", None)
                    if filamentG is not None:
                        self.EstFilamentWeightMg = int(filamentG * 1000)

        self.MostRecentPrintInfo.Update(self)


    def _GetStrOrNone(self, d:Dict[str, Any], key:str, default:Optional[str]=None) -> Optional[str]:
        v = d.get(key, None)
        if v is None:
            return default
        vStr = str(v)
        if len(vStr) == 0:
            return None
        return vStr


    def _GetIntOrNone(self, d:Dict[str, Any], key:str, default:Optional[int]=None) -> Optional[int]:
        v = d.get(key, None)
        if v is None:
            return default
        return int(v)


    def _GetFloatOrNone(self, d:Dict[str, Any], key:str, default:Optional[float]=None) -> Optional[float]:
        v = d.get(key, None)
        if v is None:
            return default
        return float(v)


    def _GetBoolOrNone(self, d:Dict[str, Any], key:str, default:Optional[bool]=None) -> Optional[bool]:
        v = d.get(key, None)
        if v is None:
            return default
        return bool(v)


    def GetMostRecentPrintInfo(self) -> "MostRecentPrintInfo":
        return self.MostRecentPrintInfo


    def GetCurrentStatus(self) -> Tuple[Optional[str], Optional[str]]:
        state = self.JobState
        if state is not None:
            state = state.upper()
            if state == "PRINTING":
                if self.PrinterState is not None and self.PrinterState.upper() == "BUSY":
                    return PrinterState.PRINT_STATUS_WARMINGUP, "Busy"
                return PrinterState.PRINT_STATUS_PRINTING, None
            if state == "PAUSED":
                return PrinterState.PRINT_STATUS_PAUSED, None
            if state == "FINISHED":
                return PrinterState.PRINT_STATUS_COMPLETE, None
            if state == "STOPPED":
                return PrinterState.PRINT_STATUS_CANCELLED, None
            if state == "ERROR":
                return PrinterState.PRINT_STATUS_ERROR, self._GetErrorMessage()

        if self.PrinterState is None:
            return None, None

        printerState = self.PrinterState.upper()
        if printerState == "PRINTING":
            return PrinterState.PRINT_STATUS_PRINTING, None
        if printerState == "PAUSED":
            return PrinterState.PRINT_STATUS_PAUSED, None
        if printerState == "FINISHED":
            return PrinterState.PRINT_STATUS_COMPLETE, None
        if printerState == "STOPPED":
            return PrinterState.PRINT_STATUS_CANCELLED, None
        if printerState == "ERROR":
            return PrinterState.PRINT_STATUS_ERROR, self._GetErrorMessage()
        if printerState == "ATTENTION":
            return PrinterState.PRINT_STATUS_ERROR, self._GetErrorMessage() or "Attention Needed"
        if printerState == "BUSY":
            return PrinterState.PRINT_STATUS_WARMINGUP, "Busy"
        if printerState == "IDLE" or printerState == "READY":
            return PrinterState.PRINT_STATUS_IDLE, None

        self.Logger.warning("Unknown Prusa Link printer state: %s job state: %s", self.PrinterState, self.JobState)
        return PrinterState.PRINT_STATUS_IDLE, None


    def _GetErrorMessage(self) -> Optional[str]:
        if self.StatusMessage is not None and self.StatusMessage.upper() != "OK":
            return self.StatusMessage
        if self.ConnectMessage is not None and self.ConnectMessage.upper() != "OK":
            return self.ConnectMessage
        return "Printer Error"


    def IsPrinting(self, includePausedAsPrinting:bool) -> bool:
        (status, _) = self.GetCurrentStatus()
        return PrinterState.IsPrintingState(status, includePausedAsPrinting)


    @staticmethod
    def IsPrintingState(status:Optional[str], includePausedAsPrinting:bool) -> bool:
        if status is None:
            return False
        if status == PrinterState.PRINT_STATUS_PRINTING or status == PrinterState.PRINT_STATUS_RESUMING:
            return True
        if includePausedAsPrinting and PrinterState.IsPausedState(status):
            return True
        return PrinterState.IsPrepareOrSlicingState(status)


    def IsPrepareOrSlicing(self) -> bool:
        (status, _) = self.GetCurrentStatus()
        return PrinterState.IsPrepareOrSlicingState(status)


    @staticmethod
    def IsPrepareOrSlicingState(status:Optional[str]) -> bool:
        return status == PrinterState.PRINT_STATUS_WARMINGUP


    def IsPaused(self) -> bool:
        (status, _) = self.GetCurrentStatus()
        return PrinterState.IsPausedState(status)


    @staticmethod
    def IsPausedState(status:Optional[str]) -> bool:
        return status == PrinterState.PRINT_STATUS_PAUSED


    def GetFileNameWithNoExtension(self) -> Optional[str]:
        return PrinterState.GetFileNameWithNoExtensionStatic(self.FileName)


    s_FixedUpFileNameCache:Dict[str, str] = {}
    @staticmethod
    def GetFileNameWithNoExtensionStatic(fileName:Optional[str]) -> Optional[str]:
        if fileName is None:
            return None
        cached = PrinterState.s_FixedUpFileNameCache.get(fileName, None)
        if cached is not None:
            return cached

        fileNameFixed = fileName
        try:
            pos = fileNameFixed.rfind(".")
            if pos != -1:
                fileNameFixed = fileNameFixed[:pos]
            fileNameFixed = fileNameFixed.replace("_", " ").strip().title()
        except Exception as e:
            Sentry.OnException("Prusa Link error in GetFileNameWithNoExtensionStatic", e)

        PrinterState.s_FixedUpFileNameCache[fileName] = fileNameFixed
        return fileNameFixed


    def GetPrintCookie(self) -> Optional[str]:
        if self.JobId is None or self.FileName is None or len(self.FileName) == 0:
            return None
        return f"{self.JobId}-{self.FileName}"


class MostRecentPrintInfo:

    def __init__(self) -> None:
        self.LastUpdateTimeSec:Optional[float] = None
        self.FileName:Optional[str] = None
        self.JobId:Optional[int] = None
        self.DurationSec:Optional[int] = None
        self.RemainingTimeSec:Optional[int] = None
        self.Progress:Optional[float] = None


    def Update(self, pState:PrinterState) -> None:
        if pState.JobId is None or pState.FileName is None or len(pState.FileName) == 0:
            return

        self.LastUpdateTimeSec = time.time()
        self.FileName = pState.FileName
        self.JobId = pState.JobId
        self.DurationSec = pState.DurationSec
        self.RemainingTimeSec = pState.RemainingTimeSec
        self.Progress = pState.Progress


    def GetFileNameWithNoExtension(self) -> Optional[str]:
        return PrinterState.GetFileNameWithNoExtensionStatic(self.FileName)


    def GetTimeRemainingSec(self) -> Optional[int]:
        return self.RemainingTimeSec
