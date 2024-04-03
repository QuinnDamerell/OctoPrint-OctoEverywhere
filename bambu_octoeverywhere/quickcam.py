import logging
import threading
import subprocess
import selectors
import struct
import ssl
import socket
import time
import os
import signal

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
            # Get the access code and the host name.
            accessCode = self.Config.GetStr(Config.SectionBambu, Config.BambuAccessToken, None)
            ipOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
            if accessCode is None or ipOrHostname is None:
                raise Exception("QuickCam doesn't have a access code or ip to use.")

            # TODO - Right now it seems the X1 doesn't send back version info on start or with the version command
            # So we use the existence of the RTSP URL to determine what we can do.
            # Ideally we would use the printer version in the future.
            rtspUrl = None
            verAttempt = 0
            while True:
                verAttempt += 1
                state = BambuClient.Get().GetState()
                # Wait until the object exists
                if state is not None:
                    rtspUrl = state.rtsp_url
                    break
                # If we can't get it return, and then the quick cam thread will be started again
                # When there's another request.
                if verAttempt > 5:
                    self.Logger.warn(f"QuickCam wasn't able to get the printer state after {verAttempt} attempts")
                    return
                # Sleep for a bit.
                time.sleep(2.0)

            # We allow a few attempts, so if there are any connection issues or errors we buffer them out.
            attempts = 0
            while attempts < 5:
                attempts += 1

                # Create the camera implementation we need for this device.
                camImpl = None
                # Since we have to use the URL....
                #     IF the URL is empty, it's an X1 with LAN streaming disabled.
                #     If the URL has an address, it's an X1 with LAN streaming.
                #     If it's None, it's a P1, A1, or another printer with no RTSP.
                if rtspUrl is not None:
                    self.Logger.debug(f"Bambu RTSP URL is: `{rtspUrl}`")
                    camImpl = QuickCam_RTSP(self.Logger)
                else:
                    # Default to the websocket impl, since it's used on the most printers.
                    camImpl = QuickCam_WebSocket(self.Logger)

                # Wrap the usage into a with, so the connection is always cleaned up
                with camImpl:
                    try:
                        # Connect to the server.
                        camImpl.Connect(ipOrHostname, accessCode)

                        # Begin the capture loop.
                        while True:
                            # Get the next image buffer.
                            # This can return None, which means we should just check the time and spin.
                            img = camImpl.GetImage()

                            # Check if we are done running, if so, leave
                            if time.time() - self.LastImageRequestTimeSec > QuickCam.c_CaptureThreadTimeoutSec:
                                # TODO - For now, we don't stop the webcam loop while the printer is printing.
                                # This allows for notifications, Gadget, snapshots, streams, and such to load super easily.
                                # We need to measure the load on this though.
                                state = BambuClient.Get().GetState()
                                if state is None or not state.IsPrinting(True):
                                    # This will invoke the finally clause and leave.
                                    return

                            # Set the image if we got one.
                            if img is not None:
                                self._SetNewImage(img)

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


# Implements the websocket camera version for the P1 and A1 series printers.
class QuickCam_WebSocket:

    def __init__(self, logger:logging.Logger):
        self.Logger = logger
        self.Socket = None
        self.SslSocket = None

        # Image getting stuff
        self.ImageBuffer = bytearray()
        self.ExpectedImageSize = 0
        self.JpegStartSequence = bytearray([0xff, 0xd8, 0xff, 0xe0])
        self.JpegEndSequence = bytearray([0xff, 0xd9])


    # ~~ Interface Function ~~
    # Connects to the server.
    # This will throw an exception if it fails.
    def Connect(self, ipOrHostname:str, accessCode:str) -> None:
        # Build the auth packet
        authData = bytearray()
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

        # Create the socket connect and wrap it in SSL.
        self.Socket = socket.create_connection((ipOrHostname, 6000), 5.0)
        self.SslSocket = ctx.wrap_socket(self.Socket, server_hostname=ipOrHostname)

        # Send the auth packet
        self.SslSocket.write(authData)


    # ~~ Interface Function ~~
    # Gets an image from the server. This should block until an image is ready.
    # This can return None to indicate there's no image but the connection is still good, this allows the host to check if we should still be running.
    # To indicate connection is closed or needs to be closed, this should throw.
    def GetImage(self) -> bytearray:
        # Read from the socket
        while True:
            # We have seen this receive fail with SSLWantReadError when the socket if valid and there's more to read. In that case, keep the current socket going and try again.
            data = None
            try:
                # We will read either the 16 byte header that starts every image or we will read the remainder of the current image.
                readSize = 16 if self.ExpectedImageSize == 0 else self.ExpectedImageSize - len(self.ImageBuffer)
                data = self.SslSocket.recv(readSize)
            except ssl.SSLWantReadError:
                time.sleep(1)
                continue

            # If the expected image size is 0, then this is the first read of 16 bytes for the header.
            if self.ExpectedImageSize == 0:
                if len(data) != 16:
                    raise Exception("QuickCam capture thread got a first payload that was longer than 16.")
                self.ExpectedImageSize = int.from_bytes(data[0:3], byteorder='little')
            # Otherwise, we are building an image
            else:
                # Always add to the current buffer.
                self.ImageBuffer += data

                # Check if the image is done.
                if len(self.ImageBuffer) == self.ExpectedImageSize:
                    # We have the full image. Sanity check the jpeg start and end bytes exist.
                    if self.ImageBuffer[:4] != self.JpegStartSequence:
                        raise Exception("QuickCam got an image of the expected size, but we failed to find the jpeg start sequence.")
                    elif self.ImageBuffer[-2:] != self.JpegEndSequence:
                        raise Exception("QuickCam got an image of the expected size, but we failed to find the jpeg end sequence.")
                    self.ExpectedImageSize = 0
                    temp = self.ImageBuffer
                    self.ImageBuffer = bytearray()
                    return temp
                # Sanity check we didn't get misaligned from the stream.
                elif len(self.ImageBuffer) > self.ExpectedImageSize:
                    raise Exception(f"QuickCam was building an image expected to be {self.ExpectedImageSize} but ended up with a buffer that was {self.ImageBuffer}")


    # Allows us to using the with: scope.
    def __enter__(self):
        return self


    # Allows us to using the with: scope.
    # Must not throw!
    def __exit__(self, t, v, tb):
        # Close in the opposite order they were opened.
        try:
            if self.SslSocket is not None:
                self.SslSocket.__exit__(t, v, tb)
        except Exception:
            pass
        try:
            if self.Socket is not None:
                self.Socket.__exit__(t, v, tb)
        except Exception:
            pass


# Implements the websocket camera version for the X1 series printers.
class QuickCam_RTSP:

    # How long we will wait for data on each read before timing out.
    c_ReadTimeoutSec = 5.0

    # Adds a ton of logging useful for debugging.
    c_DebugLogging = False


    def __init__(self, logger:logging.Logger):
        self.Logger = logger
        self.Process:subprocess.Popen = None

        # Image getting stuff
        self.Buffer = bytearray()
        self.SearchedIndex = 0
        self.JpegStartSequence = bytearray([0xff, 0xd8, 0xff, 0xfe, 0x00, 0x10])
        self.JpegStartSequenceLen = len(self.JpegStartSequence)
        self.JpegEndSequence = bytearray([0xff, 0xd9])
        self.PipeSelect = selectors.DefaultSelector()
        self.TimeSinceLastImg = time.time()

        # Std Error logic
        self.StdErrBuffer = ""
        self.ErrorReaderThread:threading.Thread = None
        self.ErrorReaderThreadRunning = True


    # ~~ Interface Function ~~
    # Connects to the server.
    # This will throw an exception if it fails.
    def Connect(self, ipOrHostname:str, accessCode:str) -> None:
        # TODO get the address from the bambu state object

        # We set the logging level of ffmpeg depending on our logging level
        # The logs are written to stderr even if they aren't errors, which is nice, so
        # we can capture them on timeouts.
        logLevel = "trace" if self.Logger.isEnabledFor(logging.DEBUG) else "warning"

        # Notes
        #   We use 15 fps because it's a good trade off of fps and cpu perf hits
        #      It also decreases the bandwidth needed, which helps on mobile
        #   We use the default jpeg image quality, for the same reasons above.
        # pylint: disable=consider-using-with # We handle this on our own.
        self.Process = subprocess.Popen(["ffmpeg",
                    "-hide_banner",
                    "-y",
                    "-loglevel", logLevel,
                    "-rtsp_transport", "udp",
                    "-use_wallclock_as_timestamps", "1",
                    "-i", f"rtsps://bblp:{accessCode}@{ipOrHostname}:322/streaming/live/1",
                    "-filter:v", "fps=15",
                    "-movflags", "+faststart",
                    "-f", "image2pipe", "-"
                    ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # pylint: disable=no-member # Linux only
        os.set_blocking(self.Process.stdout.fileno(), False)
        os.set_blocking(self.Process.stderr.fileno(), False)
        self.PipeSelect.register(self.Process.stdout, selectors.EVENT_READ)

        # Since we setup the stderr pipe, we must read from it. If it fills it's buffer it will block the ffmpeg process.
        self.ErrorReaderThread = threading.Thread(target=self._ErrorReader)
        self.ErrorReaderThread.start()

        if QuickCam_RTSP.c_DebugLogging:
            self.Logger.debug("Ffmpeg process started.")


    # ~~ Interface Function ~~
    # Gets an image from the server. This should block until an image is ready.
    # This can return None to indicate there's no image but the connection is still good, this allows the host to check if we should still be running.
    # To indicate connection is closed or needs to be closed, this should throw.
    def GetImage(self) -> bytearray:
        while True:
            # Wait on the pipe, which will signal us when there's data to be read.
            # We timeout after 5 seconds, which is plenty of time for the stream to be ready.
            self.PipeSelect.select(QuickCam_RTSP.c_ReadTimeoutSec)

            # Read all of the data we can.
            buffer = self.Process.stdout.read(100000000)

            # Check for a timeout. This can happen because the select timeout, or it's been too long since we got an image parsed.
            # This usually means that ffmpeg has died or is not running correctly.
            if self.Process.returncode is not None or (time.time() - self.TimeSinceLastImg) > QuickCam_RTSP.c_ReadTimeoutSec:
                if self.StdErrBuffer is None or len(self.StdErrBuffer) == 0:
                    self.StdErrBuffer = "<None>"
                raise Exception(f"Ffmpeg read timeout. ffmpeg output:\n{self.StdErrBuffer}")

            # If we get an empty buffer, we just need to wait for more.
            if buffer is None or len(buffer) == 0:
                if QuickCam_RTSP.c_DebugLogging:
                    self.Logger.debug("RTSP read empty buffer from stdin.")
                continue

            # If there's no pending buffered data do a quick exit if we were able to read the entire buffer in our first read.
            # If the full buffer is only one jpeg images, we don't need to do any scanning and can just return it.
            if self.Buffer is None:
                if self._CheckIfFullJpeg(buffer):
                    self._ResetLocalBuffer(True)
                    if QuickCam_RTSP.c_DebugLogging:
                        self.Logger.debug("RTSP fast path image received.")
                    return buffer

            # Append this buffer to the current pending buffer.
            if self.Buffer is None:
                self.Buffer = buffer
            else:
                self.Buffer += buffer

            # Ensure the buffer is long enough to check.
            buffLen = len(self.Buffer)
            if buffLen <= self.JpegStartSequenceLen:
                continue

            # Check if the buffer is a full image now
            if self._CheckIfFullJpeg(self.Buffer):
                img = self.Buffer
                self._ResetLocalBuffer(True)
                if QuickCam_RTSP.c_DebugLogging:
                    self.Logger.debug("RTSP second quick path image received.")
                return img

            # Scan the buffer for the jpeg end sequence.
            newImageStart = -1
            while self.SearchedIndex < buffLen - self.JpegStartSequenceLen:
                if self.Buffer[self.SearchedIndex] == self.JpegEndSequence[0] and self.Buffer[self.SearchedIndex+1] == self.JpegEndSequence[1]:
                    newImageStart = self.SearchedIndex + 2
                    break
                self.SearchedIndex += 1

            # See if we found a complete image.
            if newImageStart != -1:
                # Get the image and check it's a full image.
                imgBuffer = self.Buffer[:newImageStart]
                if self._CheckIfFullJpeg(imgBuffer) is False:
                    # If we don't have a correct buffer, we got off in out counting.
                    # So reset the buffer and continue. Note after we reset the buffer, we might
                    # hit this, since we could have a partial image in the Buffer
                    self._ResetLocalBuffer()
                    if QuickCam_RTSP.c_DebugLogging:
                        self.Logger.debug("RTSP we found a jpeg end sequence, but the buffer didn't start with a jpeg start sequence.")
                    continue
                # Take the image off the buffer.
                self.Buffer = self.Buffer[newImageStart:]
                self.SearchedIndex = 0
                self.TimeSinceLastImg = time.time()
                # Ensure the buffer isn't too long.
                self._ResetLocalBufferIfOverLimit()
                if QuickCam_RTSP.c_DebugLogging:
                    self.Logger.debug("RTSP slow path image received.")
                return imgBuffer

            # If we didn't find anything, check the limit.
            if QuickCam_RTSP.c_DebugLogging:
                self.Logger.debug("We got a new buffer with no image match.")
            self._ResetLocalBufferIfOverLimit()


    def _ResetLocalBufferIfOverLimit(self):
        # A normal image is around 37,000, so if the buffer is too long, reset it so
        # we can try to recover the buffer.
        if self.Buffer is not None and len(self.Buffer) > 50000:
            self.Logger.info("Quick cam rtsp buffer reset. This means we are running behind.")
            self._ResetLocalBuffer()


    def _ResetLocalBuffer(self, hasNewImage:bool = False):
        self.SearchedIndex = 0
        self.Buffer = None
        if hasNewImage:
            self.TimeSinceLastImg = time.time()


    # Reads the error stream from ffmpeg.
    # Since we pipe the images via stdout, stderr will have all of the logs, not just errors.
    def _ErrorReader(self):
        while self.ErrorReaderThreadRunning:
            try:
                # Use a selector, so we only wake up when there's data to be read.
                with selectors.DefaultSelector() as selector:
                    selector.register(self.Process.stderr, selectors.EVENT_READ)
                    while self.ErrorReaderThreadRunning:
                        # Wait for data to be read.
                        # We can wait on this forever, because when the processes closes, the pipe will close, and that will release the select call.
                        selector.select()

                        # Check that we aren't shutting down.
                        if self.ErrorReaderThreadRunning is False:
                            return

                        # Read the data.
                        buffer = self.Process.stderr.read(10000)
                        if buffer is not None and len(buffer) > 0:
                            # Append to the log
                            self.StdErrBuffer += buffer.decode("utf-8")
                            # Have some sanity limit
                            if len(self.StdErrBuffer) > 100000:
                                self.StdErrBuffer = self.StdErrBuffer[-100000:]

            except Exception as e:
                Sentry.Exception("RTSP error reader thread failed.", e)


    # Checks if the buffer is only one image, from start to end.
    def _CheckIfFullJpeg(self, buffer:bytearray) -> bool:
        if buffer is None or len(buffer) <= self.JpegStartSequenceLen:
            return False
        if buffer[:self.JpegStartSequenceLen] != self.JpegStartSequence:
            return False
        if buffer[-2:] != self.JpegEndSequence:
            return False
        return True


    # Allows us to using the with: scope.
    def __enter__(self):
        return self


    # Allows us to using the with: scope.
    # Must not throw!
    def __exit__(self, t, v, tb):
        # Close the error reader thread.
        # Killing the process will cause the error reader thread to exit.
        self.ErrorReaderThreadRunning = False

        # First, we want to try to gracefully kill ffmpeg. That way it has time to tell the rtsp server it's
        # going away and clean up.
        try:
            if self.Process is not None:
                # Send sig int to emulate a ctl+c
                self.Process.send_signal(signal.SIGINT)

                # Use communicate which will wait for the process to end and read it's final output.
                # We also try to issue the q terminal command to exit, just incase the ffmpeg needs it.
                # Give ffmpeg a good amount of time to exit, so ideally it gracefully exits. (usually this is really quick)
                _, stderr =self.Process.communicate("q\r\n".encode("utf-8"), timeout=10.0)

                # Report what happened.
                # For some reason communicate will eat the output instead of it being sent to our reader above, so we just print it here as well.
                if stderr is None:
                    stderr = b""
                stderr = stderr.decode("utf-8")
                self.Logger.debug(f"ffmpeg gracefully killed. Remaining ffmpeg output:\n{stderr}")
        except Exception as e:
            self.Logger.warn(f"Exception when trying to gracefully kill ffmpeg. {e}")

        # Close in the opposite order they were opened.
        try:
            if self.PipeSelect is not None:
                self.PipeSelect.close()
        except Exception:
            pass

        # Ensure the process is killed
        try:
            if self.Process is not None:
                self.Process.kill()
        except Exception:
            pass

        # And then call exit to cleanup all of the pipes and process handles.
        try:
            if self.Process is not None:
                self.Process.__exit__(t, v, tb)
        except Exception:
            pass
