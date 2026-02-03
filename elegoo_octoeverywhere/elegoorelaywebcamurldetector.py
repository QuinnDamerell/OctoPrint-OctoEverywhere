import logging
from typing import Dict

from octoeverywhere.Webcam.webcamhelper import WebcamHelper
from octoeverywhere.interfaces import IRelayWebcamStreamDetector

# Detects if an incoming relay request is a webcam stream request and modifies the request if needed.
class ElegooRelayWebcamUrlDetector(IRelayWebcamStreamDetector):


    def __init__(self, logger:logging.Logger):
        self.Logger = logger


    # !! Interface Function !!
    # This function takes in the incoming relay http request and rewrites the URL or adds to the request if needed.
    def OnIncomingRelayRequest(self, relativeOrAbsolutePath:str, headers:Dict[str, str]) -> None:
        # Since the Elegoo web frontend is always the same, we don't need to do anything too fancy.
        # <ip>:3031/video is the path the frontend uses, but the server must re-write it anyways to correct the port.
        urlLower = relativeOrAbsolutePath.lower()
        if urlLower.find("/video") != -1:
            self.Logger.debug("ElegooRelayWebcamStreamDetector: Detected webcam stream request, adding oracle headers. Url: %s", relativeOrAbsolutePath)
            headers[WebcamHelper.c_OracleStreamHeaderKey] = "true"
        # Snapshots don't exist yet, but if so, we will be ready.
        if urlLower.find("/snapshot") != -1:
            self.Logger.debug("ElegooRelayWebcamStreamDetector: Detected webcam snapshot request, adding oracle headers. Url: %s", relativeOrAbsolutePath)
            headers[WebcamHelper.c_OracleSnapshotHeaderKey] = "true"
