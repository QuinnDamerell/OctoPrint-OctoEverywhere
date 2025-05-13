import time
import json
import logging
from typing import Optional

from octoeverywhere.compat import Compat
from octoeverywhere.commandhandler import CommandResponse
from octoeverywhere.interfaces import ISmartPauseHandler

from .moonrakerclient import MoonrakerClient

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
            return CommandResponse.Error(400, "Failed to request pause")

        # Ensure the response is a simple result.
        if result.IsSimpleResult() is False:
            self.Logger.error("ExecuteSmartPause didn't return a simple result. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(400, "Bad result type")

        # Check the response
        if result.GetSimpleResult() != "ok":
            self.Logger.error("SmartPause got an invalid request response. "+json.dumps(result.GetResult()))
            return CommandResponse.Error(400, "Invalid request response.")

        # Return success.
        return CommandResponse.Success(None)


    # !! Interface Function !! - See compat.py GetSmartPauseInterface for the details.
    # Returns None if there is no current suppression or the time of the last time it was requested
    def GetAndResetLastPauseNotificationSuppressionTimeSec(self) -> Optional[float]:
        local = self.LastPauseNotificationSuppressionTimeSec
        self.LastPauseNotificationSuppressionTimeSec = None
        return local
