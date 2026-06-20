import json
import logging
import os
from typing import Any, BinaryIO, Callable, Dict, Union, Optional, List
from urllib.parse import quote

from octoeverywhere.buffer import Buffer
from octoeverywhere.commandhandler import CommandHandler, CommandResponse
from octoeverywhere.filesystemcommands import FileSystemCommandHelper, VirtualFileSystemTree
from octoeverywhere.httpresult import HttpResult
from octoeverywhere.printinfo import PrintInfoManager
from octoeverywhere.interfaces import FEATURE_LIGHT_CONTROL, FEATURE_PRINT_START, IPlatformCommandHandler, ConnectionInfo
from octoeverywhere.WebStream.uploadbody import UploadBody
from linux_host.config import Config

from .bambuclient import BambuClient
from .bambufilemanager import BambuFileManager, BambuFtpsError
from .bambumodels import BambuPrintErrors

# This class implements the Platform Command Handler Interface
class BambuCommandHandler(IPlatformCommandHandler):

    c_ChamberLightName = "chamber"

    def __init__(self, logger: logging.Logger, config: Config, fileManagerFactory:Optional[Callable[[], BambuFileManager]]=None) -> None:
        self.Logger = logger
        self.Config = config
        self.FileManagerFactory = fileManagerFactory


    # This map contains UI ready strings that map to a subset of sub-stages we can send which are more specific than the state.
    # These need to be UI ready, since they will be shown directly.
    # Some known stages are excluded, because we don't want to show them.
    # Here's a full list: https://github.com/greghesp/ha-bambulab/blob/main/custom_components/bambu_lab/pybambu/const.py
    SubStageMap = {
        # 0:  "Printing", Don't include printing, since this would make a substate string exist.
        1:  "Auto Bed Leveling",
        2:  "Bed Preheating",
        3:  "Sweeping XY Mech Mode",
        4:  "Changing Filament",
        5:  "M400 Pause",
        6:  "Filament Runout",
        7:  "Heating Hotend",
        8:  "Calibrating Extrusion",
        9:  "Scanning Bed Surface",
        10: "Inspecting First Layer",
        11: "Identifying Build Plate",
        12: "Calibrating Micro Lidar",
        13: "Homing Toolhead",
        14: "Cleaning Nozzle",
        15: "Checking Temperature",
        16: "Paused By User",
        17: "Front Cover Falling",
        18: "Calibrating Micro Lidar",
        19: "Calibrating Extrusion Flow",
        20: "Nozzle Temperature Malfunction",
        21: "Bed Temperature Malfunction",
        22: "Filament Unloading",
        23: "Skip Step Pause",
        24: "Filament Loading",
        25: "Motor Noise Calibration",
        26: "AMS lost",
        27: "Low Speed Of Heat Break Fan",
        28: "Chamber Temperature Control Error",
        29: "Cooling Chamber",
        30: "Paused By Gcode",
        31: "Motor Noise Showoff",
        32: "Nozzle Filament Covered Detected Pause",
        33: "Cutter Error",
        34: "First Layer Error",
        35: "Nozzle Clogged",
        36: "Checking Absolute Accuracy Before Calibration",
        37: "Absolute Accuracy Calibration",
        38: "Checking Absolute Accuracy After Calibration",
        39: "Calibrating Nozzle Offset",
        40: "High Temperature Bed Leveling",
        41: "Checking Quick Release",
        42: "Checking Door And Cover",
        43: "Laser Calibration",
        44: "Checking Platform",
        45: "Checking Birdseye Camera Position",
        46: "Calibrating Birdseye Camera",
        47: "Bed Leveling Phase 1",
        48: "Bed Leveling Phase 2",
        49: "Heating Chamber",
        50: "Cooling Heated Bed",
        51: "Printing Calibration Lines",
        52: "Checking Material",
        53: "Calibrating Live View Camera",
        54: "Waiting For Bed Temperature",
        55: "Checking Material Position",
        56: "Calibrating Cutter Model Offset",
        57: "Measuring Surface",
        58: "Thermal Preconditioning",
        59: "Homing Blade Holder",
        60: "Calibrating Camera Offset",
        61: "Calibrating Blade Holder Position",
        62: "Hotend Pick And Place Test",
        63: "Waiting For Chamber Temperature",
        64: "Preparing Hotend",
        65: "Calibrating Nozzle Clumping Detection",
        66: "Purifying Chamber Air",
        67: "Measuring Rotary Attachment",
        68: "Moving Toolhead Above Purge Chute",
        69: "Cooling Nozzle",
        70: "Moving Toolhead To Center Of Bed",
        71: "Active Arc Fitting",
        72: "Detecting Hotend Type",
        73: "Detecting Build Plate Alignment",
        74: "Detecting Foreign Object On Bed Surface",
        75: "Detecting Foreign Object Under Bed",
        76: "Pre-Extrusion Before Printing",
        77: "Preparing AMS",
        # X1 returns -1 for idle
        -1: "Idle",
        # P1 returns 255 for idle
        255: "Idle",
    }


    # !! Platform Command Handler Interface Function !!
    #
    # This must return the common "JobStatus" dict or None on failure.
    # The format of this must stay consistent with OctoPrint and the service.
    # Returning None send back the NoHostConnected error, assuming that the plugin isn't connected to the host or the host isn't
    # connected to the printer's firmware.
    #
    # See the JobStatusV2 class in the service for the object definition.
    #
    # Returning None will result in the "Printer not connected" state.
    # Or one of the CommandHandler.c_CommandError_... ints can be returned, which will be sent as the result.
    #
    def GetCurrentJobStatus(self) -> Union[int, None, Dict[str, Any]]:
        # Try to get the current state.
        bambuState = BambuClient.Get().GetState()

        # If the state is None, we are disconnected.
        if bambuState is None:
            if BambuClient.Get().IsDisconnectDueToAuth():
                return CommandHandler.c_CommandError_LostAuth
            # Returning None will be a "connection lost" state.
            return None

        # Map the state
        # Possible states: https://github.com/greghesp/ha-bambulab/blob/e72e343acd3279c9bccba510f94bf0e291fe5aaa/custom_components/bambu_lab/pybambu/const.py#L83C1-L83C21
        state = "idle"
        errorStr_CanBeNone = None

        # Before checking the state, see if the print is in an error state.
        # This error state can be common among other states, like "IDLE" or "PAUSE"
        printError = bambuState.GetPrinterErrorType()
        if printError is not None:
            # Always set the state to error.
            # If we can match a known state, return a good string that can be shown for the user.
            state = "error"
            if printError == BambuPrintErrors.FilamentRunOut:
                errorStr_CanBeNone = "Filament Run Out"
            elif printError == BambuPrintErrors.PrintFailureDetected:
                errorStr_CanBeNone = "Print Failure Detected"
            else:
                # This results in a long string which isn't great for the UI, but it gives the user more detail.
                detailedError = bambuState.GetDetailedPrinterErrorStr()
                if detailedError is not None:
                    errorStr_CanBeNone = "Error: " + detailedError
        # If we aren't in error, use the state
        elif bambuState.gcode_state is not None:
            gcodeState = bambuState.gcode_state
            if gcodeState == "IDLE" or gcodeState == "INIT" or gcodeState == "OFFLINE" or gcodeState == "UNKNOWN":
                state = "idle"
            elif gcodeState == "RUNNING" or gcodeState == "SLICING":
                # Only check stg_cur in the known printing state, because sometimes it doesn't get reset to idle when transitioning to an error.
                stg = bambuState.stg_cur
                if stg == 2 or stg == 7:
                    state = "warmingup"
                else:
                    # These are all a subset of printing states.
                    state = "printing"
            elif gcodeState == "PAUSE":
                state = "paused"
            elif gcodeState == "FINISH":
                # When the X1C first starts and does the first time user calibration, the state is FINISH
                # but there's really nothing done. This might happen after other calibrations, so if the total layers is 0, we are idle.
                if bambuState.total_layer_num is not None and bambuState.total_layer_num == 0:
                    state = "idle"
                else:
                    state = "complete"
            elif gcodeState == "FAILED":
                state = "cancelled"
            elif gcodeState == "PREPARE":
                state = "warmingup"
            else:
                self.Logger.warning(f"Unknown gcode_state state in print state: {gcodeState}")

        # If we have a mapped sub state, set it.
        subState_CanBeNone = None
        if bambuState.stg_cur is not None:
            if bambuState.stg_cur in BambuCommandHandler.SubStageMap:
                subState_CanBeNone = BambuCommandHandler.SubStageMap[bambuState.stg_cur]

        # Get current layer info
        # None = The platform doesn't provide it.
        # 0 = The platform provider it, but there's no info yet.
        # # = The values
        currentLayerInt = None
        totalLayersInt = None
        if bambuState.layer_num is not None:
            currentLayerInt = int(bambuState.layer_num)
        if bambuState.total_layer_num is not None:
            totalLayersInt = int(bambuState.total_layer_num)

        # Get the filename.
        fileName = bambuState.GetFileNameWithNoExtension()
        if fileName is None:
            fileName = ""

        # For Bambu, the printer doesn't report the duration or the print start time.
        # Thus we have to track it ourselves in our print info.
        # When the print is over, a final print duration is set, so this doesn't keep going from print start.
        durationSec = 0
        pi = PrintInfoManager.Get().GetPrintInfo(bambuState.GetPrintCookie())
        if pi is not None:
            durationSec = pi.GetPrintDurationSec()

        # If we have a file name, try to get the current filament usage.
        filamentUsageMm = 0
        # if fileName is not None and len(fileName) > 0:
        #     filamentUsageMm = FileMetadataCache.Get().GetEstimatedFilamentUsageMm(fileName)

        # Get the progress
        progress = 0.0
        if bambuState.mc_percent is not None:
            progress = float(bambuState.mc_percent)

        # We have special logic to handle the time left count down, since bambu only gives us minutes
        # and we want seconds. We can estimate it pretty well by counting down from the last time it changed.
        timeLeftSec = bambuState.GetContinuousTimeRemainingSec()
        if timeLeftSec is None:
            timeLeftSec = 0

        # Get the current temps if possible.
        hotendActual = 0.0
        hotendTarget = 0.0
        bedTarget = 0.0
        bedActual = 0.0
        if bambuState.nozzle_temper is not None:
            hotendActual = round(float(bambuState.nozzle_temper), 2)
        if bambuState.nozzle_target_temper is not None:
            hotendTarget = round(float(bambuState.nozzle_target_temper), 2)
        if bambuState.bed_temper is not None:
            bedActual = round(float(bambuState.bed_temper), 2)
        if bambuState.bed_target_temper is not None:
            bedTarget = round(float(bambuState.bed_target_temper), 2)

        # Get light status.
        # None if there are no lights, otherwise a list of lights and their status.
        lights: Optional[List[Dict[str, Any]]] = None
        if bambuState.chamber_light is not None:
            lights = [ {"Name": self.c_ChamberLightName, "On": bambuState.chamber_light}   ]

        # Build the object and return.
        return {
            "State": state,
            "SubState": subState_CanBeNone,
            "Error": errorStr_CanBeNone,
            "Lights": lights,
            "CurrentPrint":
            {
                "Progress" : progress,
                "DurationSec" : durationSec,
                # In some system buggy cases, the time left can be super high and won't fit into a int32, so we cap it.
                "TimeLeftSec" : min(timeLeftSec, 2147483600),
                "FileName" : fileName,
                "EstTotalFilUsedMm" : filamentUsageMm,
                "CurrentLayer": currentLayerInt,
                "TotalLayers": totalLayersInt,
                "Temps": {
                    "BedActual": bedActual,
                    "BedTarget": bedTarget,
                    "HotendActual": hotendActual,
                    "HotendTarget": hotendTarget,
                }
            }
        }


    # !! Platform Command Handler Interface Function !!
    # This must return the platform version as a string.
    def GetPlatformVersionStr(self) -> str:
        version = BambuClient.Get().GetVersion()
        if version is None:
            return "0.0.0"
        return f"{version.SoftwareVersion}-{version.PrinterName}"


    # !! Platform Command Handler Interface Function !!
    # Returns an int with the supported feature flags for this platform, such as FEATURE_LIGHT_CONTROL, etc
    def GetSupportedFeatureFlags(self) -> int:
        # These are all we support right now.
        return 0 | FEATURE_LIGHT_CONTROL | FEATURE_PRINT_START


    # !! Platform Command Handler Interface Function !!
    # Returns the current connection info from the config.
    def GetConnectionInfo(self) -> ConnectionInfo:
        return ConnectionInfo(
            self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None),
            self.Config.GetInt(Config.SectionCompanion, Config.CompanionKeyPort, None),
            self.Config.GetStr(Config.SectionBambu, Config.BambuAccessToken, None),
            self.Config.GetStr(Config.SectionBambu, Config.BambuPrinterSn, None)
        )


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the pause and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecutePause(self, smartPause:bool, suppressNotificationBool:bool, disableHotendBool:bool, disableBedBool:bool, zLiftMm:int, retractFilamentMm:int, showSmartPausePopup:bool) -> CommandResponse:
        if BambuClient.Get().SendPause():
            return CommandResponse.Success(None)
        else:
            return CommandResponse.Error(400, "Failed to send command to printer.")


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the resume and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteResume(self) -> CommandResponse:
        if BambuClient.Get().SendResume():
            return CommandResponse.Success(None)
        else:
            return CommandResponse.Error(400, "Failed to send command to printer.")


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the cancel and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteCancel(self) -> CommandResponse:
        if BambuClient.Get().SendCancel():
            return CommandResponse.Success(None)
        else:
            return CommandResponse.Error(400, "Failed to send command to printer.")


    # !! Platform Command Handler Interface Function !!
    # Starts a print from a virtual file system path.
    def ExecuteStart(self, args:Optional[Dict[str, Any]]) -> CommandResponse:
        parsedPath, errorStr = FileSystemCommandHelper.ParsePathArg(args)
        if errorStr is not None or parsedPath is None:
            return CommandResponse.Error(400, errorStr or FileSystemCommandHelper.InvalidPathError())

        payload = self._BuildStartPrintPayload(parsedPath.RelativePath, args)
        result = BambuClient.Get().SendCommand(payload, timeoutSec=30.0, waitForResponse=True)
        if result.HasError():
            if result.Connected is False:
                if BambuClient.Get().IsDisconnectDueToAuth():
                    return CommandResponse.Error(CommandHandler.c_CommandError_LostAuth, "Unauthorized - re-authenticate with the printer (check the access code / credentials).")
                return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, FileSystemCommandHelper.PrinterNotConnectedError("Bambu", CommandHandler.c_StartCommand))
            if result.Timeout:
                return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, "No response received from the printer while starting the print.")
            return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, result.GetLoggingErrorStr())

        return FileSystemCommandHelper.BuildFileStartSuccess(parsedPath, parsedPath.RelativePath, result.Result)


    # !! Platform Command Handler Interface Function !!
    # Sets the light state for the specified light type.
    def ExecuteSetLight(self, lightName:str, on:bool) -> CommandResponse:
        # Only chamber light is supported
        if lightName != self.c_ChamberLightName:
            return CommandResponse.Error(400, f"Unknown light name: {lightName}")

        if BambuClient.Get().SendSetChamberLight(on):
            return CommandResponse.Success(None)
        else:
            return CommandResponse.Error(400, "Failed to send command to printer.")


    # !! Platform Command Handler Interface Function !!
    # Moves the specified axis by the given distance in mm.
    def ExecuteMoveAxis(self, axis:str, distanceMm:float) -> CommandResponse:
        # Validate axis parameter
        axis_upper = axis.upper()
        if axis_upper not in ["X", "Y", "Z"]:
            self.Logger.error(f"ExecuteMoveAxis: Invalid axis '{axis}'")
            return CommandResponse.Error(400, "Invalid axis. Must be X, Y, or Z")
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")


    # !! Platform Command Handler Interface Function !!
    # Homes all axes.
    def ExecuteHome(self) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")


    # !! Platform Command Handler Interface Function !!
    # Extrudes or retracts filament for the specified extruder.
    def ExecuteExtrude(self, extruder:int, distanceMm:float) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")


    # !! Platform Command Handler Interface Function !!
    # Sets the temperature for bed, chamber, or tool.
    def ExecuteSetTemp(self, bedC:Optional[float], chamberC:Optional[float], toolC:Optional[float], toolNumber:Optional[int]) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")


    # !! Platform Command Handler Interface Function !!
    # Sends a Bambu MQTT JSON payload and returns the matched MQTT JSON response.
    def ExecuteSendCommand(self, transportType:str, request:Dict[str, Any], rawPayload:Dict[str, Any]) -> CommandResponse:
        if transportType != "mqtt":
            return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, f"This is a Bambu Lab printer, which communicates over MQTT, so it only accepts send-command requests with TransportType 'mqtt'. The received TransportType was '{transportType}'. Set 'TransportType' to 'mqtt' and put the full MQTT JSON payload in 'Request' (it is sent to the printer as-is). Example: {{\"TransportType\": \"mqtt\", \"Request\": {{\"pushing\": {{\"sequence_id\": \"0\", \"command\": \"pushall\"}}}}}}.")

        # We only use the common parse for the WaitForResponse flag; Bambu sends the full Request payload as-is over MQTT.
        parsed = CommandHandler.ParseMqttSendCommand(rawPayload, request)
        if isinstance(parsed, CommandResponse):
            return parsed

        result = BambuClient.Get().SendCommand(parsed.Request, timeoutSec=parsed.TimeoutSec, waitForResponse=parsed.WaitForResponse)
        if result.HasError():
            if result.Connected is False:
                if BambuClient.Get().IsDisconnectDueToAuth():
                    return CommandResponse.Error(CommandHandler.c_CommandError_LostAuth, "Unauthorized - re-authenticate with the printer (check the access code / credentials).")
                return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, "Printer Not Connected")
            if result.Timeout:
                return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, "No response received from the printer within the timeout. Some MQTT commands don't return a response - set WaitForResponse to false for those.")
            return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, result.GetLoggingErrorStr())

        # The client returns {"request": {topic, payload, qos}, "response": {topic, payload, qos}}; convert to the common
        # PascalCase envelope via the shared MQTT echo builder so the request and response are the same protocol-faithful
        # {Topic, Payload, Qos, Retain} shape. The MQTT payloads themselves are the native messages, passed through
        # untouched, so a developer has full access to the request and response messages.
        res:Dict[str, Any] = result.Result if isinstance(result.Result, dict) else {}
        reqRaw:Any = res.get("request", None)
        reqEcho:Dict[str, Any] = reqRaw if isinstance(reqRaw, dict) else {}
        requestEcho = CommandHandler.BuildMqttMessageEcho(reqEcho.get("topic", None), reqEcho.get("payload", None), reqEcho.get("qos", 0))
        if parsed.WaitForResponse is False:
            return CommandHandler.BuildSendCommandResult("mqtt", requestEcho, responseReceived=False, waitForResponse=parsed.WaitForResponse, timeoutSec=parsed.TimeoutSec)

        respRaw:Any = res.get("response", None)
        respEcho:Dict[str, Any] = respRaw if isinstance(respRaw, dict) else {}
        responseEcho = CommandHandler.BuildMqttMessageEcho(respEcho.get("topic", None), respEcho.get("payload", None), respEcho.get("qos", 0))
        return CommandHandler.BuildSendCommandResult("mqtt", requestEcho, responseEcho, isError=False, waitForResponse=parsed.WaitForResponse, timeoutSec=parsed.TimeoutSec)


    def ExecuteFileList(self, args:Optional[Dict[str, Any]]) -> CommandResponse:
        try:
            fileInfos = self._GetFileManager().ListPrintableFiles()
            tree = VirtualFileSystemTree()
            for fileInfo in fileInfos:
                tree.AddFile(
                    FileSystemCommandHelper.c_VirtualGcodeRoot + "/" + fileInfo.Path,
                    fileInfo.Path,
                    fileInfo.SizeBytes,
                    fileInfo.ModifiedTimeSec,
                )
            return CommandResponse.Success(tree.Serialize())
        except BambuFtpsError as e:
            return self._BuildFileCommandErrorResponse(CommandHandler.c_FilesListCommand, e)
        except Exception as e:
            return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, FileSystemCommandHelper.ExceptionError(CommandHandler.c_FilesListCommand, e))


    def ExecuteFileUpload(self, args:Optional[Dict[str, Any]], uploadBody:UploadBody) -> CommandResponse:
        parsedPath, errorStr = FileSystemCommandHelper.ParsePathArg(args)
        if errorStr is not None or parsedPath is None:
            return CommandResponse.Error(400, errorStr or FileSystemCommandHelper.InvalidPathError())
        if uploadBody.HasData is False:
            return CommandResponse.Error(400, FileSystemCommandHelper.EmptyUploadBodyError())

        try:
            ftpResponse = self._GetFileManager().UploadFile(parsedPath.RelativePath, uploadBody)
            response = FileSystemCommandHelper.BuildFileUploadSuccess(parsedPath, parsedPath.RelativePath, uploadBody.UploadBytesReceivedSoFar, str(ftpResponse).encode("utf-8"))
            return response
        except BambuFtpsError as e:
            return self._BuildFileCommandErrorResponse(CommandHandler.c_FilesUploadCommand, e)
        except Exception as e:
            return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, FileSystemCommandHelper.ExceptionError(CommandHandler.c_FilesUploadCommand, e))


    def ExecuteFileDownload(self, args:Optional[Dict[str, Any]]) -> HttpResult:
        parsedPath, errorStr = FileSystemCommandHelper.ParsePathArg(args)
        if errorStr is not None or parsedPath is None:
            return FileSystemCommandHelper.BuildRawError(400, errorStr or FileSystemCommandHelper.InvalidPathError(), CommandHandler.c_FilesDownloadCommand)

        try:
            tempPath, fileSizeBytes = self._GetFileManager().DownloadFileToTemp(parsedPath.RelativePath)
            fileName, _ = parsedPath.FileNameAndParent()
            return self._BuildDownloadedFileResult(tempPath, fileSizeBytes, fileName)
        except BambuFtpsError as e:
            return self._BuildFileCommandRawError(CommandHandler.c_FilesDownloadCommand, e)
        except Exception as e:
            return FileSystemCommandHelper.BuildRawError(CommandHandler.c_CommandError_ExecutionFailure, FileSystemCommandHelper.ExceptionError(CommandHandler.c_FilesDownloadCommand, e), CommandHandler.c_FilesDownloadCommand)


    def ExecuteGetPluginLogs(self, args:Optional[Dict[str, Any]]) -> HttpResult:
        return FileSystemCommandHelper.BuildLogFileResultFromLogger(self.Logger, "octoeverywhere.log", CommandHandler.c_GetPluginLogsCommand, "octoeverywhere.log", args)


    def ExecuteFileDelete(self, args:Optional[Dict[str, Any]]) -> CommandResponse:
        parsedPath, errorStr = FileSystemCommandHelper.ParsePathArg(args)
        if errorStr is not None or parsedPath is None:
            return CommandResponse.Error(400, errorStr or FileSystemCommandHelper.InvalidPathError())

        try:
            ftpResponse = self._GetFileManager().DeleteFile(parsedPath.RelativePath)
            return FileSystemCommandHelper.BuildFileDeleteSuccess(parsedPath, parsedPath.RelativePath, str(ftpResponse).encode("utf-8"))
        except BambuFtpsError as e:
            return self._BuildFileCommandErrorResponse(CommandHandler.c_FilesDeleteCommand, e)
        except Exception as e:
            return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, FileSystemCommandHelper.ExceptionError(CommandHandler.c_FilesDeleteCommand, e))


    def _GetFileManager(self) -> BambuFileManager:
        if self.FileManagerFactory is not None:
            return self.FileManagerFactory()
        return BambuFileManager(
            self.Logger,
            self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None),
            self.Config.GetStr(Config.SectionBambu, Config.BambuAccessToken, None)
        )


    def _BuildDownloadedFileResult(self, tempPath:str, fileSizeBytes:int, downloadFileName:str) -> HttpResult:
        try:
            fileObj:BinaryIO = open(tempPath, "rb") #pylint: disable=consider-using-with
        except Exception as e:
            self._DeleteTempFile(tempPath)
            return FileSystemCommandHelper.BuildRawError(500, "Failed to open downloaded Bambu file: " + str(e), CommandHandler.c_FilesDownloadCommand)

        bytesRemaining = fileSizeBytes

        def readChunk() -> Optional[Buffer]:
            nonlocal bytesRemaining
            if bytesRemaining <= 0:
                return None
            data = fileObj.read(min(bytesRemaining, BambuFileManager.c_DownloadBlockSizeBytes))
            if len(data) == 0:
                return None
            bytesRemaining -= len(data)
            return Buffer(data)

        def closeFile() -> None:
            try:
                fileObj.close()
            except Exception as e:
                self.Logger.warning("Failed to close Bambu download temp file. %s", str(e))
            self._DeleteTempFile(tempPath)

        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(fileSizeBytes),
            "Content-Disposition": "attachment; filename=\"" + self._HeaderQuote(downloadFileName) + "\"",
        }
        return HttpResult(200, headers, CommandHandler.c_FilesDownloadCommand, False, customBodyStreamCallback=readChunk, customBodyStreamClosedCallback=closeFile)


    def _BuildFileCommandErrorResponse(self, commandName:str, error:BambuFtpsError) -> CommandResponse:
        statusCode, message = self._MapFileCommandError(commandName, error)
        return CommandResponse.Error(statusCode, message)


    def _BuildFileCommandRawError(self, commandName:str, error:BambuFtpsError) -> HttpResult:
        statusCode, message = self._MapFileCommandError(commandName, error)
        return FileSystemCommandHelper.BuildRawError(statusCode, message, commandName)


    def _MapFileCommandError(self, commandName:str, error:BambuFtpsError) -> Any:
        if error.IsInvalidPath:
            return (400, FileSystemCommandHelper.InvalidPathError())
        if error.IsAuthError:
            return (CommandHandler.c_CommandError_LostAuth, FileSystemCommandHelper.AuthFailedError("Bambu", commandName))
        if error.IsConnectionError:
            return (CommandHandler.c_CommandError_HostNotConnected, FileSystemCommandHelper.PrinterNotConnectedError("Bambu", commandName))
        if error.IsNotFound:
            return (404, f"{commandName} failed on Bambu: file was not found.")
        return (CommandHandler.c_CommandError_ExecutionFailure, FileSystemCommandHelper.CleanErrorForApi(error.Message))


    def _GetBoolArg(self, args:Optional[Dict[str, Any]], defaultValue:bool, *keys:str) -> bool:
        if args is None:
            return defaultValue
        value = FileSystemCommandHelper.GetFirstArg(args, *keys)
        if value is None:
            return defaultValue
        if isinstance(value, bool):
            return value
        valueStr = str(value).lower().strip()
        return valueStr == "true" or valueStr == "1" or valueStr == "yes"


    def _GetIntArg(self, args:Optional[Dict[str, Any]], defaultValue:int, *keys:str) -> int:
        if args is None:
            return defaultValue
        value = FileSystemCommandHelper.GetFirstArg(args, *keys)
        if value is None:
            return defaultValue
        try:
            return int(str(value).strip())
        except Exception:
            return defaultValue


    def _BuildStartPrintPayload(self, printerPath:str, args:Optional[Dict[str, Any]]) -> Dict[str, Any]:
        lowerPath = printerPath.lower()
        if lowerPath.endswith(".3mf"):
            return self._BuildProjectFileStartPayload(printerPath, args)
        return {
            "print": {
                "sequence_id": "0",
                "command": "gcode_file",
                "param": printerPath,
            }
        }


    def _BuildProjectFileStartPayload(self, printerPath:str, args:Optional[Dict[str, Any]]) -> Dict[str, Any]:
        plate = self._GetIntArg(args, 1, "Plate", "plate", "PlateIndex", "plateIndex", "plate_index")
        if plate < 1:
            plate = 1

        fileName = printerPath
        slash = fileName.rfind("/")
        if slash != -1:
            fileName = fileName[slash + 1:]
        printObj:Dict[str, Any] = {
            "sequence_id": "0",
            "command": "project_file",
            "param": f"Metadata/plate_{plate}.gcode",
            "url": "ftp:///" + quote(printerPath, safe="/"),
            "subtask_name": self._RemoveKnownBambuExtension(fileName),
            "use_ams": self._GetBoolArg(args, True, "UseAms", "useAms", "use_ams"),
            "flow_cali": self._GetBoolArg(args, True, "FlowCali", "flowCali", "flow_cali"),
        }

        amsMapping = self._GetAmsMappingArg(args)
        if amsMapping is not None:
            printObj["ams_mapping"] = amsMapping

        self._AddOptionalBoolArg(printObj, args, "timelapse", "Timelapse", "timeLapse", "time_lapse")
        self._AddOptionalBoolArg(printObj, args, "bed_levelling", "BedLeveling", "BedLevelling", "bedLeveling", "bedLevelling", "bed_leveling", "bed_levelling")
        self._AddOptionalBoolArg(printObj, args, "vibration_cali", "VibrationCali", "vibrationCali", "vibration_cali")
        self._AddOptionalBoolArg(printObj, args, "layer_inspect", "LayerInspect", "layerInspect", "layer_inspect")

        return {
            "print": printObj
        }


    def _AddOptionalBoolArg(self, target:Dict[str, Any], args:Optional[Dict[str, Any]], targetKey:str, *sourceKeys:str) -> None:
        if args is None:
            return
        value = FileSystemCommandHelper.GetFirstArg(args, *sourceKeys)
        if value is None:
            return
        target[targetKey] = self._GetBoolArg(args, False, *sourceKeys)


    def _GetAmsMappingArg(self, args:Optional[Dict[str, Any]]) -> Optional[List[int]]:
        if args is None:
            return None
        value = FileSystemCommandHelper.GetFirstArg(args, "AmsMapping", "amsMapping", "ams_mapping")
        if value is None:
            return None
        if isinstance(value, str):
            valueStr = value.strip()
            if len(valueStr) == 0:
                return None
            if valueStr.startswith("["):
                try:
                    value = json.loads(valueStr)
                except Exception:
                    return None
            else:
                value = [v.strip() for v in valueStr.split(",")]
        if not isinstance(value, list):
            return None
        mapping:List[int] = []
        for item in value:
            try:
                mapping.append(int(item))
            except Exception:
                return None
        return mapping


    def _RemoveKnownBambuExtension(self, fileName:str) -> str:
        lower = fileName.lower()
        if lower.endswith(".gcode.3mf"):
            return fileName[:-10]
        if lower.endswith(".3mf"):
            return fileName[:-4]
        if lower.endswith(".gcode"):
            return fileName[:-6]
        if lower.endswith(".bgcode"):
            return fileName[:-7]
        return fileName


    def _HeaderQuote(self, value:str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


    def _DeleteTempFile(self, tempPath:str) -> None:
        try:
            if os.path.exists(tempPath):
                os.remove(tempPath)
        except Exception as e:
            self.Logger.warning("Failed to delete Bambu download temp file %s. %s", tempPath, str(e))
