import logging
import time
from typing import List, Optional

from linux_host.config import Config

from octoeverywhere.Webcam.webcamsettingitem import WebcamSettingItem
from octoeverywhere.interfaces import IWebcamPlatformHelper

from .prusalinkclient import PrusaLinkClient


class PrusaLinkWebcamHelper(IWebcamPlatformHelper):

    def __init__(self, logger:logging.Logger, config:Config) -> None:
        self.Logger = logger
        self.Config = config
        self.CachedSnapshotUrl:Optional[str] = None
        self.LastSnapshotUrlUpdateSec:float = 0.0


    def GetWebcamConfig(self) -> Optional[List[WebcamSettingItem]]:
        state = PrusaLinkClient.Get().GetState()
        if state is not None and state.HasActiveCamera is False and state.CameraId is None:
            return None

        now = time.time()
        if self.CachedSnapshotUrl is None or now - self.LastSnapshotUrlUpdateSec > 60:
            context = PrusaLinkClient.Get().GetCurrentConnectionContext()
            if context is None:
                ipOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
                portStr = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, Config.PrusaLinkDefaultPortStr)
            else:
                ipOrHostname = context.IpOrHostname
                portStr = context.PortStr
            if ipOrHostname is None or portStr is None:
                self.Logger.error("PrusaLinkWebcamHelper failed to get IP/port from config.")
                return None
            self.CachedSnapshotUrl = f"http://{ipOrHostname}:{portStr}/api/v1/cameras/snap"
            self.LastSnapshotUrlUpdateSec = now

        return [WebcamSettingItem("PrusaLink Camera", self.CachedSnapshotUrl, None, False, False, 0)]


    def ShouldQuickCamStreamKeepRunning(self) -> bool:
        state = PrusaLinkClient.Get().GetState()
        if state is None:
            return False
        return state.IsPrinting(True)


    def OnQuickCamStreamStart(self, url:str) -> None:
        pass


    def OnQuickCamStreamStall(self, url:str) -> None:
        pass
