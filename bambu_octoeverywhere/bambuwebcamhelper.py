import logging
import time
from typing import List, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from linux_host.config import Config

from octoeverywhere.Webcam.webcamsettingitem import WebcamSettingItem
from octoeverywhere.interfaces import IWebcamPlatformHelper

from .bambuclient import BambuClient


# This class implements the webcam platform helper interface for bambu.
class BambuWebcamHelper(IWebcamPlatformHelper):


    def __init__(self, logger:logging.Logger, config:Config) -> None:
        self.Logger = logger
        self.Config = config
        self.LastUrlUpdateTimeSec:float = 0.0
        self.CachedStreamingUrl = None


    # !! Interface Function !!
    # This must return an array of WebcamSettingItems.
    # Index 0 is used as the default webcam.
    # The order the webcams are returned is the order the user will see in any selection UIs.
    # Returns None on failure.
    def GetWebcamConfig(self) -> Optional[List[WebcamSettingItem]]:
        # Bambu has a special webcam setup where there's only one cam, and the streaming system is either RTSP or Websocket based.
        # We do support plugin local webcam items, which are webcams the user can setup from the website and are external webcams.
        # Note! This webcam name is shown on to the user in the UI, so it should be a good name that indicates this is a Bambu built in webcam.
        # Also, if the name changes, the default printer index might also change.
        return [WebcamSettingItem("Bambu Cam", None, self._GetStreamingUrl(), False, False, 0)]


    # !! Interface Function !!
    # This function is called to determine if a QuickCam stream should keep running or not.
    # The idea is since a QuickCam stream can take longer to start, for example, the Bambu Websocket stream on sends 1FPS,
    # we can keep the stream running while the print is running to lower the latency of getting images.
    # Most most platforms, this should return true if the print is running or paused, otherwise false.
    # Also consider something like Gadget, it takes pictures every 20-40 seconds, so the stream will be started frequently if it's not already running.
    def ShouldQuickCamStreamKeepRunning(self) -> bool:
        # For Bambu, we want to keep the stream running if the printer is printing.
        state = BambuClient.Get().GetState()
        if state is None:
            return False
        return state.IsPrinting(True)


    # !! Interface Function !!
    # Called when quick cam is about to attempt to start a stream.
    def OnQuickCamStreamStart(self, url:str) -> None:
        # Nothing to do.
        pass


    # !! Interface Function !!
    # Called when quick cam detects that the stream might have stalled.
    def OnQuickCamStreamStall(self, url:str) -> None:
        # Nothing to do.
        pass


    # Returns the current URL that should be used for snapshots and streaming.
    def _GetStreamingUrl(self) -> str:
        # We cache the urls for a little bit once they are generated, so we don't have to re-created them every time
        # But we do want to refresh them occasionally, so if the access code or IP changes, we update it.
        self._UpdateUrlsIfNeeded()

        # Ensure we got something, if not, warn about it.
        if self.CachedStreamingUrl is None:
            self.Logger.error("BambuWebcamHelper failed to get streaming URL, thus we can't stream.")
            return "none"
        return self.CachedStreamingUrl


    # This needs to be thread safe, but we don't use any locks. It's find if multiple threads get the info at the same time.
    def _UpdateUrlsIfNeeded(self) -> None:
        # Test if we need to update or use the cached values.
        if self.CachedStreamingUrl is not None and len(self.CachedStreamingUrl) > 0 and time.time() - self.LastUrlUpdateTimeSec < 30.0:
            return

        # Before we can return the webcam config, we need to know what kind of printer this is.
        # TODO - Right now it seems the X1 doesn't send back version info on start or with the version command,
        # so we use the existence of the RTSP URL to determine what we can do.
        # Ideally we would use the printer version in the future.
        rtspUrl = None
        stateGetAttempt = 0
        while True:
            stateGetAttempt += 1
            # Wait until the object exists
            state = BambuClient.Get().GetState()
            if state is not None:
                # When the state object is not None, we know we got the state sync.
                # Now we can check if there's a RTSP url or not, which will indicate what kind of stream we need to use.
                rtspUrl = state.rtsp_url
                break

            # If we didn't get the state after a few attempts, we give up and default to the websocket stream.
            if stateGetAttempt > 5:
                self.Logger.warning(f"BambuWebcamHelper wasn't able to get the printer state after {stateGetAttempt} attempts")
                break

            # Sleep for a bit before trying again.
            time.sleep(2.0)

        # Get the access code and ip from the config file, so we always get the latest.
        # The BambuClient class will update the value in the config if the IP address of the printer changes, which can happen while we are running.
        accessCode = self.Config.GetStr(Config.SectionBambu, Config.BambuAccessToken, None)
        ipOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        if accessCode is None or ipOrHostname is None:
            self.Logger.error("BambuWebcamHelper failed to get a ip or access code from the config, thus we can't stream.")
            return

        # The configured host is kept up to date by the printer connection. Bracket IPv6 addresses for the URL.
        host = ipOrHostname.strip()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        auth = f"bblp:{quote(accessCode, safe='')}@{host}"

        # If there is a RTSP URL, we know this printer uses the RTSP protocol to stream the webcam.
        if rtspUrl is not None and len(rtspUrl) > 0:
            try:
                # Some printers return an old IP or 0.0.0.0. Use the host we connect to, but keep the stream path and port.
                parsedUrl = urlsplit(rtspUrl)
                if parsedUrl.scheme not in ("rtsp", "rtsps") or parsedUrl.hostname is None:
                    raise ValueError("Invalid RTSP URL")
                port = f":{parsedUrl.port}" if parsedUrl.port is not None else ""
                self.CachedStreamingUrl = urlunsplit((parsedUrl.scheme, auth + port, parsedUrl.path, parsedUrl.query, parsedUrl.fragment))
            except ValueError:
                self.Logger.warning("BambuWebcamHelper failed to parse the RTSP URL from the printer, using the default stream path.")
                self.CachedStreamingUrl = f"rtsps://{auth}:322/streaming/live/1"
        else:
            # If there is no RTSP URL, we assume the printer uses the websocket based cam streaming.
            self.CachedStreamingUrl = f"ws://bblp:{accessCode}@{host}:6000"

        # Set the time we updated the cached values.
        self.LastUrlUpdateTimeSec = time.time()
