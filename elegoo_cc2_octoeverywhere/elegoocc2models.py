import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from octoeverywhere.sentry import Sentry


class FileInfo:

    def __init__(self, logger:logging.Logger, fileInfo:Dict[str, Any]) -> None:
        self.FileNameWithPath:str = str(fileInfo.get("name", fileInfo.get("filename", "Unknown")))
        folderIndex = self.FileNameWithPath.rfind("/")
        self.FileName = self.FileNameWithPath[folderIndex + 1:] if folderIndex != -1 else self.FileNameWithPath
        self.FileNameLower = self.FileName.lower()

        self.CreateTimeSec:Optional[int] = self._GetInt(fileInfo, "create_time")
        self.TotalLayers:Optional[int] = self._GetInt(fileInfo, "layer")
        if self.TotalLayers is None:
            self.TotalLayers = self._GetInt(fileInfo, "TotalLayers")
        self.FileSizeKb:Optional[int] = None
        fileSizeBytes = self._GetInt(fileInfo, "size")
        if fileSizeBytes is None:
            fileSizeBytes = self._GetInt(fileInfo, "FileSize")
        if fileSizeBytes is not None:
            self.FileSizeKb = int(fileSizeBytes / 1024)

        self.EstPrintTimeSec:Optional[int] = self._GetInt(fileInfo, "print_time")
        self.EstFilamentWeightMg:Optional[int] = None
        filamentUsed = self._GetFloat(fileInfo, "total_filament_used")
        if filamentUsed is not None:
            # The CC2 metadata value appears to be grams in stock firmware examples.
            self.EstFilamentWeightMg = int(filamentUsed * 1000)


    def UpdateExtraFileInfo(self, fileInfo:Dict[str, Any]) -> None:
        totalLayers = self._GetInt(fileInfo, "layer")
        if totalLayers is None:
            totalLayers = self._GetInt(fileInfo, "TotalLayers")
        if totalLayers is not None:
            self.TotalLayers = totalLayers

        estPrintTimeSec = self._GetInt(fileInfo, "print_time")
        if estPrintTimeSec is not None:
            self.EstPrintTimeSec = estPrintTimeSec

        filamentUsed = self._GetFloat(fileInfo, "total_filament_used")
        if filamentUsed is not None:
            self.EstFilamentWeightMg = int(filamentUsed * 1000)


    def HasExtraFileInfo(self) -> bool:
        return self.EstPrintTimeSec is not None or self.EstFilamentWeightMg is not None or self.TotalLayers is not None


    def _GetInt(self, d:Dict[str, Any], key:str) -> Optional[int]:
        v = d.get(key, None)
        if v is None:
            return None
        return int(v)


    def _GetFloat(self, d:Dict[str, Any], key:str) -> Optional[float]:
        v = d.get(key, None)
        if v is None:
            return None
        return float(v)


class PrinterState:

    # These are the common printer status strings returned to the service.
    PRINT_STATUS_NONE       = None
    PRINT_STATUS_IDLE       = "idle"
    PRINT_STATUS_WARMINGUP  = "warmingup"
    PRINT_STATUS_PRINTING   = "printing"
    PRINT_STATUS_PAUSED     = "paused"
    PRINT_STATUS_RESUMING   = "resuming"
    PRINT_STATUS_COMPLETE   = "complete"
    PRINT_STATUS_CANCELLED  = "cancelled"
    PRINT_STATUS_ERROR      = "error"

    # Main machine status values.
    MACHINE_INITIALIZING = 0
    MACHINE_IDLE = 1
    MACHINE_PRINTING = 2
    MACHINE_AUTO_LEVELING = 5
    MACHINE_HOMING = 10
    MACHINE_EMERGENCY_STOP = 14

    # Printing sub-status values.
    SUB_EXTRUDER_PREHEATING = 1045
    SUB_EXTRUDER_PREHEATING_2 = 1096
    SUB_BED_PREHEATING = 1405
    SUB_BED_PREHEATING_2 = 1906
    SUB_PRINTING = 2075
    SUB_PRINTING_COMPLETED = 2077
    SUB_RESUMING = 2401
    SUB_RESUMING_COMPLETED = 2402
    SUB_PAUSING = 2501
    SUB_PAUSED = 2502
    SUB_PAUSED_2 = 2505
    SUB_STOPPING = 2503
    SUB_STOPPED = 2504
    SUB_HOMING = 2801
    SUB_AUTO_LEVELING = 2901

    SubStatusMap = {
        SUB_EXTRUDER_PREHEATING: "Heating Hotend",
        SUB_EXTRUDER_PREHEATING_2: "Heating Hotend",
        SUB_BED_PREHEATING: "Bed Preheating",
        SUB_BED_PREHEATING_2: "Bed Preheating",
        SUB_PRINTING_COMPLETED: "Print Complete",
        SUB_RESUMING: "Resuming",
        SUB_RESUMING_COMPLETED: "Resuming",
        SUB_PAUSING: "Pausing",
        SUB_PAUSED: "Paused",
        SUB_PAUSED_2: "Paused",
        SUB_STOPPING: "Stopping",
        SUB_STOPPED: "Stopped",
        SUB_HOMING: "Homing",
        SUB_AUTO_LEVELING: "Bed Leveling",
    }


    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger
        self.MostRecentPrintInfo = MostRecentPrintInfo()

        self.MachineStatus:Optional[int] = None
        self.SubStatus:Optional[int] = None
        self.ExceptionStatus:List[Any] = []
        self.FileName:Optional[str] = None
        self.TaskId:Optional[str] = None
        self.CurrentLayer:Optional[int] = None
        self.TotalLayer:Optional[int] = None
        self.DurationSec:Optional[int] = None
        self.TotalPrintTimeEstSec:Optional[int] = None
        self.RemainingTimeSec:Optional[int] = None
        self.Progress:Optional[float] = None
        self.HotendActual:Optional[float] = None
        self.HotendTarget:Optional[float] = None
        self.BedActual:Optional[float] = None
        self.BedTarget:Optional[float] = None
        self.ChamberActual:Optional[float] = None
        self.ChamberTarget:Optional[float] = None
        self.ChamberLightOn:Optional[bool] = None


    def OnUpdate(self, state:Dict[str, Any]) -> None:
        machineStatus = state.get("machine_status", {})
        if isinstance(machineStatus, dict):
            self.MachineStatus = self._GetIntOrNone(machineStatus, "status", self.MachineStatus)
            self.SubStatus = self._GetIntOrNone(machineStatus, "sub_status", self.SubStatus)
            exceptionStatus = machineStatus.get("exception_status", None)
            if isinstance(exceptionStatus, list):
                self.ExceptionStatus = exceptionStatus
            self.Progress = self._GetFloatOrNone(machineStatus, "progress", self.Progress)

        printStatus = state.get("print_status", {})
        if isinstance(printStatus, dict):
            self.FileName = self._GetStrOrNone(printStatus, "filename", self.FileName)
            self.TaskId = self._GetStrOrNone(printStatus, "uuid", self.TaskId)
            self.CurrentLayer = self._GetIntOrNone(printStatus, "current_layer", self.CurrentLayer)
            self.TotalLayer = self._GetIntOrNone(printStatus, "total_layer", self.TotalLayer)
            self.DurationSec = self._GetIntOrNone(printStatus, "print_duration", self.DurationSec)
            self.TotalPrintTimeEstSec = self._GetIntOrNone(printStatus, "total_duration", self.TotalPrintTimeEstSec)
            self.RemainingTimeSec = self._GetIntOrNone(printStatus, "remaining_time_sec", self.RemainingTimeSec)
            self.Progress = self._GetFloatOrNone(printStatus, "progress", self.Progress)

        extruder = state.get("extruder", {})
        if isinstance(extruder, dict):
            self.HotendActual = self._GetFloatOrNone(extruder, "temperature", self.HotendActual)
            self.HotendTarget = self._GetFloatOrNone(extruder, "target", self.HotendTarget)

        heaterBed = state.get("heater_bed", {})
        if isinstance(heaterBed, dict):
            self.BedActual = self._GetFloatOrNone(heaterBed, "temperature", self.BedActual)
            self.BedTarget = self._GetFloatOrNone(heaterBed, "target", self.BedTarget)

        chamber = state.get("ztemperature_sensor", state.get("chamber", {}))
        if isinstance(chamber, dict):
            self.ChamberActual = self._GetFloatOrNone(chamber, "temperature", self.ChamberActual)
            self.ChamberTarget = self._GetFloatOrNone(chamber, "target", self.ChamberTarget)

        led = state.get("led", {})
        if isinstance(led, dict):
            ledStatus = led.get("status", None)
            if ledStatus is not None:
                self.ChamberLightOn = int(ledStatus) > 0

        self.MostRecentPrintInfo.Update(self)


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


    def _GetStrOrNone(self, d:Dict[str, Any], key:str, default:Optional[str]=None) -> Optional[str]:
        v = d.get(key, None)
        if v is None:
            return default
        vStr = str(v)
        if len(vStr) == 0:
            return None
        return vStr


    def GetMostRecentPrintInfo(self) -> "MostRecentPrintInfo":
        return self.MostRecentPrintInfo


    def GetCurrentStatus(self) -> Tuple[Optional[str], Optional[str]]:
        if len(self.ExceptionStatus) > 0:
            return PrinterState.PRINT_STATUS_ERROR, "Printer Error"

        if self.MachineStatus is None:
            return None, None

        if self.MachineStatus == PrinterState.MACHINE_EMERGENCY_STOP:
            return PrinterState.PRINT_STATUS_ERROR, "Emergency Stop"
        if self.MachineStatus == PrinterState.MACHINE_INITIALIZING:
            return PrinterState.PRINT_STATUS_IDLE, "Initializing"
        if self.MachineStatus == PrinterState.MACHINE_IDLE:
            return PrinterState.PRINT_STATUS_IDLE, None
        if self.MachineStatus == PrinterState.MACHINE_HOMING:
            return PrinterState.PRINT_STATUS_IDLE, "Homing"
        if self.MachineStatus == PrinterState.MACHINE_AUTO_LEVELING:
            return PrinterState.PRINT_STATUS_WARMINGUP, "Bed Leveling"

        if self.MachineStatus == PrinterState.MACHINE_PRINTING:
            subState = self.SubStatus
            subStateStr = PrinterState.SubStatusMap.get(subState, None)
            if subState in [
                PrinterState.SUB_EXTRUDER_PREHEATING,
                PrinterState.SUB_EXTRUDER_PREHEATING_2,
                PrinterState.SUB_BED_PREHEATING,
                PrinterState.SUB_BED_PREHEATING_2,
                PrinterState.SUB_HOMING,
                PrinterState.SUB_AUTO_LEVELING,
            ]:
                return PrinterState.PRINT_STATUS_WARMINGUP, subStateStr
            if subState in [PrinterState.SUB_PAUSING, PrinterState.SUB_PAUSED, PrinterState.SUB_PAUSED_2]:
                return PrinterState.PRINT_STATUS_PAUSED, subStateStr
            if subState in [PrinterState.SUB_RESUMING, PrinterState.SUB_RESUMING_COMPLETED]:
                return PrinterState.PRINT_STATUS_RESUMING, subStateStr
            if subState == PrinterState.SUB_PRINTING_COMPLETED:
                return PrinterState.PRINT_STATUS_COMPLETE, None
            if subState in [PrinterState.SUB_STOPPING, PrinterState.SUB_STOPPED]:
                return PrinterState.PRINT_STATUS_CANCELLED, subStateStr
            return PrinterState.PRINT_STATUS_PRINTING, subStateStr

        self.Logger.warning(f"Unknown Elegoo CC2 machine status: {self.MachineStatus} sub status: {self.SubStatus}")
        return PrinterState.PRINT_STATUS_IDLE, None


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


    def GetTimeRemainingSec(self) -> Optional[int]:
        if self.RemainingTimeSec is not None:
            return self.RemainingTimeSec
        return PrinterState.GetTimeRemainingSecStatic(self.DurationSec, self.TotalPrintTimeEstSec)


    @staticmethod
    def GetTimeRemainingSecStatic(durationSec:Optional[int], totalPrintTimeSec:Optional[int]) -> Optional[int]:
        if durationSec is None or totalPrintTimeSec is None:
            return None
        return max(0, int(totalPrintTimeSec - durationSec))


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
            Sentry.OnException("Elegoo CC2 error in GetFileNameWithNoExtensionStatic", e)

        PrinterState.s_FixedUpFileNameCache[fileName] = fileNameFixed
        return fileNameFixed


    def GetPrintCookie(self) -> Optional[str]:
        if self.TaskId is None or len(self.TaskId) == 0 or self.FileName is None or len(self.FileName) == 0:
            return None
        return f"{self.TaskId}-{self.FileName}"


class PrinterAttributes:

    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger
        self.Hostname:Optional[str] = None
        self.MachineModel:Optional[str] = None
        self.SerialNumber:Optional[str] = None
        self.Ip:Optional[str] = None
        self.Mac:Optional[str] = None
        self.ProtocolVersion:Optional[str] = None
        self.HardwareVersion:Optional[str] = None
        self.OtaVersion:Optional[str] = None
        self.McuVersion:Optional[str] = None
        self.SocVersion:Optional[str] = None


    def OnUpdate(self, msg:Dict[str, Any]) -> None:
        self.Hostname = msg.get("hostname", self.Hostname)
        self.MachineModel = msg.get("machine_model", self.MachineModel)
        self.SerialNumber = msg.get("sn", self.SerialNumber)
        self.Ip = msg.get("ip", self.Ip)
        self.Mac = msg.get("mac", self.Mac)
        self.ProtocolVersion = msg.get("protocol_version", self.ProtocolVersion)
        self.HardwareVersion = msg.get("hardware_version", self.HardwareVersion)
        softwareVersion = msg.get("software_version", None)
        if isinstance(softwareVersion, dict):
            self.OtaVersion = softwareVersion.get("ota_version", self.OtaVersion)
            self.McuVersion = softwareVersion.get("mcu_version", self.McuVersion)
            self.SocVersion = softwareVersion.get("soc_version", self.SocVersion)


    def GetVersionString(self) -> str:
        parts:List[str] = []
        if self.MachineModel is not None:
            parts.append(self.MachineModel)
        if self.OtaVersion is not None:
            parts.append(self.OtaVersion)
        if self.HardwareVersion is not None:
            parts.append(self.HardwareVersion)
        if len(parts) == 0:
            return "Elegoo-CC2"
        return "-".join(parts)


class MostRecentPrintInfo:

    def __init__(self) -> None:
        self.LastUpdateTimeSec:Optional[float] = None
        self.FileName:Optional[str] = None
        self.TaskId:Optional[str] = None
        self.DurationSec:Optional[int] = None
        self.TotalPrintTimeEstSec:Optional[int] = None
        self.RemainingTimeSec:Optional[int] = None
        self.Progress:Optional[float] = None
        self.CurrentLayer:Optional[int] = None
        self.TotalLayer:Optional[int] = None


    def Update(self, pState:PrinterState) -> None:
        hasPrintInfo = (pState.FileName is not None and len(pState.FileName) > 0
                        and pState.TaskId is not None and len(pState.TaskId) > 0)
        if hasPrintInfo is False:
            return

        self.LastUpdateTimeSec = time.time()
        self.FileName = pState.FileName
        self.TaskId = pState.TaskId
        self.DurationSec = pState.DurationSec
        self.TotalPrintTimeEstSec = pState.TotalPrintTimeEstSec
        self.RemainingTimeSec = pState.RemainingTimeSec
        self.Progress = pState.Progress
        self.CurrentLayer = pState.CurrentLayer
        self.TotalLayer = pState.TotalLayer


    def GetFileNameWithNoExtension(self) -> Optional[str]:
        return PrinterState.GetFileNameWithNoExtensionStatic(self.FileName)


    def GetTimeRemainingSec(self) -> Optional[int]:
        if self.RemainingTimeSec is not None:
            return self.RemainingTimeSec
        return PrinterState.GetTimeRemainingSecStatic(self.DurationSec, self.TotalPrintTimeEstSec)
