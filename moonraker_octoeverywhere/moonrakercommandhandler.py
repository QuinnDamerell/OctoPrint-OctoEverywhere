import json
import logging
from typing import Any, Dict, List, Optional, Union

from octoeverywhere.commandhandler import CommandHandler, CommandResponse
from octoeverywhere.filesystemcommands import FileSystemCommandHelper, FileSystemTreeBuilder, VirtualFilePath, VirtualFileSystemTree
from octoeverywhere.httpresult import HttpResult
from octoeverywhere.interfaces import IPlatformCommandHandler, ConnectionInfo, FEATURE_LIGHT_CONTROL, FEATURE_HOMING, FEATURE_AXIS_MOVEMENT, FEATURE_EXTRUSION, FEATURE_TEMPERATURE_CONTROL, FEATURE_PRINT_START
from octoeverywhere.localip import LocalIpHelper
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.WebStream.uploadbody import MultipartFormUploadBody, UploadBody
from linux_host.config import Config

from .moonrakerclient import MoonrakerClient
from .smartpause import SmartPause
from .filemetadatacache import FileMetadataCache
from .jsonrpcresponse import JsonRpcResponse
from .lightmanager import LightManager

# This class implements the Platform Command Handler Interface
class MoonrakerCommandHandler(IPlatformCommandHandler):
    c_MoonrakerVirtualFileRoots = [
        FileSystemCommandHelper.c_VirtualGcodeRoot,
        FileSystemCommandHelper.c_VirtualConfigRoot,
        FileSystemCommandHelper.c_VirtualLogsRoot,
    ]
    c_MoonrakerAllowedFileRoots = set(c_MoonrakerVirtualFileRoots)
    # Moonraker's logs root is read-only - only the gcodes and config roots accept upload/delete.
    c_MoonrakerWritableFileRoots = {
        FileSystemCommandHelper.c_VirtualGcodeRoot,
        FileSystemCommandHelper.c_VirtualConfigRoot,
    }
    c_MoonrakerRootByVirtualRoot = {
        FileSystemCommandHelper.c_VirtualGcodeRoot: "gcodes",
        FileSystemCommandHelper.c_VirtualConfigRoot: "config",
        FileSystemCommandHelper.c_VirtualLogsRoot: "logs",
    }

    def __init__(self, logger:logging.Logger, config:Config) -> None:
        self.Logger = logger
        self.Config = config


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

        # Build the query objects dict, including light objects if available
        query_objects: Dict[str, None] = {
            "print_stats": None,    # Needed for many things, including GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsAndVirtualSdCardResult
            "gcode_move": None,     # Needed for GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsAndVirtualSdCardResult to get the current speed
            "virtual_sdcard": None, # Needed for many things, including GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsAndVirtualSdCardResult
            "extruder": None,       # Needed for temps
            "heater_bed": None,     # Needed for temps
        }

        # Add light objects to the query if any are detected
        light_objects = LightManager.Get().GetLightObjectNames()
        query_objects.update(light_objects)

        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.objects.query",
        {
            "objects": query_objects
        })
        # Validate
        if result.HasError():
            self.Logger.error("MoonrakerCommandHandler failed GetCurrentJobStatus() query. "+result.GetLoggingErrorStr())
            errorStr = result.ErrorStr.lower() if result.ErrorStr is not None else ""
            if result.ErrorCode == JsonRpcResponse.MR_401_UNAUTHORIZED or errorStr == "unauthorized" or MoonrakerClient.Get().IsDisconnectDueToAuth():
                return CommandHandler.c_CommandError_LostAuth
            return None

        # Get the result.
        res = result.GetResult()

        # Map the state
        state = "idle"
        statusObjectOrEmptyDict:Dict[str, Any] = res.get("status", {})
        mrState = statusObjectOrEmptyDict.get("print_stats", {}).get("state", None)
        if mrState is not None:
            # https://moonraker.readthedocs.io/en/latest/printer_objects/#print_stats
            if mrState == "standby":
                state = "idle"
            elif mrState == "printing":
                # This is a special case, we consider "warmingup" a subset of printing.
                if MoonrakerClient.Get().GetMoonrakerCompat().CheckIfPrinterIsWarmingUp_WithPrintStats(result):
                    state = "warmingup"
                else:
                    state = "printing"
            elif mrState == "paused":
                state = "paused"
            elif mrState == "complete":
                state = "complete"
            elif mrState == "cancelled":
                state = "cancelled"
            elif mrState == "error":
                state = "error"
            else:
                self.Logger.warning("Unknown mrState returned from print_stats: "+str(mrState))
        else:
            self.Logger.warning("MoonrakerCommandHandler failed to find the print_stats.status")

        # TODO - If in an error state, set some context as to why.
        # This is shown to the user directly, so it must be short (think of a dashboard status) and formatted well.
        errorStr:Optional[str] = None

        # Get current layer info
        # None = The platform doesn't provide it.
        # 0 = The platform provider it, but there's no info yet.
        # # = The values
        # Note this is similar to how we also do it for notifications.
        currentLayerInt:Optional[int] = None
        totalLayersInt:Optional[int] = None
        currentLayerRaw, totalLayersRaw = MoonrakerClient.Get().GetMoonrakerCompat().GetCurrentLayerInfo()
        if totalLayersRaw is not None and totalLayersRaw > 0 and currentLayerRaw is not None and currentLayerRaw >= 0:
            currentLayerInt = int(currentLayerRaw)
            totalLayersInt = int(totalLayersRaw)

        # Get duration and filename.
        durationSec:int = 0
        fileName = ""
        ps = statusObjectOrEmptyDict.get("print_stats", None)
        if ps is not None:
            # We choose to use print_duration over "total_duration" so we only show the time actually spent printing. This is consistent across platforms.
            print_duration = ps.get("print_duration", None)
            if print_duration is not None:
                durationSec = int(print_duration)
            psFileName = ps.get("filename", None)
            if psFileName is not None:
                fileName = psFileName

        # If we have a file name, try to get the current filament usage.
        filamentUsageMm:int = 0
        if fileName is not None and len(fileName) > 0:
            filamentUsageMm = FileMetadataCache.Get().GetEstimatedFilamentUsageMm(fileName)

        # Get the progress
        progress = 0.0
        vsProgress = statusObjectOrEmptyDict.get("virtual_sdcard", {}).get("progress", None)
        if vsProgress is not None:
            # Convert progress 0->1 to 0->100
            progress = vsProgress * 100.0

        # Time left can be hard to compute correctly, so use the common function to do it based
        # on what we can get as a best effort.
        timeLeftSec = MoonrakerClient.Get().GetMoonrakerCompat().GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsVirtualSdCardAndGcodeMoveResult(result)

        # Get the current temps if possible.
        # Shared code with MoonrakerClient.GetTemps
        hotendActual = 0.0
        hotendTarget = 0.0
        bedTarget = 0.0
        bedActual = 0.0
        extruder = statusObjectOrEmptyDict.get("extruder", None)
        if extruder is not None:
            temp = extruder.get("temperature", None)
            if temp is not None:
                hotendActual = round(float(temp), 2)
            target = extruder.get("target", None)
            if target is not None:
                hotendTarget = round(float(target), 2)
        heater_bed = statusObjectOrEmptyDict.get("heater_bed", None)
        if heater_bed is not None:
            temp = heater_bed.get("temperature", None)
            if temp is not None:
                bedActual = round(float(temp), 2)
            target = heater_bed.get("target", None)
            if target is not None:
                bedTarget = round(float(target), 2)

        # Get the light status if available
        lights:Optional[List[Dict[str, Any]]] = None
        try:
            light_status_objects = LightManager.Get().GetLightStatus(statusObjectOrEmptyDict)
            # Convert LightStatus objects to dicts for JSON serialization
            if light_status_objects:
                lights = [{"Name": ls.Name, "On": ls.IsOn} for ls in light_status_objects]
        except Exception as e:
            self.Logger.debug(f"Failed to get light status: {e}")

        # Build the object and return.
        return {
            "State": state,
            "Error": errorStr,
            # List of lights with their status, or None if not supported/unknown
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
        # We don't supply this for moonraker at the moment.
        return "1.0.0"


    # !! Platform Command Handler Interface Function !!
    # Returns an int with the supported feature flags for this platform, such as FEATURE_LIGHT_CONTROL, etc
    def GetSupportedFeatureFlags(self) -> int:
        # These are all we support right now.
        return 0 | FEATURE_LIGHT_CONTROL | FEATURE_HOMING | FEATURE_AXIS_MOVEMENT | FEATURE_EXTRUSION | FEATURE_TEMPERATURE_CONTROL | FEATURE_PRINT_START


    # !! Platform Command Handler Interface Function !!
    # Moonraker doesn't have serial number or access code fields in our config.
    def GetConnectionInfo(self) -> ConnectionInfo:
        # We get the port from the config or from the Moonraker config file, but for the local IP we always use the local IP helper.
        # The IP in the config is sometimes local host, or whatever, and we need the actual device IP.
        (_, port) = MoonrakerClient.Get().GetMoonrakerHostAndPortFromConfig()
        return ConnectionInfo(
            LocalIpHelper.TryToGetLocalIpOfConnectionTarget(),
            port,
        )


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the pause and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecutePause(self, smartPause:bool, suppressNotificationBool:bool, disableHotendBool:bool, disableBedBool:bool, zLiftMm:int, retractFilamentMm:int, showSmartPausePopup:bool) -> CommandResponse:
        # Check the state and that we have a connection to the host.
        result = self._CheckIfConnectedAndForExpectedStates(["printing"])
        if result is not None:
            return result

        # The smart pause logic handles all pause commands.
        return SmartPause.Get().ExecuteSmartPause(suppressNotificationBool)


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the resume and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteResume(self) -> CommandResponse:
        # Check the state and that we have a connection to the host.
        result = self._CheckIfConnectedAndForExpectedStates(["paused"])
        if result is not None:
            return result

        # Do the resume.
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.print.resume", {})
        if result.HasError():
            self.Logger.error("ExecuteResume failed to request resume. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(400, "Failed to request resume")

        # Ensure the response is a simple result.
        if result.IsSimpleResult() is False:
            self.Logger.error("ExecuteResume didn't return a simple result. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(400, "Bad result type")

        # Check the response, we expect a simple response.
        if result.GetSimpleResult() != "ok":
            self.Logger.error("ExecuteResume got an invalid request response. "+json.dumps(result.GetSimpleResult()))
            return CommandResponse.Error(400, "Invalid request response.")

        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the cancel and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteCancel(self) -> CommandResponse:
        # Check the state and that we have a connection to the host.
        result = self._CheckIfConnectedAndForExpectedStates(["printing","paused"])
        if result is not None:
            return result

        # Do the resume.
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.print.cancel", {})
        if result.HasError():
            self.Logger.error("ExecuteCancel failed to request cancel. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(400, "Failed to request cancel")

        # Ensure the response is a simple result.
        if result.IsSimpleResult() is False:
            self.Logger.error("ExecuteCancel didn't return a simple result. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(400, "Bad result type")

        # Check the response
        if result.GetSimpleResult() != "ok":
            self.Logger.error("ExecuteCancel got an invalid request response. "+json.dumps(result.GetSimpleResult()))
            return CommandResponse.Error(400, "Invalid request response.")

        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # Starts a print from a virtual file system path.
    def ExecuteStart(self, args:Optional[Dict[str, Any]]) -> CommandResponse:
        parsedPath, errorStr = FileSystemCommandHelper.ParsePathArg(args)
        if errorStr is not None or parsedPath is None:
            return CommandResponse.Error(400, errorStr or FileSystemCommandHelper.InvalidPathError())

        platformPath = self._GetPlatformPath(parsedPath)
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.print.start", {
            "filename": platformPath
        }, timeoutSec=60.0)
        if result.HasError():
            code = result.GetErrorCode()
            if code == JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED:
                return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, FileSystemCommandHelper.PrinterNotConnectedError("Moonraker", CommandHandler.c_StartCommand))
            if code == JsonRpcResponse.MR_401_UNAUTHORIZED or MoonrakerClient.Get().IsDisconnectDueToAuth():
                return CommandResponse.Error(CommandHandler.c_CommandError_LostAuth, FileSystemCommandHelper.AuthFailedError("Moonraker", CommandHandler.c_StartCommand))
            self.Logger.error("ExecuteStart failed to request start. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(400, result.GetErrorStr() or "Failed to request start")

        if result.IsSimpleResult() and result.GetSimpleResult() != "ok":
            self.Logger.error("ExecuteStart got an invalid request response. "+json.dumps(result.GetSimpleResult()))
            return CommandResponse.Error(400, "Invalid request response.")

        return FileSystemCommandHelper.BuildFileStartSuccess(parsedPath, platformPath, result.GetRawResponseOrResult())


    # !! Platform Command Handler Interface Function !!
    # Sets the light state for the specified light type.
    def ExecuteSetLight(self, lightName:str, on:bool) -> CommandResponse:
        # Check if we have any lights available
        if not LightManager.Get().HasLights():
            self.Logger.info("ExecuteSetLight: No lights detected in printer configuration")
            return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "No lights detected in printer configuration")

        # Attempt to set the light state
        success = LightManager.Get().SetLightState(lightName, on)
        if success:
            self.Logger.info(f"ExecuteSetLight: Successfully set light to {'ON' if on else 'OFF'}")
            return CommandResponse.Success(None)
        else:
            self.Logger.error("ExecuteSetLight: Failed to set light state")
            return CommandResponse.Error(500, "Failed to set light state")


    # !! Platform Command Handler Interface Function !!
    # Moves the specified axis by the given distance in mm.
    def ExecuteMoveAxis(self, axis:str, distanceMm:float) -> CommandResponse:
        # Validate axis parameter
        axis_upper = axis.upper()
        if axis_upper not in ["X", "Y", "Z"]:
            self.Logger.error(f"ExecuteMoveAxis: Invalid axis '{axis}'")
            return CommandResponse.Error(400, "Invalid axis. Must be X, Y, or Z")

        # Build G-code command
        # G91: Set to relative positioning
        # G1: Linear move with feedrate
        # Feedrate: 3000 mm/min (50 mm/s) for X/Y, 500 mm/min for Z
        feedrate = 500 if axis_upper == "Z" else 3000
        gcode = f"G91\nG1 {axis_upper}{distanceMm} F{feedrate}\nG90"

        # Execute G-code
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.gcode.script", {
            "script": gcode
        })

        if result.HasError():
            self.Logger.error(f"ExecuteMoveAxis failed: {result.GetLoggingErrorStr()}")
            return CommandResponse.Error(500, "Failed to move axis: "+result.GetLoggingErrorStr())

        self.Logger.info(f"ExecuteMoveAxis: Successfully moved {axis_upper} by {distanceMm}mm")
        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # Homes all axes.
    def ExecuteHome(self) -> CommandResponse:

        # G28: Home all axes
        gcode = "G28"

        # Execute G-code
        # Most printers will block until the homing is done, which can include heating the bed and such.
        # So we set a long timeout so we don't miss the actual result.
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.gcode.script", {
            "script": gcode
        }, timeoutSec=120.0)

        if result.HasError():
            self.Logger.error(f"ExecuteHome failed: {result.GetLoggingErrorStr()}")
            return CommandResponse.Error(500, "Failed to home axes: "+result.GetLoggingErrorStr())

        self.Logger.info("ExecuteHome: Successfully homed all axes")
        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # Extrudes or retracts filament for the specified extruder.
    def ExecuteExtrude(self, extruder:int, distanceMm:float) -> CommandResponse:
        # Validate extruder parameter
        if extruder < 0 or extruder > 10:
            self.Logger.error(f"ExecuteExtrude: Invalid extruder index '{extruder}'")
            return CommandResponse.Error(400, "Invalid extruder index")

        # Build G-code command
        # M83: Set extruder to relative mode
        # G1: Extrude with feedrate
        # Feedrate: 300 mm/min (5 mm/s) for extrusion
        # T{n}: Select extruder (if not extruder 0)
        extruder_select = f"T{extruder}\n" if extruder > 0 else ""
        gcode = f"{extruder_select}M83\nG1 E{distanceMm} F300"

        # Execute G-code
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.gcode.script", {
            "script": gcode
        })

        if result.HasError():
            self.Logger.error(f"ExecuteExtrude failed: {result.GetLoggingErrorStr()}")
            return CommandResponse.Error(500, "Failed to extrude: "+result.GetLoggingErrorStr())

        action = "extruded" if distanceMm > 0 else "retracted"
        self.Logger.info(f"ExecuteExtrude: Successfully {action} {abs(distanceMm)}mm on extruder {extruder}")
        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # Sets the temperature for bed, chamber, or tool.
    def ExecuteSetTemp(self, bedC:Optional[float], chamberC:Optional[float], toolC:Optional[float], toolNumber:Optional[int]) -> CommandResponse:

        # Build G-code commands
        gcode_commands:List[str] = []

        if bedC:
            # M140: Set bed temperature without waiting
            gcode_commands.append(f"M140 S{bedC}")

        if toolC:
            # M104: Set hotend temperature without waiting
            gcode_commands.append(f"M104 S{toolC}")

        if chamberC:
            # Chamber heating in Klipper is typically done via heater_generic
            # Use SET_HEATER_TEMPERATURE command if chamber heater exists
            # Note: This assumes chamber heater is named "chamber" in printer.cfg
            gcode_commands.append(f"SET_HEATER_TEMPERATURE HEATER=chamber TARGET={chamberC}")

        # Combine all commands with newlines
        gcode = "\n".join(gcode_commands)

        # Execute G-code
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.gcode.script", {
            "script": gcode
        })

        if result.HasError():
            self.Logger.error(f"ExecuteSetTemp failed: {result.GetLoggingErrorStr()}")
            return CommandResponse.Error(500, "Failed to set temperature: "+result.GetLoggingErrorStr())

        # Build success message
        targets:List[str] = []
        if bedC:
            targets.append(f"bed to {bedC}°C")
        if toolC:
            targets.append(f"tool to {toolC}°C")
        if chamberC:
            targets.append(f"chamber to {chamberC}°C")

        self.Logger.info(f"ExecuteSetTemp: Successfully set {', '.join(targets)}")
        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # Sends a Moonraker JSON-RPC payload and returns the JSON-RPC result.
    def ExecuteSendCommand(self, transportType:str, request:Dict[str, Any], rawPayload:Dict[str, Any]) -> CommandResponse:
        if transportType != "websocket":
            return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, f"This is a Moonraker (Klipper) printer, which only accepts send-command requests with TransportType 'websocket'. The received TransportType was '{transportType}'. Set 'TransportType' to 'websocket' and put the Moonraker JSON-RPC call in 'Request'. Example: {{\"TransportType\": \"websocket\", \"Request\": {{\"Method\": \"printer.objects.query\", \"Params\": {{\"objects\": {{\"toolhead\": null}}}}}}}}.")

        parsed = CommandHandler.ParseWebsocketSendCommand(rawPayload, request)
        if isinstance(parsed, CommandResponse):
            return parsed

        # Moonraker speaks JSON-RPC, so the command identifier must be a method string.
        method = parsed.Method
        if method is None or not isinstance(method, str) or len(method) == 0:
            return CommandResponse.Error(400, f"This Moonraker (Klipper) printer speaks JSON-RPC, so the 'Request' object must include a non-empty string 'Method' naming the JSON-RPC method to call (e.g. 'printer.objects.query', 'printer.gcode.script'), but the received method value was {json.dumps(method, default=str)}. Provide it as \"Request\": {{\"Method\": \"<json-rpc method>\", \"Params\": {{...}}}}.")

        requestEcho:Dict[str, Any] = {"Method": method, "Params": parsed.Params}
        result = MoonrakerClient.Get().SendJsonRpcRequest(method, parsed.Params, timeoutSec=parsed.TimeoutSec, waitForResponse=parsed.WaitForResponse)
        if result.HasError():
            code = result.GetErrorCode()
            # Transport/connection/auth failures are returned as actionable OE error codes (no useful payload).
            if code == JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED:
                return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, "Printer Not Connected")
            if code == JsonRpcResponse.MR_401_UNAUTHORIZED:
                return CommandResponse.Error(CommandHandler.c_CommandError_LostAuth, "Unauthorized - refresh the Moonraker API key or credentials.")
            if code == JsonRpcResponse.OE_ERROR_TIMEOUT:
                return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, "No response received from the printer within the timeout. Some commands don't return a response - set WaitForResponse to false for those.")
            if code == JsonRpcResponse.OE_ERROR_EXCEPTION:
                return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, result.GetErrorStr() or "Failed to send command.")
            # A real JSON-RPC protocol error (e.g. unknown method, a gcode error). Surface the full native response.
            return CommandHandler.BuildSendCommandResult("websocket", requestEcho, result.GetRawResponseOrError(), isError=True, waitForResponse=parsed.WaitForResponse, timeoutSec=parsed.TimeoutSec)

        # Fire-and-forget: nothing was awaited.
        if parsed.WaitForResponse is False:
            return CommandHandler.BuildSendCommandResult("websocket", requestEcho, responseReceived=False, waitForResponse=parsed.WaitForResponse, timeoutSec=parsed.TimeoutSec)

        response = result.GetRawResponseOrResult()
        return CommandHandler.BuildSendCommandResult("websocket", requestEcho, response, isError=False, waitForResponse=parsed.WaitForResponse, timeoutSec=parsed.TimeoutSec)


    def ExecuteFileList(self, args:Optional[Dict[str, Any]]) -> CommandResponse:
        virtualRoots, errorStr = FileSystemCommandHelper.ResolveRequestedRoots(args, MoonrakerCommandHandler.c_MoonrakerVirtualFileRoots, FileSystemCommandHelper.c_RootAliases)
        if errorStr is not None:
            return CommandResponse.Error(400, errorStr)

        def addToTree(tree:VirtualFileSystemTree, virtualRoot:str, fileList:List[Dict[str, Any]]) -> None:
            FileSystemTreeBuilder.AddMoonrakerFileListToTree(tree, fileList, virtualRoot)

        return FileSystemCommandHelper.BuildMultiRootFileListResponse(
            virtualRoots,
            self._ListMoonrakerFileRoot,
            addToTree,
            self.Logger,
            "Moonraker",
            CommandHandler.c_CommandError_ExecutionFailure)


    def _ListMoonrakerFileRoot(self, virtualRoot:str) -> Union[List[Dict[str, Any]], CommandResponse]:
        moonrakerRoot = self._GetMoonrakerRoot(virtualRoot)
        path = "/server/files/list?root=" + moonrakerRoot
        headers:Dict[str, str] = {}
        self._AddMoonrakerAuth(headers)
        result = OctoHttpRequest.MakeHttpCall(
            self.Logger,
            path,
            OctoHttpRequest.GetPathType(path),
            "GET",
            headers,
            timeoutSec=60.0
        )
        if result is None:
            return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, FileSystemCommandHelper.PrinterNotConnectedError("Moonraker", "file-list"))

        try:
            result.ReadAllContentFromStreamResponse(self.Logger)
            bodyBytes = b""
            if result.FullBodyBuffer is not None:
                bodyBytes = bytes(result.FullBodyBuffer.GetBytesLike())
            if result.StatusCode == 401 or result.StatusCode == 403:
                return CommandResponse.Error(CommandHandler.c_CommandError_LostAuth, FileSystemCommandHelper.AuthFailedError("Moonraker", "file-list"))
            if result.StatusCode < 200 or result.StatusCode >= 300:
                return CommandResponse.Error(result.StatusCode, FileSystemCommandHelper.BackendHttpError("Moonraker", "file-list", result.StatusCode, bodyBytes))

            try:
                responseObj = json.loads(bodyBytes.decode("utf-8"))
            except Exception:
                return CommandResponse.Error(502, FileSystemCommandHelper.InvalidJsonResponseError("Moonraker", "file-list"))
            fileList:Any = responseObj
            if isinstance(responseObj, dict) and "result" in responseObj:
                fileList = responseObj["result"]
            if not isinstance(fileList, list):
                fileList = []
            return fileList
        finally:
            result.Free()


    def ExecuteFileUpload(self, args:Optional[Dict[str, Any]], uploadBody:UploadBody) -> CommandResponse:
        parsedPath, errorStr = FileSystemCommandHelper.ParsePathArg(args, MoonrakerCommandHandler.c_MoonrakerWritableFileRoots)
        if errorStr is not None or parsedPath is None:
            return CommandResponse.Error(400, errorStr or FileSystemCommandHelper.InvalidPathError(MoonrakerCommandHandler.c_MoonrakerWritableFileRoots))
        if uploadBody.HasData is False:
            return CommandResponse.Error(400, FileSystemCommandHelper.EmptyUploadBodyError())

        fileName, parentPath = parsedPath.FileNameAndParent()
        fields:Dict[str, str] = {
            "root": self._GetMoonrakerRoot(parsedPath.Root)
        }
        if len(parentPath) > 0:
            fields["path"] = parentPath

        multipartBody = MultipartFormUploadBody(self.Logger, uploadBody, fileName, fields)
        headers = {
            "Content-Type": multipartBody.GetContentType(),
            "Content-Length": str(multipartBody.GetContentLength()),
        }
        self._AddMoonrakerAuth(headers)
        result = OctoHttpRequest.MakeHttpCall(
            self.Logger,
            "/server/files/upload",
            OctoHttpRequest.GetPathType("/server/files/upload"),
            "POST",
            headers,
            multipartBody, #pyright: ignore[reportArgumentType]
            allowRedirects=False,
            timeoutSec=1800.0
        )
        if result is None:
            return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, FileSystemCommandHelper.PrinterNotConnectedError("Moonraker", CommandHandler.c_FilesUploadCommand))

        try:
            result.ReadAllContentFromStreamResponse(self.Logger)
            bodyBytes = b""
            if result.FullBodyBuffer is not None:
                bodyBytes = bytes(result.FullBodyBuffer.GetBytesLike())
            if result.StatusCode == 401 or result.StatusCode == 403:
                return CommandResponse.Error(CommandHandler.c_CommandError_LostAuth, FileSystemCommandHelper.AuthFailedError("Moonraker", CommandHandler.c_FilesUploadCommand))
            if result.StatusCode < 200 or result.StatusCode >= 300:
                return CommandResponse.Error(result.StatusCode, FileSystemCommandHelper.BackendHttpError("Moonraker", CommandHandler.c_FilesUploadCommand, result.StatusCode, bodyBytes))
            platformPath = self._GetPlatformPath(parsedPath)
            return FileSystemCommandHelper.BuildFileUploadSuccess(parsedPath, platformPath, uploadBody.UploadBytesReceivedSoFar, bodyBytes)
        finally:
            result.Free()


    def ExecuteFileDownload(self, args:Optional[Dict[str, Any]]) -> HttpResult:
        parsedPath, errorStr = FileSystemCommandHelper.ParsePathArg(args, MoonrakerCommandHandler.c_MoonrakerAllowedFileRoots)
        if errorStr is not None or parsedPath is None:
            return FileSystemCommandHelper.BuildRawError(400, errorStr or FileSystemCommandHelper.InvalidPathError(MoonrakerCommandHandler.c_MoonrakerAllowedFileRoots), CommandHandler.c_FilesDownloadCommand)

        headers:Dict[str, str] = {}
        self._AddMoonrakerAuth(headers)
        relativePath = FileSystemCommandHelper.EncodeRelativePathForUrl(parsedPath.RelativePath)
        downloadPath = "/server/files/" + self._GetMoonrakerRoot(parsedPath.Root) + "/" + relativePath
        result = OctoHttpRequest.MakeHttpCall(
            self.Logger,
            downloadPath,
            OctoHttpRequest.GetPathType(downloadPath),
            "GET",
            headers,
            timeoutSec=1800.0
        )
        if result is None:
            return FileSystemCommandHelper.BuildRawError(CommandHandler.c_CommandError_HostNotConnected, FileSystemCommandHelper.PrinterNotConnectedError("Moonraker", CommandHandler.c_FilesDownloadCommand), CommandHandler.c_FilesDownloadCommand)
        if result.StatusCode < 200 or result.StatusCode >= 300:
            try:
                result.ReadAllContentFromStreamResponse(self.Logger)
                bodyBytes = b""
                if result.FullBodyBuffer is not None:
                    bodyBytes = bytes(result.FullBodyBuffer.GetBytesLike())
                if result.StatusCode == 401 or result.StatusCode == 403:
                    return FileSystemCommandHelper.BuildRawError(CommandHandler.c_CommandError_LostAuth, FileSystemCommandHelper.AuthFailedError("Moonraker", CommandHandler.c_FilesDownloadCommand), CommandHandler.c_FilesDownloadCommand)
                return FileSystemCommandHelper.BuildRawError(result.StatusCode, FileSystemCommandHelper.BackendHttpError("Moonraker", CommandHandler.c_FilesDownloadCommand, result.StatusCode, bodyBytes), CommandHandler.c_FilesDownloadCommand)
            finally:
                result.Free()
        return result


    def ExecuteGetPluginLogs(self, args:Optional[Dict[str, Any]]) -> HttpResult:
        return FileSystemCommandHelper.BuildLogFileResultFromLogger(self.Logger, "octoeverywhere.log", CommandHandler.c_GetPluginLogsCommand, "octoeverywhere.log", args)


    def ExecuteFileDelete(self, args:Optional[Dict[str, Any]]) -> CommandResponse:
        parsedPath, errorStr = FileSystemCommandHelper.ParsePathArg(args, MoonrakerCommandHandler.c_MoonrakerWritableFileRoots)
        if errorStr is not None or parsedPath is None:
            return CommandResponse.Error(400, errorStr or FileSystemCommandHelper.InvalidPathError(MoonrakerCommandHandler.c_MoonrakerWritableFileRoots))

        headers:Dict[str, str] = {}
        self._AddMoonrakerAuth(headers)
        relativePath = FileSystemCommandHelper.EncodeRelativePathForUrl(parsedPath.RelativePath)
        deletePath = "/server/files/" + self._GetMoonrakerRoot(parsedPath.Root) + "/" + relativePath
        result = OctoHttpRequest.MakeHttpCall(
            self.Logger,
            deletePath,
            OctoHttpRequest.GetPathType(deletePath),
            "DELETE",
            headers,
            timeoutSec=60.0
        )
        if result is None:
            return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, FileSystemCommandHelper.PrinterNotConnectedError("Moonraker", CommandHandler.c_FilesDeleteCommand))

        try:
            result.ReadAllContentFromStreamResponse(self.Logger)
            bodyBytes = b""
            if result.FullBodyBuffer is not None:
                bodyBytes = bytes(result.FullBodyBuffer.GetBytesLike())
            if result.StatusCode == 401 or result.StatusCode == 403:
                return CommandResponse.Error(CommandHandler.c_CommandError_LostAuth, FileSystemCommandHelper.AuthFailedError("Moonraker", CommandHandler.c_FilesDeleteCommand))
            if result.StatusCode < 200 or result.StatusCode >= 300:
                return CommandResponse.Error(result.StatusCode, FileSystemCommandHelper.BackendHttpError("Moonraker", CommandHandler.c_FilesDeleteCommand, result.StatusCode, bodyBytes))
            platformPath = self._GetPlatformPath(parsedPath)
            return FileSystemCommandHelper.BuildFileDeleteSuccess(parsedPath, platformPath, bodyBytes)
        finally:
            result.Free()


    def _GetPlatformPath(self, virtPath:VirtualFilePath) -> str:
        # Moonraker file commands take file paths relative to the selected root.
        return virtPath.RelativePath


    def _GetMoonrakerRoot(self, virtualRoot:str) -> str:
        return MoonrakerCommandHandler.c_MoonrakerRootByVirtualRoot[virtualRoot]


    # Checks if the printer is connected and in the correct state (or states)
    # If everything checks out, returns None. Otherwise it returns a CommandResponse
    def _CheckIfConnectedAndForExpectedStates(self, stateArray:List[str]) -> Optional[CommandResponse]:
        # Only allow the pause if the print state is printing, otherwise the system seems to get confused.
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.objects.query",
        {
            "objects": {
                "print_stats": None
            }
        })
        if result.HasError():
            if result.ErrorCode == JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED:
                self.Logger.error("Command failed because the printer is no connected. "+result.GetLoggingErrorStr())
                return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, "Printer Not Connected")
            self.Logger.error("Command failed to get state. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(500, "Error Getting State")
        res = result.GetResult()
        state = res.get("status", {}).get("print_stats", {}).get("state", None)
        if state is None:
            self.Logger.error("Command failed to get state, state not found in dict.")
            return CommandResponse.Error(500, "Error Getting State From Dict")
        for s in stateArray:
            if s == state:
                return None

        self.Logger.warning("Command failed, printer "+state+" not the expected states.")
        return CommandResponse.Error(CommandHandler.c_CommandError_InvalidPrinterState, "Wrong State")


    def _AddMoonrakerAuth(self, headers:Dict[str, str]) -> None:
        apiKey = MoonrakerClient.Get().MoonrakerApiKey
        if apiKey is not None and len(apiKey) > 0:
            headers["X-Api-Key"] = apiKey
