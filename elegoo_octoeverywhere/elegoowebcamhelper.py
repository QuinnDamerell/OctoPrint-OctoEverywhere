import logging
import time

from linux_host.config import Config

from octoeverywhere.Webcam.webcamsettingitem import WebcamSettingItem
from octoeverywhere.Webcam.quickcam import QuickCam


# This class implements the webcam platform helper interface for elegoo os.
class ElegooWebcamHelper():

    def __init__(self, logger:logging.Logger, config:Config) -> None:
        self.Logger = logger
        self.Config = config
        self.CachedStreamUrlBase:str = None
        self.LastStreamUrlBaseUpdateSec:float = 0.0


    # !! Interface Function !!
    # This must return an array of WebcamSettingItems.
    # Index 0 is used as the default webcam.
    # The order the webcams are returned is the order the user will see in any selection UIs.
    # Returns None on failure.
    def GetWebcamConfig(self):
        # For Elegoo OS printers, there's only one webcam setup by default, it's a jmpeg server running on 3031.
        #
        # BUT - The server only allows for one stream at a time, so we can't have multiple webcams.
        # For that reason, we use the QuickCam class to handle the stream, so it can be shared by anything that needs it.

        # We cache the stream URL base for 60 seconds so we don't have to pull and create it every time.
        # But the IP will also be updated if the connection class figures out the IP changed, so we do want to pull it every so often.
        now = time.time()
        timeSinceLastUpdateSec = now - self.LastStreamUrlBaseUpdateSec
        if self.CachedStreamUrlBase is None or timeSinceLastUpdateSec > 60:
            ipOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
            if ipOrHostname is None:
                self.Logger.error("ElegooWebcamHelper - Failed to get IP or Hostname from config, so we can't return a stream.")
                return None
            self.CachedStreamUrlBase = f"{QuickCam.JMPEGProtocol}{ipOrHostname}:3031/video"
            self.LastStreamUrlBaseUpdateSec = now

        timeSinceEpochSec = int(time.time() / 1000)
        jmpegStreamUrl = f"{self.CachedStreamUrlBase}?timestamp={timeSinceEpochSec}"
        return [WebcamSettingItem("Elegoo Cam", None, jmpegStreamUrl, False, False, 0)]


    # !! Interface Function !!
    # This function is called to determine if a QuickCam stream should keep running or not.
    # The idea is since a QuickCam stream can take longer to start, for example, the Bambu Websocket stream on sends 1FPS,
    # we can keep the stream running while the print is running to lower the latency of getting images.
    # Most most platforms, this should return true if the print is running or paused, otherwise false.
    # Also consider something like Gadget, it takes pictures every 20-40 seconds, so the stream will be started frequently if it's not already running.
    def ShouldQuickCamStreamKeepRunning(self) -> bool:
        # TODO - Implement this.
        # For Bambu, we want to keep the stream running if the printer is printing.
        # state = BambuClient.Get().GetState()
        # if state is None:
        #     return False
        # return state.IsPrinting(True)
        return True
