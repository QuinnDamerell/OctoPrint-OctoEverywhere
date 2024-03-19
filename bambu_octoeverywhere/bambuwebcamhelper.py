import logging
import time
import threading

from octoeverywhere.webcamhelper import WebcamSettingItem
from octoeverywhere.octohttprequest import OctoHttpRequest

from linux_host.config import Config

from .quickcam import QuickCam


# This class implements the webcam platform helper interface for bambu.
class BambuWebcamHelper():

    # These don't really matter, but we define them to keep them consistent
    c_SpecialMockSnapshotPath = "bambu-special-snapshot"
    c_SpecialMockStreamPath = "bambu-special-stream"
    c_OeStreamBoundaryString = "oestreamboundary"


    def __init__(self, logger:logging.Logger, config:Config) -> None:
        self.Logger = logger
        self.Config = config


    # !! Interface Function !!
    # This must return an array of WebcamSettingItems.
    # Index 0 is used as the default webcam.
    # The order the webcams are returned is the order the user will see in any selection UIs.
    # Returns None on failure.
    def GetWebcamConfig(self):
        # Bambu has a special webcam setup where there's only one cam and we need to get in a special way,
        # So we return this one default webcam object.
        return [WebcamSettingItem("Default", BambuWebcamHelper.c_SpecialMockSnapshotPath, BambuWebcamHelper.c_SpecialMockStreamPath, False, False, 0)]


    # !! Optional Interface Function !!
    # If defined, this function must handle ALL snapshot requests for the platform.
    #
    # On failure, return None
    # On success, this will return a valid OctoHttpRequest that's fully filled out.
    # The snapshot will always already be fully read, and will be FullBodyBuffer var.
    def GetSnapshot_Override(self, cameraIndex:int):
        # Try to get a snapshot from our QuickCam system.
        img = QuickCam.Get().GetCurrentImage()
        if img is None:
            return None

        # If we get an image, return it!
        headers = {
            "Content-Type": "image/jpeg"
        }
        return OctoHttpRequest.Result(200, headers, BambuWebcamHelper.c_SpecialMockSnapshotPath, False, fullBodyBuffer=img)


    # !! Optional Interface Function !!
    # If defined, this function must handle ALL stream requests for the platform.
    #
    # On failure, return None
    # On success, this will return a valid OctoHttpRequest that's fully filled out.
    # This must return an OctoHttpRequest object with a custom body read stream.
    def GetStream_Override(self, cameraIndex:int):
        # We must create a new instance of this class per stream to ensure all of the vars stay in it's context
        # and the streams are cleaned up properly.
        sm = StreamInstance(self.Logger)
        return sm.StartWebRequest()


# Stream Instance is a class that is created per web stream to handle streaming QuickCam images into the http stream.
# It must be created per http request so it can manage it's own local vars.
class StreamInstance:
    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger
        self.IsFirstSend = True
        self.StreamOpenTimeSec = time.time()
        self.ImageReadyEvent = threading.Event()
        self.AwaitingImage:bytearray = None


    def StartWebRequest(self) -> OctoHttpRequest.Result:
        # First, try to get a snapshot. This will determine if we are able to get a stream or not.
        # If we can't start the stream, then we don't return success.
        # We will also use this first image to start the stream, to get it going ASAP.
        self.AwaitingImage = QuickCam.Get().GetCurrentImage()
        if self.AwaitingImage is None:
            return None

        # Note! We must be sure to call DetachImageStreamCallback to remove this stream callback!
        QuickCam.Get().AttachImageStreamCallback(self._NewImageCallback)

        # We must set the content type so that the web browser knows what kind of stream to expect.
        headers = {
            "content-type": f"multipart/x-mixed-replace; boundary={BambuWebcamHelper.c_OeStreamBoundaryString}",
        }
        # Return a result object with out callbacks setup for the stream body.
        return OctoHttpRequest.Result(200, headers, BambuWebcamHelper.c_SpecialMockStreamPath, False, customBodyStreamCallback=self._CustomBodyStreamRead, customBodyStreamClosedCallback=self._CustomBodyStreamClosed)


    # Define the callback we will get from QuickCam when there's a new image ready for us to send.
    def _NewImageCallback(self, imgBuffer:bytearray):
        self.AwaitingImage = imgBuffer
        self.ImageReadyEvent.set()


    # Define a callback for our http body reading system to call when it needs data.
    def _CustomBodyStreamRead(self) -> bytearray:
        while True:
            # See if we can capture an image. There might already be a new image we don't even have to wait for.
            capturedImage = self.AwaitingImage
            if capturedImage is not None:
                # If so, clear the awaiting image and reset the event.
                self.AwaitingImage = None
                self.ImageReadyEvent.clear()

                # Build the buffer to send
                header = f"--{BambuWebcamHelper.c_OeStreamBoundaryString}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(capturedImage)}\r\n\r\n"
                imageChunkBuffer = header.encode('utf-8') + capturedImage + b"\r\n" + header.encode('utf-8') + capturedImage + b"\r\n"

                # TODO - I don't know why, but chrome seems to delay the rendering of the image until it gets two?
                # This could be something in the pipeline not flushing correctly, or other things. But for now, on the first send we double the image to make it render instantly.
                if self.IsFirstSend:
                    imageChunkBuffer = imageChunkBuffer + imageChunkBuffer
                    self.IsFirstSend = False
                    self.Logger.info(f"QuickCam took {time.time()-self.StreamOpenTimeSec} seconds from stream open to first image sent.")
                return imageChunkBuffer
            # If we didn't get an image, wait on the event for a new one.
            self.ImageReadyEvent.wait()


    # Define a callback for when the http stream is closed.
    def _CustomBodyStreamClosed(self) -> None:
        # It's important this is called so the stream will be detached!
        QuickCam.Get().DetachImageStreamCallback(self._NewImageCallback)
