
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .jsonrpcresponse import JsonRpcResponse


# Printer-specific behavior is isolated from Moonraker transport and command handling.
# Implementations must only return a physical tool selection command when the firmware
# provides a known way to bypass logical filament/tool mappings.
class IMoonrakerPrinterAdapter(ABC):

    @abstractmethod
    def GetErrorCode(self, errorObj:Dict[str, Any]) -> Optional[str]:
        pass

    @abstractmethod
    def GetStatusQueryObjects(self) -> List[str]:
        # Return the additional Klipper objects needed by this printer's status mapping.
        # The shared handler queries every field of these objects.
        pass

    @abstractmethod
    def GetPrinterSubState(self, status:Dict[str, Any]) -> Optional[str]:
        # Receive the full printer.objects.query status dictionary so each adapter
        # can interpret its own object names and schema. Return UI-ready text or None.
        pass

    @abstractmethod
    def GetPhysicalToolSelectionCommand(self, toolIndex:int) -> Optional[str]:
        pass


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
