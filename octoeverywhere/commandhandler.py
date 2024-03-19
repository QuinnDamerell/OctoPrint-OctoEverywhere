import json

from .octostreammsgbuilder import OctoStreamMsgBuilder
from .octohttprequest import OctoHttpRequest
from .octohttprequest import PathTypes
from .webcamhelper import WebcamHelper
from .sentry import Sentry

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


    _Instance = None


    @staticmethod
    def Init(logger, notificationHandler, platCommandHandler):
        CommandHandler._Instance = CommandHandler(logger, notificationHandler, platCommandHandler)


    @staticmethod
    def Get():
        return CommandHandler._Instance


    def __init__(self, logger, notificationHandler, platCommandHandler):
        self.Logger = logger
        self.NotificationHandler = notificationHandler
        self.PlatformCommandHandler = platCommandHandler


    #
    # Command Handlers
    #

    # Must return a CommandResponse
    def GetStatus(self):
        # We want to mock the OctoPrint /api/job API since it has good stuff in it.
        # So we will return a similar result. We use similar code to what the actual API returns.
        # If we fail to get this object, we will still return a result without it.
        jobStatus = None
        try:
            if self.PlatformCommandHandler is None:
                self.Logger.warn("GetStatus command has no PlatformCommandHandler")
            else:
                # If the plugin is connected and in a good state, this should return the standard job status.
                # On error, meaning the plugin isn't connected to the host, this should return None, which then sends back the HostNotConnected error.
                jobStatus = self.PlatformCommandHandler.GetCurrentJobStatus()
                # This interface should always return None on failure, but make sure.
                if jobStatus is not None and len(jobStatus) == 0:
                    jobStatus = None
        except Exception as e:
            Sentry.ExceptionNoSend("API command GetStatus failed to get job status", e)

        # Ensure we got a job status, otherwise the host isn't connected.
        if jobStatus is None:
            return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, "Host not connected")

        # Gather info that's specific to us.
        octoeverywhereStatus = None
        try:
            if self.NotificationHandler is None:
                # This shouldn't happen, even debug should have this.
                self.Logger.warn("API command GetStatus has no notification handler")
            else:
                gadget = self.NotificationHandler.GetGadget()
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
            Sentry.ExceptionNoSend("API command GetStatus failed to get OctoEverywhere info", e)

        # Get the platform version.
        versionStr = None
        try:
            if self.PlatformCommandHandler is None:
                self.Logger.warn("GetStatus command has no PlatformCommandHandler")
            else:
                versionStr = self.PlatformCommandHandler.GetPlatformVersionStr()
        except Exception as e:
            Sentry.ExceptionNoSend("API command GetStatus failed to get OctoPrint version", e)

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
    def ListWebcams(self, includeUrls = True):
        # Get all of the known webcams
        webcamSettingsItems = WebcamHelper.Get().ListWebcams()
        if webcamSettingsItems is None:
            webcamSettingsItems = []
        # We need to convert the objects into a dic to serialize.
        # Note this format is also used for GetStatus!
        webcams = []
        for i in webcamSettingsItems:
            wc = {}
            wc["Name"] = i.Name
            wc["FlipH"] = i.FlipH
            wc["FlipV"] = i.FlipV
            wc["Rotation"] = i.Rotation
            if includeUrls:
                wc["SnapshotUrl"] = i.SnapshotUrl
                wc["StreamUrl"] = i.StreamUrl
            webcams.append(wc)

        # We always use the default index, which is a reflection of the current camera list.
        # We don't use the name, we only use that internally to keep track of the current index.
        defaultIndex = WebcamHelper.Get().GetDefaultCameraIndex(webcamSettingsItems)
        responseObj = {
            "Webcams" : webcams,
            "DefaultIndex" : defaultIndex
        }
        return CommandResponse.Success(responseObj)


    # Must return a CommandResponse
    def SetDefaultCameraName(self, jsonObjData_CanBeNone):
        name = None
        if jsonObjData_CanBeNone is not None:
            try:
                if "Name" in jsonObjData_CanBeNone:
                    name = jsonObjData_CanBeNone["Name"]
            except Exception as e:
                Sentry.Exception("Failed to SetDefaultCameraName, bad args.", e)
                return CommandResponse.Error(400, "Failed to parse args")
        if name is None:
            return CommandResponse.Error(400, "No name passed")
        # Set the name
        WebcamHelper.Get().SetDefaultCameraName(name)
        # Return success
        return CommandResponse.Success({})


    # Must return a CommandResponse
    def Pause(self, jsonObjData_CanBeNone):

        # Defaults.
        smartPause = False
        suppressNotificationBool = True

        # Smart pause options
        disableHotendBool = True
        disableBedBool = False
        zLiftMm = 0.0
        retractFilamentMm = 0.0
        showSmartPausePopup = True

        # Parse if we have args
        if jsonObjData_CanBeNone is not None:
            try:
                # Get values
                # ParseSmart Pause first, since it changes the default of suppressNotificationBool
                if "SmartPause" in jsonObjData_CanBeNone:
                    smartPause = jsonObjData_CanBeNone["SmartPause"]

                # Update the default of the notification suppression based on the type. We only suppress for smart pause
                # because it will only happen from Gadget, which will send it's own notification.
                suppressNotificationBool = smartPause

                # Parse the rest.
                if "DisableHotend" in jsonObjData_CanBeNone:
                    disableHotendBool = jsonObjData_CanBeNone["DisableHotend"]
                if "DisableBed" in jsonObjData_CanBeNone:
                    disableBedBool = jsonObjData_CanBeNone["DisableBed"]
                if "ZLiftMm" in jsonObjData_CanBeNone:
                    zLiftMm = jsonObjData_CanBeNone["ZLiftMm"]
                if "RetractFilamentMm" in jsonObjData_CanBeNone:
                    retractFilamentMm = jsonObjData_CanBeNone["RetractFilamentMm"]
                if "SuppressNotification" in jsonObjData_CanBeNone:
                    suppressNotificationBool = jsonObjData_CanBeNone["SuppressNotification"]
                if "ShowSmartPausePopup" in jsonObjData_CanBeNone:
                    showSmartPausePopup = jsonObjData_CanBeNone["ShowSmartPausePopup"]
            except Exception as e:
                Sentry.Exception("Failed to ExecuteSmartPause, bad args.", e)
                return CommandResponse.Error(400, "Failed to parse args")

        # If this throws that's fine.
        return self.PlatformCommandHandler.ExecutePause(smartPause, suppressNotificationBool, disableHotendBool, disableBedBool, zLiftMm, retractFilamentMm, showSmartPausePopup)


    def Resume(self):
        return self.PlatformCommandHandler.ExecuteResume()


    def Cancel(self):
        return self.PlatformCommandHandler.ExecuteCancel()


    #
    # Common Handler Core Logic
    #

    # Returns True or False depending if this request is a OE command or not.
    # If it is, HandleCommand should be used to get the response.
    def IsCommandRequest(self, httpInitialContext):
        # Get the path to check if it's a command or not.
        if httpInitialContext.PathType() != PathTypes.Relative:
            return None
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
    def HandleCommand(self, httpInitialContext, postBody_CanBeNone):
        # Get the command path.
        path = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Path())
        if path is None:
            raise Exception("IsCommandHttpRequest Http request has no path field in HandleCommand.")

        # Everything after our prefix is part of the command path
        commandPath = path[len(CommandHandler.c_CommandHandlerPathPrefix):]

        # Parse the args. Args are optional, it depends on the command.
        jsonObj_CanBeNone = None
        try:
            if postBody_CanBeNone is not None:
                jsonObj_CanBeNone = json.loads(postBody_CanBeNone)
        except Exception as e:
            Sentry.Exception("CommandHandler error while parsing command args.", e)
            responseObj = CommandResponse.Error(CommandHandler.c_CommandError_ArgParseFailure, str(e))

        # Handle the command
        responseObj = None
        try:
            responseObj = self.ProcessCommand(commandPath, jsonObj_CanBeNone)
        except Exception as e:
            Sentry.Exception("CommandHandler error while handling command.", e)
            responseObj = CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, str(e))


        # Build the result
        resultBytes = None
        try:
            # Build the common response.
            jsonResponse = {
                "Status" : responseObj.StatusCode
            }
            if responseObj.ErrorStr is not None:
                jsonResponse["Error"] = responseObj.ErrorStr
            if responseObj.ResultDict is not None:
                jsonResponse["Result"] = responseObj.ResultDict

            # Serialize to bytes
            resultBytes = json.dumps(jsonResponse).encode(encoding="utf-8")

        except Exception as e:
            Sentry.Exception("CommandHandler failed to serialize response.", e)
            # Use a known good json object for this error.
            resultBytes = json.dumps(
                {
                    "Status": CommandHandler.c_CommandError_ResponseSerializeFailure,
                    "Error":"Serialize Response Failed"
                }).encode(encoding="utf-8")

        # Build the full result
        return OctoHttpRequest.Result(200, {}, OctoStreamMsgBuilder.BytesToString(httpInitialContext.Path()), False, fullBodyBuffer=resultBytes)


    # The goal here is to keep as much of the common logic as common as possible.
    def ProcessCommand(self, commandPath, jsonObj_CanBeNone):
        # To lower, to match any case.
        commandPathLower = commandPath.lower()
        if commandPathLower.startswith("ping"):
            return CommandResponse.Success({"Message":"Pong"})
        elif commandPathLower.startswith("status"):
            return self.GetStatus()
        elif commandPathLower.startswith("list-webcam"):
            return self.ListWebcams()
        elif commandPathLower.startswith("set-default-webcam"):
            return self.SetDefaultCameraName(jsonObj_CanBeNone)
        elif commandPathLower.startswith("pause"):
            return self.Pause(jsonObj_CanBeNone)
        elif commandPathLower.startswith("resume"):
            return self.Resume()
        elif commandPathLower.startswith("cancel"):
            return self.Cancel()

        return CommandResponse.Error(CommandHandler.c_CommandError_UnknownCommand, "The command path didn't match any known commands.")


# A helper class that's the result of all ran commands.
class CommandResponse():

    @staticmethod
    def Success(resultDict:dict):
        if resultDict is None:
            resultDict = {}
        return CommandResponse(200, resultDict, None)


    @staticmethod
    def Error(statusCode:int, errorStr_CanBeNull:str):
        return CommandResponse(statusCode, None, errorStr_CanBeNull)


    def __init__(self, statusCode:int, resultDict:dict, errorStr_CanBeNull:str):
        self.StatusCode = statusCode
        self.ResultDict = resultDict
        self.ErrorStr = errorStr_CanBeNull
