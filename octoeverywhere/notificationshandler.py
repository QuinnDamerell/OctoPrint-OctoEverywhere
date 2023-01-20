import math
import time
import io
import threading
from random import randint

import requests

from .gadget import Gadget
from .requestsutils import RequestsUtils
from .sentry import Sentry
from .compat import Compat
from .snapshotresizeparams import SnapshotResizeParams
from .repeattimer import RepeatTimer
from .snapshothelper import SnapshotHelper

try:
    # On some systems this package will install but the import will fail due to a missing system .so.
    # Since most setups don't use this package, we will import it with a try catch and if it fails we
    # won't use it.
    from PIL import Image
    from PIL import ImageFile
except Exception as _:
    pass

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

    # This is the max snapshot file size we will allow to be sent.
    MaxSnapshotFileSizeBytes = 2 * 1024 * 1024

    def __init__(self, logger, octoPrintPrinterObject = None):
        self.Logger = logger
        # On init, set the key to empty.
        self.OctoKey = None
        self.PrinterId = None
        self.ProtocolAndDomain = "https://printer-events-v1-oeapi.octoeverywhere.com"
        self.OctoPrintPrinterObject = octoPrintPrinterObject
        self.PingTimer = None
        self.Gadget = Gadget(logger, self)

        # Define all the vars
        self.CurrentFileName = ""
        self.CurrentPrintStartTime = time.time()
        self.OctoPrintReportedProgressInt = 0
        self.PingTimerHoursReported = 0
        self.HasSendFirstLayerDoneMessage = False
        self.zOffsetLowestSeenMM = 1337.0
        self.zOffsetNotAtLowestCount = 0
        self.ProgressCompletionReported = []
        self.PrintId = 0
        self.PrintStartTimeSec = 0

        self.SpammyEventTimeDict = {}
        self.SpammyEventLock = threading.Lock()

        # Since all of the commands don't send things we need, we will also track them.
        self.ResetForNewPrint()


    def ResetForNewPrint(self):
        self.CurrentFileName = ""
        self.CurrentPrintStartTime = time.time()
        self.OctoPrintReportedProgressInt = 0
        self.PingTimerHoursReported = 0
        self.HasSendFirstLayerDoneMessage = False
        # The following values are used to figure out when the first layer is done.
        self.zOffsetLowestSeenMM = 1337.0
        self.zOffsetNotAtLowestCount = 0

        # Each time a print starts, we generate a fixed length random id to identify it.
        # This just helps the server keep track of events that are related.
        self.PrintId = randint(100000000, 999999999)

        # Note the time this print started
        self.PrintStartTimeSec = time.time()

        # Reset our anti spam times.
        self.SpammyEventTimeDict = {}

        # Build the progress completion reported list.
        # Add an entry for each progress we want to report, not including 0 and 100%.
        # This list must be in order, from the lowest value to the highest.
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


    def SetGadgetServerProtocolAndDomain(self, protocolAndDomain):
        self.Gadget.SetServerProtocolAndDomain(protocolAndDomain)


    def GetPrintId(self):
        return self.PrintId


    def GetPrintStartTimeSec(self):
        return self.PrintStartTimeSec


    def GetGadget(self):
        return self.Gadget


    # Only used for testing.
    def OnTest(self):
        self._sendEvent("test")


    # Only used for testing.
    def OnGadgetWarn(self):
        self._sendEvent("gadget-warning")


    # Only used for testing.
    def OnGadgetPaused(self):
        self._sendEvent("gadget-paused")


    # Fired when a print starts.
    def OnStarted(self, fileName):
        self.ResetForNewPrint()
        self._updateCurrentFileName(fileName)
        self.SetupPingTimer(True)
        self._sendEvent("started")
        self.Logger.info("New print started; PrintId: "+str(self.PrintId))


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
        # Always update the file name.
        self._updateCurrentFileName(fileName)

        # See if there is a pause notification suppression set. If this is not null and it was recent enough
        # suppress the notification from firing.
        # If there is no suppression, or the suppression was older than 30 seconds, fire the notification.
        if Compat.HasSmartPause():
            lastSuppressTimeSec = Compat.GetSmartPause().GetAndResetLastPauseNotificationSuppressionTimeSec()
            if lastSuppressTimeSec is None or time.time() - lastSuppressTimeSec > 20.0:
                self._sendEvent("paused")
            else:
                self.Logger.info("Not firing the pause notification due to a Smart Pause suppression.")

        # Stop the ping timer, so we don't report progress while we are paused.
        self.StopPingTimer()


    # Fired when a print is resumed
    def OnResume(self, fileName):
        self._updateCurrentFileName(fileName)
        self._sendEvent("resume")

        # Start the ping timer, to ensure it's running now.
        self.SetupPingTimer(False)


    # Fired when OctoPrint or the printer hits an error.
    def OnError(self, error):
        self.StopPingTimer()

        # This might be spammy from OctoPrint, so limit how often we bug the user with them.
        if self._shouldSendSpammyEvent("on-error"+str(error), 30.0) is False:
            return

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

        # Ensure we are in state where we should fire this (printing)
        # Otherwise we will set the flag to disable the message, which will be reset on the
        # next print start.
        if self.ShouldPrintingTimersBeRunning() is False:
            self.HasSendFirstLayerDoneMessage = True
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
        # Every time we don't see the zvalue be the lowest, we increment a counter. After n number of reports above the lowest value, we
        # consider the first layer done because we haven't seen the printer return to the first layer height.
        #
        # Typically, the flow looks something like... 0.4 -> 0.2 -> 0.4 -> 0.2 -> 0.4 -> 0.5 -> 0.7 -> 0.5 -> 0.7...
        # Where the layer hight is 0.2 (because it's the lowest first value) and the zhops are 0.4 or more.

        # Since this is a float, avoid ==
        if currentZOffsetMM > self.zOffsetLowestSeenMM - 0.01 and currentZOffsetMM < self.zOffsetLowestSeenMM + 0.01:
            # The zOffset is the same as the previously seen.
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
        # In any case, we only want to fire it every so often.
        # It's important to use the same key to make sure we de-dup the possible OnUserInteractionNeeded that might fire second.
        if self._shouldSendSpammyEvent("user-interaction-needed", 5.0) is False:
            return

        # Otherwise, send it.
        self._sendEvent("filamentchange")


    # Fired when the printer needs user interaction to continue
    def OnUserInteractionNeeded(self):
        # This event might fire over and over or might be paired with a filament change event.
        # In any case, we only want to fire it every so often.
        # It's important to use the same key to make sure we de-dup the possible OnUserInteractionNeeded that might fire second.
        if self._shouldSendSpammyEvent("user-interaction-needed", 5.0) is False:
            return

        # Otherwise, send it.
        self._sendEvent("userinteractionneeded")


    # Fired when a print is making progress.
    def OnPrintProgress(self, progressInt):

        # Update the local reported value.
        self.OctoPrintReportedProgressInt = progressInt

        # Get the computed print progress value. (see _getCurrentProgressFloat about why)
        computedProgressFloat = self._getCurrentProgressFloat()

        # Since we are computing the progress based on the ETA (see notes in _getCurrentProgressFloat)
        # It's possible we get duplicate ints or even progresses that goes back in time.
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
            # Since these items are in order, the largest progress will always be overwritten.
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
        # Since this fires once an hour, every time it fires just add one.
        self.PingTimerHoursReported += 1

        self._sendEvent("timerprogress", { "HoursCount": str(self.PingTimerHoursReported) })


    # If possible, gets a snapshot from the snapshot URL configured in OctoPrint.
    # SnapshotResizeParams can be passed BUT MIGHT BE IGNORED if the PIL lib can't be loaded.
    # SnapshotResizeParams will also be ignored if the current image is smaller than the requested size.
    # If this fails for any reason, None is returned.
    def getSnapshot(self, snapshotResizeParams = None):
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
                # Since we use Stream=True, we have to wait for the full body to download before getting it
                snapshot = RequestsUtils.ReadAllContentFromStreamResponse(octoHttpResponse.Result)
            if snapshot is None:
                self.Logger.error("Notification snapshot failed, snapshot is None")
                return None

            # Ensure the snapshot is a reasonable size. If it's not, try to resize it if there's not another resize planned.
            # If this fails, the size will be checked again later and the image will be thrown out.
            if len(snapshot) > NotificationsHandler.MaxSnapshotFileSizeBytes:
                if snapshotResizeParams is None:
                    # Try to limit the size to be 1080 tall.
                    snapshotResizeParams = SnapshotResizeParams(1080, True, False, False)

            # Manipulate the image if needed.
            flipH = SnapshotHelper.Get().GetWebcamFlipH()
            flipV = SnapshotHelper.Get().GetWebcamFlipV()
            rotate90 = SnapshotHelper.Get().GetWebcamRotate90()
            if rotate90 or flipH or flipV or snapshotResizeParams is not None:
                try:
                    if Image is not None:

                        # We noticed that on some under powered or otherwise bad systems the image returned
                        # by mjpeg is truncated. We aren't sure why this happens, but setting this flag allows us to sill
                        # manipulate the image even though we didn't get the whole thing. Otherwise, we would use the raw snapshot
                        # buffer, which is still an incomplete image.
                        # Use a try catch incase the import of ImageFile failed
                        try:
                            ImageFile.LOAD_TRUNCATED_IMAGES = True
                        except Exception as _:
                            pass

                        # Update the image
                        # Note the order of the flips and the rotates are important!
                        # If they are reordered, when multiple are applied the result will not be correct.
                        didWork = False
                        pilImage = Image.open(io.BytesIO(snapshot))
                        if flipH:
                            pilImage = pilImage.transpose(Image.FLIP_LEFT_RIGHT)
                            didWork = True
                        if flipV:
                            pilImage = pilImage.transpose(Image.FLIP_TOP_BOTTOM)
                            didWork = True
                        if rotate90:
                            pilImage = pilImage.rotate(90)
                            didWork = True

                        #
                        # Now apply any resize operations needed.
                        #
                        if snapshotResizeParams is not None:
                            # First, if we want to scale and crop to center, we will use the resize operation to get the image
                            # scale (preserving the aspect ratio). We will use the smallest side to scale to the desired outcome.
                            if snapshotResizeParams.CropSquareCenterNoPadding:
                                # We will only do the crop resize if the source image is smaller than or equal to the desired size.
                                if pilImage.height >= snapshotResizeParams.Size and pilImage.width >= snapshotResizeParams.Size:
                                    if pilImage.height < pilImage.width:
                                        snapshotResizeParams.ResizeToHeight = True
                                        snapshotResizeParams.ResizeToWidth = False
                                    else:
                                        snapshotResizeParams.ResizeToHeight = False
                                        snapshotResizeParams.ResizeToWidth = True

                            # Do any resizing required.
                            resizeHeight = None
                            resizeWidth = None
                            if snapshotResizeParams.ResizeToHeight:
                                if pilImage.height > snapshotResizeParams.Size:
                                    resizeHeight = snapshotResizeParams.Size
                                    resizeWidth = int((float(snapshotResizeParams.Size) / float(pilImage.height)) * float(pilImage.width))
                            if snapshotResizeParams.ResizeToWidth:
                                if pilImage.width > snapshotResizeParams.Size:
                                    resizeHeight = int((float(snapshotResizeParams.Size) / float(pilImage.width)) * float(pilImage.height))
                                    resizeWidth = snapshotResizeParams.Size
                            # If we have things to resize, do it.
                            if resizeHeight is not None and resizeWidth is not None:
                                pilImage = pilImage.resize((resizeWidth, resizeHeight))
                                didWork = True

                            # Now if we want to crop square, use the resized image to crop the remaining side.
                            if snapshotResizeParams.CropSquareCenterNoPadding:
                                left = 0
                                upper = 0
                                right = 0
                                lower = 0
                                if snapshotResizeParams.ResizeToHeight:
                                    # Crop the width - use floor to ensure if there's a remainder we float left.
                                    centerX = math.floor(float(pilImage.width) / 2.0)
                                    halfWidth = math.floor(float(snapshotResizeParams.Size) / 2.0)
                                    upper = 0
                                    lower = snapshotResizeParams.Size
                                    left = centerX - halfWidth
                                    right = (snapshotResizeParams.Size - halfWidth) + centerX
                                else:
                                    # Crop the height - use floor to ensure if there's a remainder we float left.
                                    centerY = math.floor(float(pilImage.height) / 2.0)
                                    halfHeight = math.floor(float(snapshotResizeParams.Size) / 2.0)
                                    upper = centerY - halfHeight
                                    lower = (snapshotResizeParams.Size - halfHeight) + centerY
                                    left = 0
                                    right = snapshotResizeParams.Size

                                # Sanity check bounds
                                if left < 0 or left > right or right > pilImage.width or upper > 0 or upper > lower or lower > pilImage.height:
                                    self.Logger.error("Failed to crop image. height: "+str(pilImage.height)+", width: "+str(pilImage.width)+", size: "+str(snapshotResizeParams.Size))
                                else:
                                    pilImage = pilImage.crop((left, upper, right, lower))
                                    didWork = True

                        #
                        # If we did some operation, save the image buffer back to a jpeg and overwrite the
                        # current snapshot buffer. If we didn't do work, keep the original, to preserve quality.
                        #
                        if didWork:
                            buffer = io.BytesIO()
                            pilImage.save(buffer, format="JPEG", quality=95)
                            snapshot = buffer.getvalue()
                            buffer.close()
                    else:
                        self.Logger.warn("Can't manipulate image because the Image rotation lib failed to import.")
                except Exception as ex:
                    # Note that in the case of an exception we don't overwrite the original snapshot buffer, so something can still be sent.
                    Sentry.ExceptionNoSend("Failed to manipulate image for notifications", ex)

            # Ensure in the end, the snapshot is a reasonable size.
            if len(snapshot) > NotificationsHandler.MaxSnapshotFileSizeBytes:
                self.Logger.error("Snapshot size if too large to send. Size: "+len(snapshot))
                return None

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
            Sentry.ExceptionNoSend("_updateToKnownDuration exception", e)


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
            ptrSec = self.GetPrintTimeRemainingEstimateInSeconds()
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
            Sentry.ExceptionNoSend("_getCurrentProgressFloat failed to compute progress.", e)

        # On failure, default to what OctoPrint has reported.
        return float(self.OctoPrintReportedProgressInt)


    # Sends the event
    # Returns True on success, otherwise False
    def _sendEvent(self, event, args = None, progressOverwriteFloat = None):
        # Push the work off to a thread so we don't hang OctoPrint's plugin callbacks.
        thread = threading.Thread(target=self._sendEventThreadWorker, args=(event, args, progressOverwriteFloat, ))
        thread.start()

        return True


    # Sends the event
    # Returns True on success, otherwise False
    def _sendEventThreadWorker(self, event, args=None, progressOverwriteFloat=None):
        try:
            # For notifications, if possible, we try to resize any image to be less than 720p.
            # This scale will preserve the aspect ratio and won't happen if the image is already less than 720p.
            # The scale might also fail if the image lib can't be loaded correctly.
            snapshotResizeParams = SnapshotResizeParams(1080, True, False, False)

            # Build the common even args.
            requestArgs = self.BuildCommonEventArgs(event, args, progressOverwriteFloat, snapshotResizeParams)

            # Handle the result indicating we don't have the proper var to send yet.
            if requestArgs is None:
                self.Logger.info("NotificationsHandler didn't send the "+str(event)+" event because we don't have the proper id and key yet.")
                return False

            # Break out the response
            args = requestArgs[0]
            files = requestArgs[1]

            # Setup the url
            eventApiUrl = self.ProtocolAndDomain + "/api/printernotifications/printerevent"

            # Attempt to send the notification twice. If the first time fails,
            # we will wait a bit and try again. It's really unlikely for a notification to fail, the biggest reason
            # would be if the server is updating, there can be a ~20 second window where the call might fail
            attempts = 0
            while attempts < 2:
                attempts += 1

                # Make the request.
                r = None
                try:
                    # Since we are sending the snapshot, we must send a multipart form.
                    # Thus we must use the data and files fields, the json field will not work.
                    r = requests.post(eventApiUrl, data=args, files=files, timeout=5*60)

                    # Check for success.
                    if r.status_code == 200:
                        self.Logger.info("NotificationsHandler successfully sent '"+event+"'")
                        return True

                except Exception as e:
                    # We must try catch the connection because sometimes it will throw for some connection issues, like DNS errors.
                    self.Logger.warn("Failed to send notification due to a connection error, trying again. "+str(e))

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
            Sentry.Exception("NotificationsHandler failed to send event code "+str(event), e)

        return False


    # Used by notifications and gadget to build a common event args.
    # Returns an array of [args, files] which are ready to be used in the request.
    # Returns None if the system isn't ready yet.
    def BuildCommonEventArgs(self, event, args=None, progressOverwriteFloat=None, snapshotResizeParams = None):

        # Ensure we have the required var set already. If not, get out of here.
        if self.PrinterId is None or self.OctoKey is None:
            return None

        # Default args
        if args is None:
            args = {}

        # Add the required vars
        args["PrinterId"] = self.PrinterId
        args["PrintId"] = self.PrintId
        args["OctoKey"] = self.OctoKey
        args["Event"] = event

        # Always add the file name
        args["FileName"] = str(self.CurrentFileName)

        # Always include the ETA, note this will be -1 if the time is unknown.
        timeRemainEstStr =  str(self.GetPrintTimeRemainingEstimateInSeconds())
        args["TimeRemainingSec"] = timeRemainEstStr

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
        snapshot = self.getSnapshot(snapshotResizeParams)
        if snapshot is not None:
            files['attachment'] = ("snapshot.jpg", snapshot)

        return [args, files]


    # This function will get the estimated time remaining for the current print.
    # It will first try to get a more accurate from plugins like PrintTimeGenius, otherwise it will fallback to the default OctoPrint total print time estimate.
    # Returns -1 if the estimate is unknown.
    def GetPrintTimeRemainingEstimateInSeconds(self):

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
            Sentry.Exception("Failed to find progress object in printer current data.", e)

        # If that fails, try to use the default OctoPrint estimate.
        try:
            jobData = self.OctoPrintPrinterObject.get_current_job()
            if "estimatedPrintTime" in jobData:

                # When the print is first starting and there is no known time, this can be none.
                # In that case, return -1, unknown.
                if jobData["estimatedPrintTime"] is None:
                    return -1

                printTimeEstSec = int(jobData["estimatedPrintTime"])
                # Compute how long this print has been running and subtract
                # Sanity check the duration isn't longer than the ETA.
                currentDurationSec = int(self._getCurrentDurationSecFloat())
                if currentDurationSec > printTimeEstSec:
                    return 0
                return printTimeEstSec - currentDurationSec
        except Exception as e:
            Sentry.Exception("Failed to find time estimate from OctoPrint. ", e)

        # We failed.
        return -1

    # Returns the current zoffset if known, otherwise -1.
    def GetCurrentZOffset(self):
        if self.OctoPrintPrinterObject is None:
            return -1

        # Try to get the current value from the data.
        try:
            # We have seen in client logs sometimes this value doesn't exist,
            # and sometime it does, but it's just None.
            currentData = self.OctoPrintPrinterObject.get_current_data()
            if "currentZ" in currentData and currentData["currentZ"] is not None:
                currentZ = float(currentData["currentZ"])
                return currentZ
        except Exception as e:
            Sentry.Exception("Failed to find current z offset.", e)

        # Failed to find it.
        return -1


    # Returns True if the printing timers (notifications and gadget) should be running.
    # False if the printer state is anything else, which means they should stop.
    def ShouldPrintingTimersBeRunning(self):
        # Get the current state
        # States can be found here:
        # https://docs.octoprint.org/en/master/modules/printer.html#octoprint.printer.PrinterInterface.get_state_id
        # Note! The docs seem to be missing some states at the moment, like STATE_RESUMING, which can be found in comm.py
        state = "UNKNOWN"
        if self.OctoPrintPrinterObject is None:
            self.Logger.warn("ShouldPrintingTimersBeRunning doesn't have a OctoPrint printer object.")
            state = "PRINTING"
        else:
            state = self.OctoPrintPrinterObject.get_state_id()

        # Return if the state is printing or not.
        if state == "PRINTING" or state == "RESUMING" or state == "FINISHING":
            return True

        self.Logger.warn("ShouldPrintingTimersBeRunning is not in a printing state: "+str(state))
        return False


    # If called while the print state is "Printing", returns True if the print is currently in the warm-up phase. Otherwise False
    def IsPrintWarmingUp(self):
        # Using the current state, if the print time is None or 0, the print hasn't started because the system is warming up..
        # Using the get_current_data in this way is the same way the /api/job uses it.
        if self.OctoPrintPrinterObject is None:
            self.Logger.warn("IsPrintWarmingUp doesn't have a OctoPrint printer object.")
            return False

        # Get the current data.
        currentData = self.OctoPrintPrinterObject.get_current_data()
        if currentData is not None:
            progress = currentData["progress"]
            if progress is not None:
                printTime = progress["printTime"]
                if printTime is None or int(printTime) == 0:
                    return True

        # We aren't warming up.
        return False


    # Starts a ping timer which is used to fire "every x minutes events".
    def SetupPingTimer(self, resetHoursReported):
        # First, stop any timer that's currently running.
        self.StopPingTimer()

        # Make sure the hours flag is cleared when we start a new timer.
        if resetHoursReported:
            self.PingTimerHoursReported = 0

        # Setup the new timer
        intervalSec = 60 * 60 # Fire every hour.
        timer = RepeatTimer(self.Logger, intervalSec, self.PingTimerCallback)
        timer.start()
        self.PingTimer = timer

        # Start Gadget From Watching
        self.Gadget.StartWatching()


    # Stops any running ping timer.
    def StopPingTimer(self):
        # Capture locally
        pingTimer = self.PingTimer
        self.PingTimer = None
        if pingTimer is not None:
            pingTimer.Stop()

        # Stop Gadget From Watching
        self.Gadget.StopWatching()


    # Fired when the ping timer fires.
    def PingTimerCallback(self):

        # Double check the state is still printing before we send the notification.
        # Even if the state is paused, we want to stop, since the resume command will restart the timers
        if self.ShouldPrintingTimersBeRunning() is False:
            self.Logger.info("Notification ping timer state doesn't seem to be printing, stopping timer.")
            self.StopPingTimer()
            return

        # Fire the event.
        self.OnPrintTimerProgress()


    # Only allows possibly spammy events to be sent every x minutes.
    # Returns true if the event can be sent, otherwise false.
    def _shouldSendSpammyEvent(self, eventName, minTimeBetweenMinutesFloat):
        with self.SpammyEventLock:

            # Check if the event has been added to the dict yet.
            if eventName not in self.SpammyEventTimeDict:
                # No event added yet, so add it now.
                self.SpammyEventTimeDict[eventName] = time.time()
                return True

            # Check how long it's been since the last notification was sent.
            # If it's less than 5 minutes, don't allow the event to send.
            deltaSec = time.time() - self.SpammyEventTimeDict[eventName]
            if deltaSec < (60.0 * minTimeBetweenMinutesFloat):
                return False

            # Allow the event to send and update the time we are allowing it.
            self.SpammyEventTimeDict[eventName] = time.time()
            return True
