from abc import ABC, abstractmethod
from typing import Any, Dict

from .bambumodels import BambuState


class IBambuStateTranslator(ABC):

    @abstractmethod
    def ResetForNewConnection(self) -> None:
        pass

    @abstractmethod
    def OnMqttMessage(self, msg:Dict[str, Any], bambuState:BambuState, isFirstFullSyncResponse:bool) -> None:
        pass
