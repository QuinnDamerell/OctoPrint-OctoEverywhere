from abc import ABC, abstractmethod

from .prusalinkmodels import PrinterState


class IStateTranslator(ABC):

    @abstractmethod
    def OnConnectionLost(self, wasFullyConnected:bool) -> None:
        pass

    @abstractmethod
    def OnStatusUpdate(self, pState:PrinterState, isFirstFullSyncResponse:bool) -> None:
        pass
