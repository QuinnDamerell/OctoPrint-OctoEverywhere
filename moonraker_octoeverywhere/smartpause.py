import time
import json
import logging
from typing import Optional

from octoeverywhere.compat import Compat
from octoeverywhere.commandhandler import CommandHandler, CommandResponse
from octoeverywhere.filesystemcommands import FileSystemCommandHelper
from octoeverywhere.interfaces import ISmartPauseHandler

from .moonrakerclient import MoonrakerClient
from .jsonrpcresponse import JsonRpcResponse

# Implements the platform specific logic for moonraker's smart pause.
class SmartPause(ISmartPauseHandler):

    # The static instance.
    _Instance:"SmartPause" = None #pyright: ignore[reportAssignmentType]

    @staticmethod
    def Init(logger:logging.Logger):
        SmartPause._Instance = SmartPause(logger)
        Compat.SetSmartPauseInterface(SmartPause._Instance)


    @staticmethod
    def Get() -> "SmartPause":
        return SmartPause._Instance


    def __init__(self, logger:logging.Logger):
        self.Logger = logger
        self.LastPauseNotificationSuppressionTimeSec = 0.0


    # Does the actual smart pause.
    # Must return a CommandResponse
    def ExecuteSmartPause(self, suppressNotificationBool:bool) -> CommandResponse:

        # Set the pause suppress, if desired.
        # Do this first, since the notification will fire before we can suppress it.
        if suppressNotificationBool:
            self.Logger.info("Setting smart pause time to suppress the pause notification.")
            self.LastPauseNotificationSuppressionTimeSec = time.time()

        # The only parameter we take is the notification suppression
        # This is because moonraker already does a "smart pause" on it's own.
        # All pauses move the head way from the print and then put it back on resume.
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.print.pause", {})
        if result.HasError():
            self.Logger.error("SmartPause failed to request pause. "+result.GetLoggingErrorStr())
            return self._BuildMoonrakerPauseError(result)

        # Ensure the response is a simple result.
        if result.IsSimpleResult() is False:
            self.Logger.error("ExecuteSmartPause didn't return a simple result. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(400, "Bad result type")

        # Check the response
        if result.GetSimpleResult() != "ok":
            self.Logger.error("SmartPause got an invalid request response. "+json.dumps(result.GetSimpleResult()))
            return CommandResponse.Error(400, "Invalid request response.")

        # Return success.
        return CommandResponse.Success(None)


    # !! Interface Function !! - See compat.py GetSmartPauseInterface for the details.
    # Returns None if there is no current suppression or the time of the last time it was requested
    def GetAndResetLastPauseNotificationSuppressionTimeSec(self) -> Optional[float]:
        local = self.LastPauseNotificationSuppressionTimeSec
        self.LastPauseNotificationSuppressionTimeSec = None
        return local


    def _BuildMoonrakerPauseError(self, result:JsonRpcResponse) -> CommandResponse:
        code = result.GetErrorCode()
        errorStr = result.GetErrorStr()
        errorStrLower = errorStr.lower() if errorStr is not None else ""
        if code == JsonRpcResponse.MR_401_UNAUTHORIZED or "unauthorized" in errorStrLower or "forbidden" in errorStrLower or MoonrakerClient.Get().IsDisconnectDueToAuth():
            return CommandResponse.Error(CommandHandler.c_CommandError_LostAuth, FileSystemCommandHelper.AuthFailedError("Moonraker", "pause"))
        if code == JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED:
            return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, FileSystemCommandHelper.PrinterNotConnectedError("Moonraker", "pause"))
        if code == JsonRpcResponse.OE_ERROR_TIMEOUT:
            return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, "pause failed on Moonraker: no response received from the printer before timeout.")
        return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, f"pause failed on Moonraker: {errorStr or 'Failed to request pause'}")
