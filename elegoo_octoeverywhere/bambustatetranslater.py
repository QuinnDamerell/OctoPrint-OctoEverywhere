import time

from octoeverywhere.notificationshandler import NotificationsHandler
from octoeverywhere.printinfo import PrintInfoManager

from .bambuclient import BambuClient
from .bambumodels import BambuState, BambuPrintErrors

# This class is responsible for listening to the mqtt messages to fire off notifications
# and to act as the printer state interface for Bambu printers.
class BambuStateTranslator:

    def __init__(self, logger) -> None:
        self.Logger = logger
        self.NotificationsHandler:NotificationsHandler = None
        self.LastState:str = None


    def SetNotificationHandler(self, notificationHandler:NotificationsHandler):
        self.NotificationsHandler = notificationHandler


    # Called by the client just before it tires to make a new connection.
    # This is used to let us know that we are in an unknown state again, until we can re-sync.
    def ResetForNewConnection(self):
        # Reset the last state to indicate that we don't know what it is.
        self.LastState = None


    # Fired when any mqtt message comes in.
    # State will always be NOT NONE, since it's going to be created before this call.
    # The isFirstFullSyncResponse flag indicates if this is the first full state sync of a new connection.
    def OnMqttMessage(self, msg:dict, bambuState:BambuState, isFirstFullSyncResponse:bool):

        # First, if we have a new connection and we just synced, make sure the notification handler is in sync.
        if isFirstFullSyncResponse:
            self.NotificationsHandler.OnRestorePrintIfNeeded(bambuState.IsPrinting(False), bambuState.IsPaused(), bambuState.GetPrintCookie())

        # Bambu does send some commands when actions happen, but they don't always get sent for all state changes.
        # For example, if a user issues a pause command, we see the command. But if the print goes into an error an pauses, we don't get a pause command.
        # Thus, we have to rely on keeping track of that state and knowing when it changes.
        # Note we check state for all messages, not just push_status, but it doesn't matter because it will only change on push_status anyways.
        # Here's a list of all states: https://github.com/greghesp/ha-bambulab/blob/e72e343acd3279c9bccba510f94bf0e291fe5aaa/custom_components/bambu_lab/pybambu/const.py#L83C1-L83C21
        if self.LastState != bambuState.gcode_state:
            # We know the state changed.
            self.Logger.debug(f"Bambu state change: {self.LastState} -> {bambuState.gcode_state}")
            if self.LastState is None:
                # If the last state is None, this is mostly likely the first time we've seen a state.
                # All we want to do here is update last state to the new state.
                pass
            # Check if we are now in a printing state we use the common function so the definition of "printing" stays common.
            elif bambuState.IsPrinting(False):
                if self.LastState == "PAUSE":
                    self.BambuOnResume(bambuState)
                else:
                    # We know the state changed and the state is now a printing state.
                    # If the last state was also a printing state, we don't want to fire this, since we already did.
                    if BambuState.IsPrintingState(self.LastState, False) is False:
                        self.BambuOnStart(bambuState)
            # Check for the paused state
            elif bambuState.IsPaused():
                # If the error is temporary, like a filament run out, the printer goes into a paused state
                # with the printer_error set.
                self.BambuOnPauseOrTempError(bambuState)
            # Check for the print ending in failure (like if the user stops it by command)
            elif bambuState.gcode_state == "FAILED":
                self.BambuOnFailed(bambuState)
            # Check for a successful print ending.
            elif bambuState.gcode_state == "FINISH":
                self.BambuOnComplete(bambuState)

            # Always capture the new state.
            self.LastState = bambuState.gcode_state

        #
        # Next - Handle the progress update.
        #
        # These are harder to get right, because the printer will send full state objects sometimes when IDLE or PRINTING.
        # Thus if we respond to them, it might not be the correct time. For example, the full sync will always include mc_percent, but we
        # don't want to fire BambuOnPrintProgress if we aren't printing.
        #
        # We only want to consider firing these events if we know this isn't the first time sync from a new connection
        # and we are currently tacking a print.
        if not isFirstFullSyncResponse and self.NotificationsHandler.IsTrackingPrint():
            # Percentage progress update
            printMsg = msg.get("print", None)
            if printMsg is not None and "mc_percent" in printMsg:
                # On the X1, the progress doesn't get reset from the last print when the printer switches into prepare or slicing for the next print.
                # So we will not send any progress updates in these states, until the state is "RUNNING" and the progress should reset to 0.
                if bambuState.IsPrepareOrSlicing() is False:
                    self.BambuOnPrintProgress(bambuState)

        # Since bambu doesn't tell us a print duration, we need to figure out when it ends ourselves.
        # This is different from the state changes above, because if we are ever not printing for any reason,
        # We want to finalize any current print.
        if bambuState.IsPrinting(True) is False:
            # See if there's a print info for the last print.
            pi = PrintInfoManager.Get().GetPrintInfo(bambuState.GetPrintCookie())
            if pi is not None:
                # Check if the print info has a final duration set yet or not.
                if pi.GetFinalPrintDurationSec() is None:
                    # We know we aren't printing, so regardless of the non-printing state, set the final duration.
                    pi.SetFinalPrintDurationSec(int(time.time()-pi.GetLocalPrintStartTimeSec()))


    def BambuOnStart(self, bambuState:BambuState):
        # We must pass the unique cookie name for this print and any other details we can.
        self.NotificationsHandler.OnStarted(bambuState.GetPrintCookie(), bambuState.GetFileNameWithNoExtension())


    def BambuOnComplete(self, bambuState:BambuState):
        # We can only get the file name from Bambu.
        self.NotificationsHandler.OnDone(bambuState.GetFileNameWithNoExtension(), None)


    def BambuOnPauseOrTempError(self, bambuState:BambuState):
        # For errors that are user fixable, like filament run outs, the printer will go into a paused state with
        # a printer error message. In this case we want to fire different things.
        err = bambuState.GetPrinterError()
        if err is None:
            # If error is none, this is a user pause
            self.NotificationsHandler.OnPaused(bambuState.GetFileNameWithNoExtension())
            return
        # Otherwise, try to match the error.
        if err == BambuPrintErrors.FilamentRunOut:
            self.NotificationsHandler.OnFilamentChange()
            return

        # Send a generic error.
        self.NotificationsHandler.OnUserInteractionNeeded()


    def BambuOnResume(self, bambuState:BambuState):
        self.NotificationsHandler.OnResume(bambuState.GetFileNameWithNoExtension())


    def BambuOnFailed(self, bambuState:BambuState):
        # TODO - Right now this is only called by what we think are use requested cancels.
        # How can we add this for print stopping errors as well?
        self.NotificationsHandler.OnFailed(bambuState.GetFileNameWithNoExtension(), None, "cancelled")


    def BambuOnPrintProgress(self, bambuState:BambuState):
        # We use the "moonrakerProgressFloat" because it's really means a progress that's
        # 100% correct and there's no estimations needed.
        self.NotificationsHandler.OnPrintProgress(None, float(bambuState.mc_percent))

    # TODO - Handlers
    #     # Fired when OctoPrint or the printer hits an error.
    #     def OnError(self, error):


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
        state = BambuClient.Get().GetState()
        if state is None:
            return -1
        # We use our special logic function that will return a almost perfect seconds based countdown
        # instead of the just minutes based countdown from bambu.
        timeRemainingSec = state.GetContinuousTimeRemainingSec()
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
        state = BambuClient.Get().GetState()
        if state is None:
            # If we dont have a state yet, return 0,0, which means we can get layer info but we don't know yet.
            return (0, 0)
        if state.IsPrepareOrSlicing():
            # The printer doesn't clear these values when a new print is starting and it's in a prepare or slicing state.
            # So if we are in that state, return 0,0, to represent we don't know the layer info yet.
            return (0, 0)
        # We can get accurate and 100% correct layers from Bambu, awesome!
        currentLayer = None
        totalLayers = None
        if state.layer_num is not None:
            currentLayer = int(state.layer_num)
        if state.total_layer_num is not None:
            totalLayers = int(state.total_layer_num)
        return (currentLayer, totalLayers)


    # ! Interface Function ! The entire interface must change if the function is changed.
    # Returns True if the printing timers (notifications and gadget) should be running, which is only the printing state. (not even paused)
    # False if the printer state is anything else, which means they should stop.
    def ShouldPrintingTimersBeRunning(self):
        state = BambuClient.Get().GetState()
        if state is None:
            return False

        gcodeState = state.gcode_state
        if gcodeState is None:
            return False

        # See the logic in GetCurrentJobStatus for a full description
        # Since we don't know 100% of the states, we will fail open.
        # Here's a possible list: https://github.com/greghesp/ha-bambulab/blob/e72e343acd3279c9bccba510f94bf0e291fe5aaa/custom_components/bambu_lab/pybambu/const.py#L83C1-L83C21
        if gcodeState == "IDLE" or gcodeState == "FINISH" or gcodeState == "FAILED":
            self.Logger.warn("ShouldPrintingTimersBeRunning is not in a printing state: "+str(gcodeState))
            return False
        return True


    # ! Interface Function ! The entire interface must change if the function is changed.
    # If called while the print state is "Printing", returns True if the print is currently in the warm-up phase. Otherwise False
    def IsPrintWarmingUp(self):
        state = BambuClient.Get().GetState()
        if state is None:
            return False

        # Check if the print timers should be running
        # This will weed out any gcode_states where we know we aren't running.
        # We have seen stg_cur not get reset in the past when the state transitions to an error.
        if not self.ShouldPrintingTimersBeRunning():
            return False

        gcodeState = state.gcode_state
        if gcodeState is not None:
            # See the logic in GetCurrentJobStatus for a full description
            # Here's a possible list: https://github.com/greghesp/ha-bambulab/blob/e72e343acd3279c9bccba510f94bf0e291fe5aaa/custom_components/bambu_lab/pybambu/const.py#L83C1-L83C21
            if gcodeState == "PREPARE" or gcodeState == "SLICING":
                return True

        if state.stg_cur is None:
            return False
        # See the logic in GetCurrentJobStatus for a full description
        # Here's a full list: https://github.com/davglass/bambu-cli/blob/398c24057c71fc6bcc5dbd818bdcacc20833f61c/lib/const.js#L104
        if state.stg_cur == 1 or state.stg_cur == 2 or state.stg_cur == 7 or state.stg_cur == 9 or state.stg_cur == 11 or state.stg_cur == 14:
            return True
        return False


    # ! Interface Function ! The entire interface must change if the function is changed.
    # Returns the current hotend temp and bed temp as a float in celsius if they are available, otherwise None.
    def GetTemps(self):
        state = BambuClient.Get().GetState()
        if state is None:
            return (None, None)

        # These will be None if they are unknown.
        return (state.nozzle_temper, state.bed_temper)
