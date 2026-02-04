from enum import Enum
from abc import ABC, abstractmethod
from typing import Any, Callable, List, Optional, Tuple, Union, Dict

from .buffer import Buffer, BufferOrNone, ByteLikeOrMemoryView
from .httpresult import HttpResult, HttpResultOrNone
from .snapshotresizeparams import SnapshotResizeParams
from .Webcam.webcamsettingitem import WebcamSettingItem

from .Proto.HttpInitialContext import HttpInitialContext

#
# Common Objects
# (used over interfaces)
#

# A simple enum to define the opcodes we use.
# This should mirror the _abnf.py file in the websocket library.
# These also are directly from the WS spec https://datatracker.ietf.org/doc/html/rfc6455#section-5.2
class WebSocketOpCode(Enum):
    CONT = 0x0
    TEXT = 0x1
    BINARY = 0x2
    CLOSE = 0x8
    PING = 0x9
    PONG = 0xA

    @staticmethod
    def FromWsLibInt(value: int) -> 'WebSocketOpCode':
        return WebSocketOpCode(value)

    def ToWsLibInt(self) -> int:
        return self.value


# A helper class that's the result of all ran commands.
class CommandResponse():

    @staticmethod
    def Success(resultDict:Optional[Dict[str, Any]]=None):
        if resultDict is None:
            resultDict = {}
        return CommandResponse(200, resultDict, None)


    @staticmethod
    def Error(statusCode:int, errorStr:Optional[str]):
        return CommandResponse(statusCode, None, errorStr)


    def __init__(self, statusCode:int, resultDict:Optional[Dict[str, Any]], errorStr:Optional[str]):
        self.StatusCode = statusCode
        self.ResultDict = resultDict
        self.ErrorStr = errorStr


#
# Common Interfaces
#

class IWebcamPlatformHelper(ABC):

    # This must return an array of WebcamSettingItems.
    # Index 0 is used as the default webcam.
    # The order the webcams are returned is the order the user will see in any selection UIs.
    # Returns None on failure.
    @abstractmethod
    def GetWebcamConfig(self) -> Optional[List[WebcamSettingItem]]:
        pass

    # This function is called to determine if a QuickCam stream should keep running or not.
    # The idea is since a QuickCam stream can take longer to start, for example, the Bambu Websocket stream on sends 1FPS,
    # we can keep the stream running while the print is running to lower the latency of getting images.
    # Most most platforms, this should return true if the print is running or paused, otherwise false.
    # Also consider something like Gadget, it takes pictures every 20-40 seconds, so the stream will be started frequently if it's not already running.
    @abstractmethod
    def ShouldQuickCamStreamKeepRunning(self) -> bool:
        pass

    # Called when quick cam is about to attempt to start a stream.
    @abstractmethod
    def OnQuickCamStreamStart(self, url:str) -> None:
        pass


    # Called when quick cam detects that the stream might have stalled.
    @abstractmethod
    def OnQuickCamStreamStall(self, url:str) -> None:
        pass


class IStateChangeHandler(ABC):

    # Called by the OctoEverywhere logic when the server connection has been established.
    @abstractmethod
    def OnPrimaryConnectionEstablished(self, octoKey:str, connectedAccounts:List[str]) -> None:
        pass

    # Called by the OctoEverywhere logic when a plugin update is required for this client.
    @abstractmethod
    def OnPluginUpdateRequired(self) -> None:
        pass

    # Called by the OctoEverywhere handshake when a rekey is required.
    @abstractmethod
    def OnRekeyRequired(self) -> None:
        pass



class IPopUpInvoker(ABC):

    # Interface function - Sends a UI popup message for various uses.
    # Must stay in sync with the OctoPrint handler!
    # title - string, the title text.
    # text  - string, the message.
    # type  - string, [notice, info, success, error] the type of message shown.
    # actionText - string, if not None or empty, this is the text to show on the action button or text link.
    # actionLink - string, if not None or empty, this is the URL to show on the action button or text link.
    # onlyShowIfLoadedViaOeBool - bool, if set, the message should only be shown on browsers loading the portal from OE.
    @abstractmethod
    def ShowUiPopup(self, title:str, text:str, msgType:str, actionText:Optional[str], actionLink:Optional[str], showForSec:int, onlyShowIfLoadedViaOeBool:bool) -> None:
        pass


class IApiRouteHandler(ABC):

    # Must return an absolute URL if it's being updated, otherwise None.
    #
    # This is only needed for relative paths, since absolute paths can't be mapped like this.
    # Basically the frontend is going to always call the https://<sub>.octoeverywhere.com/<websocket/printer/etc>
    # Since the subdomain will map the request to the correct instance bound to the moonraker instance, the
    # plugin can figure which calls are for moonraker and map them to the known instance port.
    # Note this will be used by both websockets and http calls.
    @abstractmethod
    def MapRelativePathToAbsolutePathIfNeeded(self, relativeUrl:str, protocol:str) -> Optional[str]:
        pass


class ISmartPauseHandler(ABC):

    # Returns None if there is no current suppression or the time of the last time it was requested
    @abstractmethod
    def GetAndResetLastPauseNotificationSuppressionTimeSec(self) -> Optional[float]:
        pass


class IPrinterStateReporter(ABC):

    # This function will get the estimated time remaining for the current print.
    # Returns -1 if the estimate is unknown.
    @abstractmethod
    def GetPrintTimeRemainingEstimateInSeconds(self) -> int:
        pass

    # If the printer is warming up, this value would be -1. The First Layer Notification logic depends upon this or GetCurrentLayerInfo!
    # Returns the current zoffset if known, otherwise -1.
    @abstractmethod
    def GetCurrentZOffsetMm(self) -> int:
        pass

    # Returns:
    #     (None, None) if the platform doesn't support layer info.
    #     (0,0) if the current layer is unknown.
    #     (currentLayer(int), totalLayers(int)) if the values are known.
    @abstractmethod
    def GetCurrentLayerInfo(self) -> Tuple[Optional[int], Optional[int]]:
        pass

    # Returns True if the printing timers (notifications and gadget) should be running, which is only the printing state. (not even paused)
    # False if the printer state is anything else, which means they should stop.
    @abstractmethod
    def ShouldPrintingTimersBeRunning(self) -> bool:
        pass

    # If called while the print state is "Printing", returns True if the print is currently in the warm-up phase. Otherwise False
    @abstractmethod
    def IsPrintWarmingUp(self) -> bool:
        pass

    # ! Interface Function ! The entire interface must change if the function is changed.
    # Returns the (hotend temp, bed temp) as a float in celsius if they are available, otherwise None.
    @abstractmethod
    def GetTemps(self) -> Tuple[Optional[float], Optional[float]]:
        pass


# Feature flags for the platforms.
FEATURE_LIGHT_CONTROL = 1 << 0
FEATURE_AXIS_MOVEMENT = 1 << 1
FEATURE_HOMING        = 1 << 2
FEATURE_EXTRUSION     = 1 << 3
FEATURE_TEMPERATURE_CONTROL = 1 << 4


class IPlatformCommandHandler(ABC):

    # If the plugin is connected and in a good state, this should return the standard job status.
    # On error, this should return None and then we send back the CommandHandler.c_CommandError_HostNotConnected error
    # OR it will return an int, which must be a CommandHandler.c_CommandError_... error, and we will send that back.
    @abstractmethod
    def GetCurrentJobStatus(self) -> Union[int, None, Dict[str, Any]]:
        pass

    # This must return the platform version as a string.
    @abstractmethod
    def GetPlatformVersionStr(self) -> str:
        pass

    # Returns an int with the supported feature flags for this platform, such as FEATURE_LIGHT_CONTROL, etc
    @abstractmethod
    def GetSupportedFeatureFlags(self) -> int:
        pass

    # This must check that the printer state is valid for the pause and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    @abstractmethod
    def ExecutePause(self, smartPause:bool, suppressNotificationBool:bool, disableHotendBool:bool, disableBedBool:bool, zLiftMm:int, retractFilamentMm:int, showSmartPausePopup:bool) -> CommandResponse:
        pass

    # This must check that the printer state is valid for the resume and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    @abstractmethod
    def ExecuteResume(self) -> CommandResponse:
        pass

    # This must check that the printer state is valid for the cancel and the plugin is connected to the host.
    # If not, it must return the correct two error codes accordingly.
    @abstractmethod
    def ExecuteCancel(self) -> CommandResponse:
        pass

    # Sets the light state for the specified light type.
    # lightName: The name of the light to set. This name is reflected from what's returned in the status API.
    # on: True to turn the light on, False to turn it off.
    # Returns a CommandResponse with success or an error if the light type is not supported.
    @abstractmethod
    def ExecuteSetLight(self, lightName:str, on:bool) -> CommandResponse:
        pass

    # Moves the specified axis by the given distance in mm.
    # axis: The axis to move ("X", "Y", or "Z")
    # distanceMm: The distance to move in mm (positive or negative)
    # Returns a CommandResponse with success or an error.
    @abstractmethod
    def ExecuteMoveAxis(self, axis:str, distanceMm:float) -> CommandResponse:
        pass

    # Homes all axes.
    # Returns a CommandResponse with success or an error.
    @abstractmethod
    def ExecuteHome(self) -> CommandResponse:
        pass

    # Extrudes or retracts filament for the specified extruder.
    # extruder: The extruder index (0-based)
    # distanceMm: The distance to extrude in mm (positive=extrude, negative=retract)
    # Returns a CommandResponse with success or an error.
    @abstractmethod
    def ExecuteExtrude(self, extruder:int, distanceMm:float) -> CommandResponse:
        pass

    # Sets the temperature for bed, chamber, or tool.
    # Returns a CommandResponse with success or an error.
    @abstractmethod
    def ExecuteSetTemp(self, bedC:Optional[float], chamberC:Optional[float], toolC:Optional[float], toolNumber:Optional[int]) -> CommandResponse:
        pass


class ILocalAuth(ABC):

    # Adds the auth header with the auth key.
    def AddAuthHeader(self, headers:Dict[str,str]) -> None:
        pass


class IRelayWebcamStreamDetector(ABC):

    # This function takes in the incoming relay http request and rewrites the URL or adds to the request if needed.
    @abstractmethod
    def OnIncomingRelayRequest(self, relativeOrAbsolutePath:str, headers:Dict[str,str]) -> None:
        pass


class ISlipstreamHandler(ABC):

    # If available for the given URL, this will returned the cached and ready to go OctoHttpResult.
    # Otherwise returns None
    @abstractmethod
    def GetCachedOctoHttpResult(self, httpInitialContext:HttpInitialContext) -> HttpResultOrNone:
        pass

    # This will be called when the cache should be updated.
    @abstractmethod
    def UpdateCache(self, delay:int=0) -> None:
        pass


class IWebRequestHandler(ABC):

    # Given a URL (which can be absolute or relative) check if we might want to edit the response.
    # If no, then None is returned and the call is handled as normal.
    # If yes, some kind of context object must be returned, which will be given back to us.
    #     If yes, the entire response will be read as one full byte buffer, and given for us to deal with.
    @abstractmethod
    def CheckIfResponseNeedsToBeHandled(self, uri:str) -> Optional[Any]:
        pass

    # If we returned a context above in CheckIfResponseNeedsToBeHandled, this will be called after the web request is made
    # and the body is fully read. The entire body will be read into the bodyBuffer.
    # We are able to modify the bodyBuffer as we wish or not, but we must return the full bodyBuffer back to be returned.
    @abstractmethod
    def HandleResponse(self, contextObject:Any, octoHttpResult:HttpResult, bodyBuffer:Buffer) -> Buffer:
        pass


class IHostCommandHandler(ABC):

    @abstractmethod
    def OnRekeyCommand(self) -> bool:
        pass


# Important! There are patterns these classes must follow in terms of how the use the callbacks.
# All callbacks should be fired from the async run thread, except error and close.
# The flow must be the following:
#
#    Create
#    RunAsync
#       -> Async Thread Starts
#             Wait for open
#             onWsOpen
#             Loop for messages
#                onWsData
#
#    onWsClosed can be called at anytime, even before onWsOpen is called!
#    If there is an error, onWsError will be called then onWsClosed.
class IWebSocketClient(ABC):

    @abstractmethod
    def Close(self) -> None:
        pass


    @abstractmethod
    def RunAsync(self) -> None:
        pass

    @abstractmethod
    def Send(self, buffer:Buffer, msgStartOffsetBytes:Optional[int]=None, msgSize:Optional[int]=None, isData:bool=True) -> None:
        pass

    @abstractmethod
    def SendWithOptCode(self, buffer:Buffer, msgStartOffsetBytes:Optional[int]=None, msgSize:Optional[int]=None, optCode=WebSocketOpCode.BINARY) -> None:
        pass

    @abstractmethod
    def SetDisableCertCheck(self, disable:bool) -> None:
        pass


class IRelayWebSocketProvider(ABC):

    @abstractmethod
    def GetWebsocketObject(self, path:str, pathType:int, context:HttpInitialContext,
                           onWsOpen:Optional[Callable[[IWebSocketClient], None]]=None,
                           onWsData:Optional[Callable[[IWebSocketClient, Buffer, WebSocketOpCode], None]]=None,
                           onWsClose:Optional[Callable[[IWebSocketClient], None]]=None,
                           onWsError:Optional[Callable[[IWebSocketClient, Exception], None]]=None,
                           headers:Optional[Dict[str, str]]=None,
                           subProtocolList:Optional[List[str]]=None) -> Optional[IWebSocketClient]:
        pass


# Returned from the command provider to allow for a websocket creation.
class ICommandWebsocketProvider(ABC):

    # This must return a IWebsocketClient or return None on failure.
    @abstractmethod
    def GetWebsocketObject(self, streamId:int, path:str, pathType:int, context:HttpInitialContext,
                           onWsOpen:Optional[Callable[[IWebSocketClient], None]]=None,
                           onWsData:Optional[Callable[[IWebSocketClient, Buffer, WebSocketOpCode], None]]=None,
                           onWsClose:Optional[Callable[[IWebSocketClient], None]]=None,
                           onWsError:Optional[Callable[[IWebSocketClient, Exception], None]]=None,
                           headers:Optional[Dict[str, str]]=None,
                           subProtocolList:Optional[List[str]]=None) -> Optional[IWebSocketClient]:
        pass


# Allows us to wrap a provider with the included command args to return before the websocket object creation.
class ICommandWebsocketProviderBuilder(ABC):

    # This must return a provider or None on failure.
    @abstractmethod
    def GetCommandWebsocketProvider(self, args:Optional[Dict[str, Any]]) -> Optional[ICommandWebsocketProvider]:
        pass


#
# Local Interfaces
# Usually only used to prevent circular imports.
#

class IOctoEverywhereHost(ABC):

    @abstractmethod
    def OnSummonRequest(self, summonConnectUrl:str, summonMethod:int) -> None:
        pass


class IOctoStream(ABC):

    @abstractmethod
    def OnSessionError(self, sessionId:int, backoffModifierSec:int) -> None:
        pass

    @abstractmethod
    def SendMsg(self, buffer:Buffer, msgStartOffsetBytes:int, msgSize:int) -> None:
        pass

    @abstractmethod
    def OnSummonRequest(self, sessionId:int, summonConnectUrl:str, summonMethod:int) -> None:
        pass

    @abstractmethod
    def OnHandshakeComplete(self, sessionId:int, octoKey:str, connectedAccounts:List[str]) -> None:
        pass

    @abstractmethod
    def OnPluginUpdateRequired(self) -> None:
        pass

    @abstractmethod
    def OnRekeyRequired(self) -> None:
        pass


class IOctoSession(ABC):

    @abstractmethod
    def WebStreamClosed(self, sessionId:int) -> None:
        pass

    @abstractmethod
    def OnSessionError(self, backoffModifierSec:int) -> None:
        pass

    @abstractmethod
    def Send(self, buffer:Buffer, msgStartOffsetBytes:int, msgSize:int) -> None:
        pass


class IWebStream(ABC):

    @abstractmethod
    def SendToOctoStream(self, buffer:Buffer, msgStartOffsetBytes:int, msgSize:int, isCloseFlagSet=False, silentlyFail=False) -> None:
        pass

    @abstractmethod
    def Close(self) -> None:
        pass

    @abstractmethod
    def SetClosedDueToFailedRequestConnection(self) -> None:
        pass



class INotificationHandler(ABC):

    # Note that the args must be a string, string dict, because the HTTP post form requires string values.
    # We also use any for the file type because otherwise the type is too complex.
    @abstractmethod
    def BuildCommonEventArgs(self, event:str, args:Optional[Dict[str,str]]=None, progressOverwriteFloat:Optional[float]=None, snapshotResizeParams:Optional[SnapshotResizeParams]=None, useFinalSnapSnapshot=False) -> Tuple[Optional[Dict[str,str]], Optional[Dict[str, Tuple[str, ByteLikeOrMemoryView]]]]:
        pass

    @abstractmethod
    def GetPrintId(self) -> Optional[str]:
        pass

    @abstractmethod
    def GetNotificationSnapshot(self, snapshotResizeParams:Optional[SnapshotResizeParams]=None) -> BufferOrNone:
        pass

    # We don't pass the return type of Gadget since it would require us to import the file.
    @abstractmethod
    def GetGadget(self) -> Any:
        pass

    @abstractmethod
    def GetPrintStartTimeSec(self) -> float:
        pass

    @abstractmethod
    def OnBedCooldownComplete(self, bedTempCelsius:float) -> None:
        pass


class IQuickCam(ABC):
    # This should return the current image from the webcam.
    # If the image is not available, it should return None.
    @abstractmethod
    def GetCurrentImage(self) -> BufferOrNone:
        pass

    # Used to attach a new stream handler to receive callbacks when an image is ready.
    # Note a call to detach must be called as well!
    @abstractmethod
    def AttachImageStreamCallback(self, callback:Callable[[Buffer], None]) -> None:
        pass

    # Used to detach a new stream handler to receive callbacks when an image is ready.
    @abstractmethod
    def DetachImageStreamCallback(self, callback:Callable[[Buffer], None]):
        pass


class IOctoPrintPlugin(ABC):

    @abstractmethod
    def ShowSmartPausePopUpOnPortalLoad(self) -> None:
        pass
