from octoeverywhere.notificationshandler import NotificationsHandler

from .bambuclient import BambuClient
from .bambumodels import BambuState

# This class is responsible for listening to the mqtt messages to fire off notifications
# and to act as the printer state interface for Bambu printers.
class BambuStateTranslator:

    def __init__(self, logger) -> None:
        self.Logger = logger
        self.NotificationsHandler:NotificationsHandler = None
        self.HasPendingPrintStart = False
        self.HasPendingPrintPause = False
        self.HasPendingPrintResume = False
        self.HasPendingPrintFailed = False
        self.WasInRunningStateLastUpdate = False


    def SetNotificationHandler(self, notificationHandler:NotificationsHandler):
        self.NotificationsHandler = notificationHandler


    # Fired when any mqtt message comes in.
    # State will always be NOT NONE, since it's going to be created before this call.
    # The isFirstFullSyncResponse flag indicates if this is the first full state sync of a new connection.
    def OnMqttMessage(self, msg:dict, bambuState:BambuState, isFirstFullSyncResponse:bool):

        # First, if we have a new connection and we just synced, make sure the notification handler is in sync.
        if isFirstFullSyncResponse:
            # TODO - Ideally we pass the full print duration.
            self.NotificationsHandler.OnRestorePrintIfNeeded(bambuState.IsPrinting(False), bambuState.IsPaused(), bambuState.gcode_file, None)
            self.WasInRunningStateLastUpdate = False

        # Remember that each delta could have multiple pieces of needed information in them
        # And that we will only get the delta updates once!

        #
        # Next, handle any explicit onetime command actions.
        if "print" in msg:
            if "command" in msg["print"]:
                # Note about commands. Since these commands come before the push_status update
                # The State object MIGHT NOT BE UPDATED TO THE CORRECT STATE WHEN THEY FIRE.
                # For example, the gcode_state will be the last state when project_file fires, because it hasn't updated yet.
                # That can be really bad, like for ShouldPrintingTimersBeRunning, which will then return an incorrect value.
                # Thus we defer the command action until we see the next push_status come in.
                command = msg["print"]["command"]
                if command == "project_file":
                    self.HasPendingPrintStart = True
                elif command == "pause":
                    self.HasPendingPrintPause = True
                elif command == "resume":
                    self.HasPendingPrintResume = True
                elif command == "stop":
                    # This is a stop, I think only user generated?
                    # TODO - will this fire for other types or errors?
                    self.HasPendingPrintFailed = True

                # If we have a status update, we know our State should be current, so fire any deferred commands.
                elif command == "push_status":
                    if self.HasPendingPrintStart:
                        # We have to be really careful with this notification, because it kicks off a lot of things.
                        # We have to wait until the State is reporting RUNNING before we send it, to ensure things like
                        # ShouldPrintingTimersBeRunning are in a good state when all of the new things query them.
                        if bambuState.gcode_state is not None and bambuState.gcode_state == "RUNNING":
                            self.HasPendingPrintStart = False
                            self.BambuOnStart(bambuState)
                        else:
                            self.Logger.info("Deferring print start until the gcode_state is running...")

                    if self.HasPendingPrintPause:
                        self.HasPendingPrintPause = False
                        self.BambuOnPause(bambuState)

                    if self.HasPendingPrintResume:
                        self.HasPendingPrintResume = False
                        self.BambuOnResume(bambuState)

                    if self.HasPendingPrintFailed:
                        self.HasPendingPrintFailed = False
                        self.BambuOnFailed(bambuState)

            #
            # Next - Handle notifications that aren't based off one time events.
            #
            # These are harder to get right, because the printer will send full state objects sometimes when IDLE or PRINTING.
            # Thus if we respond to them, it might not be the correct time. For example, the full sync will always include mc_percent, but we
            # don't want to fire BambuOnPrintProgress if we aren't printing.
            #
            # We only want to consider firing these events if we know this isn't the first time sync from a new connection
            # and we are currently tacking a print.
            if not isFirstFullSyncResponse and self.NotificationsHandler.IsTrackingPrint():
                # Percentage progress update
                if "mc_percent" in msg["print"]:
                    self.BambuOnPrintProgress(bambuState)

            # Complete is hard, because there's no explicitly one time command for print success.
            # We also don't want to rely on IsTrackingPrint, because there's a small window where the state could be updated
            # and one of the notification threads could check ShouldPrintingTimersBeRunning, it be False, and stop them.
            # So, we keep track of if the state was RUNNING and then goes to FINISHED
            if bambuState.gcode_state is not None and bambuState.gcode_state == "FINISH":
                if self.WasInRunningStateLastUpdate:
                    # The last state was running and now it's FINISHED, the print is complete.
                    self.BambuOnComplete(bambuState)

            # Always update the flag.
            self.WasInRunningStateLastUpdate = bambuState.gcode_state is not None and bambuState.gcode_state == "RUNNING"


    def BambuOnStart(self, bambuState:BambuState):
        # We can only get the file name from Bambu.
        self.NotificationsHandler.OnStarted(self._GetFileNameOrNone(bambuState), 0, 0)


    def BambuOnComplete(self, bambuState:BambuState):
        # We can only get the file name from Bambu.
        self.NotificationsHandler.OnDone(self._GetFileNameOrNone(bambuState), None)


    def BambuOnPause(self, bambuState:BambuState):
        self.NotificationsHandler.OnPaused(self._GetFileNameOrNone(bambuState))


    def BambuOnResume(self, bambuState:BambuState):
        self.NotificationsHandler.OnResume(self._GetFileNameOrNone(bambuState))


    def BambuOnFailed(self, bambuState:BambuState):
        # TODO - Right now this is only called by what we think are use requested cancels.
        # How can we add this for print stopping errors as well?
        self.NotificationsHandler.OnFailed(self._GetFileNameOrNone(bambuState), None, "cancelled")


    def BambuOnPrintProgress(self, bambuState:BambuState):
        # We use the "moonrakerProgressFloat" because it's really means a progress that's
        # 100% correct and there's no estimations needed.
        self.NotificationsHandler.OnPrintProgress(None, float(bambuState.mc_percent))

    # TODO - Handlers
    #
    #     # Fired when OctoPrint or the printer hits an error.
    #     def OnError(self, error):

    #     # Fired when the waiting command is received from the printer.
    #     def OnWaiting(self):

    #     # Fired when we get a M600 command from the printer to change the filament
    #     def OnFilamentChange(self):

    #     # Fired when the printer needs user interaction to continue
    #     def OnUserInteractionNeeded(self):


    def _GetFileNameOrNone(self, bambuState:BambuState) -> str:
        return bambuState.gcode_file

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
            return (None, None)
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
