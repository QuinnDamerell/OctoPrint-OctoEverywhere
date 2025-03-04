from octoeverywhere.commandhandler import CommandResponse, CommandHandler

from .elegooclient import ElegooClient
from .elegoomodels import PrinterState
from .elegoofilemanager import ElegooFileManager

# This class implements the Platform Command Handler Interface
class ElegooCommandHandler:

    def __init__(self, logger) -> None:
        self.Logger = logger


    # !! Platform Command Handler Interface Function !!
    #
    # This must return the common "JobStatus" dict or None on failure.
    # The format of this must stay consistent with OctoPrint and the service.
    # Returning None send back the NoHostConnected error, assuming that the plugin isn't connected to the host or the host isn't
    # connected to the printer's firmware.
    #
    # See the JobStatusV2 class in the service for the object definition.
    #
    # Returning None will result in the "Printer not connected" state.
    # Or one of the CommandHandler.c_CommandError_... ints can be returned, which will be sent as the result.
    #
    def GetCurrentJobStatus(self):
        # Try to get the current state.
        printerState = ElegooClient.Get().GetState()

        # If the state is None, the printer isn't connected or the state isn't known yet.
        if printerState is None:
            # Check if we aren't connected because there are too many existing clients.
            if ElegooClient.Get().IsDisconnectDueToTooManyClients():
                return CommandHandler.c_CommandError_CantConnectTooManyClients
            # Returning None will be a "connection lost" state.
            return None

        # We have common logic to determine the state, so we can map it to the common state.
        (state, subState_CanBeNone) = printerState.GetCurrentStatus()
        errorStr_CanBeNone = None

        # Get current layer info
        # None = The platform doesn't provide it.
        # 0 = The platform provider it, but there's no info yet.
        # # = The values
        currentLayerInt = printerState.CurrentLayer if printerState.CurrentLayer is not None else 0
        totalLayersInt = printerState.TotalLayer if printerState.TotalLayer is not None else 0

        # Get the filename.
        # If the status is complete, use the most recent print name, if we know it, because the current print name will be None.
        if state == PrinterState.PRINT_STATUS_COMPLETE:
            fileName = printerState.GetMostRecentPrintInfo().GetFileNameWithNoExtension()
        else:
            fileName = printerState.GetFileNameWithNoExtension()
        if fileName is None:
            fileName = ""

        # Get the time so far and time remaining
        durationSec = printerState.DurationSec if printerState.DurationSec is not None else 0
        timeLeftSec = printerState.GetTimeRemainingSec()

        # Either of these can be set, or both or none.
        filamentUsedMm = 0
        filamentWeightMg = 0
        fileInfo = ElegooFileManager.Get().GetFileInfoFromState(printerState)
        if fileInfo is not None:
            filamentWeightMg = fileInfo.EstFilamentWeightMg

        # Get the progress, as a float 100.0-0.0
        # On Elegoo, the progress is always a whole number.
        progress = printerState.Progress if printerState.Progress is not None else 0.0

        # Get the current temps if possible.
        hotendActual = printerState.HotendActual if printerState.HotendActual is not None else 0.0
        hotendTarget = printerState.HotendTarget if printerState.HotendTarget is not None else 0.0
        bedActual = printerState.BedActual if printerState.BedActual is not None else 0.0
        bedTarget = printerState.BedTarget if printerState.BedTarget is not None else 0.0
        chamberActual = printerState.ChamberActual if printerState.ChamberActual is not None else 0.0
        chamberTarget = printerState.ChamberTarget if printerState.ChamberTarget is not None else 0.0

        # Build the object and return.
        return {
            "State": state,
            "SubState": subState_CanBeNone,
            "Error": errorStr_CanBeNone,
            "CurrentPrint":
            {
                "Progress" : progress,
                "DurationSec" : durationSec,
                # In some system buggy cases, the time left can be super high and won't fit into a int32, so we cap it.
                "TimeLeftSec" : min(timeLeftSec, 2147483600),
                "FileName" : fileName,
                "EstTotalFilUsedMm" : filamentUsedMm,
                "EstTotalFilWeightMg" : filamentWeightMg,
                "CurrentLayer": currentLayerInt,
                "TotalLayers": totalLayersInt,
                "Temps": {
                    "BedActual": bedActual,
                    "BedTarget": bedTarget,
                    "HotendActual": hotendActual,
                    "HotendTarget": hotendTarget,
                    "ChamberActual": chamberActual,
                    "ChamberTarget": chamberTarget,
                }
            }
        }


    # !! Platform Command Handler Interface Function !!
    # This must return the platform version as a string.
    def GetPlatformVersionStr(self):
        # TODO - Ideally we would get this from teh attributes object, but there's nothing in there
        # right now we know IDs the printer.
        return "Elegoo-Centauri"


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the pause and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecutePause(self, smartPause, suppressNotificationBool, disableHotendBool, disableBedBool, zLiftMm, retractFilamentMm, showSmartPausePopup) -> CommandResponse:
        result = ElegooClient.Get().SendRequest(129)
        if result.HasError():
            return CommandResponse.Error(400, "Failed to send command to printer.")
        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the resume and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteResume(self) -> CommandResponse:
        result = ElegooClient.Get().SendRequest(131)
        if result.HasError():
            return CommandResponse.Error(400, "Failed to send command to printer.")
        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the cancel and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteCancel(self) -> CommandResponse:
        result = ElegooClient.Get().SendRequest(130)
        if result.HasError():
            return CommandResponse.Error(400, "Failed to send command to printer.")
        return CommandResponse.Success(None)
