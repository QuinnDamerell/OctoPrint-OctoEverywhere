import logging

from  octoeverywhere.Webcam.webcamhelper import WebcamHelper

# Detects if an incoming relay request is a webcam stream request and modifies the request if needed.
class ElegooRelayWebcamUrlDetector:


    def __init__(self, logger:logging.Logger):
        self.Logger = logger


    # !! Interface Function !!
    # This function takes in the incoming relay http request and rewrites the URL or adds to the request if needed.
    def OnIncomingRelayRequest(self, relativeOrAbsolutePath:str, headers:dict) -> None:
        # Since the Elegoo web frontend is always the same, we don't need to do anything too fancy.
        # <ip>:3031/video is the path the frontend uses, but the server must re-write it anyways to correct the port.
        urlLower = relativeOrAbsolutePath.lower()
        if urlLower.find("/video") != -1:
            self.Logger.debug(f"ElegooRelayWebcamStreamDetector: Detected webcam stream request, adding oracle headers. Url: {relativeOrAbsolutePath}")
            headers[WebcamHelper.c_OracleStreamHeaderKey] = "true"
        # Snapshots don't exist yet, but if so, we will be ready.
        if urlLower.find("/snapshot") != -1:
            self.Logger.debug(f"ElegooRelayWebcamStreamDetector: Detected webcam snapshot request, adding oracle headers. Url: {relativeOrAbsolutePath}")
            headers[WebcamHelper.c_OracleSnapshotHeaderKey] = "true"
