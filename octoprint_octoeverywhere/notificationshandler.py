import requests
import time
import io
from PIL import Image

from .repeattimer import RepeatTimer

class NotificationsHandler:

    def __init__(self, logger, octoPrintPrinterObject = None, octoPrintSettingsObject = None):
        self.Logger = logger
        # On init, set the key to empty.
        self.OctoKey = None
        self.PrinterId = None
        self.ProtocolAndDomain = "https://octoeverywhere.com"
        self.OctoPrintPrinterObject = octoPrintPrinterObject
        self.OctoPrintSettingsObject = octoPrintSettingsObject
        self.PingTimer = None

        # Since all of the commands don't send things we need, we will also track them.
        self.ResetForNewPrint()
 

    def ResetForNewPrint(self):
        self.CurrentFileName = ""
        self.CurrentPrintStartTime = time.time()
        self.CurrentProgressInt = 0
        self.PingTimerHoursReported = 0
        self.HasSendFirstLayerDoneMessage = False
        # The following values are used to figure out when the first layer is done.
        self.zOffsetLowestSeenMM = 1337.0
        self.zOffsetNotAtLowestCount = 0
    
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
        self._updateCurrentFileName(fileName)
        self.SetupPingTimer()
        self._sendEvent("started")


    # Fired when a print fails
    def OnFailed(self, fileName, durationSecStr, reason):
        self._updateCurrentFileName(fileName)
        self._updateToKnownDuration(durationSecStr)
        self.StopPingTimer()
        self._sendEvent("failed", { "Reason": reason})


    # Fired when a print done
    def OnDone(self, fileName, durationSecStr):
        self._updateCurrentFileName(fileName)
        self._updateToKnownDuration(durationSecStr)
        self.StopPingTimer()
        self._sendEvent("done")

        
    # Fired when a print is paused
    def OnPaused(self, fileName):
        self._updateCurrentFileName(fileName)
        self._sendEvent("paused")


    # Fired when a print is resumed
    def OnResume(self, fileName):
        self._updateCurrentFileName(fileName)
        self._sendEvent("resume")


    # Fired when OctoPrint or the printer hits an error.
    def OnError(self, error):
        self.StopPingTimer()
        self._sendEvent("error", {"Error": error })


    # Fired when the waiting command is received from the printer.
    def OnWaiting(self):
        # Make this the same as the paused command.
        self.OnPaused(self.CurrentFileName)


    # Fired WHENEVER the z axis changes. 
    def OnZChange(self):
        # If we have already sent the first layer done message there's nothing to do.
        if self.HasSendFirstLayerDoneMessage:
            return

        # Get the current zoffset value.
        currentZOffsetMM = self.GetCurrentZOffset()

        # Make sure we know it.
        if currentZOffsetMM == -1:
            return

        # The trick here is how we do figure out when the first layer is done with out knowing the print layer height
        # or how the gcode is written to do zhops.
        #
        # Our current solution is to keep track of the lowest zvalue we have seen for this print.
        # Everytime we don't see the zvalue be the lowest, we increment a counter. After n number of reports above the lowest value, we
        # consider the first layer done because we haven't seen the printer return to the first layer height.
        #
        # Typically, the flow looks something like... 0.4 -> 0.2 -> 0.4 -> 0.2 -> 0.4 -> 0.5 -> 0.7 -> 0.5 -> 0.7...
        # Where the layer hight is 0.2 (because it's the lowest first value) and the zhops are 0.4 or more.

        # Since this is a float, avoid ==
        if currentZOffsetMM > self.zOffsetLowestSeenMM - 0.01 and currentZOffsetMM < self.zOffsetLowestSeenMM + 0.01:
            # The zOffset is the same as the lowest we have seen.
            self.zOffsetNotAtLowestCount = 0
        elif currentZOffsetMM < self.zOffsetLowestSeenMM:
            # We found a new low, record it.
            self.zOffsetLowestSeenMM = currentZOffsetMM
            self.zOffsetNotAtLowestCount = 0        
        else:
            # The zOffset is higher than the lowest we have seen.
            self.zOffsetNotAtLowestCount += 1

        # After zOffsetNotAtLowestCount >= 2, we consider the first layer to be done.
        # This means we won't fire the event until we see two zmoves that are above the known min.
        if self.zOffsetNotAtLowestCount < 2:
            return

        # Send the message.
        self.HasSendFirstLayerDoneMessage = True
        self._sendEvent("firstlayerdone", {"ZOffsetMM" : str(currentZOffsetMM) })


    # Fired when we get a M600 command from the printer to change the filament
    def OnFilamentChange(self):
        self._sendEvent("filamentchange")


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
        self._sendEvent("progress")


    # Fired every hour while a print is running
    def OnPrintTimerProgress(self):
        # This event is fired by our internal timer only while prints are running.
        # It will only fire every hour.

        # We send a duration, but that duration is controlled by OctoPrint and can be changed.
        # Since we allow the user to pick "every x hours" to be notified, it's easier for the server to
        # keep track if we just send an int as well.
        # Since this fires once an hour, everytime it fires just add one.
        self.PingTimerHoursReported += 1

        self._sendEvent("timerprogress", { "HoursCount": str(self.PingTimerHoursReported) })


    # If possible, gets a snapshot from the snapshot URL configured in OctoPrint.
    # If this fails for any reason, None is returned.
    def getSnapshot(self):
        try:
            # Get the vars we need.
            snapshotUrl = ""
            flipH = False
            flipV = False
            rotate90 = False
            if self.OctoPrintSettingsObject != None :
                # This is the normal plugin case
                snapshotUrl = self.OctoPrintSettingsObject.global_get(["webcam", "snapshot"])
                flipH = self.OctoPrintSettingsObject.global_get(["webcam", "flipH"])
                flipV = self.OctoPrintSettingsObject.global_get(["webcam", "flipV"])
                rotate90 = self.OctoPrintSettingsObject.global_get(["webcam", "rotate90"])
            else:
                # This is the dev case
                snapshotUrl = "http://192.168.86.57/webcam/?action=snapshot"

            # If there's no URL, don't bother
            if snapshotUrl == None or len(snapshotUrl) == 0:
                return None

            # Make the http call.
            snapshot = requests.get(snapshotUrl, stream=True).content

            # Ensure the snapshot is a reasonable size.
            # Right now we will limit to < 2mb
            if len(snapshot) > 2 * 1024 * 1204:
                self.Logger.error("Snapshot size if too large to send. Size: "+len(snapshot))
                return None

            # Correct the image if needed.
            if rotate90 or flipH or flipV:
                # Update the image
                pilImage = Image.open(io.BytesIO(snapshot))
                if rotate90:
                    pilImage = pilImage.rotate(90)
                if flipH:
                    pilImage = pilImage.transpose(Image.FLIP_LEFT_RIGHT)
                if flipV:
                    pilImage = pilImage.transpose(Image.FLIP_TOP_BOTTOM) 

                # Write back to bytes.               
                buffer = io.BytesIO()
                pilImage.save(buffer, format="JPEG")
                snapshot = buffer.getvalue()
                buffer.close()
            
            # Return the image
            return snapshot

        except Exception as e:
            # Don't log here, because for those users with no webcam setup this will fail often.
            # TODO - Ideally we would log, but filter out the expected errors when snapshots are setup by the user.
            #self.Logger.info("Snapshot http call failed. " + str(e))
            pass
        
        # On failure return nothing.
        return None


    # Assuming the current time is set at the start of the printer correctly
    # This returns the time from the last known start as a string.
    def _getCurrentDurationSec(self):
        return str(time.time() - self.CurrentPrintStartTime)


    # When OctoPrint tells us the duration, make sure we are in sync.
    def _updateToKnownDuration(self, durationSecStr):
        # If the string is empty return.
        if len(durationSecStr) == 0:
            return

        # If we fail this logic don't kill the event.
        try:
            self.CurrentPrintStartTime = time.time() - float(durationSecStr)
        except Exception as e:
            self.Logger.error("_updateToKnownDuration exception "+str(e))

    
    # Updates the current file name, if there is a new name to set.
    def _updateCurrentFileName(self, fileNameStr):
        if len(fileNameStr) == 0:
            return
        self.CurrentFileName = fileNameStr


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

            # Always add the file name
            args["FileName"] = str(self.CurrentFileName)

            # Always include the ETA, note this will be -1 if the time is unknown.
            timeRemainEstStr =  str(self.GetPrintTimeRemaningEstimateInSeconds())
            args["TimeRemaningSec"] = timeRemainEstStr

            # Always add the current progress
            args["ProgressPercentage"] = str(self.CurrentProgressInt)

            # Always add the current duration
            args["DurationSec"] = str(self._getCurrentDurationSec())         

            # Also always include a snapshot if we can get one.
            files = {}
            snapshot = self.getSnapshot()
            if snapshot != None:
                files['attachment'] = ("snapshot.jpg", snapshot) 

            # Make the request.
            # Since we are sending the snapshot, we must send a multipart form.
            # Thus we must use the data and files fields, the json field will not work.
            r = requests.post(eventApiUrl, data=args, files=files)

            # Check for success.
            if r.status_code == 200:
                self.Logger.info("NotificationsHandler successfully sent '"+event+"'; ETA: "+str(timeRemainEstStr))
                return True

            # On failure, log the issue.
            self.Logger.error("NotificationsHandler failed to send event. Code:"+str(r.status_code) + "; Body:"+r.content.decode())

        except Exception as e:
            self.Logger.error("NotificationsHandler failed to send event code "+str(event)+". Exception: "+str(e))

        return False

    # This function will get the estimated time remaning for the current print.
    # It will first try to get a more accurate from plugins like PrintTimeGenius, otherwise it will fallback to the default OctoPrint total print time estimate.
    # Returns -1 if the estimate is unknown.
    def GetPrintTimeRemaningEstimateInSeconds(self):

        # If the printer object isn't set, we can't get an estimate.
        if self.OctoPrintPrinterObject == None:
            return -1

        # Try to get the progress object from the current data. This is at least set by things like PrintTimeGenius and is more accurate.
        try:
            currentData = self.OctoPrintPrinterObject.get_current_data()
            if "progress" in currentData:
                if "printTimeLeft" in currentData["progress"]:
                    # When the print is just starting, the printTimeLeft will be None.
                    printTimeLeftSec = currentData["progress"]["printTimeLeft"]
                    if printTimeLeftSec != None:
                        printTimeLeft = int(float(currentData["progress"]["printTimeLeft"]))
                        return printTimeLeft
        except Exception as e:
            self.Logger.error("Failed to find progress object in printer current data. "+str(e))

        # If that fails, try to use the default OctoPrint estimate.
        try:
            jobData = self.OctoPrintPrinterObject.get_current_job()
            if "estimatedPrintTime" in jobData:
                printTimeEstSec = int(jobData["estimatedPrintTime"])
                # Compute how long this print has been running and subtract
                # Sanity check the duration isn't longer than the ETA.
                currentDurationSec = int(float(self._getCurrentDurationSec()))
                if currentDurationSec > printTimeEstSec:
                    return 0
                return printTimeEstSec - currentDurationSec
        except Exception as e:
            self.Logger.error("Failed to find time estimate from OctoPrint. "+str(e))

        # We failed.
        return -1

    # Returns the current zoffset if known, otherwise -1.
    def GetCurrentZOffset(self):
        if self.OctoPrintPrinterObject == None:
            return -1

        # Try to get the current value from the data.
        try:
            currentData = self.OctoPrintPrinterObject.get_current_data()
            if "currentZ" in currentData:
                currentZ = float(currentData["currentZ"])
                return currentZ
        except Exception as e:
            self.Logger.error("Failed to find current z offset. "+str(e))

        # Failed to find it.
        return -1

    # Starts a ping timer which is used to fire "every x mintues events".
    def SetupPingTimer(self):
        # First, stop any timer that's currently running.
        self.StopPingTimer()

        # Make sure the hours flag is cleared when we start a new timer.
        self.PingTimerHoursReported = 0

        # Setup the new timer
        intervalSec = 60 * 60 # Fire every hour.
        timer = RepeatTimer(self.Logger, intervalSec, self.PingTimerCallback)
        timer.start()
        self.PingTimer = timer


    # Stops any running ping timer.
    def StopPingTimer(self):
        # Capture locally
        pingTimer = self.PingTimer
        self.PingTimer = None
        if pingTimer != None:
            pingTimer.Stop()

    # Fired when the ping timer fires.
    def PingTimerCallback(self):
        # Get the current state
        # States can be found here:
        # https://docs.octoprint.org/en/master/modules/printer.html#octoprint.printer.PrinterInterface.get_state_id
        state = "UNKNOWN"
        if self.OctoPrintPrinterObject == None:
            self.Logger.warn("Notification ping timer doesn't have a OctoPrint printer object.")
            state = "PRINTING"
        else:
            state = self.OctoPrintPrinterObject.get_state_id()

        # Ensure the state is still printing or paused, if not we are done.
        if state != "PRINTING" and state != "PAUSED":
            self.Logger.info("Notification ping timer state doesn't seem to be printing, stopping timer. State: "+str(state))
            self.StopPingTimer()
            return

        # Fire the event.
        self.OnPrintTimerProgress()       
