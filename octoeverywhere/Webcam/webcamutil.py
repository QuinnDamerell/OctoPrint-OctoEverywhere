import logging

import urllib3

from ..sentry import Sentry
from ..octohttprequest import OctoHttpRequest

# A simple class to hold the result of a GetSnapshotFromStream call.
class GetSnapshotFromStreamResult:
    def __init__(self, imageBuffer:bytes, contentType:str):
        self.ImageBuffer = imageBuffer
        self.ContentType = contentType


# A class of common utilities for the webcam service.
class WebcamUtil:

    # This will try to read a single jpeg image from a jmpeg stream.
    # The OctoHttpResult should be checked for success before calling this function.
    # Note this WILL NOT close the response object, it's up to the caller to do that.
    # Returns None on failure.
    @staticmethod
    def GetSnapshotFromStream(logger:logging.Logger, result:OctoHttpRequest.Result, validateMultiStreamHeader:bool = True) -> GetSnapshotFromStreamResult:
        try:
            # Only validate if requested, so we don't have to do this constantly.
            if validateMultiStreamHeader:
                # We expect this to be a multipart stream if it's going to be a mjpeg stream.
                isMultipartStream = False
                contentTypeLower = ""
                headers = result.Headers
                for name in headers:
                    nameLower = name.lower()
                    if nameLower == "content-type":
                        contentTypeLower = headers[name].lower()
                        if contentTypeLower.startswith("multipart/"):
                            isMultipartStream = True
                        break

                # If this isn't a multipart stream, get out of here.
                if isMultipartStream is False:
                    logger.info("GetSnapshotFromStream - Failed, not correct content type: "+str(contentTypeLower))
                    return None

            # Ensure we have a response object to read from.
            responseForBodyRead = result.ResponseForBodyRead
            if responseForBodyRead is None:
                logger.warning("GetSnapshotFromStream - Failed, the result didn't have a requests lib Response object to read from.")
                return None

            # Try to read some of the stream, so we can find the content type and the size of this first frame.
            # We use the raw response, so we can control directly how much we read.
            dataBuffer = responseForBodyRead.raw.read(300)
            if dataBuffer is None:
                logger.info("GetSnapshotFromStream - Failed, no data returned.")
                return None

            # Decode the headers
            # Example --boundarydonotcross\r\nContent-Type: image/jpeg\r\nContent-Length: 48861\r\nX-Timestamp: 2122192.753042\r\n\r\n\x00!AVI1\x00\x01...
            #      or \r\n--boundarydonotcross\r\nContent-Type: image/jpeg\r\nContent-Length: 48861\r\nX-Timestamp: 2122192.753042\r\n\r\n\x00!AVI1\x00\x01...
            #      or boundarydonotcross\r\nContent-Type: image/jpeg\r\nContent-Length: 48861\r\nX-Timestamp: 2122192.753042\r\n\r\n\x00!AVI1\x00\x01...
            headerStr = dataBuffer.decode(errors="ignore")

            # Find out how long the headers are. The \r\n\r\n sequence ends the headers.
            endOfAllHeadersMatch = "\r\n\r\n"
            endOfHeaderMatch = "\r\n"
            headerStrSize = headerStr.find(endOfAllHeadersMatch)
            if headerStrSize == -1:
                logger.info("GetSnapshotFromStream - Failed, no end of headers found.")
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

                # Break when done.
                if frameSizeInt > 0 and contentType is not None:
                    break

            if frameSizeInt == 0 or contentType is None:
                if frameSizeInt == 0:
                    logger.info("GetSnapshotFromStream - Failed, failed to find frame size.")
                if contentType is None:
                    logger.info("GetSnapshotFromStream - Failed, failed to find the content type.")
                return None

            # Read the entire first image into the buffer.
            totalDesiredBufferSize = frameSizeInt + headerStrSize
            toRead = totalDesiredBufferSize - len(dataBuffer)
            if toRead > 0:
                data = responseForBodyRead.raw.read(toRead)
                if data is None:
                    logger.error("GetSnapshotFromStream - Failed, failed to read the rest of the image buffer.")
                    return None
                dataBuffer += data

            # If we got extra data trim it.
            # This shouldn't happen, but just incase the api changes.
            if len(dataBuffer) > totalDesiredBufferSize:
                dataBuffer = dataBuffer[:totalDesiredBufferSize]

            # Check we got what we wanted.
            if len(dataBuffer) != totalDesiredBufferSize:
                logger.warning("GetSnapshotFromStream - Failed, the data read loop didn't produce the expected data size. desired: "+str(totalDesiredBufferSize)+", got: "+str(len(dataBuffer)))
                return None

            # Get only the jpeg buffer
            imageBuffer = dataBuffer[headerStrSize:]
            if len(imageBuffer) != frameSizeInt:
                logger.warning("GetSnapshotFromStream - Failed, final image size was not the frame size. expected: "+str(frameSizeInt)+", got: "+str(len(imageBuffer)))

            # Success!
            return GetSnapshotFromStreamResult(imageBuffer, contentType)
        except Exception as e:
            if e is ConnectionError and "Read timed out" in str(e):
                logger.debug("GetSnapshotFromStream - Failed, got a timeout while reading the stream.")
            elif e is urllib3.exceptions.ProtocolError and "IncompleteRead" in str(e):
                logger.debug("GetSnapshotFromStream - Failed, got a incomplete read while reading the stream.")
            elif e is urllib3.exceptions.ReadTimeoutError and "Read timed out" in str(e):
                logger.debug("GetSnapshotFromStream - Failed, got a read timeout while reading stream.")
            else:
                Sentry.Exception("Failed to get fallback snapshot.", e)
        return None


    # Checks if the jpeg header info is set correctly.
    # For some webcam servers, we have seen them return jpegs that have incomplete jpeg binary header data, which breaks some image processing libs.
    # This seems to break ImageSharp, whatever Telegram uses on it's server side for processing, and even browsers from showing the image.
    # It also seems that the images returned by the Elegoo OS webcam server need this to render correctly in the browser.
    # To combat this, we will check if the image is a jpeg, and if so, ensure the header is set correctly.
    @staticmethod
    def EnsureJpegHeaderInfo(logger:logging.Logger, buf:bytes) -> bytes:

        # Handle the buffer.
        # In a nutshell, all jpeg images have a lot of header segments, but they must have the app0 header.
        # This header defines what sub type of image the jpeg is. It seems the app0 header is always there, but some
        # webcam servers don't set the identifier bits, which should be JFIF\0 or something like that.
        # We need to find the app0 header, and if we do, ensure the identifier is set.
        # This has to be efficient so it can run on low power hardware!
        try:
            # Check if this is a jpeg, it must start with FF D8
            bufLen = len(buf)
            if bufLen < 2 or buf[0] != 0xFF or buf[1] != 0xD8:
                return buf

            # Search the headers for the APP0
            pos = 2
            while pos < bufLen:
                # Ensure we have room and sanity check the buffer headers.
                if pos + 1 >= bufLen:
                    logger.warning("EnsureJpegHeaderInfo - Ran out of buffer before we found a jpeg APP0 header")
                    return buf
                if buf[pos] != 0xFF:
                    logger.error("EnsureJpegHeaderInfo - jpeg segment header didn't start with 0xff")
                    return buf

                # The first byte is always FF, so we only care about the second.
                segmentType = buf[pos+1]
                if segmentType == 0xDA:
                    # This is the start of the image, the headers are over.
                    logger.debug("EnsureJpegHeaderInfo - We found the start of the jpeg image before we found the APP0 header.")
                    return buf
                elif segmentType == 0xE0:
                    # This is the APP0 header.
                    # Skip past the segment header and size bytes
                    pos += 4

                    # If these next bytes aren't set, we will set them to the default of "JFIF\0"
                    # Note that the last byte should be 0!
                    needsChanges = buf[pos] == 0 or buf[pos+1] == 0 or buf[pos+2] == 0 or buf[pos+3] == 0 or buf[pos+4] == 0 or buf[pos+5] != 0

                    # If we don't need changes, we are done.
                    # No need to process more headers.
                    if needsChanges is False:
                        return buf

                    # To edit the byte array, we need a bytearray.
                    # This adds overhead, but it's required because bytes objects aren't editable.
                    bufArray = bytearray(buf)
                    if bufArray[pos] == 0:
                        bufArray[pos] = 0x4a # J
                    pos += 1
                    if bufArray[pos] == 0:
                        bufArray[pos] = 0x46 # F
                    pos += 1
                    if bufArray[pos] == 0:
                        bufArray[pos] = 0x49 # I
                    pos += 1
                    if bufArray[pos] == 0:
                        bufArray[pos] = 0x46 # F
                    pos += 1
                    # This should be 0.
                    if bufArray[pos] != 0:
                        bufArray[pos] = 0 # /0
                    pos += 1

                    # Done, No need to process more headers.
                    return bufArray
                else:
                    # This is some other header, skip it's length.
                    # First skip past the type two bytes.
                    pos += 2
                    # Now read the length, two bytes
                    segLen = (buf[pos] << 8) + buf[pos+1]
                    # Now skip past the segment length (the two length bytes are included in the length size)
                    pos += segLen

        except Exception as e:
            Sentry.Exception("WebcamUtil EnsureJpegHeaderInfo failed to handle jpeg buffer", e)
            # On a failure, return the original result, since it's still good.
            return buf

        # If we fall out of the while loop,
        logger.debug("EnsureJpegHeaderInfo excited the while loop without finding the app0 header.")
        return buf
