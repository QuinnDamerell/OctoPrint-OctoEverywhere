from abc import ABC, abstractmethod
from typing import Optional

from .elegoocc2models import FileInfo, PrinterState


class IStateTranslator(ABC):

    @abstractmethod
    def OnConnectionLost(self, wasFullyConnected:bool) -> None:
        pass

    @abstractmethod
    def OnStatusUpdate(self, pState:PrinterState, isFirstFullSyncResponse:bool) -> None:
        pass


class IFileManager(ABC):

    @abstractmethod
    def Sync(self) -> None:
        pass

    @abstractmethod
    def GetFileInfoFromState(self, printerState:PrinterState) -> Optional[FileInfo]:
        pass
