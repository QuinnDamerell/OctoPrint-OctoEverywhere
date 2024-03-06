import logging
import threading
import struct
import ssl
import socket
import time

from octoeverywhere.sentry import Sentry

from linux_host.config import Config

from .bambuclient import BambuClient

# The goal of this class is to handle webcam streaming and snapshots. The idea is since we need to establish a socket and stream to even get snapshots,
# rather than doing it over and over, we will keep the stream alive for a short period of time and take snapshots, so when the user wants them, they are ready.
class QuickCam:

    # The amount of time the capture thread will stay connected before it will close.
    # Whenever an image is accessed, the time is reset.
    c_CaptureThreadTimeoutSec = 60

    _Instance = None

    @staticmethod
    def Init(logger:logging.Logger, config:Config):
        QuickCam._Instance = QuickCam(logger, config)


    @staticmethod
    def Get():
        return QuickCam._Instance


    def __init__(self, logger:logging.Logger, config:Config ) -> None:
        self.Logger = logger
        self.Config = config
        self.Lock = threading.Lock()
        self.ImageReady = threading.Event()
        self.IsCaptureThreadRunning = False
        self.CurrentImage:bytearray = None
        self.LastImageRequestTimeSec:float = 0.0
        self.ImageStreamCallbacks = []
        self.ImageStreamCallbackLock = threading.Lock()


    # Tries to get the current image from the printer and returns it as a raw jpeg.
    # This will return None if it fails.
    def GetCurrentImage(self) -> bytearray:
        # Set the last time someone requested an image.
        self.LastImageRequestTimeSec = time.time()

        # If there is a current image, return it.
        img = self.CurrentImage
        if img is not None:
            return img

        # We will try to kick the thread twice, just incase it was in the middle of cleaning
        # up when we called _ensureCaptureThreadRunning the first time.
        kickAttempt = 0
        while kickAttempt < 2:
            kickAttempt += 1
            self._ensureCaptureThreadRunning()
            # For the timeout, we want to make it quite long. The reason is a lot of things depend on this snapshot
            # like Gadget, Notification images, the stream capture system, and more.
            # Some printers can take a long time to get the socket ready and working, so we want to give them
            # a lot of time. It's better to have a longer delay than get no snapshot.
            # Since we loop twice, this will be a 8 second delay max.
            self.ImageReady.wait(4)
            if self.CurrentImage is not None:
                return self.CurrentImage
        return self.CurrentImage


    # Used to attach a new stream handler to receive callbacks when an image is ready.
    # Note a call to detach must be called as well!
    def AttachImageStreamCallback(self, callback):
        with self.ImageStreamCallbackLock:
            self.ImageStreamCallbacks.append(callback)


    # Used to detach a new stream handler to receive callbacks when an image is ready.
    def DetachImageStreamCallback(self, callback):
        with self.ImageStreamCallbackLock:
            self.ImageStreamCallbacks.remove(callback)


    # Called when there's a new image from the capture thread.
    def _SetNewImage(self, img:bytearray) -> None:
        # Set the new image.
        self.CurrentImage = img
        # Release anyone waiting on it.
        self.ImageReady.set()
        # Fire the callbacks, if there are any.
        with self.ImageStreamCallbackLock:
            if len(self.ImageStreamCallbacks) > 0:
                # Update the last image request time to ensure the stream keeps going.
                self.LastImageRequestTimeSec = time.time()
                for callback in self.ImageStreamCallbacks:
                    callback(self.CurrentImage)


    # Call to make sure the capture thread is running.
    def _ensureCaptureThreadRunning(self):
        with self.Lock:
            if self.IsCaptureThreadRunning:
                return
            self.Logger.info("QuickCam capture thread starting.")
            self.IsCaptureThreadRunning = True
            t = threading.Thread(target=self._captureThread)
            t.daemon = True
            t.start()


    # Does the image image capture work.
    def _captureThread(self):
        try:
            authData = bytearray()
            accessCode = self.Config.GetStr(Config.SectionBambu, Config.BambuAccessToken, None)
            ipOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
            if accessCode is None or ipOrHostname is None:
                raise Exception("QuickCam doesn't have a access code or ip to use.")

            # Build the auth packet
            authData += struct.pack("<I", 0x40)
            authData += struct.pack("<I", 0x3000)
            authData += struct.pack("<I", 0)
            authData += struct.pack("<I", 0)
            name = "bblp"
            for _, char in enumerate(name):
                authData += struct.pack("<c", char.encode('ascii'))
            for _ in range(0, 32 - len(name)):
                authData += struct.pack("<x")
            for _, char in enumerate(accessCode):
                authData += struct.pack("<c", char.encode('ascii'))
            for _ in range(0, 32 - len(accessCode)):
                authData += struct.pack("<x")

            # Setup the SSL context to not verify the cert or check the hostname.
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            jpegStartSequence = bytearray([0xff, 0xd8, 0xff, 0xe0])
            jpegEndSequence = bytearray([0xff, 0xd9])

            # We allow a few attempts, so if there are any connection issues or errors we buffer them out.
            attempts = 0
            while attempts < 5:
                attempts += 1
                try:
                    # Create a socket, use a 5 second timeout.
                    with socket.create_connection((ipOrHostname, 6000), 5.0) as sock:
                        # Wrap the socket in a SSL socket
                        with ctx.wrap_socket(sock, server_hostname=ipOrHostname) as sslSock:
                            # Write the auth packet.
                            sslSock.write(authData)

                            expectedImageSize = 0
                            imgBuffer = bytearray()
                            # Read from the socket
                            while True:
                                # We have seen this receive fail with SSLWantReadError when the socket if valid and there's more to read. In that case, keep the current socket going and try again.
                                data = None
                                try:
                                    # We will read either the 16 byte header that starts every image or we will read the remainder of the current image.
                                    readSize = 16 if expectedImageSize == 0 else expectedImageSize - len(imgBuffer)
                                    data = sslSock.recv(readSize)
                                except ssl.SSLWantReadError:
                                    time.sleep(1)
                                    continue

                                # Check if we are done running, if so, leave
                                if time.time() - self.LastImageRequestTimeSec > QuickCam.c_CaptureThreadTimeoutSec:
                                    # TODO - For now, we don't stop the webcam loop while the printer is printing.
                                    # This allows for notifications, Gadget, snapshots, streams, and such to load super easily.
                                    # We need to measure the load on this though.
                                    state = BambuClient.Get().GetState()
                                    if state is None or not state.IsPrinting(True):
                                        # This will invoke the finally clause and leave.
                                        return

                                # If the expected image size is 0, then this is the first read of 16 bytes for the header.
                                if expectedImageSize == 0:
                                    if len(data) != 16:
                                        raise Exception("QuickCam capture thread got a first payload that was longer than 16.")
                                    expectedImageSize = int.from_bytes(data[0:3], byteorder='little')
                                # Otherwise, we are building an image
                                else:
                                    # Always add to the current buffer.
                                    imgBuffer += data

                                    # Check if the image is done.
                                    if len(imgBuffer) == expectedImageSize:
                                        # We have the full image. Sanity check the jpeg start and end bytes exist.
                                        if imgBuffer[:4] != jpegStartSequence:
                                            raise Exception("QuickCam got an image of the expected size, but we failed to find the jpeg start sequence.")
                                        elif imgBuffer[-2:] != jpegEndSequence:
                                            raise Exception("QuickCam got an image of the expected size, but we failed to find the jpeg end sequence.")
                                        self._SetNewImage(imgBuffer)
                                        expectedImageSize = 0
                                        imgBuffer = bytearray()

                                    # Sanity check we didn't get misaligned from the stream.
                                    elif len(imgBuffer) > expectedImageSize:
                                        raise Exception(f"QuickCam was building an image expected to be {expectedImageSize} but ended up with a buffer that was {imgBuffer}")

                except Exception as e:
                    # We have seen times where random errors are returned, like on boot or if the stream is opened too soon after closing.
                    # This exception block is designed to eat any connection or buffer parsing errors, eat them, and try again.
                    self.Logger.warn("Exception in QuickCam capture thread. "+str(e))
                    time.sleep(2)
        except Exception as e:
            Sentry.Exception("Exception in QuickCam capture thread. ", e)
        finally:
            # Before exit the thread...
            # Note that order is important here!
            # Ensure we clear the image ready event.
            self.ImageReady.clear()
            # Clear the flag that we are running.
            with self.Lock:
                self.IsCaptureThreadRunning = False
            # And ensure that the current image is cleaned up, so clients don't get a stale image.
            self.CurrentImage = None
            self.Logger.info("QuickCam capture thread exit.")
