from typing import Any, Dict, Optional

# The response object for a json rpc request made to Moonraker.
# Contains information on the state, and if successful, the result.
#
# General usage is check if HasError is true. If it is, then use
# GetErrorCode and GetErrorStr to get the error code and string.
# If there is no error, use IsSimpleResult to check if it's a simple result
# a dict object result. Simple results are usually just for commands like pause, resume, ect.
class JsonRpcResponse:

    # Our specific errors
    OE_ERROR_WS_NOT_CONNECTED = 99990001
    OE_ERROR_TIMEOUT = 99990002
    OE_ERROR_EXCEPTION = 99990003
    # Range helpers.
    OE_ERROR_MIN = OE_ERROR_WS_NOT_CONNECTED
    OE_ERROR_MAX = OE_ERROR_EXCEPTION


    @staticmethod
    def FromSuccess(resultObj:Dict[str, Any]) -> 'JsonRpcResponse':
        return JsonRpcResponse(resultObj=resultObj)


    @staticmethod
    def FromSimpleSuccess(result:str) -> 'JsonRpcResponse':
        return JsonRpcResponse(simpleResult=result)


    @staticmethod
    def FromError(errorCode:int, errorStr:Optional[str]=None) -> 'JsonRpcResponse':
        return JsonRpcResponse(errorCode=errorCode, errorStr=errorStr)


    def __init__(self, resultObj:Optional[Dict[str, Any]]=None, simpleResult:Optional[str]=None, errorCode=0, errorStr:Optional[str]=None) -> None:
        # Sometimes the Result is a dict, sometimes it's just a string "ok" like in command responses.
        self.Result = resultObj
        self.SimpleResult = simpleResult
        self.ErrorCode = errorCode
        self.ErrorStr = errorStr
        if self.Result is None and self.SimpleResult is None and self.ErrorCode == 0:
            raise Exception("JsonRpcResponse was created with no result, simple result, and no error code.")
        if self.ErrorCode == JsonRpcResponse.OE_ERROR_TIMEOUT:
            self.ErrorStr = "Timeout waiting for RPC response."
        if self.ErrorCode == JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED:
            self.ErrorStr = "No active websocket connected."
        if self.SimpleResult is not None:
            if isinstance(self.SimpleResult, str) is False:
                raise Exception("JsonRpcResponse was created with a simple result but it wasn't a string?")


    # This must be checked first, if it returns True then GetResult can be called.
    def HasError(self) -> bool:
        if self.ErrorCode != 0 or (self.Result is None and self.SimpleResult is None):
            return True
        return False


    # Returns if this is a simple result (IsSimpleResultOk) or a dict result GetResult.
    # Normally commands like pause, resume, ect return a simple result.
    def IsSimpleResult(self) -> bool:
        return self.SimpleResult is not None


    # This can only be called after HasError is false and IsSimpleResult is false.
    def GetResult(self) -> Dict[str, Any]:
        if self.Result is None:
            raise Exception("JsonRpcResponse GetResult was called when the result was None. HasError needs to be called first.")
        return self.Result


    # This can only be called after HasError is false and IsSimpleResult is true.
    def GetSimpleResult(self) -> str:
        if self.SimpleResult is None:
            raise Exception("JsonRpcResponse GetSimpleResult was called when the result was None. HasError needs to be called first.")
        return self.SimpleResult


    def GetErrorCode(self) -> int:
        return self.ErrorCode


    def IsErrorCodeOeError(self) -> bool:
        return self.ErrorCode >= JsonRpcResponse.OE_ERROR_MIN and self.ErrorCode <= JsonRpcResponse.OE_ERROR_MAX


    def GetErrorStr(self) -> Optional[str]:
        return self.ErrorStr


    def GetLoggingErrorStr(self) -> str:
        return str(self.ErrorCode) + " - " + str(self.ErrorStr)
