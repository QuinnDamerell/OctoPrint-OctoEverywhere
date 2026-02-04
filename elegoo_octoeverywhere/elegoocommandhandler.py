import logging
from typing import Any, Dict, Optional, Union, List

from octoeverywhere.commandhandler import CommandResponse, CommandHandler
from octoeverywhere.interfaces import IPlatformCommandHandler

from .elegooclient import ElegooClient
from .elegoomodels import PrinterState
from .elegoofilemanager import ElegooFileManager

# This class implements the Platform Command Handler Interface
class ElegooCommandHandler(IPlatformCommandHandler):

    c_ChamberLightName = "chamber"


    def __init__(self, logger:logging.Logger) -> None:
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
    def GetCurrentJobStatus(self) -> Union[int, None, Dict[str, Any]]:
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
        if timeLeftSec is not None:
            # In some system buggy cases, the time left can be super high and won't fit into a int32, so we cap it.
            timeLeftSec = min(timeLeftSec, 2147483600)

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

        # Get light status.
        # None if there are no lights, otherwise a list of lights and their status.
        lights: Optional[List[Dict[str, Any]]] = None
        if printerState.ChamberLightOn is not None:
            lights = [ {"Name": self.c_ChamberLightName, "On": printerState.ChamberLightOn} ]

        # Build the object and return.
        return {
            "State": state,
            "SubState": subState_CanBeNone,
            "Error": errorStr_CanBeNone,
            "Lights": lights,
            "CurrentPrint":
            {
                "Progress" : progress,
                "DurationSec" : durationSec,
                "TimeLeftSec" : timeLeftSec,
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
    def GetPlatformVersionStr(self) -> str:
        # TODO - Ideally we would get this from teh attributes object, but there's nothing in there
        # right now we know IDs the printer.
        return "Elegoo-Centauri"


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the pause and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecutePause(self, smartPause:bool, suppressNotificationBool:bool, disableHotendBool:bool, disableBedBool:bool, zLiftMm:int, retractFilamentMm:int, showSmartPausePopup:bool) -> CommandResponse:
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


    # !! Platform Command Handler Interface Function !!
    # Sets the light state for the specified light type.
    def ExecuteSetLight(self, lightName:str, on:bool) -> CommandResponse:
        if lightName != self.c_ChamberLightName:
            return CommandResponse.Error(400, f"Unknown light name: {lightName}")

        # Command 403 is the light control command
        # SecondLight is the chamber light, RgbLight is the LED strip (we keep it off)
        result = ElegooClient.Get().SendRequest(403, {"LightStatus": {"SecondLight": on, "RgbLight": [0, 0, 0]}})
        if result.HasError():
            return CommandResponse.Error(400, "Failed to send command to printer.")
        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # Moves the specified axis by the given distance in mm.
    def ExecuteMoveAxis(self, axis:str, distanceMm:float) -> CommandResponse:
        # Validate axis parameter
        axis_upper = axis.upper()
        if axis_upper not in ["X", "Y", "Z"]:
            self.Logger.error(f"ExecuteMoveAxis: Invalid axis '{axis}'")
            return CommandResponse.Error(400, "Invalid axis. Must be X, Y, or Z")
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")


    # !! Platform Command Handler Interface Function !!
    # Homes all axes.
    def ExecuteHome(self) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")


    # !! Platform Command Handler Interface Function !!
    # Extrudes or retracts filament for the specified extruder.
    def ExecuteExtrude(self, extruder:int, distanceMm:float) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")


    # !! Platform Command Handler Interface Function !!
    # Sets the temperature for bed, chamber, or tool.
    def ExecuteSetTemp(self, bedC:Optional[float], chamberC:Optional[float], toolC:Optional[float], toolNumber:Optional[int]) -> CommandResponse:
        return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "Not Supported")
