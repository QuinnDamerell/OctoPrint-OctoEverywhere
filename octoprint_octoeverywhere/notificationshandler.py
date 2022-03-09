import time
import io
import threading

import requests
try:
    # On some systems this package will install but the import will fail due to a missing system .so.
    # Since most setups don't use this package, we will import it with a try catch and if it fails we
    # wont use it.
    from PIL import Image
except Exception as _:
    pass

from .repeattimer import RepeatTimer
from .snapshothelper import SnapshotHelper

class ProgressCompletionReportItem:
    def __init__(self, value, reported):
        self.value = value
        self.reported = reported

    def Value(self):
        return self.value

    def Reported(self):
        return self.reported

    def SetReported(self, reported):
        self.reported = reported

class NotificationsHandler:

    def __init__(self, logger, octoPrintPrinterObject = None):
        self.Logger = logger
        # On init, set the key to empty.
        self.OctoKey = None
        self.PrinterId = None
        self.ProtocolAndDomain = "https://octoeverywhere.com"
        self.OctoPrintPrinterObject = octoPrintPrinterObject
        self.PingTimer = None

        # Define all the vars
        self.CurrentFileName = ""
        self.CurrentPrintStartTime = time.time()
        self.OctoPrintReportedProgressInt = 0
        self.PingTimerHoursReported = 0
        self.HasSendFirstLayerDoneMessage = False
        self.LastUserInteractionNotificationTime = 0
        self.zOffsetLowestSeenMM = 1337.0
        self.zOffsetNotAtLowestCount = 0
        self.ProgressCompletionReported = []

        # Since all of the commands don't send things we need, we will also track them.
        self.ResetForNewPrint()


    def ResetForNewPrint(self):
        self.CurrentFileName = ""
        self.CurrentPrintStartTime = time.time()
        self.OctoPrintReportedProgressInt = 0
        self.PingTimerHoursReported = 0
        self.HasSendFirstLayerDoneMessage = False
        self.LastUserInteractionNotificationTime = 0
        # The following values are used to figure out when the first layer is done.
        self.zOffsetLowestSeenMM = 1337.0
        self.zOffsetNotAtLowestCount = 0

        # Build the progress completion reported list.
        # Add an entry for each progress we want to report, not including 0 and 100%.
        # This list must be in order, from the loweset value to the highest.
        # See _getCurrentProgressFloat for usage.
        self.ProgressCompletionReported = []
        self.ProgressCompletionReported.append(ProgressCompletionReportItem(10.0, False))
        self.ProgressCompletionReported.append(ProgressCompletionReportItem(20.0, False))
        self.ProgressCompletionReported.append(ProgressCompletionReportItem(30.0, False))
        self.ProgressCompletionReported.append(ProgressCompletionReportItem(40.0, False))
        self.ProgressCompletionReported.append(ProgressCompletionReportItem(50.0, False))
        self.ProgressCompletionReported.append(ProgressCompletionReportItem(60.0, False))
        self.ProgressCompletionReported.append(ProgressCompletionReportItem(70.0, False))
        self.ProgressCompletionReported.append(ProgressCompletionReportItem(80.0, False))
        self.ProgressCompletionReported.append(ProgressCompletionReportItem(90.0, False))

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
        # This event might fire over and over or might be paired with a filament change event.
        # In anycase, we only want to fire it every so often.
        if self._shouldFireUserInteractionEvent() is False:
            return

        # Otherwise, send it.
        self._sendEvent("filamentchange")


    # Fired when the printer needs user interaction to continue
    def OnUserInteractionNeeded(self):
        # This event might fire over and over or might be paired with a filament change event.
        # In anycase, we only want to fire it every so often.
        if self._shouldFireUserInteractionEvent() is False:
            return

        # Otherwise, send it.
        self._sendEvent("userinteractionneeded")


    def _shouldFireUserInteractionEvent(self):
        if self.LastUserInteractionNotificationTime > 0:
            # Only send every 5 mintues at most.
            deltaSec = time.time() - self.LastUserInteractionNotificationTime
            if deltaSec < (60.0 * 5.0):
                return False

        # Update the time we sent the notification.
        self.LastUserInteractionNotificationTime = time.time()
        return True


    # Fired when a print is making progress.
    def OnPrintProgress(self, progressInt):

        # Update the local reported value.
        self.OctoPrintReportedProgressInt = progressInt

        # Get the computed print progress value. (see _getCurrentProgressFloat about why)
        computedProgressFloat = self._getCurrentProgressFloat()

        # Since we are computing the progress based on the ETA (see notes in _getCurrentProgressFloat)
        # It's possible we get duplicate ints or even progresses that go back in time.
        # To account for this, we will make sure we only send the update for each progress update once.
        # We will also collapse many progress updates down to one event. For example, if the progress went from 5% -> 45%, we wil only report once for 10, 20, 30, and 40%.
        # We keep track of the highest progress that hasn't been reported yet.
        progressToSendFloat = 0.0
        for item in self.ProgressCompletionReported:
            # Keep going through the items until we find one that's over our current progress.
            # At that point, we are done.
            if item.Value() > computedProgressFloat:
                break

            # If we are over this value and it's not reported, we need to report.
            # Since these items are in order, the largest progress will always overwrite.
            if item.Reported() is False:
                progressToSendFloat = item.Value()

            # Make sure this is marked reported.
            item.SetReported(True)

        # Return if there is nothing to do.
        if progressToSendFloat < 0.1:
            return

        # It's important we send the "snapped" progress here (rounded to the tens place) because the service depends on it
        # to filter out % increments the user didn't want to get notifications for.
        self._sendEvent("progress", None, progressToSendFloat)


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

            # Use the snapshot helper to get the snapshot. This will handle advance logic like relative and absolute URLs
            # as well as getting a snapshot directly from a mjpeg stream if there's no snapshot URL.
            octoHttpResponse = SnapshotHelper.Get().GetSnapshot()

            # Check for a valid response.
            if octoHttpResponse is None or octoHttpResponse.Result is None or octoHttpResponse.Result.status_code != 200:
                return None

            # There are two options here for a result buffer, either
            #   1) it will be already read for us
            #   2) we need to read it out of the http response.
            snapshot = None
            if octoHttpResponse.FullBodyBuffer is not None:
                snapshot = octoHttpResponse.FullBodyBuffer
            else:
                snapshot = octoHttpResponse.Result.content
            if snapshot is None:
                self.Logger.error("Notification snapshot failed, snapshot is None")
                return None

            # Ensure the snapshot is a reasonable size.
            # Right now we will limit to < 2mb
            if len(snapshot) > 2 * 1024 * 1204:
                self.Logger.error("Snapshot size if too large to send. Size: "+len(snapshot))
                return None

            # Correct the image if needed.
            flipH = SnapshotHelper.Get().GetWebcamFlipH()
            flipV = SnapshotHelper.Get().GetWebcamFlipV()
            rotate90 = SnapshotHelper.Get().GetWebcamRotate90()
            if rotate90 or flipH or flipV:
                try:
                    if Image is not None:
                        # Update the image
                        # Note the order of the flips and the rotates are important!
                        # If they are reordered, when multiple are applied the result will not be correct.
                        pilImage = Image.open(io.BytesIO(snapshot))
                        if flipH:
                            pilImage = pilImage.transpose(Image.FLIP_LEFT_RIGHT)
                        if flipV:
                            pilImage = pilImage.transpose(Image.FLIP_TOP_BOTTOM)
                        if rotate90:
                            pilImage = pilImage.rotate(90)

                        # Write back to bytes.
                        buffer = io.BytesIO()
                        pilImage.save(buffer, format="JPEG")
                        snapshot = buffer.getvalue()
                        buffer.close()
                    else:
                        self.Logger.warm("Can't flip image because the Image rotation lib failed to import.")
                except Exception as ex:
                    self.Logger.warm("Failed to flip image for notifications: "+str(ex))

            # Return the image
            return snapshot

        except Exception as _:
            # Don't log here, because for those users with no webcam setup this will fail often.
            # TODO - Ideally we would log, but filter out the expected errors when snapshots are setup by the user.
            #self.Logger.info("Snapshot http call failed. " + str(e))
            pass

        # On failure return nothing.
        return None


    # Assuming the current time is set at the start of the printer correctly
    def _getCurrentDurationSecFloat(self):
        return float(time.time() - self.CurrentPrintStartTime)


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


    # Returns the current print progress as a float.
    def _getCurrentProgressFloat(self):
        # OctoPrint updates us with a progress int, but it turns out that's not the same progress as shown in the web UI.
        # The web UI computes the progress % based on the total print time and ETA. Thus for our notifications to have accurate %s that match
        # the web UIs, we will also try to do the same.
        try:
            # Try to get the print time remaining, which will use smart ETA plugins if possible.
            ptrSec = self.GetPrintTimeRemaningEstimateInSeconds()
            # If we can't get the ETA, default to OctoPrint's value.
            if ptrSec == -1:
                return float(self.OctoPrintReportedProgressInt)

            # Compute the total print time (estimated) and the time thus far
            currentDurationSecFloat = self._getCurrentDurationSecFloat()
            totalPrintTimeSec = currentDurationSecFloat + ptrSec

            # Sanity check for / 0
            if totalPrintTimeSec == 0:
                return float(self.OctoPrintReportedProgressInt)

            # Compute the progress
            printProgressFloat = float(currentDurationSecFloat) / float(totalPrintTimeSec) * float(100.0)

            # Bounds check
            printProgressFloat = max(printProgressFloat, 0.0)
            printProgressFloat = min(printProgressFloat, 100.0)

            # Return the computed value.
            return printProgressFloat

        except Exception as e:
            self.Logger.error("_getCurrentProgressFloat failed to compute progress. Exception: "+str(e))

        # On failure, default to what OctoPrint has reported.
        return float(self.OctoPrintReportedProgressInt)


    # Sends the event
    # Returns True on success, otherwise False
    def _sendEvent(self, event, args = None, progressOverwriteFloat = None):
        # Ensure we are ready.
        if self.PrinterId is None or self.OctoKey is None:
            self.Logger.info("NotificationsHandler didn't send the "+str(event)+" event because we don't have the proper id and key yet.")
            return False

        # Push the work off to a thread so we don't hang OctoPrint's plugin callbacks.
        thread = threading.Thread(target=self._sendEventThreadWorker, args=(event, args, progressOverwriteFloat, ))
        thread.start()

        return True


    # Sends the event
    # Returns True on success, otherwise False
    def _sendEventThreadWorker(self, event, args=None, progressOverwriteFloat=None):
        try:
            # Setup the event.
            eventApiUrl = self.ProtocolAndDomain + "/api/printernotifications/printerevent"

            # Setup the post body
            if args is None:
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
            # -> int to round -> to string for the API.
            # Allow the caller to overwrite the progress we report. This allows the progress update to snap the progress to a hole 10s value.
            progressFloat = 0.0
            if progressOverwriteFloat is not None:
                progressFloat = progressOverwriteFloat
            else:
                progressFloat = self._getCurrentProgressFloat()
            args["ProgressPercentage"] = str(int(progressFloat))

            # Always add the current duration
            args["DurationSec"] = str(self._getCurrentDurationSecFloat())

            # Also always include a snapshot if we can get one.
            files = {}
            snapshot = self.getSnapshot()
            if snapshot is not None:
                files['attachment'] = ("snapshot.jpg", snapshot)

            # Attempt to send the notification twice. If the first time fails,
            # we will wait a bit and try again. It's really unlikely for a notification to fail, the biggest reason
            # would be if the server is updating, there can be a ~20 second window where the call might fail
            attempts = 0
            while attempts < 2:
                attempts += 1

                # Make the request.
                # Since we are sending the snapshot, we must send a multipart form.
                # Thus we must use the data and files fields, the json field will not work.
                r = requests.post(eventApiUrl, data=args, files=files)

                # Check for success.
                if r.status_code == 200:
                    self.Logger.info("NotificationsHandler successfully sent '"+event+"'; ETA: "+str(timeRemainEstStr))
                    return True

                # On failure, log the issue.
                self.Logger.error("NotificationsHandler failed to send event "+str(event)+". Code:"+str(r.status_code) + "; Body:"+r.content.decode())

                # If the error is in the 400 class, don't retry since these are all indications there's something
                # wrong with the request, which won't change.
                if r.status_code < 500:
                    return False

                # If the error is a 500 error, we will try again. Sleep for about 30 seconds to give the server time
                # to boot and be ready again. We would rather wait too long but succeeded, rather than not wait long
                # enough and fail again.
                time.sleep(30)

        except Exception as e:
            self.Logger.error("NotificationsHandler failed to send event code "+str(event)+". Exception: "+str(e))

        return False

    # This function will get the estimated time remaning for the current print.
    # It will first try to get a more accurate from plugins like PrintTimeGenius, otherwise it will fallback to the default OctoPrint total print time estimate.
    # Returns -1 if the estimate is unknown.
    def GetPrintTimeRemaningEstimateInSeconds(self):

        # If the printer object isn't set, we can't get an estimate.
        if self.OctoPrintPrinterObject is None:
            return -1

        # Try to get the progress object from the current data. This is at least set by things like PrintTimeGenius and is more accurate.
        try:
            currentData = self.OctoPrintPrinterObject.get_current_data()
            if "progress" in currentData:
                if "printTimeLeft" in currentData["progress"]:
                    # When the print is just starting, the printTimeLeft will be None.
                    printTimeLeftSec = currentData["progress"]["printTimeLeft"]
                    if printTimeLeftSec is not None:
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
                currentDurationSec = int(self._getCurrentDurationSecFloat())
                if currentDurationSec > printTimeEstSec:
                    return 0
                return printTimeEstSec - currentDurationSec
        except Exception as e:
            self.Logger.error("Failed to find time estimate from OctoPrint. "+str(e))

        # We failed.
        return -1

    # Returns the current zoffset if known, otherwise -1.
    def GetCurrentZOffset(self):
        if self.OctoPrintPrinterObject is None:
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
        if pingTimer is not None:
            pingTimer.Stop()

    # Fired when the ping timer fires.
    def PingTimerCallback(self):
        # Get the current state
        # States can be found here:
        # https://docs.octoprint.org/en/master/modules/printer.html#octoprint.printer.PrinterInterface.get_state_id
        state = "UNKNOWN"
        if self.OctoPrintPrinterObject is None:
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
