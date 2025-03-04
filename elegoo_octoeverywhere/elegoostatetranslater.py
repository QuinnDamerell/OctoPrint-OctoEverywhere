from octoeverywhere.notificationshandler import NotificationsHandler

from .elegooclient import ElegooClient
from .elegoomodels import PrinterState
from .elegoofilemanager import ElegooFileManager, FileInfo

# This class is responsible for listening to the mqtt messages to fire off notifications
# and to act as the printer state interface for Bambu printers.
class ElegooStateTranslator:

    def __init__(self, logger) -> None:
        self.Logger = logger
        self.NotificationsHandler:NotificationsHandler = None
        self.LastStatus:str = None
        self.IsWaitingOnPrintInfoToFirePrintStart = False


    def SetNotificationHandler(self, notificationHandler:NotificationsHandler):
        self.NotificationsHandler = notificationHandler


    # Fired when the websocket connection is lost to the printer.
    # This is used to let us know that we are in an unknown state again, until we can re-sync.
    # If wasFullyConnected was set, we know we were fully connected before the loss.
    def OnConnectionLost(self, wasFullyConnected:bool) -> None:
        # If we were fully connected and were printing or warming up, then report  the connection loss.
        # Otherwise, don't bother, since it might just be the user turning off the printer.
        if wasFullyConnected and (PrinterState.IsPrepareOrSlicingState(self.LastStatus) or PrinterState.IsPrintingState(self.LastStatus, False)):
            self.NotificationsHandler.OnError("Connection to printer lost during a print.")

        # Always reset our state.
        self.LastStatus = None
        self.IsWaitingOnPrintInfoToFirePrintStart = False


    # Fired when any mqtt message comes in.
    # State will always be NOT NONE, since it's going to be created before this call.
    # The isFirstFullSyncResponse flag indicates if this is the first full state sync of a new connection.
    def OnStatusUpdate(self, pState:PrinterState, isFirstFullSyncResponse:bool):

        # First, if we have a new connection and we just synced, make sure the notification handler is in sync.
        if isFirstFullSyncResponse:
            self.NotificationsHandler.OnRestorePrintIfNeeded(pState.IsPrinting(False), pState.IsPaused(), pState.GetPrintCookie())

        # Next, handle the state changes.
        (newStatus, _) = pState.GetCurrentStatus()

        # Based on the old state and the new state, we can determine what to do.
        if self.LastStatus != newStatus:
            # We know the state changed.
            self.Logger.debug(f"Elegoo printer state change: {self.LastStatus} -> {newStatus} - New Print Info Status: {pState.PrintInfoStatus} New Current Status: {pState.CurrentStatus}")
            if self.LastStatus is None:
                # If the last state is None, this is mostly likely the first time we've seen a state.
                # All we want to do here is update last state to the new state.
                pass
            # Check if we are now in a printing (or warming up) state we use the common function so the definition of "printing" stays common.
            elif pState.IsPrinting(False):
                # If we are now printing, but the last state was paused, we want to fire the resume event.
                if self.LastStatus == PrinterState.PRINT_STATUS_PAUSED:
                    self.OnResume(pState)
                else:
                    # We know the state changed and the state is now a printing state.
                    # If the last state was also a printing state, we don't want to fire this, since we already did.
                    if PrinterState.IsPrintingState(self.LastStatus, False) is False:
                        # Important! We can only fire the start event if we have the print info,
                        # and thus can generate the print cookie. Since this flag is checked right after this,
                        # we can always set it here, and then have one check.
                        self.IsWaitingOnPrintInfoToFirePrintStart = True
                        # But this is a good time to fire a file sync, since we should have the file on the system now, and the start
                        # event will try to pull info from it.
                        ElegooFileManager.Get().Sync()
            # Check for the paused state
            elif pState.IsPaused():
                # If the error is temporary, like a filament run out, the printer goes into a paused state with the printer_error set.
                self.OnPauseOrTempError(pState)
            # Check for the print ending in failure (like if the user stops it by command)
            elif newStatus == PrinterState.PRINT_STATUS_CANCELLED:
                self.OnCancelled(pState)
            # Check for a successful print ending.
            elif newStatus == PrinterState.PRINT_STATUS_COMPLETE:
                self.OnComplete(pState)

            # Always capture the new state.
            self.LastStatus = newStatus

        # Check to see if we have deferred the print start event because we were waiting on the print info.
        if self.IsWaitingOnPrintInfoToFirePrintStart:
            # If we were waiting on the print info to fire the start event, check if we have it now.
            # We need the print cookie before we can call start, but we also want to wait for other data like the est print time
            # For the notifications.
            printCookie = pState.GetPrintCookie()
            # Make sure we can get an estimate of the print time, for a few ticks after the print starts.
            # We get the time remaining, but not the duration right at the start.
            etaSec = pState.GetTimeRemainingSec()
            if printCookie is not None and len(printCookie) > 0 and etaSec is not None and etaSec > 0:
                # We have the print info, so we can fire the start event.
                self.IsWaitingOnPrintInfoToFirePrintStart = False
                # Ensure we are still in a printing state.
                if pState.IsPrinting(True):
                    self.OnStart(pState)

        #
        # Next - Handle the progress updates
        #
        if not isFirstFullSyncResponse and self.NotificationsHandler.IsTrackingPrint():
            if pState.Progress is not None:
                self.OnPrintProgress(pState)


    def OnStart(self, printerState:PrinterState):
        fileSizeKb = 0
        totalFilamentWeightMg = 0
        # Try to get the file info if we can - this will come from a in-memory cache if we have it.
        fileInfo:FileInfo = ElegooFileManager.Get().GetFileInfoFromState(printerState)
        if fileInfo is not None:
            if fileInfo.FileSizeKb is not None:
                fileSizeKb = fileInfo.FileSizeKb
            if fileInfo.EstFilamentWeightMg is not None:
                totalFilamentWeightMg = fileInfo.EstFilamentWeightMg

        # We must pass the unique cookie name for this print and any other details we can.
        self.NotificationsHandler.OnStarted(printerState.GetPrintCookie(), printerState.GetFileNameWithNoExtension(), fileSizeKBytes=fileSizeKb, totalFilamentWeightMg=totalFilamentWeightMg)


    def OnComplete(self, printerState:PrinterState):
        # Use the most recent print info, to ensure the data still exists. It gets cleared out of the print info sometimes
        mostRecentPrint = printerState.GetMostRecentPrintInfo()
        self.NotificationsHandler.OnDone(mostRecentPrint.GetFileNameWithNoExtension(), str(mostRecentPrint.DurationSec))


    def OnPauseOrTempError(self, printerState:PrinterState):
        # Use the most recent print info, to ensure the data still exists. It gets cleared out of the print info sometimes
        mostRecentPrint = printerState.GetMostRecentPrintInfo()
        self.NotificationsHandler.OnPaused(mostRecentPrint.GetFileNameWithNoExtension())
        # TODO - Right now we don't seem to have a way to get error states, they just always "pause"
        # For errors that are user fixable, like filament run outs, the printer will go into a paused state with
        # a printer error message. In this case we want to fire different things.
        # err = bambuState.GetPrinterError()
        # if err is None:
        #     # If error is none, this is a user pause
        #     self.NotificationsHandler.OnPaused(printerState.GetFileNameWithNoExtension())
        #     return
        # # Otherwise, try to match the error.
        # if err == BambuPrintErrors.FilamentRunOut:
        #     self.NotificationsHandler.OnFilamentChange()
        #     return

        # # Send a generic error.
        # self.NotificationsHandler.OnUserInteractionNeeded()


    def OnResume(self, printerState:PrinterState):
        # Use the most recent print info, to ensure the data still exists. It gets cleared out of the print info sometimes
        mostRecentPrint = printerState.GetMostRecentPrintInfo()
        self.NotificationsHandler.OnResume(mostRecentPrint.GetFileNameWithNoExtension())


    def OnCancelled(self, printerState:PrinterState):
        # Use the most recent print info, to ensure the data still exists. It gets cleared out of the print info sometimes
        mostRecentPrint = printerState.GetMostRecentPrintInfo()
        self.NotificationsHandler.OnFailed(mostRecentPrint.GetFileNameWithNoExtension(), None, "cancelled")


    def OnPrintProgress(self, printerState:PrinterState):
        # We use the "moonrakerProgressFloat" because it's really means a progress that's
        # 100% correct and there's no estimations needed.
        self.NotificationsHandler.OnPrintProgress(None, float(printerState.Progress))


    #
    #
    #  Printer State Interface
    #
    #

    # ! Interface Function ! The entire interface must change if the function is changed.
    # This function will get the estimated time remaining for the current print.
    # Returns -1 if the estimate is unknown.
    def GetPrintTimeRemainingEstimateInSeconds(self):
        # Get the current state.
        state = ElegooClient.Get().GetState()
        if state is None:
            return -1
        # Use the most recent print info, since this will get cleared before the final notifications fire.
        timeRemainingSec = state.GetMostRecentPrintInfo().GetTimeRemainingSec()
        if timeRemainingSec is None:
            return -1
        return timeRemainingSec


    # ! Interface Function ! The entire interface must change if the function is changed.
    # If the printer is warming up, this value would be -1. The First Layer Notification logic depends upon this or GetCurrentLayerInfo!
    # Returns the current zoffset if known, otherwise -1.
    def GetCurrentZOffset(self):
        # This is only used for the first layer logic, but only if GetCurrentLayerInfo fails.
        # Since our GetCurrentLayerInfo shouldn't always work, this shouldn't really matter.
        # We can't get this value, but since it doesn't really matter, we can estimate it.
        (currentLayer, _) = self.GetCurrentLayerInfo()
        if currentLayer is None:
            return -1

        # Since the standard layer height is 0.20mm, we just use that for a guess.
        return currentLayer * 0.2


    # ! Interface Function ! The entire interface must change if the function is changed.
    # If this platform DOESN'T support getting the layer info from the system, this returns (None, None)
    # If the platform does support it...
    #     If the current value is unknown, (0,0) is returned.
    #     If the values are known, (currentLayer(int), totalLayers(int)) is returned.
    #          Note that total layers will always be > 0, but current layer can be 0!
    def GetCurrentLayerInfo(self):
        state = ElegooClient.Get().GetState()
        if state is None:
            # If we dont have a state yet, return 0,0, which means we can get layer info but we don't know yet.
            return (0, 0)
        if state.IsPrepareOrSlicing():
            # The printer doesn't clear these values when a new print is starting and it's in a prepare or slicing state.
            # So if we are in that state, return 0,0, to represent we don't know the layer info yet.
            return (0, 0)
        # We can get accurate and 100% correct layers from Elegoo, awesome!
        # Use the most recent print info, since this will get cleared before the final notifications fire.
        currentLayer = state.GetMostRecentPrintInfo().CurrentLayer
        totalLayers = state.GetMostRecentPrintInfo().TotalLayer
        return (currentLayer, totalLayers)


    # ! Interface Function ! The entire interface must change if the function is changed.
    # Returns True if the printing timers (notifications and gadget) should be running, which is only the printing state. (not even paused)
    # False if the printer state is anything else, which means they should stop.
    def ShouldPrintingTimersBeRunning(self):
        state = ElegooClient.Get().GetState()
        if state is None:
            return False
        return state.IsPrinting(False)


    # ! Interface Function ! The entire interface must change if the function is changed.
    # If called while the print state is "Printing", returns True if the print is currently in the warm-up phase. Otherwise False
    def IsPrintWarmingUp(self):
        state = ElegooClient.Get().GetState()
        if state is None:
            return False

        # Check if the print timers should be running
        # This will weed out any gcode_states where we know we aren't running.
        # We have seen stg_cur not get reset in the past when the state transitions to an error.
        if not self.ShouldPrintingTimersBeRunning():
            return False

        return state.IsPrepareOrSlicing()


    # ! Interface Function ! The entire interface must change if the function is changed.
    # Returns the (hotend temp, bed temp) as a float in celsius if they are available, otherwise None.
    def GetTemps(self):
        state = ElegooClient.Get().GetState()
        if state is None:
            return (None, None)
        # These will be None if they are unknown.
        return (state.HotendActual, state.BedActual)
