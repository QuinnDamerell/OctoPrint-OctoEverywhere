import requests
import time

class NotificationsHandler:

    def __init__(self, logger):
        self.Logger = logger
        # On init, set the key to empty.
        self.OctoKey = None
        self.PrinterId = None
        self.ProtocolAndDomain = "https://octoeverywhere.com"

        # Since all of the commands don't send things we need, we will also track them.
        self.ResetForNewPrint()
 

    def ResetForNewPrint(self):
        self.CurrentFileName = ""
        self.CurrentPrintStartTime = time.time()
        self.CurrentProgressInt = 0
        self.ZChangeCount = 0

    
    def SetPrinterId(self, printerId):
        self.PrinterId = printerId


    def SetOctoKey(self, octoKey):
        self.OctoKey = octoKey


    def SetServerProtocolAndDomain(self, protocolAndDomain):
        self.Logger.info("NotificationsHandler default domain and protocol set to: "+protocolAndDomain)
        self.ProtocolAndDomain = protocolAndDomain


    # Sends the test notification.
    def OnTest(self):
        self._sendEvent("test")


    # Fired when a print starts.
    def OnStarted(self, fileName):
        self.ResetForNewPrint()
        self.CurrentFileName = fileName
        self._sendEvent("started", {"FileName": fileName})


    # Fired when a print fails
    def OnFailed(self, fileName, durationSec, reason):
        self._sendEvent("failed", {"FileName": fileName, "DurationSec": str(durationSec), "Reason": reason})


    # Fired when a print done
    def OnDone(self, fileName, durationSec):
        self._sendEvent("done", {"FileName": fileName, "DurationSec": str(durationSec) })

        
    # Fired when a print is paused
    def OnPaused(self, fileName):
        self._sendEvent("paused", {"FileName": fileName, "DurationSec" : self._getCurrentDurationSec(), "ProgressPercentage" : str(self.CurrentProgressInt)})


    # Fired when a print is resumed
    def OnResume(self, fileName):
        self.CurrentFileName = fileName
        self._sendEvent("resume", {"FileName": fileName, "DurationSec" : self._getCurrentDurationSec(), "ProgressPercentage" : str(self.CurrentProgressInt)})


    # Fired when OctoPrint or the printer hits an error.
    def OnError(self, error):
        self._sendEvent("error", {"Error": error, "FileName": self.CurrentFileName, "DurationSec" : self._getCurrentDurationSec(), "ProgressPercentage" : str(self.CurrentProgressInt)})


    # Fired when the waiting command is received from the printer.
    def OnWaiting(self):
        # Make this the same as the paused command.
        self.OnPaused(self.CurrentFileName)


    # Fired WHENEVER the z axis changes. 
    def OnZChange(self):
        self.ZChangeCount += 1
        # Sanity check
        if self.ZChangeCount < 0:
            # Set higher than what we send, so we don't send weird notifications
            self.ZChangeCount = 10

        # The first zchange happens when the printer is actually starting to print the first layer (after temp is reached and bed leveling is done)
        # The second zchange will happen after the first layer is done.
        # We report layers 1-5 so that the user has choice of what they want notifications for.
        if self.ZChangeCount > 5:
            return

        self.Logger.info("Sending zchange notification. Layer:"+str(self.ZChangeCount))
        self._sendEvent("zchange", {"Layer" : str(self.ZChangeCount), "FileName": self.CurrentFileName, "DurationSec" : self._getCurrentDurationSec(), "ProgressPercentage" : str(self.CurrentProgressInt)})


    # Fired when we get a M600 command from the printer to change the filament
    def OnFilamentChange(self):
        self._sendEvent("filamentchange", { "FileName": self.CurrentFileName, "DurationSec" : self._getCurrentDurationSec(), "ProgressPercentage" : str(self.CurrentProgressInt)})
        

    # Fired when a print is making progress.
    def OnPrintProgress(self, progressInt):
        # Save a local value.
        self.CurrentProgressInt = progressInt

        # Don't handle 0 or 100, since other notifications will handle that.
        if progressInt == 0 or progressInt == 100:
            return
        # Only send update for 10% increments.
        if progressInt % 10 != 0:
            return

        # We use the current print file name, which will be empty string if not set correctly.
        self._sendEvent("progress", {"FileName": self.CurrentFileName, "DurationSec" : self._getCurrentDurationSec(), "ProgressPercentage" : str(progressInt) })


    # Assuming the current time is set at the start of the printer correctly
    # This returns the time from the last known start as a string.
    def _getCurrentDurationSec(self):
        return str(time.time() - self.CurrentPrintStartTime)


    # Sends the event
    # Returns True on success, otherwise False
    def _sendEvent(self, event, args = None):
        # Ensure we are ready.
        if self.PrinterId == None or self.OctoKey == None:
            self.Logger.info("NotificationsHandler didn't send the "+str(event)+" event because we don't have the proper id and key yet.")
            return False

        try:
            # Setup the event.
            eventApiUrl = self.ProtocolAndDomain + "/api/printernotifications/printerevent"

            # Setup the post body
            if args == None:
                args = {}

            # Add the required vars
            args["PrinterId"] = self.PrinterId
            args["OctoKey"] = self.OctoKey
            args["Event"] = event

            # Make the request.
            r = requests.post(eventApiUrl, json=args)

            # Check for success.
            if r.status_code == 200:
                self.Logger.info("NotificationsHandler successfully sent "+event)
                return True

            # On failure, log the issue.
            self.Logger.error("NotificationsHandler failed to send event. Code:"+str(r.status_code) + "; Body:"+r.content.decode())

        except Exception as e:
            self.Logger.error("NotificationsHandler failed to send event code "+str(event)+". Exception: "+str(e))

        return False
