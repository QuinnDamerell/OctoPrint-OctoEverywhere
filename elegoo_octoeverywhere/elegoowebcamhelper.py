import logging
import time

from linux_host.config import Config

from octoeverywhere.Webcam.webcamsettingitem import WebcamSettingItem
from octoeverywhere.Webcam.quickcam import QuickCam

from .elegooclient import ElegooClient

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
        # For Elegoo, we want to keep the stream running if the printer is printing.
        state = ElegooClient.Get().GetState()
        if state is None:
            return False
        return state.IsPrinting(True)


    # !! Interface Function !!
    # Called when quick cam is about to attempt to start a stream.
    def OnQuickCamStreamStart(self, url:str) -> None:
        # Only try to send commands if the WS is connected, to prevent log spam.
        if ElegooClient.Get().IsWebsocketConnected():
            # Before any stream is started, we must send a command to the printer to enable the webcam.
            # We have to do this a lot, because the web frontend will stop the stream if the user hits pause.
            # and there's no way to tell what the camera state is.
            #
            # We don't need to wait on this because the webcam video http request sill be successful, it just
            # wont's send data until the command is issued. So we return to let quick cam get connected.
            ElegooClient.Get().SendEnableWebcamCommand(False)
            # We also try to enable the light, to make sure Gadget can see the print.
            # We send the command formatted the same as the frontend.
            ElegooClient.Get().SendRequest(403, {"LightStatus":{"SecondLight":True,"RgbLight": [0,0,0]}}, waitForResponse=False)


    # !! Interface Function !!
    # Called when quick cam detects that the stream might have stalled.
    def OnQuickCamStreamStall(self, url:str) -> None:
        # The Elegoo webcam has an odd behavior where the http stream connects but sends no data
        # if the webcam isn't enabled or it gets disabled (the Elegoo frontend does this when the stream pauses)
        # So if we detect the stream is stalled, we send the enable webcam command again.
        # Only try to send commands if the WS is connected, to prevent log spam
        if ElegooClient.Get().IsWebsocketConnected():
            result = ElegooClient.Get().SendEnableWebcamCommand()
            if result.HasError():
                self.Logger.error(f"ElegooWebcamHelper failed to enable webcam, thus we can't stream. {result.GetLoggingErrorStr()}")
