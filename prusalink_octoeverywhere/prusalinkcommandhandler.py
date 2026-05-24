import logging
from typing import Any, Dict, List, Optional, Union

from octoeverywhere.commandhandler import CommandHandler, CommandResponse
from octoeverywhere.interfaces import IPlatformCommandHandler

from .prusalinkclient import PrusaLinkClient
from .prusalinkmodels import PrinterState


class PrusaLinkCommandHandler(IPlatformCommandHandler):

    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger


    def GetCurrentJobStatus(self) -> Union[int, None, Dict[str, Any]]:
        printerState = PrusaLinkClient.Get().GetState()
        if printerState is None:
            return None

        (state, subState_CanBeNone) = printerState.GetCurrentStatus()
        errorStr_CanBeNone = None
        if state == PrinterState.PRINT_STATUS_ERROR:
            errorStr_CanBeNone = subState_CanBeNone or "Printer Error"

        if state == PrinterState.PRINT_STATUS_COMPLETE or state == PrinterState.PRINT_STATUS_CANCELLED:
            fileName = printerState.GetMostRecentPrintInfo().GetFileNameWithNoExtension()
        else:
            fileName = printerState.GetFileNameWithNoExtension()
        if fileName is None:
            fileName = ""

        durationSec = printerState.DurationSec if printerState.DurationSec is not None else 0
        timeLeftSec = printerState.RemainingTimeSec
        if timeLeftSec is not None:
            timeLeftSec = min(timeLeftSec, 2147483600)

        progress = printerState.Progress if printerState.Progress is not None else 0.0

        hotendActual = printerState.HotendActual if printerState.HotendActual is not None else 0.0
        hotendTarget = printerState.HotendTarget if printerState.HotendTarget is not None else 0.0
        bedActual = printerState.BedActual if printerState.BedActual is not None else 0.0
        bedTarget = printerState.BedTarget if printerState.BedTarget is not None else 0.0

        filamentUsedMm = printerState.EstFilamentUsedMm if printerState.EstFilamentUsedMm is not None else 0
        filamentWeightMg = printerState.EstFilamentWeightMg if printerState.EstFilamentWeightMg is not None else 0

        return {
            "State": state,
            "SubState": subState_CanBeNone,
            "Error": errorStr_CanBeNone,
            "Lights": None,
            "CurrentPrint":
            {
                "Progress" : progress,
                "DurationSec" : durationSec,
                "TimeLeftSec" : timeLeftSec,
                "FileName" : fileName,
                "EstTotalFilUsedMm" : filamentUsedMm,
                "EstTotalFilWeightMg" : filamentWeightMg,
                "CurrentLayer": None,
                "TotalLayers": None,
                "Temps": {
                    "BedActual": bedActual,
                    "BedTarget": bedTarget,
                    "HotendActual": hotendActual,
                    "HotendTarget": hotendTarget,
                }
            }
        }


    def GetPlatformVersionStr(self) -> str:
        version = PrusaLinkClient.Get().GetVersion()
        info = PrusaLinkClient.Get().GetInfo()
        parts: List[str] = []
        if version is not None:
            text = version.get("text", None)
            if text is not None:
                parts.append(str(text))
            else:
                v = version.get("version", None)
                if v is not None:
                    parts.append(str(v))
        if info is not None:
            name = info.get("name", None)
            if name is not None:
                parts.append(str(name))
        if len(parts) == 0:
            return "PrusaLink"
        return "-".join(parts)


    def GetSupportedFeatureFlags(self) -> int:
        return 0


    def ExecutePause(self, smartPause:bool, suppressNotificationBool:bool, disableHotendBool:bool, disableBedBool:bool, zLiftMm:int, retractFilamentMm:int, showSmartPausePopup:bool) -> CommandResponse:
        if PrusaLinkClient.Get().SendPause():
            return CommandResponse.Success(None)
        return CommandResponse.Error(400, "Failed to send pause command to printer.")


    def ExecuteResume(self) -> CommandResponse:
        if PrusaLinkClient.Get().SendResume():
            return CommandResponse.Success(None)
        return CommandResponse.Error(400, "Failed to send resume command to printer.")


    def ExecuteCancel(self) -> CommandResponse:
        if PrusaLinkClient.Get().SendCancel():
            return CommandResponse.Success(None)
        return CommandResponse.Error(400, "Failed to send cancel command to printer.")


    def ExecuteSetLight(self, lightName:str, on:bool) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")


    def ExecuteMoveAxis(self, axis:str, distanceMm:float) -> CommandResponse:
        axis_upper = axis.upper()
        if axis_upper not in ["X", "Y", "Z"]:
            self.Logger.error("ExecuteMoveAxis: Invalid axis '%s'", axis)
            return CommandResponse.Error(400, "Invalid axis. Must be X, Y, or Z")
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")


    def ExecuteHome(self) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")


    def ExecuteExtrude(self, extruder:int, distanceMm:float) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")


    def ExecuteSetTemp(self, bedC:Optional[float], chamberC:Optional[float], toolC:Optional[float], toolNumber:Optional[int]) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")
