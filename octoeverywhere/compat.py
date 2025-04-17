from typing import Optional

from .interfaces import IApiRouteHandler, ISmartPauseHandler, IRelayWebSocketProvider, ILocalAuth, IRelayWebcamStreamDetector, ISlipstreamHandler, IWebRequestHandler

# Some of the features we need to integrate into the octoeverywhere package only exist on
# some platforms. This is basically an interface that allows us to dynamically control
# if some objects are available depending on the platform.
class Compat:

    _IsOctoPrintHost = False
    _IsMoonrakerHost = False
    _IsCompanionMode = False
    _IsBambu = False
    _IsElegooOs = False
    @staticmethod
    def IsOctoPrint() -> bool:
        return Compat._IsOctoPrintHost
    @staticmethod
    def IsMoonraker() -> bool:
        return Compat._IsMoonrakerHost
    @staticmethod
    def IsCompanionMode() -> bool:
        return Compat._IsCompanionMode
    @staticmethod
    def SetIsOctoPrint(b:bool):
        Compat._IsOctoPrintHost = b
    @staticmethod
    def SetIsMoonraker(b:bool):
        Compat._IsMoonrakerHost = b
    @staticmethod
    def SetIsCompanionMode(b:bool):
        Compat._IsCompanionMode = b
    @staticmethod
    def SetIsBambu(b:bool):
        Compat._IsBambu = b
    @staticmethod
    def SetIsElegooOs(b:bool):
        Compat._IsElegooOs = b


    _LocalAuthObj:Optional[ILocalAuth] = None
    @staticmethod
    def GetLocalAuth() -> Optional[ILocalAuth]:
        return Compat._LocalAuthObj
    @staticmethod
    def SetLocalAuth(obj:ILocalAuth):
        Compat._LocalAuthObj = obj


    _SlipstreamObj:Optional[ISlipstreamHandler] = None
    @staticmethod
    def GetSlipstream() -> Optional[ISlipstreamHandler]:
        return Compat._SlipstreamObj
    @staticmethod
    def SetSlipstream(obj:ISlipstreamHandler):
        Compat._SlipstreamObj = obj


    _SmartPauseInterfaceObj:Optional[ISmartPauseHandler] = None
    @staticmethod
    def GetSmartPauseInterface():
        return Compat._SmartPauseInterfaceObj
    @staticmethod
    def SetSmartPauseInterface(obj:ISmartPauseHandler):
        Compat._SmartPauseInterfaceObj = obj


    _WebRequestResponseHandler:Optional[IWebRequestHandler] = None
    @staticmethod
    def GetWebRequestResponseHandler() -> Optional[IWebRequestHandler]:
        return Compat._WebRequestResponseHandler
    @staticmethod
    def SetWebRequestResponseHandler(obj:IWebRequestHandler):
        Compat._WebRequestResponseHandler = obj


    _ApiRouterHandler:Optional[IApiRouteHandler] = None
    @staticmethod
    def GetApiRouterHandler() -> Optional[IApiRouteHandler]:
        return Compat._ApiRouterHandler
    @staticmethod
    def SetApiRouterHandler(obj:IApiRouteHandler):
        Compat._ApiRouterHandler = obj


    _RelayWebcamStreamDetector:Optional[IRelayWebcamStreamDetector] = None
    @staticmethod
    def GetRelayWebcamStreamDetector() -> Optional[IRelayWebcamStreamDetector]:
        return Compat._RelayWebcamStreamDetector
    @staticmethod
    def SetRelayWebcamStreamDetector(obj:IRelayWebcamStreamDetector):
        Compat._RelayWebcamStreamDetector = obj


    _RelayWebsocketProvider:Optional[IRelayWebSocketProvider] = None
    @staticmethod
    def GetRelayWebsocketProvider() -> Optional[IRelayWebSocketProvider]:
        return Compat._RelayWebsocketProvider
    @staticmethod
    def SetRelayWebsocketProvider(obj:IRelayWebSocketProvider):
        Compat._RelayWebsocketProvider = obj
