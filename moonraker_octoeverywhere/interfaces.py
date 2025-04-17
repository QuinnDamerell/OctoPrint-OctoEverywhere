
from abc import ABC, abstractmethod


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