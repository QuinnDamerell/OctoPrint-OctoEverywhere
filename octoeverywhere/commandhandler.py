import json
import logging
from urllib.parse import unquote
from typing import Any, Dict, List, Optional, Tuple

from .buffer import Buffer
from .gadget import Gadget
from .sentry import Sentry
from .compat import Compat
from .httpresult import HttpResult
from .octohttprequest import PathTypes
from .Webcam.webcamhelper import WebcamHelper
from .octostreammsgbuilder import OctoStreamMsgBuilder
from .Webcam.webcamsettingitem import WebcamSettingItem
from .interfaces import INotificationHandler, IPlatformCommandHandler, IHostCommandHandler, CommandResponse, ICommandWebsocketProvider

from .Proto.HttpInitialContext import HttpInitialContext


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


    # Processes special commands that return a raw HttpResult instead of a command result.
    def ProcessRawCommand(self, commandPathLower:str, jsonObj:Optional[Dict[str, Any]]) -> Optional[HttpResult]:
        if commandPathLower.startswith("webcam/"):
            if commandPathLower.startswith("webcam/snapshot"):
                return WebcamHelper.Get().GetSnapshot(self._GetWebcamCamIndex(jsonObj))
            elif commandPathLower.startswith("webcam/stream"):
                return WebcamHelper.Get().GetWebcamStream(self._GetWebcamCamIndex(jsonObj))
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
        elif commandPathLower.startswith("pause"):
            return self.Pause(jsonObj_CanBeNone)
        elif commandPathLower.startswith("resume"):
            return self.Resume()
        elif commandPathLower.startswith("cancel"):
            return self.Cancel()
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
            "ListWebcams" : webcamInfoCommandResponse.ResultDict
        }
        return CommandResponse.Success(responseObj)


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
    def HandleCommand(self, httpInitialContext:HttpInitialContext, postBody:Optional[Buffer]) -> HttpResult:
        # Parse the command path and the optional json args.
        commandPath:str = ""
        commandPathLower:str = ""
        jsonObj:Optional[Dict[str, Any]] = None
        responseObj:Optional[CommandResponse] = None
        try:
            # Get the command path and json args, the json object can be null if there are no args.
            commandPath, commandPathLower, jsonObj = self._GetPathAndJsonArgs(httpInitialContext, postBody)
        except Exception as e:
            Sentry.OnException("CommandHandler error while parsing command args.", e)
            responseObj = CommandResponse.Error(CommandHandler.c_CommandError_ArgParseFailure, str(e))

        # If the args parse was successful, try to handle the command.
        if responseObj is None:
            # For some commands, they will create their own HttpResult and return it like snapshot or webcam streams.
            # But these are only special commands, most commands should use the command response.
            try:
                # If a result was returned, it was handled.
                result = self.ProcessRawCommand(commandPathLower, jsonObj)
                if result is not None:
                    return result
            except Exception as e:
                Sentry.OnException("CommandHandler error while handling raw command.", e)

            # Otherwise, handle our wrapped API commands
            try:
                responseObj = self.ProcessCommand(commandPath, jsonObj)
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
    def _GetPathAndJsonArgs(self, httpInitialContext:HttpInitialContext, postBody:Optional[Buffer]) -> Tuple[str, str, Optional[Dict[str, Any]]]:
        # Get the command path.
        path = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Path())
        if path is None:
            raise Exception("IsCommandHttpRequest Http request has no path field in HandleCommand.")

        # Everything after our prefix is part of the command path
        commandPath = path[len(CommandHandler.c_CommandHandlerPathPrefix):]
        commandPathLower = commandPath.lower()

        # Parse the args. Args are optional, it depends on the command.
        # Note some of these commands can also be GET requests, so we need to handle that.
        jsonObj:Optional[Dict[str, Any]] = None

        # Parse the POST body if there is one.
        if postBody is not None:
            jsonObj = json.loads(postBody.GetBytesLike())

        # If there is no json object, try for get args.
        if jsonObj is None:
            # This will return None if there are no args.
            # Use the cased version of the string, so get args keep the correct case.
            jsonObj = self._ParseGetArgsAsJson(commandPath)
        return (commandPath, commandPathLower,  jsonObj)


    # If there are GET args, this will parse them into a json object where all values as strings
    # If there are no args, this will return None.
    def _ParseGetArgsAsJson(self, commandPath:str) -> Optional[Dict[str, str]]:
        # We need to remove the ? and split on & to get the args.
        if "?" not in commandPath:
            return None
        try:
            args = commandPath.split("?")[1]
            # Split on & to get the args.
            args = args.split("&")
            # Parse each arg and add it to the jsonObj.
            jsonObj:Dict[str, str] = {}
            for i in args:
                # Split on = to get the key and value.
                keyValue = i.split("=")
                if len(keyValue) != 2:
                    self.Logger.warning("CommandHandler failed to parse args, invalid key value pair: " + i)
                    continue
                else:
                    # Ensure the key is always lower case, but don't mess with the value, things like passwords might need to be case sensitive.
                    key = (str(keyValue[0])).lower()
                    # The value needs to be URL escaped, so we need to decode it.
                    value = unquote(str(keyValue[1]))
                    jsonObj[key] = value
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
