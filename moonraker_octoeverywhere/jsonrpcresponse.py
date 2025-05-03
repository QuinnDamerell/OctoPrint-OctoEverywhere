from typing import Any, Dict, Optional

# The response object for a json rpc request made to Moonraker.
# Contains information on the state, and if successful, the result.
#
# General usage is check if HasError is true. If it is, then use
# GetErrorCode and GetErrorStr to get the error code and string.
# If there is no error, then use GetResult to get the result object.
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
        return JsonRpcResponse(resultObj)


    @staticmethod
    def FromError(errorCode:int, errorStr:Optional[str]=None) -> 'JsonRpcResponse':
        return JsonRpcResponse(None, errorCode, errorStr=errorStr)


    def __init__(self, resultObj:Optional[Dict[str, Any]], errorCode=0, errorStr:Optional[str]=None) -> None:
        self.Result = resultObj
        self.ErrorCode = errorCode
        self.ErrorStr = errorStr
        if self.Result is None and self.ErrorCode == 0:
            raise Exception("JsonRpcResponse was created with no result and no error code.")
        if self.ErrorCode == JsonRpcResponse.OE_ERROR_TIMEOUT:
            self.ErrorStr = "Timeout waiting for RPC response."
        if self.ErrorCode == JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED:
            self.ErrorStr = "No active websocket connected."


    # This must be checked first, if it returns True then GetResult can be called.
    def HasError(self) -> bool:
        return self.ErrorCode != 0 or self.Result is None


    # This can only be called after HasError is false.
    def GetResult(self) -> Dict[str, Any]:
        if self.Result is None:
            raise Exception("JsonRpcResponse GetResult was called when the result was None. HasError needs to be called first.")
        return self.Result


    def GetErrorCode(self) -> int:
        return self.ErrorCode


    def IsErrorCodeOeError(self) -> bool:
        return self.ErrorCode >= JsonRpcResponse.OE_ERROR_MIN and self.ErrorCode <= JsonRpcResponse.OE_ERROR_MAX


    def GetErrorStr(self) -> Optional[str]:
        return self.ErrorStr


    def GetLoggingErrorStr(self) -> str:
        return str(self.ErrorCode) + " - " + str(self.ErrorStr)
