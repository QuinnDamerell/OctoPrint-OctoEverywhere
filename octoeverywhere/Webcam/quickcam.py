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

from enum import Enum

from octoeverywhere.sentry import Sentry

from .webcamutil import WebcamUtil
from ..octohttprequest import OctoHttpRequest
from .webcamsettingitem import WebcamSettingItem
from .webcamstreaminstance import WebcamStreamInstance


# Indicates the stream type for the QuickCam class.
# The NotSupported means that the URL parsed isn't supported by QuickCam.
class QuickCamStreamTypes(Enum):
    NotSupported = 0
    RTSP = 1
    WebSocket = 2
    JMPEG = 3

    # Makes to str() cast not to include the class name.
    def __str__(self):
        return self.name


# This is a helper class that manages active instances of QuickCam and allows all requests for the same URL to share a common stream.
class QuickCamManager:

    _Instance = None

    @staticmethod
    def Init(logger:logging.Logger, webcamPlatformHelperInterface):
        QuickCamManager._Instance = QuickCamManager(logger, webcamPlatformHelperInterface)


    @staticmethod
    def Get():
        return QuickCamManager._Instance


    def __init__(self, logger:logging.Logger, webcamPlatformHelperInterface) -> None:
        self.Logger = logger
        self.WebcamPlatformHelperInterface = webcamPlatformHelperInterface
        self.QuickCamMap = {}
        self.QuickCamMapLock = threading.Lock()


    # Given the webcam settings item, this will check if the settings item needs to use any of the supported QuickCam streaming capture methods.
    # On success, this will return a complete OctoHttpResult object, otherwise None
    def TryToGetSnapshot(self, webcamSettingsItem:WebcamSettingItem):
        # To know if we need to use Quick cam, we check the protocols.
        # We check both the snapshot and streaming URL, since we can get a snapshot from either
        url = webcamSettingsItem.SnapshotUrl
        t = QuickCam.GetStreamTypeFromUrl(url)
        if t == QuickCamStreamTypes.NotSupported:
            url = webcamSettingsItem.StreamUrl
            t = QuickCam.GetStreamTypeFromUrl(url)
            if t == QuickCamStreamTypes.NotSupported:
                return None

        # If we got here, then this is a URL type we need to use QuickCam for.
        # Get the QuickCam instance for this URL.
        qc = self._GetOrCreate(url)

        # Try to get a snapshot.
        img = qc.GetCurrentImage()
        if img is None:
            # If we can't get an image from a quick cam URL, no future system will be able to.
            # All of these URLs have prefixes like rtsp, ws, or jmpeg, so we can't get a snapshot from them.
            # So, don't return none, just return a failed http response.
            return OctoHttpRequest.Result.Error(404, url)

        # If we get an image, return it!
        headers = {
            "Content-Type": "image/jpeg"
        }
        return OctoHttpRequest.Result(200, headers, url, False, fullBodyBuffer=img)


    # Given the webcam settings item, this will check if the settings item needs to use any of the supported QuickCam streaming capture methods.
    # On failure, return None
    # On success, this will return a valid OctoHttpRequest that's fully filled out.
    # This must return an OctoHttpRequest object with a custom body read stream.
    def TryGetStream(self, webcamSettingsItem:WebcamSettingItem):
        # To know if we need to use Quick cam, we check the protocols.
        # We check both the snapshot and streaming URL, since we can get a snapshot from either
        url = webcamSettingsItem.StreamUrl
        t = QuickCam.GetStreamTypeFromUrl(url)
        if t == QuickCamStreamTypes.NotSupported:
            return None

        # If we got here, then this is a url type we need to use QuickCam for.
        # Get the QuickCam instance for this URL.
        qc = self._GetOrCreate(url)

        # We must create a new instance of this class per stream to ensure all of the vars stay in it's context and the streams are cleaned up properly.
        # Create the stream instance and start the web request.
        sm = WebcamStreamInstance(self.Logger, qc)
        return sm.StartWebRequest()


    # Returns a QuickCam instances for this url. If auth is required, the auth should be added to the URL in the http:// style. Ex rtsp://username:password@hostname...
    # The QuickCam class will be shared across multiple instances, it's thread safe.
    def _GetOrCreate(self, url:str):

        # We need to be careful with the URL to make sure it doesn't have any cache busting query params.
        # But we do need to support query params for things like /webcam/?action=stream
        queryParamsStart = url.find("?")
        if queryParamsStart != -1:
            # Remove all of the query params to start.
            ogUrl = url
            url = url[:queryParamsStart]

            # We decided to do an opt-in for query params, so we will only remove them if they are not in the opt-in list.
            # This is because some printers use query params for things like /webcam/?action=stream
            # We don't want to remove those, since they are part of the URL.
            actionIndex = ogUrl.find("action=")
            if actionIndex != -1:
                actionEnd = ogUrl.find("&", queryParamsStart)
                if actionEnd == -1:
                    actionEnd = len(ogUrl)
                url += "?" + ogUrl[queryParamsStart:actionEnd]

        with self.QuickCamMapLock:
            # If it already exists, get it and return it.
            qc = self.QuickCamMap.get(url, None)
            if qc is not None:
                return qc

            # Otherwise create it.
            qc = QuickCam(self.Logger, url, self.WebcamPlatformHelperInterface)
            self.QuickCamMap[url] = qc
            return qc


# This class handles webcam streaming from different streaming endpoints that aren't http.
# Right now it supports RTSP camera feeds and the Bambu Websocket based streaming protocol.
class QuickCam:

    # The protocol definition of our special jmpeg streams.
    # Any requests using this protocol will be handled by the QuickCam_Jmpeg class.
    JMPEGProtocol = "jmpeg://"
    JMPEGProtocolSecure = "jmpegs://"

    # The amount of time the capture thread will stay connected before it will close.
    # Whenever an image is accessed, the time is reset.
    c_CaptureThreadTimeoutSec = 60

    # How often the stall out monitor will check for a stall.
    c_StallMonitorThreadCheckIntervalSec = 5


    def __init__(self, logger:logging.Logger, url:str, webcamPlatformHelperInterface) -> None:
        self.Logger = logger
        self.WebcamPlatformHelperInterface = webcamPlatformHelperInterface
        self.Type = QuickCam.GetStreamTypeFromUrl(url)
        self.Url = url
        self.Lock = threading.Lock()
        self.ImageReady = threading.Event()
        self.IsCaptureThreadRunning = False
        self.CurrentImage:bytearray = None
        self.ImageCounter = 0 # Used to monitor stalls
        self.LastImageRequestTimeSec:float = 0.0
        self.ImageStreamCallbacks = []
        self.ImageStreamCallbackLock = threading.Lock()


    # Given a URL, this function returns the quick cam type that will be used and if it's supported.
    @staticmethod
    def GetStreamTypeFromUrl(url:str) -> QuickCamStreamTypes:
        # Ensure there's something to parse
        if url is None or len(url) == 0:
            return QuickCamStreamTypes.NotSupported
        url = url.lower()
        # Check if the URL is RTSP
        if url.startswith("rtsps://") or url.startswith("rtsp://"):
            return QuickCamStreamTypes.RTSP
        # If the URL is a websocket. We will assume it's the Bambu websocket protocol if so.
        if url.startswith("ws://") or url.startswith("wss://"):
            return QuickCamStreamTypes.WebSocket
        # This is a protocol handler we use internally to indicate a stream is a JMPEG stream.
        if url.startswith(QuickCam.JMPEGProtocol) or url.startswith(QuickCam.JMPEGProtocolSecure):
            return QuickCamStreamTypes.JMPEG
        return QuickCamStreamTypes.NotSupported


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
        # Add our callback to the list.
        with self.ImageStreamCallbackLock:
            self.ImageStreamCallbacks.append(callback)

        # Ensure that the capture thread is running.
        self._ensureCaptureThreadRunning()


    # Used to detach a new stream handler to receive callbacks when an image is ready.
    def DetachImageStreamCallback(self, callback):
        # Remove our callback.
        with self.ImageStreamCallbackLock:
            self.ImageStreamCallbacks.remove(callback)


    # Called when there's a new image from the capture thread.
    def _SetNewImage(self, img:bytearray) -> None:
        # Set the new image.
        self.CurrentImage = img
        self.ImageCounter += 1
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
            t = threading.Thread(target=self._captureThread, name=f"QuickCamCaptureThread-{self.Type}")
            t.daemon = True
            t.start()
            # We only use this thread for jmpeg right now, since it's the only one that can stall and we can do something about (for Elegoo)
            if self.Type == QuickCamStreamTypes.JMPEG:
                m = threading.Thread(target=self._stallMonitor, name=f"QuickCamCaptureThread-StallMonitor-{self.Type}")
                m.daemon = True
                m.start()


    # Does the image image capture work.
    def _captureThread(self):
        try:
            # We allow a few attempts, so if there are any connection issues or errors we buffer them out.
            # This is really helpful for ffmpeg and for the Elegoo OS webcam server, which can be flaky.
            attempts = 0
            while attempts < 5:
                attempts += 1

                # Some systems don't lke a single stream to be too long.
                # If this time is set, we will abort the connection after this amount of time.
                # But we will auto reconnect, so any clients streaming won't even know, beyond a small delay in the image stream.
                maxSingleStreamTimeSec = None

                # Create the camera implementation we need for this device.
                camImpl = None
                if self.Type == QuickCamStreamTypes.RTSP:
                    self.Logger.debug(f"QuickCam capture thread started for RTSP. {self.Url}")
                    camImpl = QuickCam_RTSP(self.Logger)
                elif self.Type == QuickCamStreamTypes.WebSocket:
                    self.Logger.debug(f"QuickCam capture thread started for Websocket. {self.Url}")
                    camImpl = QuickCam_WebSocket(self.Logger)
                elif self.Type == QuickCamStreamTypes.JMPEG:
                    self.Logger.debug(f"QuickCam capture thread started for JMPEG. {self.Url}")
                    camImpl = QuickCam_Jmpeg(self.Logger)
                    # The elegoo webcam server doesn't like us to stream too long, so set a short-ish max time
                    # remember the client streams will not be effected, there will only be a small gap in the stream images.
                    maxSingleStreamTimeSec = 60
                else:
                    self.Logger.error("Quick cam tried to start a capture thread with an unsupported type. "+self.Url)
                    return

                # Wrap the usage into a with, so the connection is always cleaned up
                with camImpl:
                    try:
                        # Tell the platform we are starting the stream.
                        self.WebcamPlatformHelperInterface.OnQuickCamStreamStart(self.Url)

                        # Connect to the server.
                        connectionStartSec = time.time()
                        camImpl.Connect(self.Url)

                        # Begin the capture loop.
                        while True:
                            # Get the next image buffer.
                            # This can return None, which means we should just check the time and spin.
                            img = camImpl.GetImage()

                            # Check if we are done running
                            if time.time() - self.LastImageRequestTimeSec > QuickCam.c_CaptureThreadTimeoutSec:
                                # We are past our max time between image requests, ask the platform if we should keep running or not.
                                # The decision is platform specific, but usually if a print is running we want to keep this stream alive for lower latency snapshots.
                                if self.WebcamPlatformHelperInterface.ShouldQuickCamStreamKeepRunning() is False:
                                    return
                                # If we don't want to be done, set the last image request time to now, so we don't constantly query the platform.
                                self.LastImageRequestTimeSec = time.time()

                            # Set the image if we got one.
                            if img is not None:
                                self._SetNewImage(img)
                                # If we got an image, we are connected, so reset the connection attempts.
                                attempts = 0

                            # Check if we have hit the max single connection limit, and if we need to reconnect.
                            if maxSingleStreamTimeSec is not None and time.time() - connectionStartSec > maxSingleStreamTimeSec:
                                self.Logger.debug("QuickCam capture thread hit the max single stream time. Ending this connect to start a new one...")
                                break

                    except Exception as e:
                        # We have seen times where random errors are returned, like on boot or if the stream is opened too soon after closing.
                        # This exception block is designed to eat any connection or buffer parsing errors, eat them, and try again.
                        self.Logger.warning("Exception in QuickCam capture thread. "+str(e))
                        time.sleep(1)
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


    def _stallMonitor(self):
        # Keep running while the capture thread is running.
        while self.IsCaptureThreadRunning:
            try:
                # We loop here so that we don't keep making the exception block
                # Set the last counter here so if something throws we still get current values.
                lastImageCounter = self.ImageCounter
                while self.IsCaptureThreadRunning:
                    # Sleep for a bit until we want to check for a stall.quic
                    time.sleep(QuickCam.c_StallMonitorThreadCheckIntervalSec)

                    # Ensure we are still running.
                    if self.IsCaptureThreadRunning is False:
                        return

                    # Check if the image counter hasn't changed.
                    if self.ImageCounter == lastImageCounter:
                        self.Logger.debug("QuickCam capture thread stalled.")
                        # Report the stall.
                        self.WebcamPlatformHelperInterface.OnQuickCamStreamStall(self.Url)

                    # Update the counter.
                    self.ImageCounter = lastImageCounter
            except Exception as e:
                Sentry.Exception("Exception in QuickCam stall monitor thread. ", e)


    # Given a URL in the protocol://username:password@example.com/ format, returns the username and password
    # This will always return the URL. If a username and password were found, the will be removed.
    # This will return the username and password if found, otherwise None
    @staticmethod
    def ParseOurUsernameAndPasswordFromUrlIfExists(url:str):
        # Parse the username and password from the URL if it exists.
        userName = None
        password = None
        if url.find("://") != -1:
            hostnameAndPath = url.split("://")[1]
            if hostnameAndPath.find("@") != -1:
                userNameAndPassword = hostnameAndPath.split("@")[0]
                if userNameAndPassword.find(":") -1:
                    userName, password = userNameAndPassword.split(":")
                else:
                    userName = userNameAndPassword
                # Remove the username and password from the URL
                protocolEnd = url.find("://") + 3
                atSign = url.find("@")
                url = url[:protocolEnd] + url[atSign+1:]
        return url, userName, password


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
    def Connect(self, url:str) -> None:

        # Parse the username and password from the URL if it exists.
        # This will always return the URL, striped of the username and password if found.
        (url, userName, password) = QuickCam.ParseOurUsernameAndPasswordFromUrlIfExists(url)

        # Build the auth packet if needed
        authData = None
        if userName is not None and password is not None:
            authData= bytearray()
            authData += struct.pack("<I", 0x40)
            authData += struct.pack("<I", 0x3000)
            authData += struct.pack("<I", 0)
            authData += struct.pack("<I", 0)
            for _, char in enumerate(userName):
                authData += struct.pack("<c", char.encode('ascii'))
            for _ in range(0, 32 - len(userName)):
                authData += struct.pack("<x")
            for _, char in enumerate(password):
                authData += struct.pack("<c", char.encode('ascii'))
            for _ in range(0, 32 - len(password)):
                authData += struct.pack("<x")

        # Setup the SSL context to not verify the cert or check the hostname.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # Parse just the hostname from the url.
        hostname = url
        port = 80
        if hostname.find("://") != -1:
            hostname = hostname.split("://")[1]
        # Remove any URL path
        if hostname.find("/") != -1:
            hostname = hostname.split("/")[0]
        # Finally, if there's a port, parse it.
        if hostname.find(":") != -1:
            hostname, port = hostname.split(":")
            port = int(port)

        # Create the socket connect and wrap it in SSL.
        self.Socket = socket.create_connection((hostname, port), 5.0)
        self.SslSocket = ctx.wrap_socket(self.Socket, server_hostname=hostname)

        # Send the auth packet
        if authData is not None:
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
                    raise Exception(f"QuickCam capture thread got a first payload that was not 16 bytes. len:{len(data)}, bytes:{data.hex()}")
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


# Implements the websocket camera for any jmpeg URL.
class QuickCam_Jmpeg:

    def __init__(self, logger:logging.Logger):
        self.Logger = logger
        self.OctoResult:OctoHttpRequest.Result = None
        self.IsFirstImagePull = True


    # ~~ Interface Function ~~
    # Connects to the server.
    # This will throw an exception if it fails.
    def Connect(self, url:str) -> None:
        # We don't need to worry about parsing a user name and password, because we will just keep it in the normal URL.
        # We support both http and https, so we will just replace the jmpeg:// with http:// or https://.
        url = url.replace("jmpeg://", "http://")
        url = url.replace("jmpegs://", "https://")
        self.OctoResult = OctoHttpRequest.MakeHttpCall(self.Logger, url, OctoHttpRequest.GetPathType(url), "GET", {}, allowRedirects=True)


    # ~~ Interface Function ~~
    # Gets an image from the server. This should block until an image is ready.
    # This can return None to indicate there's no image but the connection is still good, this allows the host to check if we should still be running.
    # To indicate connection is closed or needs to be closed, this should throw.
    def GetImage(self) -> bytearray:

        # Ensure we have a valid result
        if self.OctoResult is None:
            raise Exception("QuickCam_Jmpeg failed to make the http request.")
        if self.OctoResult.StatusCode != 200:
            raise Exception(f"QuickCam_Jmpeg failed to get a valid OctoHttpRequest result. Status code: {self.OctoResult.StatusCode}")

        # Try to get an image from the stream using the common logic.
        result = WebcamUtil.GetSnapshotFromStream(self.Logger, self.OctoResult, validateMultiStreamHeader=self.IsFirstImagePull)
        self.IsFirstImagePull = False
        if result is None:
            raise Exception("QuickCam_Jmpeg failed to get an image from the stream.")

        # We must use the ensure jpeg header info function to ensure the image is a valid jpeg.
        # We know, for example, the Elegoo OS webcam server doesn't send the jpeg header info properly.
        return WebcamUtil.EnsureJpegHeaderInfo(self.Logger, result.ImageBuffer)


    # Allows us to using the with: scope.
    def __enter__(self):
        return self


    # Allows us to using the with: scope.
    # Must not throw!
    def __exit__(self, t, v, tb):
        try:
            if self.OctoResult is not None:
                self.OctoResult.__exit__(t, v, tb)
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
    def Connect(self, url:str) -> None:

        # We set the logging level of ffmpeg depending on our logging level
        # The logs are written to stderr even if they aren't errors, which is nice, so
        # we can capture them on timeouts.
        logLevel = "trace" if self.Logger.isEnabledFor(logging.DEBUG) else "warning"

        # For FPS, we have found that we can stream and transcode the X1 rtsp stream at a smooth 15 fps on a Pi 4.
        # But for other RTSP streams like Wzye bridge cams, it's more intensive and we need to drop to 10 fps.
        # If we don't drop the FPS, the stream will fall behind.
        fps = 10
        if url.find("bblp:") != -1:
            fps = 15

        # For auth, if there's a username and password it will already be in the URL in the http:// basic auth style,
        # So there's nothing else we need to do.

        # Notes
        #   We use the default jpeg image quality, for the same FPS reasons above.
        # pylint: disable=consider-using-with # We handle this on our own.
        self.Process = subprocess.Popen(["ffmpeg",
                    "-hide_banner",
                    "-y",
                    "-loglevel", logLevel,
                    "-rtsp_transport", "0", # Use a value of 0, so both TCP and UDP can be used.
                    "-use_wallclock_as_timestamps", "1",
                    "-i", url,
                    "-filter:v", f"fps={fps}",
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
