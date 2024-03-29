import math
import time
import io
import threading
import random
import string
import logging

import requests

from .gadget import Gadget
from .sentry import Sentry
from .compat import Compat
from .finalsnap import FinalSnap
from .repeattimer import RepeatTimer
from .webcamhelper import WebcamHelper
from .printinfo import PrintInfoManager, PrintInfo
from .snapshotresizeparams import SnapshotResizeParams

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

    # The length of the random print id. This must be a large number, since it needs to be
    # globally unique. This value must stay in sync with the service.
    PrintIdLength = 60

    def __init__(self, logger:logging.Logger, printerStateInterface):
        self.Logger = logger
        # On init, set the key to empty.
        self.OctoKey = None
        self.PrinterId = None
        self.ProtocolAndDomain = "https://printer-events-v1-oeapi.octoeverywhere.com"
        self.PrinterStateInterface = printerStateInterface
        self.ProgressTimer = None
        self.FirstLayerTimer = None
        self.FinalSnapObj:FinalSnap = None
        self.Gadget = Gadget(logger, self, self.PrinterStateInterface)

        # Define all the vars we use locally in the notification handler
        self.PrintCookie = ""
        self.FallbackProgressInt = 0
        self.MoonrakerReportedProgressFloat_CanBeNone = None
        self.PingTimerHoursReported = 0
        self.HasSendFirstLayerDoneMessage = False
        self.HasSendThirdLayerDoneMessage = False
        self.zOffsetLowestSeenMM = 1337.0
        self.zOffsetNotAtLowestCount = 0
        self.zOffsetHasSeenPositiveExtrude = False
        self.zOffsetTrackingStartTimeSec = 0.0
        self.FirstLayerDoneSince = 0.0
        self.ThirdLayerDoneSince = 0.0
        self.ProgressCompletionReported = []
        self.RestorePrintProgressPercentage = False

        self.SpammyEventTimeDict = {}
        self.SpammyEventLock = threading.Lock()

        # Call this to init all of the vars to their default values.
        # But we pass none, so we don't delete any print infos that might be on disk we will try to recover when connected to the server.
        self._RecoverOrRestForNewPrint(None)


    # Called to start a new print.
    # On class init, this can be called with printCookie=None, but after that we should always have a print cookie.
    def _RecoverOrRestForNewPrint(self, printCookie:str):
        # We always reset these local notification handler values for new prints or recovered prints.
        self.FallbackProgressInt = 0
        self.MoonrakerReportedProgressFloat_CanBeNone = None
        self.PingTimerHoursReported = 0
        self.HasSendFirstLayerDoneMessage = False
        self.HasSendThirdLayerDoneMessage = False
        self.FirstLayerDoneSince = 0.0
        self.ThirdLayerDoneSince = 0.0
        # The following values are used to figure out when the first layer is done.
        self.zOffsetLowestSeenMM = 1337.0
        self.zOffsetNotAtLowestCount = 0
        self.zOffsetTrackingStartTimeSec = 0.0
        self.zOffsetHasSeenPositiveExtrude = False
        self.RestorePrintProgressPercentage = False

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

        # Reset our anti spam times.
        self._clearSpammyEventContexts()

        # Ensure there's no final snap running.
        self._getFinalSnapSnapshotAndStop()

        # The print cookie can only be None on class init.
        # We pass None so we don't call the PrintInfoManager, which might create a new print info on disk.
        # There might be a print info on disk we want to restore when the host connects to the printer.
        if printCookie is None:
            return

        # Always set the new print cookie
        self.PrintCookie = printCookie

        # See if we have an existing print that matches this cookie on disk.
        if PrintInfoManager.Get().GetPrintInfo(printCookie) is not None:
            self.Logger.info(f"Print Manager recovered a print info from disk matching cookie: {printCookie}")
            return

        # If we didn't find an existing print info, we need to make a new one.

        # Each time a print starts, we generate a fixed length random id to identify it.
        # This id is used to globally identify the print for the user, so it needs to have high entropy.
        printId = ''.join(random.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=NotificationsHandler.PrintIdLength))

        # Always make a new print info for this new print.
        # This is where we will store all of the vars for this print, and it's also written to disk if we need to recover the info.
        PrintInfoManager.Get().CreateNewPrintInfo(printCookie, printId)


    def SetPrinterId(self, printerId):
        self.PrinterId = printerId


    def SetOctoKey(self, octoKey):
        self.OctoKey = octoKey


    def SetServerProtocolAndDomain(self, protocolAndDomain):
        self.Logger.info("NotificationsHandler default domain and protocol set to: "+protocolAndDomain)
        self.ProtocolAndDomain = protocolAndDomain


    def SetGadgetServerProtocolAndDomain(self, protocolAndDomain):
        self.Gadget.SetServerProtocolAndDomain(protocolAndDomain)


    # If there is an valid print cookie and we can get the info, this returns it.
    # Returns None if there's no current print info.
    def GetPrintInfo(self) -> PrintInfo:
        if self.PrintCookie is None or len(self.PrintCookie) == 0:
            return None
        return PrintInfoManager.Get().GetPrintInfo(self.PrintCookie)


    def GetPrintId(self) -> str:
        pi = self.GetPrintInfo()
        if pi is None:
            return None
        return pi.GetPrintId()


    def GetPrintStartTimeSec(self) -> float:
        pi = self.GetPrintInfo()
        if pi is None:
            return 0.0
        return pi.GetLocalPrintStartTimeSec()


    def GetGadget(self):
        return self.Gadget


    def ReportPositiveExtrudeCommandSent(self):
        self.zOffsetHasSeenPositiveExtrude = True
        fsLocal = self.FinalSnapObj
        if fsLocal is not None:
            fsLocal.ReportPositiveExtrudeCommandSent()


    # Hints at if we are tracking a print or not.
    def IsTrackingPrint(self) -> bool:
        return self._IsPingTimerRunning()


    # A special case used by moonraker and bambu to restore the state of an ongoing print that we don't know of.
    # What we want to do is check moonraker or bambu's current state and our current state, to see if there's anything that needs to be synced.
    # Remember that we might be syncing because our service restarted during a print, or moonraker restarted, so we might already have
    # the correct context.
    #
    # Most importantly, we want to make sure the ping timer and thus Gadget get restored to the correct states.
    #
    def OnRestorePrintIfNeeded(self, isPrinting:bool, isPaused:bool, printCookie_CanBeNoneIfNoPrintIsActive:str = None):

        # First, check if there's no active print currently.
        if (isPrinting is False and isPaused is False) or printCookie_CanBeNoneIfNoPrintIsActive is None:
            # There's no print running.
            if self._IsPingTimerRunning():
                self.Logger.info("Restore client sync state: There's no print running but the ping timers are running. Stopping them now.")
                self.StopTimers()
                return
            else:
                self.Logger.info("Restore client sync state: There's no print and none of the timers are running.")
                return


        # Next, we know there's an active print, so check if we already are tracking it's print cookie.
        # This is a scenario like the plugin didn't crash, but it lost the connection to the server, but it's back now.
        printCookie = printCookie_CanBeNoneIfNoPrintIsActive
        if self.PrintCookie is not None and self.PrintCookie == printCookie:
            # We have a print cookie and the cookie matches.
            # This means we just need to make sure the timer states are correct.
            if isPrinting:
                # There is an active print. Check our state.
                if self._IsPingTimerRunning():
                    self.Logger.info("Restore client sync state: We have the print cookie, detected an active print, and our timers are already running. So there's nothing to do.")
                    return
                else:
                    self.Logger.info("Restore client sync state: We have a print cookie, detected and active print, but the timers aren't running, so we will start them now.")
                    self.StartPrintTimers(False, None)
                    return
                    # We need to restore so we start the print timers.
            elif isPaused:
                # The print is paused, check our state.
                if self._IsPingTimerRunning():
                    self.Logger.info("Restore client sync state: We have a print cookie, detected a paused print, but our ping timers ARE RUNNING. Stopping them now.")
                    self.StopTimers()
                    return
                else:
                    self.Logger.info("Restore client sync state: We have a print cookie, detected a paused print, and timers aren't running. So there's nothing to do.")
                    return

        # If we are here, there's a print running or paused and we aren't tracking it currently.
        # This scenario probably is due to the plugin restarting.

        # This function will take the print cookie and (hopefully) recover an existing print info.
        # If it can't recover an existing print info, it will create a new one.
        self._RecoverOrRestForNewPrint(printCookie)

        # Disable the first layer complete logic, since we don't know what the base z-axis was
        self.HasSendFirstLayerDoneMessage = True
        self.HasSendThirdLayerDoneMessage = True

        # Set this flag so the first progress update will restore the progress to the current progress without
        # firing all of the progress points we missed.
        self.RestorePrintProgressPercentage = True

        # Make sure the timers are set correctly
        if isPrinting:
            # If we can get a duration, set the hours reported to that.
            hoursReportedInt = 0
            durationSec = self.GetCurrentDurationSecFloat()
            if durationSec > 0:
                # Convert seconds to hours, floor the value, make it an int.
                hoursReportedInt = int(math.floor(durationSec / 60.0 / 60.0))

            # Setup the timers, with hours reported, to make sure that the ping timer and Gadget are running.
            self.Logger.info("Restore client sync state: Restoring printing timer with existing duration of "+str(durationSec))
            self.StartPrintTimers(False, hoursReportedInt)
        else:
            # On paused, make sure they are stopped.
            self.StopTimers()
            self.Logger.info("Restore client sync state: Restoring into a paused print state.")



    # Only used for testing.
    def OnTest(self):
        if self._shouldIgnoreEvent():
            return
        self._sendEvent("test")


    # Only used for testing.
    def OnGadgetWarn(self):
        if self._shouldIgnoreEvent():
            return
        self._sendEvent("gadget-warning")


    # Only used for testing.
    def OnGadgetPaused(self):
        if self._shouldIgnoreEvent():
            return
        self._sendEvent("gadget-paused")


    # Fired when a print starts.
    # The print cookie is required. It's a per platform print unique string that's used to identify the print.
    # The string can be anything, but it must be a valid file name.
    # The string should also be unique between prints, but common for the same print. This allows us to pull up the print info for the same print if we crash or
    # or lose the printer connection.
    def OnStarted(self, printCookie:str, fileName:str = None, fileSizeKBytes:int = 0, totalFilamentUsageMm:int = 0):
        # Validate
        if self._shouldIgnoreEvent(fileName):
            return
        if printCookie is None or len(printCookie) == 0:
            raise Exception("NotificationHandler OnStarted called with no print cookie.")

        # Since know we are starting a new print, we want to clear any existing print infos.
        # This is important for Moonraker, because there's no way to differentiate between prints beyond the filename.
        # So we have to use the file name, so we can still restore will work.
        # But in the case of printing the same print back to back, the print cookie will be the same.
        PrintInfoManager.Get().ClearAllPrintInfos()

        # This will reset the class for this new print and create the print info.
        self._RecoverOrRestForNewPrint(printCookie)

        # Update vars
        self._updateCurrentFileName(fileName)

        pi = self.GetPrintInfo()
        if pi is None:
            self.Logger.error("No print info returned after a new print started, this should not be possible.")
            return
        pi.SetFileSizeKBytes(fileSizeKBytes)
        pi.SetEstFilamentUsageMm(totalFilamentUsageMm)

        self.StartPrintTimers(True, None)
        self._sendEvent("started")
        self.Logger.info(f"New print started; PrintId: {str(self.GetPrintId())} file:{str(pi.GetFileName())} size:{str(pi.GetFileSizeKBytes())} filament:{str(pi.GetEstFilamentUsageMm())}")


    # Fired when a print fails
    def OnFailed(self, fileName:str, durationSecStr:str = None, reason:str = None):
        if self._shouldIgnoreEvent(fileName):
            return
        self._updateCurrentFileName(fileName)
        self._updateToKnownDuration(durationSecStr)
        self.StopTimers()
        self._sendEvent("failed", { "Reason": reason})


    # Fired when a print done
    # For moonraker, these vars aren't known, so they are None
    def OnDone(self, fileName:str = None, durationSecStr:str = None):
        if self._shouldIgnoreEvent(fileName):
            return
        self._updateCurrentFileName(fileName)
        self._updateToKnownDuration(durationSecStr)
        self.StopTimers()
        self._sendEvent("done", useFinalSnapSnapshot=True)


    # Fired when a print is paused
    def OnPaused(self, fileName:str = None):
        if self._shouldIgnoreEvent(fileName):
            return

        # Always update the file name.
        self._updateCurrentFileName(fileName)

        # See if there is a pause notification suppression set. If this is not null and it was recent enough
        # suppress the notification from firing.
        # If there is no suppression, or the suppression was older than 30 seconds, fire the notification.
        if Compat.HasSmartPauseInterface():
            lastSuppressTimeSec = Compat.GetSmartPauseInterface().GetAndResetLastPauseNotificationSuppressionTimeSec()
            if lastSuppressTimeSec is None or time.time() - lastSuppressTimeSec > 20.0:
                self._sendEvent("paused")
            else:
                self.Logger.info("Not firing the pause notification due to a Smart Pause suppression.")
        else:
            self._sendEvent("paused")

        # Stop the ping timer, so we don't report progress while we are paused.
        self.StopTimers()


    # Fired when a print is resumed
    def OnResume(self, fileName:str = None):
        Sentry.Breadcrumb("OnResume called.", {"filename":fileName})
        if self._shouldIgnoreEvent(fileName):
            return
        self._updateCurrentFileName(fileName)
        self._sendEvent("resume")

        # Clear any spammy event contexts we have, assuming the user cleared any issues before resume.
        self._clearSpammyEventContexts()

        # Start the ping timer, to ensure it's running now.
        self.StartPrintTimers(False, None)


    # Fired when OctoPrint or the printer hits an error.
    def OnError(self, error):
        if self._shouldIgnoreEvent():
            return

        self.StopTimers()

        # This might be spammy from OctoPrint, so limit how often we bug the user with them.
        if self._shouldSendSpammyEvent("on-error"+str(error), 30.0) is False:
            return

        self._sendEvent("error", {"Error": error })


    # Fired when the waiting command is received from the printer.
    def OnWaiting(self):
        if self._shouldIgnoreEvent():
            return
        # Make this the same as the paused command.
        self.OnPaused()


    # Fired when we get a M600 command from the printer to change the filament
    def OnFilamentChange(self):
        if self._shouldIgnoreEvent():
            return
        # This event might fire over and over or might be paired with a filament change event.
        # In any case, we only want to fire it every so often.
        # It's important to use the same key to make sure we de-dup the possible OnUserInteractionNeeded that might fire second.
        if self._shouldSendSpammyEvent("user-interaction-needed", 5.0) is False:
            return

        # Otherwise, send it.
        self._sendEvent("filamentchange")


    # Fired when the printer needs user interaction to continue
    def OnUserInteractionNeeded(self):
        if self._shouldIgnoreEvent():
            return
        # This event might fire over and over or might be paired with a filament change event.
        # In any case, we only want to fire it every so often.
        # It's important to use the same key to make sure we de-dup the possible OnUserInteractionNeeded that might fire second.
        if self._shouldSendSpammyEvent("user-interaction-needed", 5.0) is False:
            return

        # Otherwise, send it.
        self._sendEvent("userinteractionneeded")


    # Fired when a print is making progress.
    def OnPrintProgress(self, octoPrintProgressInt, moonrakerProgressFloat):
        if self._shouldIgnoreEvent():
            return

        # Always set the fallback progress, which will be used if something better can be found.
        # For moonraker, make sure to set the reported float. See _getCurrentProgressFloat about why.
        #
        # Note that in moonraker this is called very frequently, so this logic must be fast!
        #
        if octoPrintProgressInt is not None:
            self.FallbackProgressInt = octoPrintProgressInt
        elif moonrakerProgressFloat is not None:
            self.FallbackProgressInt = int(moonrakerProgressFloat)
            self.MoonrakerReportedProgressFloat_CanBeNone = moonrakerProgressFloat
        else:
            self.Logger.error("OnPrintProgress called with no args!")
            return

        # Get the computed print progress value. (see _getCurrentProgressFloat about why)
        computedProgressFloat = self._getCurrentProgressFloat()

        # If we are near the end of the print, start the final snap image capture system, to ensure we get a good "done" image.
        # This is a tricky number to set. For long prints, 1% can be very long, where as for quick prints we might not even see
        # all of the % updates.
        # First of all, don't bother unless the % complete is > 90% (this also guards from divide by 0)
        if computedProgressFloat > 90.0 and self.FinalSnapObj is None:
            currentTimeSec = self.GetCurrentDurationSecFloat()
            estTimeRemainingSec = (self.GetCurrentDurationSecFloat() * 100.0) / computedProgressFloat
            estTimeUntilCompleteSec = estTimeRemainingSec - currentTimeSec
            # If we guess the print will be done in less than one minute, then start the final snap system.
            if estTimeUntilCompleteSec < 60.0:
                if self.FinalSnapObj is None:
                    self.FinalSnapObj = FinalSnap(self.Logger, self)

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

        # The first progress update after a restore won't fire any notifications. We use this update
        # to clear out all progress points under the current progress, so we don't fire them.
        # Do this before we check if we had something to send, so we always do this on the first tick
        # after a restore.
        if self.RestorePrintProgressPercentage:
            self.RestorePrintProgressPercentage = False
            return

        # Return if there is nothing to do.
        if progressToSendFloat < 0.1:
            return

        # It's important we send the "snapped" progress here (rounded to the tens place) because the service depends on it
        # to filter out % increments the user didn't want to get notifications for.
        self._sendEvent("progress", None, progressToSendFloat)


    # Fired every hour while a print is running
    def OnPrintTimerProgress(self):
        if self._shouldIgnoreEvent():
            return
        # This event is fired by our internal timer only while prints are running.
        # It will only fire every hour.

        # We send a duration, but that duration is controlled by OctoPrint and can be changed.
        # Since we allow the user to pick "every x hours" to be notified, it's easier for the server to
        # keep track if we just send an int as well.
        # Since this fires once an hour, every time it fires just add one.
        self.PingTimerHoursReported += 1

        self._sendEvent("timerprogress", { "HoursCount": str(self.PingTimerHoursReported) })


    #
    # Note this values are important!
    # The cost of getting the current z offset is decently high, and thus we can't check it too often.
    # However, the our "first layer complete" logic works by watching the zoffset to detect when it has moved above
    # the "lowest ever seen.". It will only fire the notification after we have seen something above "the lowest ever seen"
    # so many times. If we don't poll frequently enough, the notification will be delayed and we might miss some of the layer heights changes.
    #
    # For now, we settled on checking every 2 seconds and a FirstLayerCountAboveLowestBeforeNotify value of 5, meaning we need to constantly see
    # a layer height above the lowest for 10 seconds before we will fire the notification.
    FirstLayerTimerIntervalSec = 2.0
    FirstLayerCountAboveLowestBeforeNotify = 5


    # Called by our firstLayerTimer at a fixed interval defined by FirstLayerTimerIntervalSec.
    # Returns True if the timer should continue, otherwise False
    def _OnFirstLayerWatchTimer(self):

        # If we have already sent the first layer done message there's nothing to do.
        # Remember! This timer will be started mid print for a Moonraker state restore or on resume, so we need to make
        # Sure we handle that. Right now we use HasSendFirstLayerDoneMessage to ensure we don't run the logic anymore until the print restarts.
        if self.HasSendFirstLayerDoneMessage and self.HasSendThirdLayerDoneMessage:
            return False

        # Ensure we are in state where we should fire this (printing)
        if self.PrinterStateInterface.ShouldPrintingTimersBeRunning() is False:
            self.HasSendFirstLayerDoneMessage = True
            self.HasSendThirdLayerDoneMessage = True
            return False

        # We have two ways of computing the layer heights.
        # 1) On some platforms (Moonraker) we can query the actual layer from the system, so we don't have to guess.
        # 2) If the platform doesn't support getting the actual layer height, we can try to figure it out with z offsets.
        currentLayer, totalLayers = self.PrinterStateInterface.GetCurrentLayerInfo()
        if currentLayer is not None and totalLayers is not None:
            # We have layer info from the system, use this to handle the events.

            # If we are over the first layer and haven't sent the notification, start the timer.
            # We use this time to make sure that the print is still in the first layer complete state and it's not a zhop or something.
            if currentLayer > 1 and self.HasSendFirstLayerDoneMessage is False:
                if self.FirstLayerDoneSince < 0.1:
                    self.Logger.debug("First Layer Logic - Starting delay timer.")
                    self.FirstLayerDoneSince = time.time()
                elif time.time() - self.FirstLayerDoneSince < 10.0:
                    self.Logger.debug("First Layer Logic - Waiting delay time to expire.")
                else:
                    self.Logger.debug("First Layer Logic - Done.")
                    self.HasSendFirstLayerDoneMessage = True
                    self._sendEvent("firstlayerdone")

            # If we fall out of the delay timer wait, reset the timer.
            if currentLayer <= 1 and self.FirstLayerDoneSince > 0.0:
                self.Logger.debug("First Layer Logic - Reset.")
                self.FirstLayerDoneSince = 0.0

            # If we are past the 3rd, layer, do the same.
            if currentLayer > 3 and self.HasSendThirdLayerDoneMessage is False:
                if self.ThirdLayerDoneSince < 0.1:
                    self.Logger.debug( "Third Layer Logic - Starting delay timer.")
                    self.ThirdLayerDoneSince = time.time()
                elif time.time() - self.ThirdLayerDoneSince < 10.0:
                    self.Logger.debug( "Third Layer Logic - Waiting delay time to expire.")
                else:
                    self.Logger.debug( "Third Layer Logic - Done.")
                    self.HasSendThirdLayerDoneMessage = True
                    self._sendEvent("thirdlayerdone")

            if currentLayer <= 3 and self.ThirdLayerDoneSince > 0.0:
                self.Logger.debug("Third Layer Logic - Reset.")
                self.ThirdLayerDoneSince = 0.0

            # If we return true, the time will continue, otherwise it will stop.
            isDone = self.HasSendFirstLayerDoneMessage is True and self.HasSendThirdLayerDoneMessage is True
            return isDone is False

        #
        # We don't have a system provided layer info, use the second option with the z-offset.

        # Get the current zoffset value.
        currentZOffsetMM = self.PrinterStateInterface.GetCurrentZOffset()

        # Make sure we know it.
        # If not, return True so we keep checking.
        if currentZOffsetMM == -1:
            self.Logger.debug("First Layer Logic - Waiting for positive z axis measurement.")
            return True

        # If the value is 0.0, the printer is still warming up or getting ready. We can't print at 0.0, because that's the nozzle touching the plate.
        # Ignore this value, so we don't lock to it as the "lowest we have seen."
        # I'm not sure if OctoPrint does this, but moonraker will report the value of 0.0
        # In this case, return True so we keep checking.
        if currentZOffsetMM < 0.0001:
            self.Logger.debug("First Layer Logic - Waiting for >0 z axis measurement.")
            return True

        # Wait to do any zAxis tracking until after we see a positive extrude.
        # This prevents us from tracking the zAxis during some pre-print gcode marcos, like bed level probing and such.
        # The only thing this doesn't really exclude is a purge line.
        if self.zOffsetHasSeenPositiveExtrude is False:
            self.Logger.debug("First Layer Logic - Waiting for the first extrude.")
            return True

        # Finally, before tracking the zAxisOffset, we need to wait for a possible purge line.
        # Hopefully with the blocking logic above, like the warm up check and waiting to see an extrude, we will ignore any z-axis values
        # from most pre-print macros.
        # The final thing we need to exclude is a purge line, since the purge line's layer height might be less than the first layer height used
        # for the actual print. That means our system will lock onto the purge line's layer height, instead of the print's first layer height.
        #
        # We choose 10 seconds as the time to wait. The trade off is that we want to wait longer than most purge line extrudes, but we don't want to miss the first layer
        # being printed. Since we only start our time after the first extrude, this gives us ~10 seconds after the first extrude for the purge line to be done.
        if self.zOffsetTrackingStartTimeSec < 0.1:
            self.Logger.debug("First Layer Logic - Starting delay timer.")
            self.zOffsetTrackingStartTimeSec = time.time()
        if time.time() - self.zOffsetTrackingStartTimeSec < 20.0:
            self.Logger.debug("First Layer Logic - Waiting delay time to expire.")
            return True

        # The trick here is how we do figure out when the first layer is done with out knowing the print layer height
        # or how the gcode is written to do zhops.
        #
        # Our current solution is to keep track of the lowest zvalue we have seen for this print.
        # Every time we don't see the zvalue be the lowest, we increment a counter. After n number of reports above the lowest value, we
        # consider the first layer done because we haven't seen the printer return to the first layer height.
        #
        # Typically, the flow looks something like... 0.4 -> 0.2 -> 0.4 -> 0.2 -> 0.4 -> 0.5 -> 0.7 -> 0.5 -> 0.7...
        # Where the layer hight is 0.2 (because it's the lowest first value) and the zhops are 0.4 or more.
        #
        # This system is pumped every FirstLayerTimerIntervalSec and the z offset is checked.

        # First, do the logic for the first layer
        if self.HasSendFirstLayerDoneMessage is False:
            # Since this is a float, avoid ==
            if currentZOffsetMM > self.zOffsetLowestSeenMM - 0.01 and currentZOffsetMM < self.zOffsetLowestSeenMM + 0.01:
                # The zOffset is the same as the previously seen.
                self.zOffsetNotAtLowestCount = 0
                self.Logger.debug("First Layer Logic - currentOffset: %.4f; lowestSeen: %.4f; notAtLowestCount: %d - Same as the 'lowest ever seen', resetting the counter.", currentZOffsetMM, self.zOffsetLowestSeenMM, self.zOffsetNotAtLowestCount)
            elif currentZOffsetMM < self.zOffsetLowestSeenMM:
                # We found a new low, record it.
                self.zOffsetLowestSeenMM = currentZOffsetMM
                self.zOffsetNotAtLowestCount = 0
                self.Logger.debug("First Layer Logic - currentOffset: %.4f; lowestSeen: %.4f; notAtLowestCount: %d - New lowest zoffset ever seen.", currentZOffsetMM, self.zOffsetLowestSeenMM, self.zOffsetNotAtLowestCount)
            else:
                # The zOffset is higher than the lowest we have seen.
                self.zOffsetNotAtLowestCount += 1
                self.Logger.debug("First Layer Logic - currentOffset: %.4f; lowestSeen: %.4f; notAtLowestCount: %d - Offset is higher than lowest seen, adding to the count.", currentZOffsetMM, self.zOffsetLowestSeenMM, self.zOffsetNotAtLowestCount)

            # Check if we have been above the min layer height for FirstLayerCountAboveLowestBeforeNotify of times in a row.
            # If not, keep waiting, if so, fire the notification.
            if self.zOffsetNotAtLowestCount < NotificationsHandler.FirstLayerCountAboveLowestBeforeNotify:
                # Not done yet, return True to keep checking.
                return True

            # Set the flag and reset the count, since it will now be used for the third lowest layer notification.
            self.HasSendFirstLayerDoneMessage = True
            self.zOffsetNotAtLowestCount = 0

            # Send the message.
            self._sendEvent("firstlayerdone", {"ZOffsetMM" : str(currentZOffsetMM) })

        # Next, after we know the first layer is done, do the logic for the third layer notification.
        elif self.HasSendThirdLayerDoneMessage is False:
            # Sanity check we have a valid value for self.zOffsetLowestSeenMM, from the first layer notification.
            if self.zOffsetLowestSeenMM > 50.0:
                self.Logger.warn("First layer notification has sent but third layer hans't but the zOffsetLowestSeenMM value is really high, seems like it's unset. Value: "+str(self.zOffsetLowestSeenMM))
                self.HasSendThirdLayerDoneMessage = True
                return False
            if self.zOffsetLowestSeenMM <= 0.0001:
                self.Logger.warn("zOffsetLowestSeenMM is too low for third layer notification. Value: "+str(self.zOffsetLowestSeenMM))
                self.HasSendThirdLayerDoneMessage = True
                return False

            # To compute the third layer, we assume the lowest z offset height is the layer height.
            # Since we don't allow a value of 0, this is reasonable.
            thirdLayerHeight = self.zOffsetLowestSeenMM * 3
            if currentZOffsetMM > thirdLayerHeight + 0.001:
                # The current offset is larger than the third layer height, count it.
                self.zOffsetNotAtLowestCount += 1
                self.Logger.debug("Third Layer Logic - currentOffset: %.4f; thirdLayerHeight: %.4f; notAtLowestCount: %d - Offset is higher than the third layer height, adding to the count.", currentZOffsetMM, thirdLayerHeight, self.zOffsetNotAtLowestCount)

            else:
                # The current layer height is equal to or at the third layer height, reset the count
                self.zOffsetNotAtLowestCount = 0
                self.Logger.debug("Third Layer Logic - currentOffset: %.4f; thirdLayerHeight: %.4f; notAtLowestCount: %d - Offset less than or equal to the third layer height, resetting the count.", currentZOffsetMM, thirdLayerHeight, self.zOffsetNotAtLowestCount)

            # Check if we have been above the third layer height for FirstLayerCountAboveLowestBeforeNotify of times in a row.
            # If not, keep waiting, if so, fire the notification.
            if self.zOffsetNotAtLowestCount < NotificationsHandler.FirstLayerCountAboveLowestBeforeNotify:
                # Not done yet, return True to keep checking.
                return True

            # Set the flag to indicate we sent the notification
            self.HasSendThirdLayerDoneMessage = True

            # Send the notification.
            self._sendEvent("thirdlayerdone", {"ZOffsetMM" : str(currentZOffsetMM) })

        # If we have fired both, we are done.
        # If we are not done, return True, so we keep going.
        # Otherwise, return false, to stop the timer, because we are done.
        isDone = self.HasSendFirstLayerDoneMessage is True and self.HasSendThirdLayerDoneMessage is True
        return isDone is False


    # If possible, gets a snapshot from the snapshot URL configured in OctoPrint.
    # SnapshotResizeParams can be passed BUT MIGHT BE IGNORED if the PIL lib can't be loaded.
    # SnapshotResizeParams will also be ignored if the current image is smaller than the requested size.
    # If this fails for any reason, None is returned.
    def GetNotificationSnapshot(self, snapshotResizeParams = None):

        # If no snapshot resize param was specified, use the default for notifications.
        if snapshotResizeParams is None:
            # For notifications, if possible, we try to resize any image to be less than 720p.
            # This scale will preserve the aspect ratio and won't happen if the image is already less than 720p.
            # The scale might also fail if the image lib can't be loaded correctly.
            snapshotResizeParams = SnapshotResizeParams(1080, True, False, False)

        try:

            # Use the snapshot helper to get the snapshot. This will handle advance logic like relative and absolute URLs
            # as well as getting a snapshot directly from a mjpeg stream if there's no snapshot URL.
            octoHttpResponse = WebcamHelper.Get().GetSnapshot()

            # Check for a valid response.
            if octoHttpResponse is None or octoHttpResponse.StatusCode != 200:
                return None

            # GetSnapshot will always return the full result already read.
            snapshot = octoHttpResponse.FullBodyBuffer
            if snapshot is None:
                self.Logger.error("WebcamHelper.Get().GetSnapshot() returned a web response but no FullBodyBuffer")
                return None

            # Ensure the snapshot is a reasonable size. If it's not, try to resize it if there's not another resize planned.
            # If this fails, the size will be checked again later and the image will be thrown out.
            if len(snapshot) > NotificationsHandler.MaxSnapshotFileSizeBytes:
                if snapshotResizeParams is None:
                    # Try to limit the size to be 1080 tall.
                    snapshotResizeParams = SnapshotResizeParams(1080, True, False, False)

            # Manipulate the image if needed.
            flipH = WebcamHelper.Get().GetWebcamFlipH()
            flipV = WebcamHelper.Get().GetWebcamFlipV()
            rotation = WebcamHelper.Get().GetWebcamRotation()
            if rotation != 0 or flipH or flipV or snapshotResizeParams is not None:
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

                        # In pillow ~9.1.0 these constants moved.
                        # pylint: disable=no-member
                        OE_FLIP_LEFT_RIGHT = 0
                        OE_FLIP_TOP_BOTTOM = 0
                        try:
                            OE_FLIP_LEFT_RIGHT = Image.FLIP_LEFT_RIGHT
                            OE_FLIP_TOP_BOTTOM = Image.FLIP_TOP_BOTTOM
                        except Exception:
                            OE_FLIP_LEFT_RIGHT = Image.Transpose.FLIP_LEFT_RIGHT
                            OE_FLIP_TOP_BOTTOM = Image.Transpose.FLIP_TOP_BOTTOM
                        # pylint: enable=no-member

                        # Update the image
                        # Note the order of the flips and the rotates are important!
                        # If they are reordered, when multiple are applied the result will not be correct.
                        didWork = False
                        pilImage = Image.open(io.BytesIO(snapshot))
                        if flipH:
                            pilImage = pilImage.transpose(OE_FLIP_LEFT_RIGHT)
                            didWork = True
                        if flipV:
                            pilImage = pilImage.transpose(OE_FLIP_TOP_BOTTOM)
                            didWork = True
                        if rotation != 0:
                            # Our rotation is clockwise while PIL is counter clockwise.
                            # Subtract from 360 to get the opposite rotation.
                            rotation = 360 - rotation
                            pilImage = pilImage.rotate(rotation)
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
                except Exception as e:
                    # Note that in the case of an exception we don't overwrite the original snapshot buffer, so something can still be sent.
                    if "name 'Image' is not defined" in str(e):
                        self.Logger.info("Can't manipulate image because the Image rotation lib failed to import.")
                    if "cannot identify image file" in str(e):
                        self.Logger.info("Can't manipulate image because the Image lib can't figure out the image type.")
                    else:
                        Sentry.Exception("Failed to manipulate image for notifications", e)

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


    # Assuming the current time is set at the start of the printer correctly.
    # This is also a live duration, if this is called once the print is over it will keep incrementing.
    def GetCurrentDurationSecFloat(self):
        pi = self.GetPrintInfo()
        if pi is None:
            return 0.0
        return float(time.time() - pi.GetLocalPrintStartTimeSec())


    # If we get a known duration from the platform, be sure to update it.
    def _updateToKnownDuration(self, durationSecStr):
        # If the string is empty or None, return.
        # This is important for Moonraker
        if durationSecStr is None or len(durationSecStr) == 0:
            return

        # If we fail this logic don't kill the event.
        try:
            pi = self.GetPrintInfo()
            if pi is None:
                return
            pi.SetLocalPrintStartTimeSec(time.time() - float(durationSecStr))
        except Exception as e:
            Sentry.ExceptionNoSend("_updateToKnownDuration exception", e)


    # Updates the current file name, if there is a new name to set.
    def _updateCurrentFileName(self, fileName:str):
        # The None check is important for Moonraker
        if fileName is None or len(fileName) == 0:
            return
        pi = PrintInfoManager.Get().GetPrintInfo(self.PrintCookie)
        if pi is None:
            return
        pi.SetFileName(fileName)


    # Stops the final snap object if it's running and returns
    # the final image if possible.
    def _getFinalSnapSnapshotAndStop(self):
        # Capture the class member locally.
        localFs = self.FinalSnapObj
        self.FinalSnapObj = None

        # If there is one, stop it and return it's snapshot.
        if localFs is not None:
            return localFs.GetFinalSnapAndStop()
        return None


    # Returns the current print progress as a float.
    def _getCurrentProgressFloat(self):
        # Special platform logic here!
        # Since this function is used to get the progress for all platforms, we need to do things a bit differently.

        # For moonraker, the progress is reported via websocket messages super frequently. There's no better way to compute the
        # progress (unlike OctoPrint) so we just want to use it, if we have it.
        #
        # We also don't want to constantly call GetPrintTimeRemainingEstimateInSeconds on moonraker, since it will result in a lot of RPC calls.
        if self.MoonrakerReportedProgressFloat_CanBeNone is not None:
            return self.MoonrakerReportedProgressFloat_CanBeNone

        # Then for OctoPrint, we will do the following logic to get a better progress.
        # OctoPrint updates us with a progress int, but it turns out that's not the same progress as shown in the web UI.
        # The web UI computes the progress % based on the total print time and ETA. Thus for our notifications to have accurate %s that match
        # the web UIs, we will also try to do the same.
        try:
            # Try to get the print time remaining, which will use smart ETA plugins if possible.
            ptrSec = self.PrinterStateInterface.GetPrintTimeRemainingEstimateInSeconds()
            # If we can't get the ETA, default to OctoPrint's value.
            if ptrSec == -1:
                return float(self.FallbackProgressInt)

            # Compute the total print time (estimated) and the time thus far
            currentDurationSecFloat = self.GetCurrentDurationSecFloat()
            totalPrintTimeSec = currentDurationSecFloat + ptrSec

            # Sanity check for / 0
            if totalPrintTimeSec == 0:
                return float(self.FallbackProgressInt)

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
        return float(self.FallbackProgressInt)


    # Sends the event
    # Returns True on success, otherwise False
    def _sendEvent(self, event:str, args = None, progressOverwriteFloat = None, useFinalSnapSnapshot = False):
        # Push the work off to a thread so we don't hang OctoPrint's plugin callbacks.
        thread = threading.Thread(target=self._sendEventThreadWorker, args=(event, args, progressOverwriteFloat, useFinalSnapSnapshot, ))
        thread.start()

        return True


    # Sends the event
    # Returns True on success, otherwise False
    def _sendEventThreadWorker(self, event:str, args = None, progressOverwriteFloat = None, useFinalSnapSnapshot = False):
        try:
            # Build the common even args.
            requestArgs = self.BuildCommonEventArgs(event, args, progressOverwriteFloat=progressOverwriteFloat, useFinalSnapSnapshot=useFinalSnapSnapshot)

            # Handle the result indicating we don't have the proper var to send yet.
            if requestArgs is None:
                self.Logger.info("NotificationsHandler didn't send the "+str(event)+" event because we don't have the proper id and key yet.")
                return False

            # Break out the response
            args = requestArgs[0]
            files = requestArgs[1]

            # Setup the url
            eventApiUrl = self.ProtocolAndDomain + "/api/printernotifications/printerevent"

            # Use fairly aggressive retry logic on notifications if they fail to send.
            # This is important because they power some of the other features of OctoEverywhere now, so having them as accurate as possible is ideal.
            attempts = 0
            while attempts < 6:
                attempts += 1
                statusCode = 0
                try:
                    # Since we are sending the snapshot, we must send a multipart form.
                    # Thus we must use the data and files fields, the json field will not work.
                    r = requests.post(eventApiUrl, data=args, files=files, timeout=5*60)

                    # Capture the status code.
                    statusCode = r.status_code

                    # Check for success.
                    if statusCode == 200:
                        self.Logger.info("NotificationsHandler successfully sent '"+event+"'")
                        return True

                except Exception as e:
                    # We must try catch the connection because sometimes it will throw for some connection issues, like DNS errors, server not connectable, etc.
                    self.Logger.warn("Failed to send notification due to a connection error. "+str(e))

                # On failure, log the issue.
                self.Logger.warn(f"NotificationsHandler failed to send event {str(event)}. Code:{str(statusCode)}. Waiting and then trying again.")

                # If the error is in the 400 class, don't retry since these are all indications there's something
                # wrong with the request, which won't change. But we don't want to include anything above or below that.
                if statusCode > 399 and statusCode < 500:
                    return False

                # We have quite a few reties and back off a decent amount. As said above, we want these to be reliable as possible, even if they are late.
                # We want the first few retires to be quick, so the notifications happens ASAP. This will help in teh case where the server is updating, it should be
                # back withing 2-4 seconds, but 20 is a good time to wait.
                # If it's still failing, we want to allow the system some time to do a do a fail over or something, thus we give the retry timer more time.
                if attempts < 3: # Attempt 1 and 2 will wait 20 seconds.
                    time.sleep(20)
                else: # Attempt 3, 4, 5 will wait longer.
                    time.sleep(60 * attempts)

            # We never sent it successfully.
            self.Logger.error("NotificationsHandler failed to send event "+str(event)+" due to a network issues after many retries.")

        except Exception as e:
            Sentry.Exception("NotificationsHandler failed to send event code "+str(event), e)

        return False


    # Used by notifications and gadget to build a common event args.
    # Returns an array of [args, files] which are ready to be used in the request.
    # The args and files will always contain any information that can be gathered at the time of the call.
    # Returns None if we don't have the printer id or octokey yet.
    def BuildCommonEventArgs(self, event:str, args=None, progressOverwriteFloat=None, snapshotResizeParams = None, useFinalSnapSnapshot = False):

        # Ensure we have the required var set already. If not, get out of here.
        if self.PrinterId is None or self.OctoKey is None:
            return None

        # Default args
        if args is None:
            args = {}

        # Define files so we can return an empty dict on any failures.
        files = {}

        # Get the print info if there is a current print.
        # Remember that some notifications will fire when there's no print running, like if OctoPrint loses it's connection to the printer while idle.
        pi = PrintInfoManager.Get().GetPrintInfo(self.PrintCookie)
        if pi is not None:
            args["PrintId"] = pi.GetPrintId()
            args["FileName"] = str(pi.GetFileName())
            args["FileSizeKb"] = str(pi.GetFileSizeKBytes())
            args["FilamentUsageMm"] = str(pi.GetEstFilamentUsageMm())
        else:
            Sentry.LogError("NotificationsHandler failed to get the print info for the current print.", {"Cookie": self.PrintCookie, "Event": event})

        # Add the required vars
        args["PrinterId"] = self.PrinterId
        args["OctoKey"] = self.OctoKey
        args["Event"] = event

        # Always include the ETA, note this will be -1 if the time is unknown.
        timeRemainEstStr =  str(self.PrinterStateInterface.GetPrintTimeRemainingEstimateInSeconds())
        args["TimeRemainingSec"] = timeRemainEstStr

        # Always include the layer height, if it can be gotten from the platform.
        currentLayer, totalLayers = self.PrinterStateInterface.GetCurrentLayerInfo()
        if currentLayer is not None and totalLayers is not None:
            # Note both of these values can be 0 if the layer counts aren't known yet!
            args["CurrentLayer"] = str(currentLayer)
            args["TotalLayers"] = str(totalLayers)

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
        args["DurationSec"] = str(self.GetCurrentDurationSecFloat())

        # Also always include a snapshot if we can get one.
        snapshot = None

        # If we are requested to use a final snapshot, try to use the snapshot from it.
        # This should only be requested for the "done" notification.
        if useFinalSnapSnapshot:
            snapshot = self._getFinalSnapSnapshotAndStop()

        # If we don't have a snapshot, try to get one now.
        if snapshot is None:
            snapshot = self.GetNotificationSnapshot(snapshotResizeParams)

        # If we got one, save it to the request.
        if snapshot is not None:
            files['attachment'] = ("snapshot.jpg", snapshot)

        return [args, files]


    # Stops any running timer, be it the progress timer, the Gadget timer, or something else.
    def StopTimers(self):
        # Capture locally & Stop
        progressTimer = self.ProgressTimer
        self.ProgressTimer = None
        if progressTimer is not None:
            progressTimer.Stop()

        # Stop the first layer timer.
        self.StopFirstLayerTimer()

        # Stop Gadget From Watching
        self.Gadget.StopWatching()


    def StopFirstLayerTimer(self):
        # Capture locally & Stop
        firstLayerTimer = self.FirstLayerTimer
        self.FirstLayerTimer = None
        if firstLayerTimer is not None:
            firstLayerTimer.Stop()


    # Starts all print timers, including the progress time, Gadget, and the first layer watcher.
    def StartPrintTimers(self, resetHoursReported:bool, restoreActionSetHoursReported:int = None):
        # First, stop any timer that's currently running.
        self.StopTimers()

        # Make sure the hours flag is cleared when we start a new timer.
        if resetHoursReported:
            self.PingTimerHoursReported = 0

        # If this is a restore, set the value
        if restoreActionSetHoursReported is not None:
            self.PingTimerHoursReported = int(restoreActionSetHoursReported)

        # Setup the progress timer
        intervalSec = 60 * 60 # Fire every hour.
        timer = RepeatTimer(self.Logger, intervalSec, self.ProgressTimerCallback)
        timer.start()
        self.ProgressTimer = timer

        # Setup the first layer watcher - we use a different timer since this timer is really short lived and it fires much more often.
        intervalSec = NotificationsHandler.FirstLayerTimerIntervalSec
        firstLayerTimer = RepeatTimer(self.Logger, intervalSec, self.FirstLayerTimerCallback)
        firstLayerTimer.start()
        self.FirstLayerTimer = firstLayerTimer

        # Start Gadget From Watching
        self.Gadget.StartWatching()


    # Let's the caller know if the ping timer is running, and thus we are tracking a print.
    def _IsPingTimerRunning(self):
        return self.ProgressTimer is not None


    # Fired when the ping timer fires.
    def ProgressTimerCallback(self):

        # Double check the state is still printing before we send the notification.
        # Even if the state is paused, we want to stop, since the resume command will restart the timers
        if self.PrinterStateInterface.ShouldPrintingTimersBeRunning() is False:
            self.Logger.info("Notification progress timer state doesn't seem to be printing, stopping timer.")
            self.StopTimers()
            return

        # Fire the event.
        self.OnPrintTimerProgress()


    # Fired when the ping timer fires.
    def FirstLayerTimerCallback(self):

        # Don't check the printer state, we will allow the function to handle all of that
        # If the function returns True, the timer should continue. If it returns false, the time should be stopped.
        if self._OnFirstLayerWatchTimer() is True:
            return

        # Stop the timer.
        self.Logger.info("First layer timer is done. Stopping.")
        self.StopFirstLayerTimer()


    # Only allows possibly spammy events to be sent every x minutes.
    # Returns true if the event can be sent, otherwise false.
    def _shouldSendSpammyEvent(self, eventName, minTimeBetweenMinutesFloat):
        with self.SpammyEventLock:

            # Check if the event has been added to the dict yet.
            if eventName not in self.SpammyEventTimeDict:
                # No event added yet, so add it now.
                self.SpammyEventTimeDict[eventName] = SpammyEventContext()
                return True

            # Check how long it's been since the last notification was sent.
            # If it's less than 5 minutes, don't allow the event to send.
            if self.SpammyEventTimeDict[eventName].ShouldSendEvent(minTimeBetweenMinutesFloat) is False:
                return False

            # Report we are sending an event and return true.
            self.SpammyEventTimeDict[eventName].ReportEventSent()
            return True


    def _clearSpammyEventContexts(self):
        with self.SpammyEventLock:
            self.SpammyEventTimeDict = {}


    # Very rarely, we want to ignore some notifications based on different metrics.
    # A filename can be passed to check, if not, the current file name will be used.
    def _shouldIgnoreEvent(self, fileName:str = None) -> bool:
        # Check if there was a file name passed, if so use it.
        # If not, fall back to the current file name.
        # If there is neither, dont ignore.
        if fileName is None or len(fileName) == 0:
            pi = self.GetPrintInfo()
            if pi is None:
                return False
            fileName = pi.GetFileName()
            if fileName is None or len(fileName) == 0:
                return False
        # One case we want to ignore is when the continuous print plugin uses it's "placeholder" .gcode files.
        # These files are used between prints to hold the printer before a new print starts.
        # The events are listed here, and the file name will be 'continuousprint_finish.gcode' for example.
        # https://github.com/smartin015/continuousprint/blob/bfb2c13da2ebbe0bfbfaa90f62a91db332c43b1b/continuousprint/data/__init__.py#L62
        fileNameLower = fileName.lower()
        if fileNameLower.startswith("continuousprint_"):
            self.Logger.info("Ignoring notification because it's a continuous print place holder file. "+str(fileName))
            return True
        return False


class SpammyEventContext:

    def __init__(self):
        self.ConcurrentCount = 0
        self.LastSentTimeSec = 0
        self.ReportEventSent()


    def ReportEventSent(self):
        self.ConcurrentCount += 1
        self.LastSentTimeSec = time.time()


    def ShouldSendEvent(self, baseTimeIntervalMinutesFloat):
        # Figure out what the delay multiplier should be.
        delayMultiplier = 1

        # For the first 3 events, don't back off.
        if self.ConcurrentCount > 3:
            delayMultiplier = self.ConcurrentCount

        # Sanity check.
        delayMultiplier = max(delayMultiplier, 1)

        # Ensure we don't try to delay too long.
        # Most of these timers are base intervals of 5 minutes, so 288 is one every 24 hours.
        delayMultiplier = min(delayMultiplier, 288)

        timeSinceLastSendSec = time.time() - self.LastSentTimeSec
        sendIntervalSec = baseTimeIntervalMinutesFloat * 60.0
        if timeSinceLastSendSec > sendIntervalSec * delayMultiplier:
            return True
        return False
