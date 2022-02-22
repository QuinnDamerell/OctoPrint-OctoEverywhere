import time

from .octohttprequest import OctoHttpRequest

#
# The point of this class is to abstract the logic that needs to be done to reliably get a snapshot from many types of
# OctoPrint setups. The main entry point is GetSnapshot() which will try a number of ways to get a snapshot from whatever camera system is
# setup. This includes USB based cameras, external IP based cameras, and OctoPrint instances that don't have a snapshot URL defined.
#
class SnapshotHelper:

    # Logic for a static singleton
    _Instance = None

    @staticmethod
    def Init(logger, octoPrintSettingsObject):
        SnapshotHelper._Instance = SnapshotHelper(logger, octoPrintSettingsObject)

    @staticmethod
    def Get():
        return SnapshotHelper._Instance

    def __init__(self, logger, octoPrintSettingsObject):
        self.Logger = logger
        self.OctoPrintSettingsObject = octoPrintSettingsObject

    # Returns the snapshot URL from the settings. Can be null if there is no snapshot URL set in the settings!
    # This URL can be absolute or relative.
    def GetSnapshotUrl(self):
        if self.OctoPrintSettingsObject is not None :
            # This is the normal plugin case
            snapshotUrl = self.OctoPrintSettingsObject.global_get(["webcam", "snapshot"])
            if snapshotUrl is None:
                return None
            if len(snapshotUrl) == 0:
                return None
            return snapshotUrl
        else:
            # This is the dev case
            return "http://192.168.86.57/webcam/?action=snapshot"

    # Returns the mjpeg stream URL from the settings. Can be null if there is no URL set in the settings!
    # This URL can be absolute or relative.
    def GetMjpegStreamUrl(self):
        if self.OctoPrintSettingsObject is not None :
            # This is the normal plugin case
            streamUrl = self.OctoPrintSettingsObject.global_get(["webcam", "stream"])
            if streamUrl is None:
                return None
            if len(streamUrl) == 0:
                return None
            return streamUrl
        else:
            # This is the dev case
            return "http://192.168.86.57/webcam/?action=stream"

    # Given a set of request headers, this determins if this is a special Oracle call indicating it's a snapshot.
    def IsSnapshotOracleRequest(self, requestHeadersDict):
        # If this is a snapshot request from Oracle, this header will be set.
        # Otherwise this is a normal request or even a snapshot request, but not one we will help with from Oracle.
        return "oe-snapshot" in requestHeadersDict

    # Called by the OctoWebStreamHelper when a Oracle snapshot request is detected.
    # It's important that this function returns a OctoHttpRequest that's very similar to what the default MakeHttpCall function
    # returns, to ensure the rest of the octostream http logic can handle the response.
    def MakeHttpCall(self, httpInitialContext, method, sendHeaders, uploadBuffer):
        # Instead of using any of the call context, (like the snapshot URL), we are just going to use GetSnapshot
        # which will use the latest OctoPrint settings.
        return self.GetSnapshot()

    # Tries to get a snapshot from the system using the snapshot URL or falling back to the mjpeg stream.
    # Returns a OctoHttpResult on success and None on failure.
    #
    # On failure, this returns None
    # On success, this will return a valid OctoHttpRequest that's fully filled out. It most likely will also set the FullBodyBuffer
    # variable in the OctoHttpRequest class.
    def GetSnapshot(self):
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
        streamUrl = self.GetMjpegStreamUrl()
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
            # Clear all of the curenet
            response.headers.clear()
            # Set the content type to the header we got from the stream chunk.
            response.headers["content-type"] = contentType
            # It's very important this size matches the body buffer we give OctoHttpRequest, or the logic in the http loop will fail because it will keep trying to read more.
            response.headers["content-length"] = str(len(imageBuffer))
            # Return a result. Return the full image buffer which will be used as the response body.
            return OctoHttpRequest.Result(response, url, True, imageBuffer)

        except Exception as e:
            self.Logger.info("Failed to get fallback snapshot. " + str(e))

        return None
