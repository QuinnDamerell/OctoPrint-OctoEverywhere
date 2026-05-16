import logging
from typing import Any, Dict, List, Optional, Union

from octoeverywhere.commandhandler import CommandHandler, CommandResponse
from octoeverywhere.interfaces import (
    FEATURE_AXIS_MOVEMENT,
    FEATURE_HOMING,
    FEATURE_LIGHT_CONTROL,
    FEATURE_TEMPERATURE_CONTROL,
    IPlatformCommandHandler,
)

from .elegoocc2client import ElegooCc2Client
from .elegoocc2filemanager import ElegooCc2FileManager
from .elegoocc2models import PrinterState


class ElegooCc2CommandHandler(IPlatformCommandHandler):

    c_ChamberLightName = "chamber"

    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger


    def GetCurrentJobStatus(self) -> Union[int, None, Dict[str, Any]]:
        printerState = ElegooCc2Client.Get().GetState()
        if printerState is None:
            if ElegooCc2Client.Get().IsDisconnectDueToTooManyClients():
                return CommandHandler.c_CommandError_CantConnectTooManyClients
            return None

        (state, subState_CanBeNone) = printerState.GetCurrentStatus()
        errorStr_CanBeNone = None
        if state == PrinterState.PRINT_STATUS_ERROR:
            errorStr_CanBeNone = "Printer Error"

        currentLayerInt = printerState.CurrentLayer if printerState.CurrentLayer is not None else 0
        totalLayersInt = printerState.TotalLayer if printerState.TotalLayer is not None else 0

        if state == PrinterState.PRINT_STATUS_COMPLETE:
            fileName = printerState.GetMostRecentPrintInfo().GetFileNameWithNoExtension()
        else:
            fileName = printerState.GetFileNameWithNoExtension()
        if fileName is None:
            fileName = ""

        durationSec = printerState.DurationSec if printerState.DurationSec is not None else 0
        timeLeftSec = printerState.GetTimeRemainingSec()
        if timeLeftSec is not None:
            timeLeftSec = min(timeLeftSec, 2147483600)

        filamentUsedMm = 0
        filamentWeightMg = 0
        fileInfo = ElegooCc2FileManager.Get().GetFileInfoFromState(printerState)
        if fileInfo is not None and fileInfo.EstFilamentWeightMg is not None:
            filamentWeightMg = fileInfo.EstFilamentWeightMg

        progress = printerState.Progress if printerState.Progress is not None else 0.0

        hotendActual = printerState.HotendActual if printerState.HotendActual is not None else 0.0
        hotendTarget = printerState.HotendTarget if printerState.HotendTarget is not None else 0.0
        bedActual = printerState.BedActual if printerState.BedActual is not None else 0.0
        bedTarget = printerState.BedTarget if printerState.BedTarget is not None else 0.0
        chamberActual = printerState.ChamberActual if printerState.ChamberActual is not None else 0.0
        chamberTarget = printerState.ChamberTarget if printerState.ChamberTarget is not None else 0.0

        lights: Optional[List[Dict[str, Any]]] = None
        if printerState.ChamberLightOn is not None:
            lights = [ {"Name": self.c_ChamberLightName, "On": printerState.ChamberLightOn} ]

        return {
            "State": state,
            "SubState": subState_CanBeNone,
            "Error": errorStr_CanBeNone,
            "Lights": lights,
            "CurrentPrint":
            {
                "Progress" : progress,
                "DurationSec" : durationSec,
                "TimeLeftSec" : timeLeftSec,
                "FileName" : fileName,
                "EstTotalFilUsedMm" : filamentUsedMm,
                "EstTotalFilWeightMg" : filamentWeightMg,
                "CurrentLayer": currentLayerInt,
                "TotalLayers": totalLayersInt,
                "Temps": {
                    "BedActual": bedActual,
                    "BedTarget": bedTarget,
                    "HotendActual": hotendActual,
                    "HotendTarget": hotendTarget,
                    "ChamberActual": chamberActual,
                    "ChamberTarget": chamberTarget,
                }
            }
        }


    def GetPlatformVersionStr(self) -> str:
        attrs = ElegooCc2Client.Get().GetAttributes()
        if attrs is None:
            return "Elegoo-CC2"
        return attrs.GetVersionString()


    def GetSupportedFeatureFlags(self) -> int:
        return 0 | FEATURE_LIGHT_CONTROL | FEATURE_HOMING | FEATURE_AXIS_MOVEMENT | FEATURE_TEMPERATURE_CONTROL


    def ExecutePause(self, smartPause:bool, suppressNotificationBool:bool, disableHotendBool:bool, disableBedBool:bool, zLiftMm:int, retractFilamentMm:int, showSmartPausePopup:bool) -> CommandResponse:
        result = ElegooCc2Client.Get().SendRequest(1021)
        if result.HasError():
            return CommandResponse.Error(400, "Failed to send command to printer.")
        return CommandResponse.Success(None)


    def ExecuteResume(self) -> CommandResponse:
        result = ElegooCc2Client.Get().SendRequest(1023)
        if result.HasError():
            return CommandResponse.Error(400, "Failed to send command to printer.")
        return CommandResponse.Success(None)


    def ExecuteCancel(self) -> CommandResponse:
        result = ElegooCc2Client.Get().SendRequest(1022)
        if result.HasError():
            return CommandResponse.Error(400, "Failed to send command to printer.")
        return CommandResponse.Success(None)


    def ExecuteSetLight(self, lightName:str, on:bool) -> CommandResponse:
        if lightName != self.c_ChamberLightName:
            return CommandResponse.Error(400, f"Unknown light name: {lightName}")

        result = ElegooCc2Client.Get().SendRequest(1029, {"brightness": 255 if on else 0})
        if result.HasError():
            return CommandResponse.Error(400, "Failed to send command to printer.")
        return CommandResponse.Success(None)


    def ExecuteMoveAxis(self, axis:str, distanceMm:float) -> CommandResponse:
        axisLower = axis.lower()
        if axisLower not in ["x", "y", "z"]:
            self.Logger.error(f"ExecuteMoveAxis: Invalid axis '{axis}'")
            return CommandResponse.Error(400, "Invalid axis. Must be X, Y, or Z")

        result = ElegooCc2Client.Get().SendRequest(1027, {"axes": axisLower, "distance": distanceMm})
        if result.HasError():
            return CommandResponse.Error(400, "Failed to send command to printer.")
        return CommandResponse.Success(None)


    def ExecuteHome(self) -> CommandResponse:
        result = ElegooCc2Client.Get().SendRequest(1026, {"homed_axes": "xyz"})
        if result.HasError():
            return CommandResponse.Error(400, "Failed to send command to printer.")
        return CommandResponse.Success(None)


    def ExecuteExtrude(self, extruder:int, distanceMm:float) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")


    def ExecuteSetTemp(self, bedC:Optional[float], chamberC:Optional[float], toolC:Optional[float], toolNumber:Optional[int]) -> CommandResponse:
        params:Dict[str, float] = {}
        if bedC is not None:
            params["heater_bed"] = bedC
        if toolC is not None:
            params["extruder"] = toolC
        if chamberC is not None:
            return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Chamber temperature control is not supported")
        if len(params) == 0:
            return CommandResponse.Success(None)

        result = ElegooCc2Client.Get().SendRequest(1028, params)
        if result.HasError():
            return CommandResponse.Error(400, "Failed to send command to printer.")
        return CommandResponse.Success(None)
