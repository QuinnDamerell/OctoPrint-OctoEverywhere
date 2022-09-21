import flask

from octoprint.access.permissions import Permissions
from octoprint import __version__

from .sentry import Sentry
from .smartpause import SmartPause

# A simple class that handles some the API commands we use for various things.
class ApiCommandHandler:

    def __init__(self, logger, notificationHandler, octoPrintPrinterObject, mainPluginImpl):
        self.Logger = logger
        self.OctoPrintPrinterObject = octoPrintPrinterObject
        self.NotificationHandler = notificationHandler
        self.MainPluginImpl = mainPluginImpl


    # Called by octoprint to get what static commands we expose. We must expose all commands here and any required POST data that's required.
    @staticmethod
    def GetApiCommands():
        return dict(
            # Our frontend js logic calls this API when it detects a local LAN connection and reports the port used.
            # We use the port internally as a solid indicator for what port the http proxy in front of OctoPrint is on.
            # This is required because it's common to also have webcams setup behind the http proxy and there's no other
            # way to query the port value from the system.
            setFrontendLocalPort=["port"],
            # This API is used by the service to get the status of the printer. It can include many default OctoPrint API
            # status information calls as well as our own. No required parameters
            status=[],
            # This API is used to send a print pause, but one that's smarter than the default and is able to move the head back
            # so the print doesn't get messed up.
            smartpause=[]
        )


    # Handles known commands.
    # Can return None or a flask response.
    def HandleApiCommand(self, command, data):
        # This is called by the OctoPrint plugin on_api_command API.
        # Note that all calls to the commands must be POSTs. There is a way to also handle GET commands, but it's honestly just easier
        # always use posts even if there is no post data sent in the response.
        #
        # This function must return None, where OctoPrint will then return a 204 (no content), or it must return a flask response object.
        #   Ex:
        #       flask.abort(404)
        #       flask.jsonify(result="some json result")
        #
        # Finally, all commands handled here must be added to the dict returned by GetApiCommands

        # Right now, to access any of these commands we assert the user at least has SETTINGS_READ permissions, which is required to load the
        # OctoPrint UI, and thus
        if not Permissions.SETTINGS_READ.can():
            return flask.abort(403)

        if command == "status":
            return self.GetStatus()
        if command == "smartpause":
            return self.ExecuteSmartPause(data)
        else:
            # Default
            self.Logger.info("Unknown API command. "+command)
        return None


    # Must return a flask response object or None
    def GetStatus(self):
        # We want to mock the octoprint /api/job API since it has good stuff in it.
        # So we will return a similar result. We use similar code to what the actual API returns.
        # If we fail to get this object, we will still return a result without it.
        octoPrintJobStatus = None
        try:
            # In debug, we will not have this object.
            if self.OctoPrintPrinterObject is not None:
                currentData = self.OctoPrintPrinterObject.get_current_data()
                octoPrintJobStatus = {
                    "job": currentData["job"],
                    "progress": currentData["progress"],
                    "state": currentData["state"]["text"],
                }
                if currentData["state"]["error"]:
                    octoPrintJobStatus["error"] = currentData["state"]["error"]
        except Exception as e:
            Sentry.ExceptionNoSend("API command GetStatus failed to get job status", e)

        # Try to get the temp info from the printer elements.
        tempsObject = None
        try:
            # In debug, we will not have this object.
            if self.OctoPrintPrinterObject is not None:
                # Just dump the temp object into this object, and we will send whatever is in it.
                # This is great because then we can just adapt to new OctoPrint changes as they come.
                tempsObject = self.OctoPrintPrinterObject.get_current_temperatures()
        except Exception as e:
            Sentry.ExceptionNoSend("API command GetStatus failed to get temps", e)

        # Gather info that's specific to us.
        octoeverywhereStatus = None
        try:
            if self.NotificationHandler is None:
                # This shouldn't happen, even debug should have this.
                self.Logger.warn("API command GetStatus has no notification handler")
            else:
                gadget = self.NotificationHandler.GetGadget()
                octoeverywhereStatus = {
                    # <int> The most recent print id. This is only updated when a new print starts, so it will remain until replaced.
                    # Defaults to some print id.
                    "MostRecentPrintId" : self.NotificationHandler.GetPrintId(),
                    # <int> The number of seconds since the epoch when the print started, AKA when MostRecentPrintId was created.
                    # Defaults to the current time.
                    "PrintStartTimeSec" : self.NotificationHandler.GetPrintStartTimeSec(),
                    # Gadget status stuffs
                    "Gadget" :{
                        # <float> The most recent gadget score. This value also remains between prints and is only updated when Gadget returns a new valid score.
                        # Note this score is the average of the most recent 2 scores, to even things out a bit.
                        # Defaults to 0.0
                        "LastScore" : gadget.GetLastGadgetScoreFloat(),
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

        # Try to get the OctoPrint version. This is helpful since we pull OctoPrint dicts directly
        versionStr = None
        try:
            versionStr = str(__version__)
        except Exception as e:
            Sentry.ExceptionNoSend("API command GetStatus failed to get OctoPrint version", e)

        # Build the final response
        responseObj = {
            "OctoPrintJobStatus" : octoPrintJobStatus,
            "OctoEverywhereStatus" : octoeverywhereStatus,
            "OctoPrintTemps" : tempsObject,
            "OctoPrintVersion" : versionStr
        }

        # The double ** dumps this dict into a new dict
        # I'm not 100% sure why this is needed, but other example code (from those who know more than I) use it :)
        return flask.jsonify(**responseObj)


    # Note if there is no print running, this will do nothing and will return success.
    def ExecuteSmartPause(self, postDataDict):

        # Defaults.
        disableHotendBool = True
        disableBedBool = False
        zLiftMm = 0.0
        retractFilamentMm = 0.0
        suppressNotificationBool = False
        showSmartPausePopup = True

        # Parse
        try:
            # Get values
            if "DisableHotend" in postDataDict:
                disableHotendBool = postDataDict["DisableHotend"]
            if "DisableBed" in postDataDict:
                disableBedBool = postDataDict["DisableBed"]
            if "ZLiftMm" in postDataDict:
                zLiftMm = postDataDict["ZLiftMm"]
            if "RetractFilamentMm" in postDataDict:
                retractFilamentMm = postDataDict["RetractFilamentMm"]
            if "SuppressNotification" in postDataDict:
                suppressNotificationBool = postDataDict["SuppressNotification"]
            if "ShowSmartPausePopup" in postDataDict:
                showSmartPausePopup = postDataDict["ShowSmartPausePopup"]
        except Exception as e:
            Sentry.Exception("Failed to ExecuteSmartPause, bad args.", e)
            return flask.abort(400)

        # Ensure we are printing, if not, we want to respond with a 409, like the OctoPrint pause command would.
        if self.OctoPrintPrinterObject.is_printing() is False:
            self.Logger.info("ExecuteSmartPause is retuning a 409 because there is no print in progress.")
            return flask.abort(409)

        # Do it!
        try:
            # If this doesn't throw it's successful
            SmartPause.Get().DoSmartPause(disableHotendBool, disableBedBool, zLiftMm, retractFilamentMm, suppressNotificationBool)
        except Exception as e:
            Sentry.Exception("Failed to ExecuteSmartPause, SmartPause error.", e)
            return flask.abort(500)

        # On success, if we did a smart pause, send a notification to tell the user.
        if self.MainPluginImpl is not None and showSmartPausePopup and (disableBedBool or disableHotendBool or zLiftMm > 0 or retractFilamentMm > 0):
            # Show the notification, but don't auto hide it, to ensure the user sees it.
            title = "OctoEverywhere Smart Pause"
            message = "OctoEverywhere used Smart Pause to protect your print while paused. Smart Pause cooled down your hotend and retracted the z-axis away from the print.<br/><br />When the printing is resumed, the hotend temp and z-axis state will automatically be restored <strong>before</strong> the print resumes."
            self.MainPluginImpl.ShowUiPopup(title, message, "notice", False)

        # Success!
        return flask.jsonify(result="success")
