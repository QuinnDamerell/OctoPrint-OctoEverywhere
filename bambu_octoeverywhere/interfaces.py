from abc import ABC, abstractmethod

from .bambumodels import BambuState


class IBambuStateTranslator(ABC):

    @abstractmethod
    def ResetForNewConnection(self) -> None:
        pass

    @abstractmethod
    def OnMqttMessage(self, msg:dict, bambuState:BambuState, isFirstFullSyncResponse:bool) -> None:
        pass