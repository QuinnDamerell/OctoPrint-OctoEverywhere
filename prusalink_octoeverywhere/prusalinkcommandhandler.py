import logging
from typing import Any, Dict, List, Optional, Tuple, Union

from octoeverywhere.commandhandler import CommandHandler, CommandResponse
from octoeverywhere.filesystemcommands import FileSystemCommandHelper
from octoeverywhere.httpresult import HttpResult
from octoeverywhere.interfaces import FEATURE_PRINT_START, IPlatformCommandHandler, ConnectionInfo
from octoeverywhere.WebStream.uploadbody import UploadBody
from linux_host.config import Config

from .prusalinkclient import PrusaLinkClient
from .prusalinkmodels import PrinterState


class PrusaLinkCommandHandler(IPlatformCommandHandler):

    def __init__(self, logger:logging.Logger, config: Config) -> None:
        self.Logger = logger
        self.Config = config


    def GetCurrentJobStatus(self) -> Union[int, None, Dict[str, Any]]:
        printerState = PrusaLinkClient.Get().GetState()
        if printerState is None:
            if PrusaLinkClient.Get().IsDisconnectDueToAuth():
                return CommandHandler.c_CommandError_LostAuth
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
        return 0 | FEATURE_PRINT_START


    def GetConnectionInfo(self) -> ConnectionInfo:
        portStr = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, Config.PrusaLinkDefaultPortStr)
        portInt: Optional[int] = None
        if portStr is not None:
            try:
                portInt = int(portStr)
            except ValueError:
                portInt = None
        return ConnectionInfo(
            localIp=self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None),
            localPort=portInt,
            apiKey=self.Config.GetStr(Config.SectionPrusaLink, Config.PrusaLinkApiKey, None),
            username=self.Config.GetStr(Config.SectionPrusaLink, Config.PrusaLinkUsername, None),
            password=self.Config.GetStr(Config.SectionPrusaLink, Config.PrusaLinkPassword, None),
        )


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


    def ExecuteStart(self, args:Optional[Dict[str, Any]]) -> CommandResponse:
        parsedPath, errorStr = FileSystemCommandHelper.ParsePathArg(args)
        if errorStr is not None or parsedPath is None:
            return CommandResponse.Error(400, errorStr or FileSystemCommandHelper.InvalidPathError())

        storage, storageError = self._GetStartStorage(args)
        if storageError is not None or storage is None:
            return CommandResponse.Error(400, storageError)

        platformPath = storage + "/" + parsedPath.RelativePath
        startPath = "/api/v1/files/" + FileSystemCommandHelper.EncodeRelativePathForUrl(storage) + "/" + FileSystemCommandHelper.EncodeRelativePathForUrl(parsedPath.RelativePath)
        try:
            response = PrusaLinkClient.Get().SendHttpCommand("POST", startPath, {}, None, 60.0)
        except Exception as e:
            self.Logger.warning("PrusaLink start request failed. %s", e)
            if PrusaLinkClient.Get().IsDisconnectDueToAuth():
                return CommandResponse.Error(CommandHandler.c_CommandError_LostAuth, FileSystemCommandHelper.AuthFailedError("PrusaLink", CommandHandler.c_StartCommand))
            return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, FileSystemCommandHelper.PrinterNotConnectedError("PrusaLink", CommandHandler.c_StartCommand))

        bodyBytes = response.content if response.content is not None else b""
        if response.status_code == 401 or response.status_code == 403:
            return CommandResponse.Error(CommandHandler.c_CommandError_LostAuth, FileSystemCommandHelper.AuthFailedError("PrusaLink", CommandHandler.c_StartCommand))
        if response.status_code == 409:
            return CommandResponse.Error(CommandHandler.c_CommandError_InvalidPrinterState, FileSystemCommandHelper.BackendHttpError("PrusaLink", CommandHandler.c_StartCommand, response.status_code, bodyBytes))
        if response.status_code < 200 or response.status_code >= 300:
            return CommandResponse.Error(response.status_code, FileSystemCommandHelper.BackendHttpError("PrusaLink", CommandHandler.c_StartCommand, response.status_code, bodyBytes))
        return FileSystemCommandHelper.BuildFileStartSuccess(parsedPath, platformPath, self._BuildHttpResponse(response))


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


    # !! Platform Command Handler Interface Function !!
    # Sends an HTTP request to PrusaLink and returns the HTTP response.
    def ExecuteSendCommand(self, transportType:str, request:Dict[str, Any], rawPayload:Dict[str, Any]) -> CommandResponse:
        if transportType != "http":
            return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, f"This is a PrusaLink printer, which only accepts send-command requests with TransportType 'http'. The received TransportType was '{transportType}'. Set 'TransportType' to 'http', put the PrusaLink API Path/Method/Headers at the top level of the payload, and put any JSON request body in 'Request'. Example: {{\"TransportType\": \"http\", \"Path\": \"/api/version\", \"Method\": \"GET\", \"Request\": {{}}}}.")

        parsed = CommandHandler.ParseHttpSendCommand(rawPayload, request)
        if isinstance(parsed, CommandResponse):
            return parsed

        try:
            response = PrusaLinkClient.Get().SendHttpCommand(parsed.Method, parsed.Path, parsed.Headers, parsed.BodyBytes, parsed.TimeoutSec)
        except Exception as e:
            self.Logger.warning("PrusaLink send-command HTTP request failed. %s", e)
            return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, "Printer Not Connected")

        # The HTTP request itself succeeded; the printer's response (including a 4xx/5xx) is the meaningful payload.
        responseObj = self._BuildHttpResponse(response)
        isError = bool(responseObj.get("StatusCode", 0) >= 400)
        return CommandHandler.BuildSendCommandResult("http", {"Path": parsed.Path, "Method": parsed.Method}, responseObj, isError, waitForResponse=True, timeoutSec=parsed.TimeoutSec)


    def ExecuteFileList(self, args:Optional[Dict[str, Any]]) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, FileSystemCommandHelper.UnsupportedPlatformError("PrusaLink"))


    def ExecuteFileUpload(self, args:Optional[Dict[str, Any]], uploadBody:UploadBody) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, FileSystemCommandHelper.UnsupportedPlatformError("PrusaLink"))


    def ExecuteFileDownload(self, args:Optional[Dict[str, Any]]) -> HttpResult:
        return FileSystemCommandHelper.BuildRawError(CommandHandler.c_CommandError_FeatureNotSupported, FileSystemCommandHelper.UnsupportedPlatformError("PrusaLink"), CommandHandler.c_FilesDownloadCommand)


    def ExecuteGetPluginLogs(self, args:Optional[Dict[str, Any]]) -> HttpResult:
        return FileSystemCommandHelper.BuildLogFileResultFromLogger(self.Logger, "octoeverywhere.log", CommandHandler.c_GetPluginLogsCommand, "octoeverywhere.log", args)


    def ExecuteFileDelete(self, args:Optional[Dict[str, Any]]) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, FileSystemCommandHelper.UnsupportedPlatformError("PrusaLink"))


    def _GetStartStorage(self, args:Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
        storage = "local"
        if args is not None:
            value = FileSystemCommandHelper.GetFirstArg(args, "Storage", "storage")
            if value is not None:
                storage = str(value)

        storage = storage.replace("\\", "/").strip().strip("/")
        if len(storage) == 0:
            return (None, "Invalid storage. Use a non-empty PrusaLink storage name such as 'local'.")
        for part in storage.split("/"):
            if len(part) == 0 or part == "." or part == "..":
                return (None, "Invalid storage. Use a PrusaLink storage name without '.', '..', or empty segments.")
        return (storage, None)


    def _BuildHttpResponse(self, response:Any) -> Dict[str, Any]:
        bodyBytes = response.content if response.content is not None else b""
        responseObj:Dict[str, Any] = {
            "StatusCode": response.status_code,
            "Headers": dict(response.headers),
            "Url": response.url,
        }
        # Add the body to the response.
        FileSystemCommandHelper.BuildHttpResponseBody(responseObj, bodyBytes)
        return responseObj
