import time
import logging
import threading

from ..octohttprequest import OctoHttpRequest

# Stream Instance is a class that is created per web stream to handle streaming QuickCam images into the http stream.
class WebcamStreamInstance:

    # The string doesn't matter what it is, but we define it so it's consistent
    c_OeStreamBoundaryString = "oestreamboundary"


    def __init__(self, logger:logging.Logger, quickCam) -> None:
        self.Logger = logger
        self.QuickCam = quickCam
        self.IsFirstSend = True
        self.StreamOpenTimeSec = time.time()
        self.ImageReadyEvent = threading.Event()
        self.AwaitingImage:bytearray = None


    # This will attempt to start a stream of the webcam.
    # On success, it will return an OctoHttpRequest.Result object with a data callback setup.
    # On failure, it will return None.
    def StartWebRequest(self) -> OctoHttpRequest.Result:
        # First, try to get a snapshot. This will determine if we are able to get a stream or not.
        # If we can't start the stream, then we don't return success.
        # We will also use this first image to start the stream, to get it going ASAP.
        self.AwaitingImage = self.QuickCam.GetCurrentImage()
        if self.AwaitingImage is None:
            return None

        # Note! We must be sure to call DetachImageStreamCallback to remove this stream callback!
        self.QuickCam.AttachImageStreamCallback(self._NewImageCallback)

        # We must set the content type so that the web browser knows what kind of stream to expect.
        headers = {
            "content-type": f"multipart/x-mixed-replace; boundary={WebcamStreamInstance.c_OeStreamBoundaryString}",
        }

        # Return a result object with out callbacks setup for the stream body.
        return OctoHttpRequest.Result(200, headers, WebcamStreamInstance.c_OeStreamBoundaryString, False, customBodyStreamCallback=self._CustomBodyStreamRead, customBodyStreamClosedCallback=self._CustomBodyStreamClosed)


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
                header = f"--{WebcamStreamInstance.c_OeStreamBoundaryString}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(capturedImage)}\r\n\r\n"
                imageChunkBuffer = header.encode('utf-8') + capturedImage + b"\r\n" + header.encode('utf-8') + capturedImage + b"\r\n"

                # TODO - I don't know why, but chrome seems to delay the rendering of the image until it gets two?
                # This could be something in the pipeline not flushing correctly, or other things. But for now, on the first send we double the image to make it render instantly.
                if self.IsFirstSend:
                    imageChunkBuffer = imageChunkBuffer + imageChunkBuffer
                    self.IsFirstSend = False
                    if self.Logger.isEnabledFor(logging.DEBUG):
                        self.Logger.debug(f"QuickCam took {round(time.time()-self.StreamOpenTimeSec, 3)} seconds from octostream stream open to first image sent.")
                return imageChunkBuffer
            # If we didn't get an image, wait on the event for a new one.
            self.ImageReadyEvent.wait()


    # Define a callback for when the http stream is closed.
    def _CustomBodyStreamClosed(self) -> None:
        # It's important this is called so the stream will be detached!
        self.QuickCam.DetachImageStreamCallback(self._NewImageCallback)
