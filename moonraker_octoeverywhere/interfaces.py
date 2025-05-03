
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from .jsonrpcresponse import JsonRpcResponse

# The interface for the Moonraker connection status handler.
class IMoonrakerConnectionStatusHandler(ABC):

    @abstractmethod
    def OnMoonrakerClientConnected(self) -> None:
        pass

    @abstractmethod
    def OnMoonrakerWsOpenAndAuthed(self) -> None:
        pass

    @abstractmethod
    def OnWebcamSettingsChanged(self) -> None:
        pass


# The interface for the Moonraker client.
class IMoonrakerClient(ABC):

    @abstractmethod
    def SendJsonRpcRequest(self, method:str, paramsDict:Optional[Dict[str, Any]]=None) -> JsonRpcResponse:
        pass
