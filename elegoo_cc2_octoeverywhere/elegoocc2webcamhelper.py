import logging
import time
from typing import List, Optional

from linux_host.config import Config

from octoeverywhere.Webcam.quickcam import QuickCam
from octoeverywhere.Webcam.webcamsettingitem import WebcamSettingItem
from octoeverywhere.interfaces import IWebcamPlatformHelper

from .elegoocc2client import ElegooCc2Client


class ElegooCc2WebcamHelper(IWebcamPlatformHelper):

    def __init__(self, logger:logging.Logger, config:Config) -> None:
        self.Logger = logger
        self.Config = config
        self.CachedStreamUrlBase:Optional[str] = None
        self.LastStreamUrlBaseUpdateSec:float = 0.0


    def GetWebcamConfig(self) -> Optional[List[WebcamSettingItem]]:
        now = time.time()
        timeSinceLastUpdateSec = now - self.LastStreamUrlBaseUpdateSec
        if self.CachedStreamUrlBase is None or timeSinceLastUpdateSec > 60:
            ipOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
            if ipOrHostname is None:
                self.Logger.error("ElegooCc2WebcamHelper - Failed to get IP or Hostname from config, so we can't return a stream.")
                return None
            self.CachedStreamUrlBase = f"{QuickCam.JMPEGProtocol}{ipOrHostname}:8080/?action=stream"
            self.LastStreamUrlBaseUpdateSec = now

        return [WebcamSettingItem("Elegoo CC2 Cam", None, self.CachedStreamUrlBase, False, False, 0)]


    def ShouldQuickCamStreamKeepRunning(self) -> bool:
        state = ElegooCc2Client.Get().GetState()
        if state is None:
            return False
        return state.IsPrinting(True)


    def OnQuickCamStreamStart(self, url:str) -> None:
        if ElegooCc2Client.Get().IsMqttConnected():
            ElegooCc2Client.Get().SendEnableWebcamCommand(False)
            if self.Config.GetBool(Config.SectionElegoo, Config.AutoActivateChamberLightForWebcam, False):
                ElegooCc2Client.Get().SendRequest(1029, {"brightness": 255}, waitForResponse=False)


    def OnQuickCamStreamStall(self, url:str) -> None:
        if ElegooCc2Client.Get().IsMqttConnected():
            result = ElegooCc2Client.Get().SendEnableWebcamCommand()
            if result.HasError():
                self.Logger.error(f"ElegooCc2WebcamHelper failed to enable webcam, thus we can't stream. {result.GetLoggingErrorStr()}")
