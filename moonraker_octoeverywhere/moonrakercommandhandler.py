import json
import logging
from typing import Any, Dict, List, Optional, Union

from octoeverywhere.commandhandler import CommandHandler, CommandResponse
from octoeverywhere.interfaces import IPlatformCommandHandler

from .moonrakerclient import MoonrakerClient
from .smartpause import SmartPause
from .filemetadatacache import FileMetadataCache
from .jsonrpcresponse import JsonRpcResponse
from .lightmanager import LightManager

# This class implements the Platform Command Handler Interface
class MoonrakerCommandHandler(IPlatformCommandHandler):

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

        # Build the query objects dict, including light objects if available
        query_objects: Dict[str, None] = {
            "print_stats": None,    # Needed for many things, including GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsAndVirtualSdCardResult
            "gcode_move": None,     # Needed for GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsAndVirtualSdCardResult to get the current speed
            "virtual_sdcard": None, # Needed for many things, including GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsAndVirtualSdCardResult
            "extruder": None,       # Needed for temps
            "heater_bed": None,     # Needed for temps
        }

        # Add light objects to the query if any are detected
        light_objects = LightManager.Get().GetLightObjectNames()
        query_objects.update(light_objects)

        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.objects.query",
        {
            "objects": query_objects
        })
        # Validate
        if result.HasError():
            self.Logger.error("MoonrakerCommandHandler failed GetCurrentJobStatus() query. "+result.GetLoggingErrorStr())
            return None

        # Get the result.
        res = result.GetResult()

        # Map the state
        state = "idle"
        statusObjectOrEmptyDict:dict[str, Any] = res.get("status", {})
        mrState = statusObjectOrEmptyDict.get("print_stats", {}).get("state", None)
        if mrState is not None:
            # https://moonraker.readthedocs.io/en/latest/printer_objects/#print_stats
            if mrState == "standby":
                state = "idle"
            elif mrState == "printing":
                # This is a special case, we consider "warmingup" a subset of printing.
                if MoonrakerClient.Get().GetMoonrakerCompat().CheckIfPrinterIsWarmingUp_WithPrintStats(result):
                    state = "warmingup"
                else:
                    state = "printing"
            elif mrState == "paused":
                state = "paused"
            elif mrState == "complete":
                state = "complete"
            elif mrState == "cancelled":
                state = "cancelled"
            elif mrState == "error":
                state = "error"
            else:
                self.Logger.warning("Unknown mrState returned from print_stats: "+str(mrState))
        else:
            self.Logger.warning("MoonrakerCommandHandler failed to find the print_stats.status")

        # TODO - If in an error state, set some context as to why.
        # This is shown to the user directly, so it must be short (think of a dashboard status) and formatted well.
        errorStr:Optional[str] = None

        # Get current layer info
        # None = The platform doesn't provide it.
        # 0 = The platform provider it, but there's no info yet.
        # # = The values
        # Note this is similar to how we also do it for notifications.
        currentLayerInt:Optional[int] = None
        totalLayersInt:Optional[int] = None
        currentLayerRaw, totalLayersRaw = MoonrakerClient.Get().GetMoonrakerCompat().GetCurrentLayerInfo()
        if totalLayersRaw is not None and totalLayersRaw > 0 and currentLayerRaw is not None and currentLayerRaw >= 0:
            currentLayerInt = int(currentLayerRaw)
            totalLayersInt = int(totalLayersRaw)

        # Get duration and filename.
        durationSec:int = 0
        fileName = ""
        ps = statusObjectOrEmptyDict.get("print_stats", None)
        if ps is not None:
            # We choose to use print_duration over "total_duration" so we only show the time actually spent printing. This is consistent across platforms.
            print_duration = ps.get("print_duration", None)
            if print_duration is not None:
                durationSec = int(print_duration)
            psFileName = ps.get("filename", None)
            if psFileName is not None:
                fileName = psFileName

        # If we have a file name, try to get the current filament usage.
        filamentUsageMm:int = 0
        if fileName is not None and len(fileName) > 0:
            filamentUsageMm = FileMetadataCache.Get().GetEstimatedFilamentUsageMm(fileName)

        # Get the progress
        progress = 0.0
        vsProgress = statusObjectOrEmptyDict.get("virtual_sdcard", {}).get("progress", None)
        if vsProgress is not None:
            # Convert progress 0->1 to 0->100
            progress = vsProgress * 100.0

        # Time left can be hard to compute correctly, so use the common function to do it based
        # on what we can get as a best effort.
        timeLeftSec = MoonrakerClient.Get().GetMoonrakerCompat().GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsVirtualSdCardAndGcodeMoveResult(result)

        # Get the current temps if possible.
        # Shared code with MoonrakerClient.GetTemps
        hotendActual = 0.0
        hotendTarget = 0.0
        bedTarget = 0.0
        bedActual = 0.0
        extruder = statusObjectOrEmptyDict.get("extruder", None)
        if extruder is not None:
            temp = extruder.get("temperature", None)
            if temp is not None:
                hotendActual = round(float(temp), 2)
            target = extruder.get("target", None)
            if target is not None:
                hotendTarget = round(float(target), 2)
        heater_bed = statusObjectOrEmptyDict.get("heater_bed", None)
        if heater_bed is not None:
            temp = heater_bed.get("temperature", None)
            if temp is not None:
                bedActual = round(float(temp), 2)
            target = heater_bed.get("target", None)
            if target is not None:
                bedTarget = round(float(target), 2)

        # Get the light status if available
        lights:Optional[List[Dict[str, Any]]] = None
        try:
            light_status_objects = LightManager.Get().GetLightStatus(statusObjectOrEmptyDict)
            # Convert LightStatus objects to dicts for JSON serialization
            if light_status_objects:
                lights = [{"Name": ls.Name, "On": ls.IsOn} for ls in light_status_objects]
        except Exception as e:
            self.Logger.debug(f"Failed to get light status: {e}")

        # Build the object and return.
        return {
            "State": state,
            "Error": errorStr,
            # List of lights with their status, or None if not supported/unknown
            "Lights": lights,
            "CurrentPrint":
            {
                "Progress" : progress,
                "DurationSec" : durationSec,
                # In some system buggy cases, the time left can be super high and won't fit into a int32, so we cap it.
                "TimeLeftSec" : min(timeLeftSec, 2147483600),
                "FileName" : fileName,
                "EstTotalFilUsedMm" : filamentUsageMm,
                "CurrentLayer": currentLayerInt,
                "TotalLayers": totalLayersInt,
                "Temps": {
                    "BedActual": bedActual,
                    "BedTarget": bedTarget,
                    "HotendActual": hotendActual,
                    "HotendTarget": hotendTarget,
                }
            }
        }


    # !! Platform Command Handler Interface Function !!
    # This must return the platform version as a string.
    def GetPlatformVersionStr(self) -> str:
        # We don't supply this for moonraker at the moment.
        return "1.0.0"


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the pause and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecutePause(self, smartPause:bool, suppressNotificationBool:bool, disableHotendBool:bool, disableBedBool:bool, zLiftMm:int, retractFilamentMm:int, showSmartPausePopup:bool) -> CommandResponse:
        # Check the state and that we have a connection to the host.
        result = self._CheckIfConnectedAndForExpectedStates(["printing"])
        if result is not None:
            return result

        # The smart pause logic handles all pause commands.
        return SmartPause.Get().ExecuteSmartPause(suppressNotificationBool)


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the resume and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteResume(self) -> CommandResponse:
        # Check the state and that we have a connection to the host.
        result = self._CheckIfConnectedAndForExpectedStates(["paused"])
        if result is not None:
            return result

        # Do the resume.
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.print.resume", {})
        if result.HasError():
            self.Logger.error("ExecuteResume failed to request resume. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(400, "Failed to request resume")

        # Ensure the response is a simple result.
        if result.IsSimpleResult() is False:
            self.Logger.error("ExecuteResume didn't return a simple result. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(400, "Bad result type")

        # Check the response, we expect a simple response.
        if result.GetSimpleResult() != "ok":
            self.Logger.error("ExecuteResume got an invalid request response. "+json.dumps(result.GetSimpleResult()))
            return CommandResponse.Error(400, "Invalid request response.")

        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # This must check that the printer state is valid for the cancel and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    # This must return a CommandResponse.
    def ExecuteCancel(self) -> CommandResponse:
        # Check the state and that we have a connection to the host.
        result = self._CheckIfConnectedAndForExpectedStates(["printing","paused"])
        if result is not None:
            return result

        # Do the resume.
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.print.cancel", {})
        if result.HasError():
            self.Logger.error("ExecuteCancel failed to request cancel. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(400, "Failed to request cancel")

        # Ensure the response is a simple result.
        if result.IsSimpleResult() is False:
            self.Logger.error("ExecuteCancel didn't return a simple result. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(400, "Bad result type")

        # Check the response
        if result.GetSimpleResult() != "ok":
            self.Logger.error("ExecuteCancel got an invalid request response. "+json.dumps(result.GetSimpleResult()))
            return CommandResponse.Error(400, "Invalid request response.")

        return CommandResponse.Success(None)


    # !! Platform Command Handler Interface Function !!
    # Sets the light state for the specified light type.
    def ExecuteSetLight(self, lightName:str, on:bool) -> CommandResponse:
        # Check if we have any lights available
        if not LightManager.Get().HasLights():
            self.Logger.info("ExecuteSetLight: No lights detected in printer configuration")
            return CommandResponse.Error(CommandHandler.c_CommandError_FeatureNotSupported, "No lights detected in printer configuration")

        # Check that we're connected to the printer
        result = self._CheckIfConnectedAndForExpectedStates(["standby", "printing", "paused", "complete"])
        if result is not None:
            # If the printer is not connected, return the appropriate error
            # But we still want to allow light control in most states except error
            if result.StatusCode == CommandHandler.c_CommandError_HostNotConnected:
                return result

        # Attempt to set the light state
        success = LightManager.Get().SetLightState(lightName, on)
        if success:
            self.Logger.info(f"ExecuteSetLight: Successfully set light to {'ON' if on else 'OFF'}")
            return CommandResponse.Success(None)
        else:
            self.Logger.error("ExecuteSetLight: Failed to set light state")
            return CommandResponse.Error(500, "Failed to set light state")


    # Checks if the printer is connected and in the correct state (or states)
    # If everything checks out, returns None. Otherwise it returns a CommandResponse
    def _CheckIfConnectedAndForExpectedStates(self, stateArray:List[str]) -> Optional[CommandResponse]:
        # Only allow the pause if the print state is printing, otherwise the system seems to get confused.
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.objects.query",
        {
            "objects": {
                "print_stats": None
            }
        })
        if result.HasError():
            if result.ErrorCode == JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED:
                self.Logger.error("Command failed because the printer is no connected. "+result.GetLoggingErrorStr())
                return CommandResponse.Error(CommandHandler.c_CommandError_HostNotConnected, "Printer Not Connected")
            self.Logger.error("Command failed to get state. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(500, "Error Getting State")
        res = result.GetResult()
        state = res.get("status", {}).get("print_stats", {}).get("state", None)
        if state is None:
            self.Logger.error("Command failed to get state, state not found in dict.")
            return CommandResponse.Error(500, "Error Getting State From Dict")
        for s in stateArray:
            if s == state:
                return None

        self.Logger.warning("Command failed, printer "+state+" not the expected states.")
        return CommandResponse.Error(CommandHandler.c_CommandError_InvalidPrinterState, "Wrong State")
