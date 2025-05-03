from abc import ABC, abstractmethod
from typing import Optional

from octoeverywhere.buffer import Buffer
from octoeverywhere.interfaces import WebSocketOpCode

from .elegoomodels import PrinterState

#
# Elegoo Type Interfaces
#

class IStateTranslator(ABC):

    # Fired when the connection to the printer is lost.
    @abstractmethod
    def OnConnectionLost(self, wasFullyConnected: bool) -> None:
        pass

    # Fired when a status update message has been received from the printer.
    @abstractmethod
    def OnStatusUpdate(self, pState:PrinterState, isFirstFullSyncResponse:bool) -> None:
        pass


class IFileManager(ABC):

    # Fired when the printer has connected/reconnected, so it's a good time to sync the file list.
    @abstractmethod
    def Sync(self) -> None:
        pass


class IWebsocketMux(ABC):

    # Fired when the websocket mux has received a message from the printer that should be sent to the web clients.
    @abstractmethod
    def OnIncomingMessage(self, sendToMuxSocketId:Optional[int], buffer:Buffer, msgType:WebSocketOpCode) -> None:
        pass
