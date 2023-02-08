import time

from .sentry import Sentry
from .octohttprequest import OctoHttpRequest

# A platform agnostic definition of a webcam stream.
#
# Note that this data structure must stay in sync with the service!
class WebcamSettingItem:

    # The snapshotUrl and streamUrl can be relative or absolute.
    #
    #  snapshotUrl OR streamUrl can be None if the values aren't available, but not both.
    #  flipHBool & flipVBool & rotationInt must exist.
    #  rotationInt must be 0, 90, 180, or 270
    def __init__(self, snapshotUrl, streamUrl, flipHBool, flipVBool, rotationInt) -> None:
        self.SnapshotUrl = snapshotUrl
        self.StreamUrl = streamUrl
        self.FlipH = flipHBool
        self.FlipV = flipVBool
        self.Rotation = rotationInt


# The point of this class is to abstract the logic that needs to be done to reliably get a webcam snapshot and stream from many types of
# printer setups. The main entry point is GetSnapshot() which will try a number of ways to get a snapshot from whatever camera system is
# setup. This includes USB based cameras, external IP based cameras, and OctoPrint instances that don't have a snapshot URL defined.
class WebcamHelper:

    # A header we apply to all snapshot and webcam streams so the client can get the correct transforms the user has setup.
    c_OeWebcamTransformHeaderKey = "x-oe-webcam-transform"

    # Logic for a static singleton
    _Instance = None


    @staticmethod
    def Init(logger, webcamPlatformHelperInterface):
        WebcamHelper._Instance = WebcamHelper(logger, webcamPlatformHelperInterface)


    @staticmethod
    def Get():
        return WebcamHelper._Instance


    def __init__(self, logger, webcamPlatformHelperInterface):
        self.Logger = logger
        self.WebcamPlatformHelperInterface = webcamPlatformHelperInterface


    # Returns the snapshot URL from the settings.
    # Can be None if there is no snapshot URL set in the settings!
    # This URL can be absolute or relative.
    def GetSnapshotUrl(self):
        obj = self._GetWebcamSettingObj(0)
        if obj is None:
            return None
        return obj.SnapshotUrl


    # Returns the mjpeg stream URL from the settings.
    # Can be None if there is no URL set in the settings!
    # This URL can be absolute or relative.
    def GetWebcamStreamUrl(self):
        obj = self._GetWebcamSettingObj(0)
        if obj is None:
            return None
        return obj.StreamUrl


    # Returns if flip H is set in the settings.
    def GetWebcamFlipH(self):
        obj = self._GetWebcamSettingObj(0)
        if obj is None:
            return None
        return obj.FlipH


    # Returns if flip V is set in the settings.
    def GetWebcamFlipV(self):
        obj = self._GetWebcamSettingObj(0)
        if obj is None:
            return None
        return obj.FlipV


    # Returns if rotate 90 is set in the settings.
    def GetWebcamRotation(self):
        obj = self._GetWebcamSettingObj(0)
        if obj is None:
            return None
        return obj.Rotation


    # Given a set of request headers, this determine if this is a special Oracle call indicating it's a snapshot or webcam stream.
    def IsSnapshotOrWebcamStreamOracleRequest(self, requestHeadersDict):
        return self.IsSnapshotOracleRequest(requestHeadersDict) or self.IsWebcamStreamOracleRequest(requestHeadersDict)


    # Check if the special header is set, indicating this is a snapshot request.
    def IsSnapshotOracleRequest(self, requestHeadersDict):
        return "oe-snapshot" in requestHeadersDict


    # Check if the special header is set, indicating this is a webcam stream request.
    def IsWebcamStreamOracleRequest(self, requestHeadersDict):
        return "oe-webcamstream" in requestHeadersDict


    # Called by the OctoWebStreamHelper when a Oracle snapshot or webcam stream request is detected.
    # It's important that this function returns a OctoHttpRequest that's very similar to what the default MakeHttpCall function
    # returns, to ensure the rest of the octostream http logic can handle the response.
    def MakeSnapshotOrWebcamStreamRequest(self, httpInitialContext, method, sendHeaders, uploadBuffer):
        if self.IsSnapshotOracleRequest(sendHeaders):
            return self.GetSnapshot()
        elif self.IsWebcamStreamOracleRequest(sendHeaders):
            return self.GetWebcamStream()
        else:
            raise Exception("Webcam helper MakeSnapshotOrWebcamStreamRequest was called but the request didn't have the oracle headers?")


    # Tries to get a webcam stream from the system using the webcam stream URL or falling back to the passed path.
    # Returns a OctoHttpResult on success and None on failure.
    #
    # On failure, this returns None. Returning None will fail out the request.
    # On success, this will return a valid OctoHttpRequest.
    def GetWebcamStream(self):
        # Wrap the entire result in the add transform function, so on success the header gets added.
        return self._AddOeWebcamTransformHeader(self._GetWebcamStreamInternal())


    def _GetWebcamStreamInternal(self):
        # Try to get the URL from the settings.
        webcamStreamUrl = self.GetWebcamStreamUrl()
        if webcamStreamUrl is not None:
            # Try to make a standard http call with this stream url
            # Use use this HTTP call helper system because it might be somewhat tricky to know
            # Where to actually make the webcam request in terms of IP and port.
            #
            # Whatever this returns, the rest of the request system will handle it, since it's expecting the OctoHttpRequest object
            return OctoHttpRequest.MakeHttpCall(self.Logger, webcamStreamUrl, OctoHttpRequest.GetPathType(webcamStreamUrl), "GET", {})

        # If we can't get the webcam stream URL, return None to fail out the request.
        return None


    # Tries to get a snapshot from the system using the snapshot URL or falling back to the mjpeg stream.
    # Returns a OctoHttpResult on success and None on failure.
    #
    # On failure, this returns None. Returning None will fail out the request.
    # On success, this will return a valid OctoHttpRequest that's fully filled out. It most likely will also set the FullBodyBuffer
    # variable in the OctoHttpRequest class. Note that if a requests result is returned, Stream=True was used and the body must be read
    # using: RequestsUtils.ReadAllContentFromStreamResponse()
    def GetSnapshot(self):
        # Wrap the entire result in the add transform function, so on success the header gets added.
        return self._AddOeWebcamTransformHeader(self._GetSnapshotInternal())


    def _GetSnapshotInternal(self):
        # First, try to get the snapshot using the string defined in settings.
        snapshotUrl = self.GetSnapshotUrl()
        if snapshotUrl is not None:
            # Try to make a standard http call with this snapshot url
            # Use use this HTTP call helper system because it might be somewhat tricky to know
            # Where to actually make the webcam request in terms of IP and port.
            octoHttpResult = OctoHttpRequest.MakeHttpCall(self.Logger, snapshotUrl, OctoHttpRequest.GetPathType(snapshotUrl), "GET", {})
            # If the result was successful, we are done.
            if octoHttpResult is not None and octoHttpResult.Result is not None and octoHttpResult.Result.status_code == 200:
                return octoHttpResult

        # If getting the snapshot from the snapshot URL fails, try getting a single frame from the mjpeg stream
        streamUrl = self.GetWebcamStreamUrl()
        if streamUrl is None:
            return None
        return self._GetSnapshotFromStream(streamUrl)


    def _GetSnapshotFromStream(self, url):
        try:
            # Try to connect the the mjpeg stream using the http helper class.
            # This is required because knowing the port to connect to might be tricky.
            octoHttpResult = OctoHttpRequest.MakeHttpCall(self.Logger, url, OctoHttpRequest.GetPathType(url), "GET", {})
            if octoHttpResult is None or octoHttpResult.Result is None:
                return None

            # Check for success.
            response = octoHttpResult.Result
            if response is None or response.status_code != 200:
                self.Logger.info("Snapshot fallback failed due to bad http call.")
                return None

            # We expect this to be a multipart stream if it's going to be a mjpeg stream.
            isMultipartStream = False
            contentTypeLower = ""
            for name in response.headers:
                nameLower = name.lower()
                if nameLower == "content-type":
                    contentTypeLower = response.headers[name].lower()
                    if contentTypeLower.startswith("multipart/"):
                        isMultipartStream = True
                    break

            # If this isn't a multipart stream, get out of here.
            if isMultipartStream is False:
                self.Logger.info("Snapshot fallback failed not correct content type: "+str(contentTypeLower))
                return None

            # Try to read some of the stream, so we can find the content type and the size of this first frame.
            dataBuffer = None
            for data in response.iter_content(chunk_size=300):
                # Skip keep alive
                if data:
                    dataBuffer = data
                    break
            if dataBuffer is None:
                self.Logger.info("Snapshot fallback failed no data returned.")
                return None

            # Decode the headers
            # Example --boundarydonotcross\r\nContent-Type: image/jpeg\r\nContent-Length: 48861\r\nX-Timestamp: 2122192.753042\r\n\r\n\x00!AVI1\x00\x01...
            headerStr = dataBuffer.decode(errors="ignore")

            # Find out how long the headers are. The \r\n\r\n sequence ends the headers.
            endOfAllHeadersMatch = "\r\n\r\n"
            endOfHeaderMatch = "\r\n"
            headerStrSize = headerStr.find(endOfAllHeadersMatch)
            if headerStrSize == -1:
                self.Logger.info("Snapshot fallback failed no end of headers found.")
                return None

            # Add 4 bytes for the \r\n\r\n end of header sequence.
            headerStrSize += 4

            # Try to find the size of this chunk.
            frameSizeInt = 0
            contentType = None
            headers = headerStr.split(endOfHeaderMatch)
            for header in headers:
                headerLower = header.lower()
                if headerLower.startswith("content-type"):
                    # We found the content-length header!
                    p = header.split(':')
                    if len(p) == 2:
                        contentType = p[1].strip()

                if headerLower.startswith("content-length"):
                    # We found the content-length header!
                    p = header.split(':')
                    if len(p) == 2:
                        frameSizeInt = int(p[1].strip())

            if frameSizeInt == 0 or contentType is None:
                self.Logger.info("Snapshot fallback failed to find frame size or content type.")
                return None

            # Read the entire first image into the buffer.
            totalDesiredBufferSize = frameSizeInt + headerStrSize
            toRead = totalDesiredBufferSize - len(dataBuffer)
            spinCount = 0
            if toRead > 0:
                # This API should either return toRead or it should throw, but we have this logic just incase.
                for data in response.iter_content(chunk_size=toRead):
                    # For loop sanity.
                    spinCount += 1
                    if spinCount > 100:
                        self.Logger.error("Snapshot fallback broke out of the data read loop.")
                        break

                    # Skip keep alive
                    if data:
                        dataBuffer += data
                        toRead = totalDesiredBufferSize - len(dataBuffer)
                        if len(dataBuffer) >= totalDesiredBufferSize:
                            break

                    # If we didn't get the full buffer sleep for a bit, so we don't spin in a tight loop.
                    time.sleep(0.01)

            # Since this is a stream, ideally we close it as soon as possible to not waste resources.
            # Otherwise this will be auto closed when the object is GCed, which happens really quickly after it
            # goes out of scope. Thus it's not a big deal if we early return and don't close it.
            try:
                response.close()
            except Exception:
                pass

            # If we got extra data trim it.
            # This shouldn't happen, but just incase the api changes.
            if len(dataBuffer) > totalDesiredBufferSize:
                dataBuffer = dataBuffer[:totalDesiredBufferSize]

            # Check we got what we wanted.
            if len(dataBuffer) != totalDesiredBufferSize:
                self.Logger.warn("Snapshot callback failed, the data read loop didn't produce the expected data size. desired: "+str(totalDesiredBufferSize)+", got: "+str(len(dataBuffer)))
                return None

            # Get only the jpeg buffer
            imageBuffer = dataBuffer[headerStrSize:]
            if len(imageBuffer) != frameSizeInt:
                self.Logger.warn("Snapshot callback final image size was not the frame size. expected: "+str(frameSizeInt)+", got: "+str(len(imageBuffer)))

            # If successful, we will use the already existing response object but update the values to match the fixed size body and content type.
            response.status_code = 200
            # Clear all of the current
            response.headers.clear()
            # Set the content type to the header we got from the stream chunk.
            response.headers["content-type"] = contentType
            # It's very important this size matches the body buffer we give OctoHttpRequest, or the logic in the http loop will fail because it will keep trying to read more.
            response.headers["content-length"] = str(len(imageBuffer))
            # Return a result. Return the full image buffer which will be used as the response body.
            return OctoHttpRequest.Result(response, url, True, imageBuffer)
        except ConnectionError as e:
            # We have a lot of telemetry indicating a read timeout can happen while trying to read from the stream
            # in that case we should just get out of here.
            if "Read timed out" in str(e):
                return None
            else:
                Sentry.Exception("Failed to get fallback snapshot due to ConnectionError", e)
        except Exception as e:
            Sentry.Exception("Failed to get fallback snapshot.", e)

        return None


    # Returns the default webcam setting object or None if there isn't one.
    def _GetWebcamSettingObj(self, index):
        try:
            a = self.WebcamPlatformHelperInterface.GetWebcamConfig()
            if a is None or len(a) < index:
                return None
            return a[index]
        except Exception as e:
            Sentry.Exception("WebcamHelper _GetWebcamSettingObj exception.", e)
        return None


    # Checks if the result was success and if so adds the common header.
    # Returns the octoHttpResult, so the function is chainable
    def _AddOeWebcamTransformHeader(self, octoHttpResult):
        if octoHttpResult is None or octoHttpResult.Result is None:
            return octoHttpResult

        # Default to none
        transformStr = "none"

        # If there are any settings build a string with them all contaminated.
        settings = self._GetWebcamSettingObj(0)
        if settings.FlipH or settings.FlipV or settings.Rotation != 0:
            transformStr = ""
            if settings.FlipH:
                transformStr += "fliph "
            if settings.FlipV:
                transformStr += "flipv "
            if settings.Rotation != 0:
                transformStr += "rotate="+str(settings.Rotation)+" "

        # Set the header
        octoHttpResult.Result.headers[WebcamHelper.c_OeWebcamTransformHeaderKey] = transformStr
        return octoHttpResult


    def GetDevAddress(self):
        return OctoHttpRequest.GetLocalhostAddress()+":"+str(OctoHttpRequest.GetLocalOctoPrintPort())
