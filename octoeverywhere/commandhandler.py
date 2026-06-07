import json
import logging
from dataclasses import dataclass
from urllib.parse import parse_qsl
from typing import Any, Dict, List, Optional, Tuple, Union, cast

from .buffer import Buffer
from .gadget import Gadget
from .sentry import Sentry
from .compat import Compat
from .httpresult import HttpResult
from .filesystemcommands import FileSystemCommandHelper
from .octohttprequest import PathTypes
from .WebStream.uploadbody import UploadBody, UploadBodyOrNone
from .Webcam.webcamhelper import WebcamHelper
from .octostreammsgbuilder import OctoStreamMsgBuilder
from .Webcam.webcamsettingitem import WebcamSettingItem
from .interfaces import INotificationHandler, IPlatformCommandHandler, IHostCommandHandler, CommandResponse, ICommandWebsocketProvider

from .Proto.HttpInitialContext import HttpInitialContext


#
# Parsed send-command transport payloads.
#
# These are the typed results of the per-transport send-command parse helpers on CommandHandler.
# Each platform calls the parse helper for the transport(s) it supports and then acts on the typed result.
#

# The result of parsing an "http" transport send-command payload.
@dataclass
class ParsedHttpSendCommand:
    # The path to make the request to. Required.
    Path:str
    # The upper-cased HTTP method (defaults to GET).
    Method:str
    # The request headers (string keys and values). Empty if none were provided.
    Headers:Dict[str, str]
    # The serialized JSON body bytes, or None when no body should be sent (e.g. a GET with no request object).
    BodyBytes:Optional[bytes]


# The result of parsing a "websocket" transport send-command request.
@dataclass
class ParsedWebsocketSendCommand:
    # The command identifier. For JSON-RPC platforms this is a method string; for SDCP-style platforms an int command id.
    # The platform is responsible for validating this is the expected type. Can be None if the request didn't include one.
    Method:Optional[Any]
    # The params/data object sent with the command. Defaults to an empty dict.
    Params:Dict[str, Any]


# The result of parsing a "mqtt" transport send-command request.
@dataclass
class ParsedMqttSendCommand:
    # An optional command identifier, for platforms that split a method/command id out of the request (e.g. Elegoo CC2).
    Method:Optional[Any]
    # The params object, for platforms that split params out of the request. Defaults to an empty dict.
    Params:Dict[str, Any]
    # The full raw request object, for platforms that send the request payload as-is (e.g. Bambu).
    Request:Dict[str, Any]


#
# Platform Command Handler Interface
#
# This interface provides the platform specific code for command handlers
# Each platform MUST implement this interface and MUST implement the function signatures in the same way.
#
# GetCurrentJobStatus()
# GetPlatformVersionStr()
# ExecutePause(smartPause, suppressNotificationBool, disableHotendBool, disableBedBool, zLiftMm, retractFilamentMm, showSmartPausePopup)
# ExecuteResume()
# ExecuteCancel()

# This class is responsible for handling OctoStream commands.
#
# OctoStream commands are a platform agnostic way for the service to ask the plugin for data.
# For things that are platform specific, the service can use the API calls directly to the server.
class CommandHandler:

    # The prefix all commands must use to be handled as a command.
    # This must be lowercase, to match the lower() we call on the incoming path.
    # This must end with a /, so it's the correct length when we remove the prefix.
    c_CommandHandlerPathPrefix = "/octoeverywhere-command-api/"

    # This is a special command that allows the a websocket to be created to proxy MQTT messages.
    c_MqttWebsocketProxyCommand = "proxy/mqtt"

    # File system commands that have some special body logic.
    c_FilesListCommand = "files-list"
    c_FilesUploadCommand = "files-upload"
    c_FilesDownloadCommand = "files-download"
    c_FilesDeleteCommand = "files-delete"

    # For webcam calls, this is an optional GET arg that will be an int of the webcam index.
    # The webcam index is the index of the webcam in the list-webcam response.
    c_WebcamIndexGetKey = "index"


    #
    # Common Errors
    #
    # These are also defined in the service and need to stay in sync.
    #
    # These are all command system errors.
    c_CommandError_UnknownFailure = 750
    c_CommandError_ArgParseFailure = 751
    c_CommandError_ExecutionFailure = 752
    c_CommandError_ResponseSerializeFailure = 753
    c_CommandError_UnknownCommand = 754
    # These are common command specific errors.
    # This means the plugin isn't connected to the host, or that possibly the host isn't connected to the firmware.
    c_CommandError_HostNotConnected = 785
    # Used for things like the print, resume, or cancel command, to indicate there's nothing to take action on.
    c_CommandError_InvalidPrinterState = 786
    # Used for any printer that can only support a limited number of connections.
    # This indicates the plugin can't connect because too many other clients are connected.
    c_CommandError_CantConnectTooManyClients = 787
    # Used when a feature is not supported on the current platform.
    c_CommandError_FeatureNotSupported = 788
    # Used when we know we can't connect to the printer because we dont have valid auth
    c_CommandError_LostAuth = 789


    _Instance:"CommandHandler" = None #pyright: ignore[reportAssignmentType]


    @staticmethod
    def Init(logger:logging.Logger, notificationHandler:INotificationHandler, platCommandHandler:IPlatformCommandHandler, hostCommandHandler:IHostCommandHandler):
        CommandHandler._Instance = CommandHandler(logger, notificationHandler, platCommandHandler, hostCommandHandler)


    @staticmethod
    def Get() -> "CommandHandler":
        return CommandHandler._Instance


    def __init__(self, logger:logging.Logger, notificationHandler:INotificationHandler, platCommandHandler:IPlatformCommandHandler, hostCommandHandler:IHostCommandHandler):
        self.Logger = logger
        self.NotificationHandler = notificationHandler
        self.PlatformCommandHandler = platCommandHandler
        self.HostCommandHandler = hostCommandHandler


    # Processes special commands that need raw body handling or return raw HTTP data.
    def ProcessRawCommand(self, commandPathLower:str, jsonObj:Optional[Dict[str, Any]], uploadBody:UploadBodyOrNone=None) -> Union[HttpResult, CommandResponse, None]:
        if commandPathLower.startswith("webcam/"):
            if commandPathLower.startswith("webcam/snapshot"):
                return WebcamHelper.Get().GetSnapshot(self._GetWebcamCamIndex(jsonObj))
            elif commandPathLower.startswith("webcam/stream"):
                return WebcamHelper.Get().GetWebcamStream(self._GetWebcamCamIndex(jsonObj))
        if commandPathLower.startswith(CommandHandler.c_FilesUploadCommand):
            if self.PlatformCommandHandler is None:
                return CommandResponse.Error(400, FileSystemCommandHelper.MissingPlatformHandlerError(CommandHandler.c_FilesUploadCommand))
            if uploadBody is None:
                return CommandResponse.Error(400, FileSystemCommandHelper.MissingUploadBodyError())
            return self.PlatformCommandHandler.ExecuteFileUpload(jsonObj, uploadBody)
        if commandPathLower.startswith(CommandHandler.c_FilesDownloadCommand):
            if self.PlatformCommandHandler is None:
                return FileSystemCommandHelper.BuildRawError(400, FileSystemCommandHelper.MissingPlatformHandlerError(CommandHandler.c_FilesDownloadCommand), CommandHandler.c_FilesDownloadCommand)
            return self.PlatformCommandHandler.ExecuteFileDownload(jsonObj)
        # If we didn't match, return None, so the ProcessCommand handler is called.
        return None


    # The goal here is to keep as much of the common logic as common as possible.
    def ProcessCommand(self, commandPathLower:str, jsonObj_CanBeNone:Optional[Dict[str, Any]]) -> CommandResponse:
        if commandPathLower.startswith("ping"):
            return CommandResponse.Success({"Message":"Pong"})
        elif commandPathLower.startswith("status"):
            return self.GetStatus()
        # This works for both, in the docks we moved it to the webcam path so it's more clear.
        elif commandPathLower.startswith("list-webcam") or commandPathLower.startswith("webcam/list"):
            return self.ListWebcams()
        elif commandPathLower.startswith("set-default-webcam"):
            return self.SetDefaultCameraName(jsonObj_CanBeNone)
        elif commandPathLower.startswith("get-local-plugin-webcam-items"):
            return self.GetPluginLocalWebcamSettingsItems(jsonObj_CanBeNone)
        elif commandPathLower.startswith("set-local-plugin-webcam-items"):
            return self.SetPluginLocalWebcamSettingsItems(jsonObj_CanBeNone)
        elif commandPathLower.startswith("get-connection-info"):
            return self.GetConnectionInfo()
        elif commandPathLower.startswith("pause"):
            return self.Pause(jsonObj_CanBeNone)
        elif commandPathLower.startswith("resume"):
            return self.Resume()
        elif commandPathLower.startswith("cancel"):
            return self.Cancel()
        elif commandPathLower.startswith("set-light"):
            return self.SetLight(jsonObj_CanBeNone)
        elif commandPathLower.startswith("move-axis"):
            return self.MoveAxis(jsonObj_CanBeNone)
        elif commandPathLower.startswith("home"):
            return self.Home()
        elif commandPathLower.startswith("extrude"):
            return self.Extrude(jsonObj_CanBeNone)
        elif commandPathLower.startswith("set-temp"):
            return self.SetTemp(jsonObj_CanBeNone)
        elif commandPathLower.startswith("send-command"):
            return self.SendCommand(jsonObj_CanBeNone)
        elif commandPathLower.startswith(CommandHandler.c_FilesListCommand):
            return self.FileList(jsonObj_CanBeNone)
        elif commandPathLower.startswith(CommandHandler.c_FilesDeleteCommand):
            return self.FileDelete(jsonObj_CanBeNone)
        elif commandPathLower.startswith("rekey"):
            return self.Rekey()
        elif commandPathLower.startswith(CommandHandler.c_MqttWebsocketProxyCommand):
            # This is a special command that only works with websocket connections, but if we are here it's http.
            # So return an error so the user will know to use a websocket.
            return CommandResponse.Error(400, "MQTT proxy requires a websocket connection.")
        return CommandResponse.Error(CommandHandler.c_CommandError_UnknownCommand, "The command path didn't match any known commands.")


    # Processes websocket commands, return None on failure to close the incoming WS.
    def ProcessWebsocketCommand(self, commandPathLower:str, jsonObj:Optional[Dict[str, Any]]) -> Optional[ICommandWebsocketProvider]:
        # If the command path is the mqtt proxy, we need to return a provider.
        if commandPathLower.startswith(CommandHandler.c_MqttWebsocketProxyCommand):
            # This builder will take the optional json args and will return a provider.
            mqttWsProxyProvider = Compat.GetMqttWebsocketProxyProviderBuilder()
            if mqttWsProxyProvider is None:
                self.Logger.error("CommandHandler got a websocket mqtt proxy request but we don't have a mqtt websocket provider in compat.")
                return None
            return mqttWsProxyProvider.GetCommandWebsocketProvider(jsonObj)

        # If we are here, this is an invalid command for a websocket command.
        self.Logger.error(f"CommandHandler got a websocket request but the command path didn't match any known commands. Path: {commandPathLower}")
        return None


    #
    # Command Handlers
    #

    # Must return a CommandResponse
    def GetStatus(self) -> CommandResponse:
        # We want to mock the OctoPrint /api/job API since it has good stuff in it.
        # So we will return a similar result. We use similar code to what the actual API returns.
        # If we fail to get this object, we will still return a result without it.
        jobStatus = None
        try:
            if self.PlatformCommandHandler is None:
                self.Logger.warning("GetStatus command has no PlatformCommandHandler")
            else:
                # If the plugin is connected and in a good state, this should return the standard job status.
                # On error, this should return None and then we send back the CommandHandler.c_CommandError_HostNotConnected error
                # OR it will return an int, which must be a CommandHandler.c_CommandError_... error, and we will send that back.
                jobStatus = self.PlatformCommandHandler.GetCurrentJobStatus()
                # This interface should always return None, an int, or a dict with details.
                if jobStatus is not None and (isinstance(jobStatus, dict) and len(jobStatus) == 0):
                    jobStatus = None
        except Exception as e:
            Sentry.OnExceptionNoSend("API command GetStatus failed to get job status", e)

        # Ensure we got a job status, otherwise the host isn't connected.
        if jobStatus is None:
            return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, "Host not connected")
        # If we got an int back, it's an error code.
        if isinstance(jobStatus, int):
            return CommandResponse.Error(jobStatus, "Failed to get current status.")

        # Gather info that's specific to us.
        octoeverywhereStatus = None
        try:
            if self.NotificationHandler is None:
                # This shouldn't happen, even debug should have this.
                self.Logger.warning("API command GetStatus has no notification handler")
            else:
                gadget:Gadget = self.NotificationHandler.GetGadget()
                octoeverywhereStatus = {
                    # <str> The most recent print id. This is only updated when a new print starts, so it will remain until replaced.
                    # Defaults to "none", but it should always be set when the notification manager is inited.
                    # Note there was an older MostRecentPrintId (with no str) that's deprecated.
                    "MostRecentPrintIdStr" : self.NotificationHandler.GetPrintId(),
                    # <int> The number of seconds since the epoch when the print started, AKA when MostRecentPrintId was created.
                    # Defaults to the current time.
                    "PrintStartTimeSec" : self.NotificationHandler.GetPrintStartTimeSec(),
                    # Gadget status stuffs
                    "Gadget" :{
                        # <float> The most recent gadget score. This value also remains between prints and is only updated when Gadget returns a new valid score.
                        # Note this score is the average of the most recent 2 scores, to even things out a bit.
                        # Defaults to 0.0
                        "LastScore" : gadget.GetLastGadgetScoreFloat(),
                        # <float>[] The score history, capped at a limit of a value in Gadget.
                        # The most recent is in the front of the list.
                        # Defaults to empty list.
                        "ScoreHistory": gadget.GetScoreHistoryFloats(),
                        # <float> The last time LastGadgetScore was updated.
                        # Defaults to a large number since it's (currentTime - 0)
                        "TimeSinceLastScoreSec" : gadget.GetLastTimeSinceScoreUpdateSecFloat(),
                        # <float> The last time interval Gadget returned to check. This value remains between prints.
                        # Defaults to Gadget's default interval time.
                        "IntervalSec" : gadget.GetCurrentIntervalSecFloat(),
                        # Indicates if the current print is suppressed from Gadget watching.
                        "IsSuppressed" : gadget.IsPrintSuppressed(),
                        # None if there has been no warning for this print, otherwise the time in seconds since the last
                        # warning action was sent. (int)
                        "TimeSinceLastWarnSec" : gadget.GetTimeOrNoneSinceLastWarningIntSec(),
                        # None if there has been no pause for this print, otherwise the time in seconds since the last
                        # pause action was done. (int)
                        "TimeSinceLastPauseSec" : gadget.GetTimeOrNoneSinceLastPauseIntSec(),
                    },
                }
        except Exception as e:
            Sentry.OnExceptionNoSend("API command GetStatus failed to get OctoEverywhere info", e)

        # Get the platform version.
        versionStr:Optional[str] = None
        try:
            if self.PlatformCommandHandler is None:
                self.Logger.warning("GetStatus command has no PlatformCommandHandler")
            else:
                versionStr = self.PlatformCommandHandler.GetPlatformVersionStr()
        except Exception as e:
            Sentry.OnExceptionNoSend("API command GetStatus failed to get OctoPrint version", e)

        # Get the supported features for this platform.
        features:int = 0
        try:
            if self.PlatformCommandHandler is None:
                self.Logger.warning("GetStatus command has no PlatformCommandHandler")
            else:
                features = self.PlatformCommandHandler.GetSupportedFeatureFlags()
        except Exception as e:
            Sentry.OnExceptionNoSend("API command GetStatus failed to get OctoPrint version", e)

        # Get the list webcams response as well
        # Don't include the URL to reduce the payload size.
        webcamInfoCommandResponse = self.ListWebcams(False)
        # This shouldn't be possible, but we will check for sanity sake.
        if webcamInfoCommandResponse is None or webcamInfoCommandResponse.StatusCode != 200:
            self.Logger.error("GetStatus command failed to get webcam info.")
            webcamInfoCommandResponse = CommandResponse.Success({})

        # Build the final response
        responseObj = {
            "JobStatus" : jobStatus,
            "OctoEverywhereStatus" : octoeverywhereStatus,
            "PlatformVersion" : versionStr,
            "Features": features,
            "ListWebcams" : webcamInfoCommandResponse.ResultDict
        }
        return CommandResponse.Success(responseObj)


    # Must return a CommandResponse
    def GetConnectionInfo(self) -> CommandResponse:
        try:
            if self.PlatformCommandHandler is None:
                self.Logger.warning("GetConnectionInfo command has no PlatformCommandHandler")
                return CommandResponse.Error(400, "No PlatformCommandHandler available")
            else:
                info = self.PlatformCommandHandler.GetConnectionInfo()
                if info is None:
                    return CommandResponse.Error(400, "Failed to get connection info")
                return CommandResponse.Success(info.Serialize())
        except Exception as e:
            Sentry.OnExceptionNoSend("API command GetConnectionInfo failed", e)
            return CommandResponse.Error(500, "Failed to get connection info")


    # Must return a CommandResponse
    def ListWebcams(self, includeUrls=True) -> CommandResponse:
        # Get all of the known webcams
        webcamSettingsItems = WebcamHelper.Get().ListWebcams()
        if webcamSettingsItems is None:
            webcamSettingsItems = []
        # We need to convert the objects into a dic to serialize.
        # Note this format is also used for GetStatus!
        webcams:List[Dict[str, Any]] = []
        for i in webcamSettingsItems:
            webcams.append(i.Serialize(includeUrls))

        # We always use the default index, which is a reflection of the current camera list.
        # We don't use the name, we only use that internally to keep track of the current index.
        defaultIndex = WebcamHelper.Get().GetDefaultCameraIndex(webcamSettingsItems)
        responseObj = {
            "Webcams" : webcams,
            "DefaultIndex" : defaultIndex
        }
        return CommandResponse.Success(responseObj)


    # Must return a CommandResponse
    def SetDefaultCameraName(self, jsonObjData:Optional[Dict[str,Any]]) -> CommandResponse:
        name:Optional[str] = None
        if jsonObjData is not None:
            try:
                name = jsonObjData.get("Name", None)
            except Exception as e:
                Sentry.OnException("Failed to SetDefaultCameraName, bad args.", e)
                return CommandResponse.Error(400, "Failed to parse args")
        if name is None:
            return CommandResponse.Error(400, "No name passed")
        # Set the name
        WebcamHelper.Get().SetDefaultCameraName(name)
        # Return success
        return CommandResponse.Success({})


    # Must return a CommandResponse
    def GetPluginLocalWebcamSettingsItems(self, jsonObjData:Optional[Dict[str,Any]]) -> CommandResponse:
        # Get the list, make sure we also include any disabled items
        localWebcams = WebcamHelper.Get().GetPluginLocalWebcamList(returnDisabledItems=True)

        # Serialize them
        webcamDicts:List[Dict[str, Any]] = []
        for i in localWebcams:
            webcamDicts.append(i.Serialize())

        # Build the final response
        responseObj = {
            "LocalPluginWebcams" : webcamDicts,
        }
        return CommandResponse.Success(responseObj)


    # Must return a CommandResponse
    def SetPluginLocalWebcamSettingsItems(self, jsonObjData:Optional[Dict[str,Any]]) -> CommandResponse:
        localWebcamSettingItems:List[WebcamSettingItem] = []
        try:
            if jsonObjData is None:
                raise Exception("No args passed")

            # Get the list.
            items = jsonObjData.get("LocalPluginWebcams", None)
            if items is None:
                raise Exception("No LocalPluginWebcams found")

            # Convert the list to objects
            for i in items:
                # This will deserialize the dict and also validate.
                # If None is returned, the item failed, and we won't try to set anything.
                o = WebcamSettingItem.Deserialize(i, self.Logger)
                if o is None:
                    raise Exception("Failed to deserialize item")
                localWebcamSettingItems.append(o)
        except Exception as e:
            Sentry.OnException("Failed to SetPluginLocalWebcamSettingsItems, bad args.", e)
            return CommandResponse.Error(400, "Failed to parse args")

        # Set the new list
        WebcamHelper.Get().SetPluginLocalWebcamList(localWebcamSettingItems)
        return CommandResponse.Success({})


    # Must return a CommandResponse
    def Pause(self, jsonObjData:Optional[Dict[str,Any]]) -> CommandResponse:

        # Defaults.
        smartPause = False
        suppressNotificationBool = True

        # Smart pause options
        disableHotendBool = True
        disableBedBool = False
        zLiftMm:int = 0
        retractFilamentMm:int = 0
        showSmartPausePopup = True

        # Parse if we have args
        if jsonObjData is not None:
            try:
                # Get values
                # ParseSmart Pause first, since it changes the default of suppressNotificationBool
                if "SmartPause" in jsonObjData:
                    smartPause = jsonObjData["SmartPause"]

                # Update the default of the notification suppression based on the type. We only suppress for smart pause
                # because it will only happen from Gadget, which will send it's own notification.
                suppressNotificationBool = smartPause

                # Parse the rest.
                if "DisableHotend" in jsonObjData:
                    disableHotendBool = jsonObjData["DisableHotend"]
                if "DisableBed" in jsonObjData:
                    disableBedBool = jsonObjData["DisableBed"]
                if "ZLiftMm" in jsonObjData:
                    zLiftMm:int = jsonObjData["ZLiftMm"]
                if "RetractFilamentMm" in jsonObjData:
                    retractFilamentMm:int = jsonObjData["RetractFilamentMm"]
                if "SuppressNotification" in jsonObjData:
                    suppressNotificationBool = jsonObjData["SuppressNotification"]
                if "ShowSmartPausePopup" in jsonObjData:
                    showSmartPausePopup = jsonObjData["ShowSmartPausePopup"]
            except Exception as e:
                Sentry.OnException("Failed to ExecuteSmartPause, bad args.", e)
                return CommandResponse.Error(400, "Failed to parse args")

        # If this throws that's fine.
        return self.PlatformCommandHandler.ExecutePause(smartPause, suppressNotificationBool, disableHotendBool, disableBedBool, zLiftMm, retractFilamentMm, showSmartPausePopup)


    def Resume(self) -> CommandResponse:
        return self.PlatformCommandHandler.ExecuteResume()


    def Cancel(self) -> CommandResponse:
        return self.PlatformCommandHandler.ExecuteCancel()


    # Must return a CommandResponse
    def SetLight(self, jsonObjData:Optional[Dict[str,Any]]) -> CommandResponse:
        # Parse if we have args
        if jsonObjData is None:
            return CommandResponse.Error(400, "No args passed")

        lightName = None
        on = False
        try:
            lightName = jsonObjData.get("Name", None)
            on = jsonObjData.get("On", None)
            if lightName is None or not isinstance(lightName, str):
                return CommandResponse.Error(400, "No light name passed")
            if on is None or not isinstance(on, bool):
                return CommandResponse.Error(400, "No light on/off state passed")
        except Exception as e:
            Sentry.OnException("Failed to SetLight, bad args.", e)
            return CommandResponse.Error(400, "Failed to parse args")

        # Execute the command
        return self.PlatformCommandHandler.ExecuteSetLight(lightName, on)


    def MoveAxis(self, jsonObjData:Optional[Dict[str,Any]]) -> CommandResponse:
        # Parse args
        if jsonObjData is None:
            return CommandResponse.Error(400, "No args passed")

        axis = None
        distanceMm = None
        try:
            axis = jsonObjData.get("Axis", None)
            distanceMm = jsonObjData.get("DistanceMm", None)
            if axis is None or not isinstance(axis, str):
                return CommandResponse.Error(400, "No axis specified or invalid type")
            if distanceMm is None or not isinstance(distanceMm  , (int, float)):
                return CommandResponse.Error(400, "No distance specified or invalid type")
            distanceMm = float(distanceMm)
        except Exception as e:
            Sentry.OnException("Failed to MoveAxis, bad args.", e)
            return CommandResponse.Error(400, "Failed to parse args")

        # Execute the command
        return self.PlatformCommandHandler.ExecuteMoveAxis(axis, distanceMm)


    def Home(self) -> CommandResponse:
        # Execute the command (no args needed)
        return self.PlatformCommandHandler.ExecuteHome()


    def Extrude(self, jsonObjData:Optional[Dict[str,Any]]) -> CommandResponse:
        # Parse args
        if jsonObjData is None:
            return CommandResponse.Error(400, "No args passed")

        extruder = None
        distanceMm = None
        try:
            extruder = jsonObjData.get("Extruder", None)
            distanceMm = jsonObjData.get("DistanceMm", None)
            if extruder is None or not isinstance(extruder, int):
                return CommandResponse.Error(400, "No extruder specified or invalid type")
            if distanceMm is None or not isinstance(distanceMm, (int, float)):
                return CommandResponse.Error(400, "No distanceMm specified or invalid type")
            distanceMm = float(distanceMm)
        except Exception as e:
            Sentry.OnException("Failed to Extrude, bad args.", e)
            return CommandResponse.Error(400, "Failed to parse args")

        # Execute the command
        return self.PlatformCommandHandler.ExecuteExtrude(extruder, distanceMm)


    def SetTemp(self, jsonObjData:Optional[Dict[str,Any]]) -> CommandResponse:
        # Parse args
        if jsonObjData is None:
            return CommandResponse.Error(400, "No args passed")

        bedC:Optional[float] = None
        chamberC:Optional[float] = None
        toolC:Optional[float] = None
        toolNumber:Optional[int] = None
        try:
            bedC = jsonObjData.get("BedC", None)
            chamberC = jsonObjData.get("ChamberC", None)
            toolC = jsonObjData.get("ToolC", None)
            toolNumber = jsonObjData.get("ToolNumber", None)
            if bedC is not None and not isinstance(bedC, (int, float)):
                return CommandResponse.Error(400, "Invalid bedC type")
            if chamberC is not None and not isinstance(chamberC, (int, float)):
                return CommandResponse.Error(400, "Invalid chamberC type")
            if toolC is not None and not isinstance(toolC, (int, float)):
                return CommandResponse.Error(400, "Invalid toolC type")
            if toolNumber is not None and not isinstance(toolNumber, int):
                return CommandResponse.Error(400, "Invalid toolNumber type")
        except Exception as e:
            Sentry.OnException("Failed to Extrude, bad args.", e)
            return CommandResponse.Error(400, "Failed to parse args")

        # Safety check the temps
        # Some printers might be able to do higher than these, but these are reasonable max temps to prevent issues.
        MAX_BED_TEMP_C = 75.0
        MAX_CHAMBER_TEMP_C = 75.0
        MAX_TOOL_TEMP_C = 260.0
        if not bedC and not chamberC and not toolC:
            self.Logger.error("ExecuteSetTemp: No heater specified")
            return CommandResponse.Error(400, "At least one heater must be specified")

        # Safety check: enforce maximum temperatures
        if bedC and bedC > MAX_BED_TEMP_C:
            self.Logger.error(f"ExecuteSetTemp: Bed temperature {bedC}°C exceeds maximum {MAX_BED_TEMP_C}°C")
            return CommandResponse.Error(400, f"Bed temperature cannot exceed {MAX_BED_TEMP_C}°C")

        if chamberC and chamberC > MAX_CHAMBER_TEMP_C:
            self.Logger.error(f"ExecuteSetTemp: Chamber temperature {chamberC}°C exceeds maximum {MAX_CHAMBER_TEMP_C}°C")
            return CommandResponse.Error(400, f"Chamber temperature cannot exceed {MAX_CHAMBER_TEMP_C}°C")

        if toolC and toolC > MAX_TOOL_TEMP_C:
            self.Logger.error(f"ExecuteSetTemp: Tool temperature {toolC}°C exceeds maximum {MAX_TOOL_TEMP_C}°C")
            return CommandResponse.Error(400, f"Tool temperature cannot exceed {MAX_TOOL_TEMP_C}°C")

        # Execute the command
        return self.PlatformCommandHandler.ExecuteSetTemp(bedC, chamberC, toolC, toolNumber)


    def SendCommand(self, jsonObjData:Optional[Dict[str,Any]]) -> CommandResponse:
        if jsonObjData is None:
            return CommandResponse.Error(400, "The send-command request body was empty. Provide a single JSON object containing at least a 'transportType' (string) and a 'request' (object). Example: {\"transportType\": \"http\", \"path\": \"/api/version\", \"method\": \"GET\", \"request\": {}}.")
        if not isinstance(jsonObjData, dict):
            return CommandResponse.Error(400, f"The send-command request body must be a single JSON object, but a value of type '{type(jsonObjData).__name__}' was received. Send a JSON object with a 'transportType' (string) and a 'request' (object), e.g. {{\"transportType\": \"http\", \"path\": \"/api/version\", \"method\": \"GET\", \"request\": {{}}}}.")

        transportType = jsonObjData.get("transportType", jsonObjData.get("TransportType", None))
        if transportType is None or not isinstance(transportType, str) or len(transportType) == 0:
            return CommandResponse.Error(400, f"The send-command request is missing the required 'transportType' field, or it was not a non-empty string (received value: {json.dumps(transportType, default=str)}). Set 'transportType' to one of 'http', 'websocket', or 'mqtt' to select how the command is delivered to the printer. The valid transport depends on the printer platform.")
        if transportType not in {"http", "websocket", "mqtt"}:
            return CommandResponse.Error(400, f"The send-command 'transportType' value '{transportType}' is not recognized. It must be exactly one of 'http', 'websocket', or 'mqtt' (lowercase). A given printer platform only supports one of these transports.")

        requestObj = jsonObjData.get("request", jsonObjData.get("Request", None))
        if requestObj is None or not isinstance(requestObj, dict):
            return CommandResponse.Error(400, f"The send-command request is missing the required 'request' field, or it was not a JSON object (received value: {json.dumps(requestObj, default=str)}). 'request' must be a JSON object whose shape depends on 'transportType'. For 'http' it is the JSON request body (use {{}} for an empty body, with the HTTP options 'path'/'method'/'headers' set at the top level of the payload, not inside 'request'). For 'websocket' and 'mqtt' it holds the command itself (e.g. {{\"method\": ..., \"params\": {{...}}}}).")

        return self.PlatformCommandHandler.ExecuteSendCommand(transportType, requestObj, jsonObjData)


    def FileList(self, jsonObjData:Optional[Dict[str,Any]]) -> CommandResponse:
        if self.PlatformCommandHandler is None:
            return CommandResponse.Error(400, FileSystemCommandHelper.MissingPlatformHandlerError(CommandHandler.c_FilesListCommand))
        return self.PlatformCommandHandler.ExecuteFileList(jsonObjData)


    def FileDelete(self, jsonObjData:Optional[Dict[str,Any]]) -> CommandResponse:
        if self.PlatformCommandHandler is None:
            return CommandResponse.Error(400, FileSystemCommandHelper.MissingPlatformHandlerError(CommandHandler.c_FilesDeleteCommand))
        return self.PlatformCommandHandler.ExecuteFileDelete(jsonObjData)



    def Rekey(self) -> CommandResponse:
        self.Logger.warning("Rekey command received!")
        resultBool = self.HostCommandHandler.OnRekeyCommand()
        if resultBool:
            return CommandResponse.Success()
        else:
            return CommandResponse.Error(400, "Failed to process rekey command.")


    #
    # Common Handler Core Logic
    #

    # Returns True or False depending if this request is a OE command or not.
    # If it is, HandleCommand should be used to get the response.
    def IsCommandRequest(self, httpInitialContext:HttpInitialContext) -> bool:
        # Get the path to check if it's a command or not.
        if httpInitialContext.PathType() != PathTypes.Relative:
            return False
        path = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Path())
        if path is None:
            raise Exception("IsCommandHttpRequest Http request has no path field in IsCommandRequest.")
        pathLower = path.lower()
        # If the path starts with our special prefix, it's for us!
        return pathLower.startswith(CommandHandler.c_CommandHandlerPathPrefix)


    # Handles a command and returns an OctoHttpResult
    #
    # Note! It's very important that the OctoHttpResult has all of the properties the generic system expects! For example,
    # it must have the FullBodyBuffer (similar to the snapshot helper) and a valid response object JUST LIKE the requests lib would return.
    #
    def HandleCommand(self, httpInitialContext:HttpInitialContext, postBody:UploadBody) -> HttpResult:
        # Parse the command path and the optional json args.
        commandPath:str = ""
        commandPathLower:str = ""
        jsonObj:Optional[Dict[str, Any]] = None
        responseObj:Optional[CommandResponse] = None
        try:
            # Get the command path and json args, the json object can be null if there are no args.
            commandPath, commandPathLower = self._GetCommandPath(httpInitialContext)
            # There are some very special commands where the body is data, so for those we don't try to
            # parse the json args. But remember those take args via GET parameters.
            postBodyForJsonArgs:Optional[UploadBody] = None
            if commandPathLower.startswith(CommandHandler.c_FilesUploadCommand) is False and commandPathLower.startswith(CommandHandler.c_FilesDownloadCommand) is False:
                postBodyForJsonArgs = postBody
            # We always call this, if there's no upload body it will parse the args from get params.
            jsonObj = self._GetJsonArgs(commandPath, postBodyForJsonArgs)
        except Exception as e:
            Sentry.OnException("CommandHandler error while parsing command args.", e)
            responseObj = CommandResponse.Error(CommandHandler.c_CommandError_ArgParseFailure, str(e))

        # If the args parse was successful, try to handle the command.
        if responseObj is None:
            # Some commands need raw-body handling before normal command dispatch; a few also return raw HttpResult data.
            try:
                # If a result was returned, it was handled.
                result = self.ProcessRawCommand(commandPathLower, jsonObj, postBody)
                if isinstance(result, HttpResult):
                    return result
                if isinstance(result, CommandResponse):
                    responseObj = result
            except Exception as e:
                Sentry.OnException("CommandHandler error while handling raw command.", e)
                responseObj = CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, FileSystemCommandHelper.ExceptionError(commandPathLower, e))

        if responseObj is None:
            # Otherwise, handle our wrapped API commands
            try:
                responseObj = self.ProcessCommand(commandPathLower, jsonObj)
            except Exception as e:
                Sentry.OnException("CommandHandler error while handling command.", e)
                responseObj = CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, str(e))

        if responseObj is None:
            responseObj = CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, str("No response object returned."))

        # Build the result
        resultBytes:Optional[bytes] = None
        try:
            # Build the common response.
            jsonResponse:Dict[str,Any] = {
                "Status" : responseObj.StatusCode
            }
            if responseObj.ErrorStr is not None:
                jsonResponse["Error"] = responseObj.ErrorStr
            if responseObj.ResultDict is not None:
                jsonResponse["Result"] = responseObj.ResultDict

            # Serialize to bytes
            resultBytes = json.dumps(jsonResponse).encode(encoding="utf-8")

        except Exception as e:
            Sentry.OnException("CommandHandler failed to serialize response.", e)
            # Use a known good json object for this error.
            resultBytes = json.dumps(
                {
                    "Status": CommandHandler.c_CommandError_ResponseSerializeFailure,
                    "Error":"Serialize Response Failed"
                }).encode(encoding="utf-8")

        # Build the full result
        # Make sure to set the content type, so the response can be compressed.
        headers = {
            "Content-Type": "text/json"
        }
        url = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Path())
        if url is None:
            url = "Unknown"
        return HttpResult(200, headers, url, False, fullBodyBuffer=Buffer(resultBytes))


    # Called after IsCommandRequest, so we know this is a command request.
    # If we fail, we return None, which will then close the incoming ws.
    def HandleWebsocketCommand(self, context:HttpInitialContext) -> Optional[ICommandWebsocketProvider]:
        try:
            # Get the command path and json args, the json object can be null if there are no args.
            _, commandPathLower, jsonObj = self._GetPathAndJsonArgs(context, None)

            # Returns None on failure.
            return self.ProcessWebsocketCommand(commandPathLower, jsonObj)
        except Exception as e:
            Sentry.OnException("CommandHandler error while handling websocket command.", e)
            return None


    # A helper to parse the context and json args. Throws if it fails!
    def _GetPathAndJsonArgs(self, httpInitialContext:HttpInitialContext, postBody:UploadBodyOrNone) -> Tuple[str, str, Optional[Dict[str, Any]]]:
        commandPath, commandPathLower = self._GetCommandPath(httpInitialContext)
        jsonObj = self._GetJsonArgs(commandPath, postBody)
        return (commandPath, commandPathLower, jsonObj)


    def _GetCommandPath(self, httpInitialContext:HttpInitialContext) -> Tuple[str, str]:
        # Get the command path.
        path = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Path())
        if path is None:
            raise Exception("IsCommandHttpRequest Http request has no path field in HandleCommand.")

        # Everything after our prefix is part of the command path
        commandPath = path[len(CommandHandler.c_CommandHandlerPathPrefix):]
        commandPathLower = commandPath.lower()
        return (commandPath, commandPathLower)


    def _GetJsonArgs(self, commandPath:str, postBody:UploadBodyOrNone) -> Optional[Dict[str, Any]]:
        # Parse the args. Args are optional, it depends on the command.
        # Note some of these commands can also be GET requests, so we need to handle that.
        jsonObj:Optional[Dict[str, Any]] = None

        # Parse the POST body if there is one.
        if postBody is not None:
            bodyBuffer = postBody.GetBodyAsBuffer()
            if bodyBuffer is not None:
                jsonObj = json.loads(bodyBuffer.GetBytesLike())

        # If there is no json object, try for get args.
        if jsonObj is None:
            # This will return None if there are no args.
            # Use the cased version of the string, so get args keep the correct case.
            jsonObj = self._ParseGetArgsAsJson(commandPath)
        return jsonObj


    # If there are GET args, this will parse them into a json object where all values as strings
    # If there are no args, this will return None.
    def _ParseGetArgsAsJson(self, commandPath:str) -> Optional[Dict[str, str]]:
        # We need to remove the ? and split on & to get the args.
        if "?" not in commandPath:
            return None
        try:
            jsonObj:Dict[str, str] = {}
            query = commandPath.split("?", 1)[1]
            for key, value in parse_qsl(query, keep_blank_values=True):
                # Ensure the key is always lower case, but don't mess with the value; things like passwords might need to be case sensitive.
                jsonObj[str(key).lower()] = value
            return jsonObj
        except Exception as e:
            Sentry.OnException("CommandHandler error while parsing GET command args.", e)
        return None


    # This is a helper function to get the webcam index from the json object.
    def _GetWebcamCamIndex(self, jsonObj:Optional[Dict[str, Any]]) -> Optional[int]:
        # This command can take an optional GET param that specifies the camera index, which can be gotten from list-webcam.
        # Remember the json object will have all values as strings!
        webcamIndex:Optional[int] = None
        if jsonObj is not None:
            try:
                webcamIndexStr = jsonObj.get(CommandHandler.c_WebcamIndexGetKey, None)
                if webcamIndexStr is not None:
                    webcamIndex = int(webcamIndexStr)
            except Exception as e:
                Sentry.OnException("CommandHandler error while parsing webcam index.", e)
        return webcamIndex


    #
    # Common send-command transport parsers.
    #
    # CommandHandler.SendCommand has already pulled the common `transportType` and `request` fields out of the raw payload
    # before calling the platform's ExecuteSendCommand(transportType, request, rawPayload). These helpers do the
    # transport-specific parsing once, so every platform that speaks a given transport shares the same parsing and
    # validation. Each returns a typed Parsed*SendCommand on success, or a CommandResponse error to return as-is.
    #

    # Parses an "http" transport payload. The HTTP transport options (path, method, headers, timeoutSec, allowRedirects)
    # live at the payload root; the `request` object is the JSON body that gets sent.
    @staticmethod
    def ParseHttpSendCommand(rawPayload:Dict[str, Any], request:Dict[str, Any]) -> Union[ParsedHttpSendCommand, CommandResponse]:
        path = rawPayload.get("path", rawPayload.get("Path", None))
        if path is None or not isinstance(path, str) or len(path) == 0:
            return CommandResponse.Error(400, f"For an 'http' send-command, the top-level 'path' field is required and must be a non-empty string, but the received value was {json.dumps(path, default=str)}. Note that for http the 'path', 'method', 'headers', 'timeoutSec', and 'allowRedirects' fields go at the top level of the payload (next to 'transportType'), while 'request' holds only the JSON request body. Example payload: {{\"transportType\": \"http\", \"path\": \"/api/printer\", \"method\": \"GET\", \"request\": {{}}}}.")
        method = str(rawPayload.get("method", rawPayload.get("Method", "GET"))).upper()
        if len(method) == 0:
            return CommandResponse.Error(400, "For an 'http' send-command, the top-level 'method' field must be a non-empty string HTTP verb such as 'GET', 'POST', 'PUT', 'PATCH', or 'DELETE'. It is optional and defaults to 'GET' when omitted, but an empty string is not allowed.")
        try:
            headers = CommandHandler._ParseHeaders(rawPayload)
        except Exception as e:
            return CommandResponse.Error(400, str(e))

        # Build the JSON body from the request object. We send a body for any non GET/HEAD method, or whenever a request
        # object was provided. When we send a body and no content type was set, default to application/json.
        bodyBytes:Optional[bytes] = None
        if len(request) > 0 or method not in ("GET", "HEAD"):
            if "Content-Type" not in headers and "content-type" not in headers:
                headers["Content-Type"] = "application/json"
            bodyBytes = json.dumps(request, default=str).encode("utf-8")

        return ParsedHttpSendCommand(path, method, headers, bodyBytes)


    # Parses a "websocket" transport request. We accept a command identifier as `method`/`cmd` and a params object as
    # `params`/`data` (all case-insensitive). The platform validates the command identifier is the type it expects.
    @staticmethod
    def ParseWebsocketSendCommand(rawPayload:Dict[str, Any], request:Dict[str, Any]) -> Union[ParsedWebsocketSendCommand, CommandResponse]:
        method = CommandHandler._FirstPresent(request, "method", "Method", "cmd", "Cmd")
        params = CommandHandler._FirstPresent(request, "params", "Params", "data", "Data")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return CommandResponse.Error(400, f"For a 'websocket' send-command, the request's params object (provided as 'params', 'Params', 'data', or 'Data' inside 'request') must be a JSON object, but a value of type '{type(params).__name__}' was received. Provide the command arguments as a JSON object, e.g. \"request\": {{\"method\": \"printer.objects.query\", \"params\": {{...}}}}, or omit it entirely to send no arguments.")
        return ParsedWebsocketSendCommand(method, cast(Dict[str, Any], params))


    # Parses a "mqtt" transport request. We surface an optional command identifier (`method`/`cmd`) and params
    # (`params`/`data`) for platforms that split them out, plus the full raw request for platforms that send it as-is.
    @staticmethod
    def ParseMqttSendCommand(rawPayload:Dict[str, Any], request:Dict[str, Any]) -> Union[ParsedMqttSendCommand, CommandResponse]:
        method = CommandHandler._FirstPresent(request, "method", "Method", "cmd", "Cmd")
        params = CommandHandler._FirstPresent(request, "params", "Params", "data", "Data")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return CommandResponse.Error(400, f"For an 'mqtt' send-command, the request's params object (provided as 'params', 'Params', 'data', or 'Data' inside 'request') must be a JSON object, but a value of type '{type(params).__name__}' was received. Provide the command arguments as a JSON object, e.g. \"request\": {{\"method\": 1, \"params\": {{...}}}}, or omit it entirely to send no arguments.")
        return ParsedMqttSendCommand(method, cast(Dict[str, Any], params), request)


    # Returns the value of the first of the given keys that exists in the dict, or None if none are present.
    @staticmethod
    def _FirstPresent(d:Dict[str, Any], *keys:str) -> Optional[Any]:
        for k in keys:
            if k in d:
                return d[k]
        return None


    # Parses the optional 'headers' field (case-insensitive) into a string->string dict. Raises on a non-object value.
    @staticmethod
    def _ParseHeaders(rawPayload:Dict[str, Any]) -> Dict[str, str]:
        headersRaw = rawPayload.get("headers", rawPayload.get("Headers", None))
        if headersRaw is None:
            return {}
        if not isinstance(headersRaw, dict):
            raise Exception(f"The optional top-level 'headers' field must be a JSON object mapping header names to values, but a value of type '{type(headersRaw).__name__}' was received. Example: \"headers\": {{\"Content-Type\": \"application/json\", \"X-Api-Key\": \"...\"}}. Omit the field entirely if no extra headers are needed.")
        headers:Dict[str, str] = {}
        for k, v in headersRaw.items():
            headers[str(k)] = str(v)
        return headers
