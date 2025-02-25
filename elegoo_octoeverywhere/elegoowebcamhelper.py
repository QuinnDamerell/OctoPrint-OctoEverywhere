import logging
import time

from linux_host.config import Config

from octoeverywhere.sentry import Sentry
from octoeverywhere.Webcam.webcamsettingitem import WebcamSettingItem

from .bambuclient import BambuClient


# This class implements the webcam platform helper interface for elegoo os.
class ElegooWebcamHelper():


    def __init__(self, logger:logging.Logger, config:Config) -> None:
        self.Logger = logger
        self.Config = config


    # !! Interface Function !!
    # This must return an array of WebcamSettingItems.
    # Index 0 is used as the default webcam.
    # The order the webcams are returned is the order the user will see in any selection UIs.
    # Returns None on failure.
    def GetWebcamConfig(self):
        # For Elegoo OS printers, there's only one webcam setup by default.
        # It's running on a webcam server on 3031.
        # The frontend also always adds a timestamp, probably for cache busting.
        # TODO - This is a hardcoded URL, we should get this from the config.
        timeSinceEpochSec = int(time.time() / 1000)
        jmpegStreamUrl = f"http://10.0.0.101:3031/video?timestamp={timeSinceEpochSec}"
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
