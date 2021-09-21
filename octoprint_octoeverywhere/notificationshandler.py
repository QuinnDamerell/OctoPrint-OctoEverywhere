import requests
import time
import io
from PIL import Image

class NotificationsHandler:

    def __init__(self, logger, octoPrintPrinterObject = None, octoPrintSettingsObject = None):
        self.Logger = logger
        # On init, set the key to empty.
        self.OctoKey = None
        self.PrinterId = None
        self.ProtocolAndDomain = "https://octoeverywhere.com"
        self.OctoPrintPrinterObject = octoPrintPrinterObject
        self.OctoPrintSettingsObject = octoPrintSettingsObject

        # Since all of the commands don't send things we need, we will also track them.
        self.ResetForNewPrint()
 

    def ResetForNewPrint(self):
        self.CurrentFileName = ""
        self.CurrentPrintStartTime = time.time()
        self.CurrentProgressInt = 0
        self.HasSendFirstFewLayersMessage = False

    
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
        # If we have already sent the "first few layers" message there's nothing to do.
        if self.HasSendFirstFewLayersMessage:
            return

        # We can't found the number of times the z-height changes because if slicers use "z-hop" the z will change multiple times
        # on the same layer. We can get the current z-offset, but we don't know the layer height of the print. So for that reason
        # when the zchange goes above some threadhold, we fire the "first few layers" event. 
        currentZOffsetMM = self.GetCurrentZOffset()

        # Make sure we know it.
        if currentZOffsetMM == -1:
            return

        # Only fire once the z offset is greater than. Most layer heights are 0.07 - 0.3.
        if currentZOffsetMM < 3.1:
            return

        # Send the message.
        self.HasSendFirstFewLayersMessage = True
        self._sendEvent("firstfewlayersdone", {"ZOffsetMM" : str(currentZOffsetMM), "FileName": self.CurrentFileName, "DurationSec" : self._getCurrentDurationSec(), "ProgressPercentage" : str(self.CurrentProgressInt)})


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
            self.Logger.info("Snapshot http call failed. " + str(e))
        
        # On failure return nothing.
        return None


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

            # Always include the ETA, note this will be -1 if the time is unknown.
            timeRemainEstStr =  str(self.GetPrintTimeRemaningEstimateInSeconds())
            args["TimeRemaningSec"] = timeRemainEstStr

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