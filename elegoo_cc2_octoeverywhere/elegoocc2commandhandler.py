import json
import logging
from typing import Any, Dict, List, Optional, Union

from octoeverywhere.commandhandler import CommandHandler, CommandResponse
from octoeverywhere.filesystemcommands import FileSystemCommandHelper
from octoeverywhere.httpresult import HttpResult
from octoeverywhere.interfaces import (
    FEATURE_AXIS_MOVEMENT,
    FEATURE_HOMING,
    FEATURE_LIGHT_CONTROL,
    FEATURE_TEMPERATURE_CONTROL,
    IPlatformCommandHandler,
    ConnectionInfo
)
from octoeverywhere.WebStream.uploadbody import UploadBody
from linux_host.config import Config

from .elegoocc2client import ElegooCc2Client
from .elegoocc2filemanager import ElegooCc2FileManager
from .elegoocc2models import PrinterState


class ElegooCc2CommandHandler(IPlatformCommandHandler):

    c_ChamberLightName = "chamber"

    def __init__(self, logger:logging.Logger, config:Config) -> None:
        self.Logger = logger
        self.Config = config


    def GetCurrentJobStatus(self) -> Union[int, None, Dict[str, Any]]:
        printerState = ElegooCc2Client.Get().GetState()
        if printerState is None:
            if ElegooCc2Client.Get().IsDisconnectDueToAuth():
                return CommandHandler.c_CommandError_LostAuth
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


    def GetConnectionInfo(self) -> ConnectionInfo:
        return ConnectionInfo(
            self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None),
            self.Config.GetInt(Config.SectionCompanion, Config.CompanionKeyPort, None),
            self.Config.GetStr(Config.SectionElegoo, Config.ElegooCc2AccessCode, None),
            self.Config.GetStr(Config.SectionElegoo, Config.ElegooCc2PrinterSn, None)
        )


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

        # According to the docs, both brightness is the correct value, but it doesn't wok on some firmware versions and the official elegoo HTML page uses "power" instead.
        result = ElegooCc2Client.Get().SendRequest(1029, {"brightness": 255 if on else 0, "power" : 1 if on else 0})
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


    # !! Platform Command Handler Interface Function !!
    # Sends an Elegoo CC2 MQTT request and returns the matched MQTT response.
    def ExecuteSendCommand(self, transportType:str, request:Dict[str, Any], rawPayload:Dict[str, Any]) -> CommandResponse:
        if transportType != "mqtt":
            return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, f"This is an Elegoo Centauri (CC2) printer, which communicates over MQTT, so it only accepts send-command requests with TransportType 'mqtt'. The received TransportType was '{transportType}'. Set 'TransportType' to 'mqtt' and put the command in 'Request'. Example: {{\"TransportType\": \"mqtt\", \"Request\": {{\"Method\": 1, \"Params\": {{}}}}}}.")

        parsed = CommandHandler.ParseMqttSendCommand(rawPayload, request)
        if isinstance(parsed, CommandResponse):
            return parsed

        # Elegoo CC2 uses an int method id and a params object.
        method = parsed.Method
        if method is None or not isinstance(method, int) or isinstance(method, bool):
            return CommandResponse.Error(400, f"This Elegoo CC2 printer requires an integer 'Method' id inside 'Request', e.g. \"Request\": {{\"Method\": 1, \"Params\": {{...}}}}, but the received method value was {json.dumps(method, default=str)}. The 'Method' selects which CC2 command to run and must be a JSON integer.")

        # The MQTT request/response topics and payloads are surfaced so a developer has full access to the messages.
        # The shared echo builder gives the request and response the same protocol-faithful {Topic, Payload, Qos, Retain} shape.
        topics = ElegooCc2Client.Get().GetApiRequestResponseTopics()
        requestEcho = CommandHandler.BuildMqttMessageEcho(topics["RequestTopic"], {"method": method, "params": parsed.Params})

        result = ElegooCc2Client.Get().SendRequest(method, parsed.Params, waitForResponse=parsed.WaitForResponse, timeoutSec=parsed.TimeoutSec)
        if result.HasError():
            code = result.GetErrorCode()
            # Transport failures are returned as actionable OE error codes (no useful payload).
            if code == result.OE_ERROR_MQTT_NOT_CONNECTED:
                if ElegooCc2Client.Get().IsDisconnectDueToAuth():
                    return CommandResponse.Error(CommandHandler.c_CommandError_LostAuth, "Unauthorized - re-authenticate with the printer (check the access code / credentials).")
                return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, "Printer Not Connected")
            if code == result.OE_ERROR_TIMEOUT:
                return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, "No response received from the printer within the timeout. Some MQTT commands don't return a response - set WaitForResponse to false for those.")
            if code == result.OE_ERROR_EXCEPTION:
                return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, result.GetErrorStr() or "Failed to send command.")
            # A printer-side command error (e.g. a failed result) still carries the full response payload. Surface it.
            payload = result.GetRawResponseOrResult()
            if payload is not None:
                return CommandHandler.BuildSendCommandResult("mqtt", requestEcho, CommandHandler.BuildMqttMessageEcho(topics["ResponseTopic"], payload), isError=True, waitForResponse=parsed.WaitForResponse, timeoutSec=parsed.TimeoutSec)
            return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, result.GetLoggingErrorStr())

        # Fire-and-forget: nothing was awaited.
        if parsed.WaitForResponse is False:
            return CommandHandler.BuildSendCommandResult("mqtt", requestEcho, responseReceived=False, waitForResponse=parsed.WaitForResponse, timeoutSec=parsed.TimeoutSec)

        responseEcho = CommandHandler.BuildMqttMessageEcho(topics["ResponseTopic"], result.GetRawResponseOrResult())
        return CommandHandler.BuildSendCommandResult("mqtt", requestEcho, responseEcho, isError=False, waitForResponse=parsed.WaitForResponse, timeoutSec=parsed.TimeoutSec)


    def ExecuteFileList(self, args:Optional[Dict[str, Any]]) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, FileSystemCommandHelper.UnsupportedPlatformError("Elegoo CC2"))


    def ExecuteFileUpload(self, args:Optional[Dict[str, Any]], uploadBody:UploadBody) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, FileSystemCommandHelper.UnsupportedPlatformError("Elegoo CC2"))


    def ExecuteFileDownload(self, args:Optional[Dict[str, Any]]) -> HttpResult:
        return FileSystemCommandHelper.BuildRawError(CommandHandler.c_CommandError_FeatureNotSupported, FileSystemCommandHelper.UnsupportedPlatformError("Elegoo CC2"), CommandHandler.c_FilesDownloadCommand)


    def ExecuteGetPluginLogs(self, args:Optional[Dict[str, Any]]) -> HttpResult:
        return FileSystemCommandHelper.BuildLogFileResultFromLogger(self.Logger, "octoeverywhere.log", CommandHandler.c_GetPluginLogsCommand, "octoeverywhere.log", args)


    def ExecuteFileDelete(self, args:Optional[Dict[str, Any]]) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, FileSystemCommandHelper.UnsupportedPlatformError("Elegoo CC2"))
