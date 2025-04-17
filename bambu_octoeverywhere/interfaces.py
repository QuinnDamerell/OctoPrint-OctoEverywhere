from abc import ABC, abstractmethod
from typing import Any

from .bambumodels import BambuState


class IBambuStateTranslator(ABC):

    @abstractmethod
    def ResetForNewConnection(self) -> None:
        pass

    @abstractmethod
    def OnMqttMessage(self, msg:dict[str, Any], bambuState:BambuState, isFirstFullSyncResponse:bool) -> None:
        pass