import logging
from typing import Optional

from ..sentry import Sentry
from ..httpresult import HttpResult
from ..buffer import Buffer
from ..streamreadhelper import StreamReadHelper


# A simple class to hold the result of a GetSnapshotFromStream call.
class GetSnapshotFromStreamResult:
    def __init__(self, imageBuffer:Buffer, contentType:str):
        self.ImageBuffer = imageBuffer
        self.ContentType = contentType


# A result that includes what kind of jpeg header fix was needed, so callers that
# repeatedly process frames from the same camera can skip the full header scan.
class EnsureJpegHeaderInfoResult:
    def __init__(self, imageBuffer:Buffer, fixMode:int, app0IdentifierStart:int = 0):
        self.ImageBuffer = imageBuffer
        self.FixMode = fixMode
        self.App0IdentifierStart = app0IdentifierStart


# A class of common utilities for the webcam service.
class WebcamUtil:

    c_JpegHeaderFixModeUnknown = 0
    c_JpegHeaderFixModeNone = 1
    c_JpegHeaderFixModeSetApp0Identifier = 2
    c_JpegHeaderFixModeInsertApp0 = 3

    c_JfifApp0Header = bytes([
        0xFF, 0xE0, 0x00, 0x10,
        0x4A, 0x46, 0x49, 0x46, 0x00,
        0x01, 0x01, 0x00,
        0x00, 0x01, 0x00, 0x01,
        0x00, 0x00,
    ])

    # This will try to read a single jpeg image from a jmpeg stream.
    # The OctoHttpResult should be checked for success before calling this function.
    # Note this WILL NOT close the response object, it's up to the caller to do that.
    # Returns None on failure.
    @staticmethod
    def GetSnapshotFromStream(logger:logging.Logger, result:HttpResult, validateMultiStreamHeader:bool = True) -> Optional[GetSnapshotFromStreamResult]:
        try:
            # Only validate if requested, so we don't have to do this constantly.
            if validateMultiStreamHeader:
                # We expect this to be a multipart stream if it's going to be a mjpeg stream.
                # If this isn't a multipart stream, get out of here.
                contentTypeLower = result.Headers.get("content-type", "").lower()
                if contentTypeLower.startswith("multipart/") is False:
                    logger.info("GetSnapshotFromStream - Failed, not correct content type: "+str(contentTypeLower))
                    return None

            # Ensure we have a response object to read from.
            responseForBodyRead = result.ResponseForBodyRead
            if responseForBodyRead is None:
                logger.warning("GetSnapshotFromStream - Failed, the result didn't have a requests lib Response object to read from.")
                return None

            # Try to read some of the stream, so we can find the content type and the size of this first frame.
            # We use the raw response, so we can control directly how much we read.
            endOfAllHeadersMatch = b"\r\n\r\n"
            dataBuffer = responseForBodyRead.raw.read(300)
            if dataBuffer is None or len(dataBuffer) == 0:
                logger.info("GetSnapshotFromStream - Failed, no data returned.")
                return None
            while dataBuffer.find(endOfAllHeadersMatch) == -1 and len(dataBuffer) < 4096:
                moreData = responseForBodyRead.raw.read(300)
                if moreData is None or len(moreData) == 0:
                    break
                dataBuffer += moreData

            # Example --boundarydonotcross\r\nContent-Type: image/jpeg\r\nContent-Length: 48861\r\nX-Timestamp: 2122192.753042\r\n\r\n\x00!AVI1\x00\x01...
            #      or \r\n--boundarydonotcross\r\nContent-Type: image/jpeg\r\nContent-Length: 48861\r\nX-Timestamp: 2122192.753042\r\n\r\n\x00!AVI1\x00\x01...
            #      or boundarydonotcross\r\nContent-Type: image/jpeg\r\nContent-Length: 48861\r\nX-Timestamp: 2122192.753042\r\n\r\n\x00!AVI1\x00\x01...
            # Find out how long the headers are. The \r\n\r\n sequence ends the headers.
            headerStrSize = dataBuffer.find(endOfAllHeadersMatch)
            if headerStrSize == -1:
                logger.info("GetSnapshotFromStream - Failed, no end of headers found.")
                return None

            # Add 4 bytes for the \r\n\r\n end of header sequence.
            headerStrSize += 4

            # Try to find the size of this chunk.
            frameSizeInt = 0
            contentType = None
            headers = dataBuffer[0:headerStrSize].split(b"\r\n")
            for header in headers:
                name, _, value = header.partition(b":")
                nameLower = name.strip().lower()
                if nameLower == b"content-type":
                    contentType = value.strip().decode(errors="ignore")

                elif nameLower == b"content-length":
                    # We found the content-length header!
                    # In some webcam servers, they add the content break --<boundary> to the content-length line.
                    # So we need to strip that off if it's there.
                    value = value.strip()
                    boundaryStart = value.find(b"--")
                    if boundaryStart != -1:
                        value = value[0:boundaryStart].strip()
                    # We have seen weird cases where there's a content-length header, but it's empty, and then there's another that has a length.
                    if len(value) == 0:
                        continue
                    frameSizeInt = int(value)

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
            # We avoid buffering the multipart headers with the image, which saves a large copy per frame.
            imageBuffer = bytearray(frameSizeInt)
            imageBytesRead = len(dataBuffer) - headerStrSize
            if imageBytesRead > 0:
                imageBytesRead = min(imageBytesRead, frameSizeInt)
                imageBuffer[0:imageBytesRead] = dataBuffer[headerStrSize:headerStrSize + imageBytesRead]
            useReadInto = StreamReadHelper.CanTryReadInto(responseForBodyRead.raw)
            bytesRead, useReadInto = StreamReadHelper.ReadIntoByteArrayFull(responseForBodyRead.raw, imageBuffer, imageBytesRead, frameSizeInt - imageBytesRead, useReadInto)
            if bytesRead < frameSizeInt:
                logger.error("GetSnapshotFromStream - Failed, failed to read the rest of the image buffer. bytesRead: %d, expected: %d", bytesRead, frameSizeInt)
                return None

            # Success!
            return GetSnapshotFromStreamResult(Buffer(imageBuffer), contentType)
        except Exception as e:
            if Sentry.IsCommonHttpError(e):
                logger.debug("GetSnapshotFromStream - Failed, got a common http error while reading the stream. %s", e)
            else:
                Sentry.OnException("Failed to get fallback snapshot.", e)
        return None


    # Checks if the jpeg header info is set correctly.
    # For some webcam servers, we have seen them return jpegs that have incomplete jpeg binary header data, which breaks some image processing libs.
    # This seems to break ImageSharp, whatever Telegram uses on it's server side for processing, and even browsers from showing the image.
    # It also seems that the images returned by the Elegoo OS webcam server need this to render correctly in the browser.
    # To combat this, we will check if the image is a jpeg, and if so, ensure the header is set correctly.
    @staticmethod
    def EnsureJpegHeaderInfo(logger:logging.Logger, buf:Buffer) -> Buffer:
        return WebcamUtil.EnsureJpegHeaderInfoWithDetails(logger, buf).ImageBuffer


    @staticmethod
    def EnsureJpegHeaderInfoWithDetails(logger:logging.Logger, buf:Buffer) -> EnsureJpegHeaderInfoResult:

        # Handle the buffer.
        # In a nutshell, all jpeg images have a lot of header segments, but they must have the app0 header.
        # This header defines what sub type of image the jpeg is. It seems the app0 header is always there, but some
        # webcam servers don't set the identifier bits, which should be JFIF\0 or something like that.
        # We need to find the app0 header, and if we do, ensure the identifier is set.
        # This has to be efficient so it can run on low power hardware!
        try:
            # Check if this is a jpeg, it must start with FF D8
            bufLen = len(buf)
            rawBuffer = buf.Get()
            if bufLen < 2 or rawBuffer[0] != 0xFF or rawBuffer[1] != 0xD8:
                return EnsureJpegHeaderInfoResult(buf, WebcamUtil.c_JpegHeaderFixModeNone)

            if (
                bufLen >= 11
                and rawBuffer[2] == 0xFF
                and rawBuffer[3] == 0xE0
                and rawBuffer[4] == 0x00
                and rawBuffer[5] == 0x10
                and rawBuffer[6] == 0x4A
                and rawBuffer[7] == 0x46
                and rawBuffer[8] == 0x49
                and rawBuffer[9] == 0x46
                and rawBuffer[10] == 0
            ):
                return EnsureJpegHeaderInfoResult(buf, WebcamUtil.c_JpegHeaderFixModeNone)

            # Search the headers for the APP0
            pos = 2
            while pos < bufLen:
                # Ensure we have room and sanity check the buffer headers.
                if pos + 1 >= bufLen:
                    logger.warning("EnsureJpegHeaderInfo - Ran out of buffer before we found a jpeg APP0 header")
                    return EnsureJpegHeaderInfoResult(buf, WebcamUtil.c_JpegHeaderFixModeNone)
                if rawBuffer[pos] != 0xFF:
                    logger.error("EnsureJpegHeaderInfo - jpeg segment header didn't start with 0xff")
                    return EnsureJpegHeaderInfoResult(buf, WebcamUtil.c_JpegHeaderFixModeNone)

                # The first byte is always FF, so we only care about the second.
                segmentType = rawBuffer[pos+1]
                if segmentType == 0xDA:
                    # This is the start of the image, the headers are over.
                    # Some webcam servers, including the Elegoo CC2, send JPEGs without a JFIF APP0 marker.
                    # Add a standard JFIF APP0 segment immediately after the SOI marker.
                    newBuffer = bytearray(bufLen + len(WebcamUtil.c_JfifApp0Header))
                    newBuffer[0] = 0xFF
                    newBuffer[1] = 0xD8
                    app0End = 2 + len(WebcamUtil.c_JfifApp0Header)
                    newBuffer[2:app0End] = WebcamUtil.c_JfifApp0Header
                    newBuffer[app0End:] = rawBuffer[2:]
                    return EnsureJpegHeaderInfoResult(Buffer(newBuffer), WebcamUtil.c_JpegHeaderFixModeInsertApp0)
                elif segmentType == 0xE0:
                    # This is the APP0 header.
                    # Skip past the segment header and size bytes
                    if pos + 8 >= bufLen:
                        logger.warning("EnsureJpegHeaderInfo - Ran out of buffer while reading the jpeg APP0 header")
                        return EnsureJpegHeaderInfoResult(buf, WebcamUtil.c_JpegHeaderFixModeNone)
                    pos += 4

                    # If the identifier isn't JFIF\0, set it to the default.
                    # Note that the last byte should be 0!
                    needsChanges = (
                        rawBuffer[pos] != 0x4A
                        or rawBuffer[pos+1] != 0x46
                        or rawBuffer[pos+2] != 0x49
                        or rawBuffer[pos+3] != 0x46
                        or rawBuffer[pos+4] != 0
                    )

                    # If we don't need changes, we are done.
                    # No need to process more headers.
                    if needsChanges is False:
                        return EnsureJpegHeaderInfoResult(buf, WebcamUtil.c_JpegHeaderFixModeNone)

                    # To edit the byte array, we need a bytearray.
                    # This adds overhead, but it's required because bytes objects aren't editable.
                    editableBuffer = buf.ForceAsByteArray()
                    editableBuffer[pos:pos+5] = b"JFIF\0"

                    # Done, No need to process more headers.
                    return EnsureJpegHeaderInfoResult(buf, WebcamUtil.c_JpegHeaderFixModeSetApp0Identifier, pos)
                else:
                    # This is some other header, skip it's length.
                    # First skip past the type two bytes.
                    pos += 2
                    # Now read the length, two bytes
                    if pos + 1 >= bufLen:
                        logger.warning("EnsureJpegHeaderInfo - Ran out of buffer before we found a jpeg segment length")
                        return EnsureJpegHeaderInfoResult(buf, WebcamUtil.c_JpegHeaderFixModeNone)
                    segLen = (rawBuffer[pos] << 8) + rawBuffer[pos+1]
                    if segLen < 2:
                        logger.warning("EnsureJpegHeaderInfo - jpeg segment length was too small")
                        return EnsureJpegHeaderInfoResult(buf, WebcamUtil.c_JpegHeaderFixModeNone)
                    # Now skip past the segment length (the two length bytes are included in the length size)
                    pos += segLen

        except Exception as e:
            Sentry.OnException("WebcamUtil EnsureJpegHeaderInfo failed to handle jpeg buffer", e)
            # On a failure, return the original result, since it's still good.
            return EnsureJpegHeaderInfoResult(buf, WebcamUtil.c_JpegHeaderFixModeUnknown)

        # If we fall out of the while loop,
        logger.debug("EnsureJpegHeaderInfo excited the while loop without finding the app0 header.")
        return EnsureJpegHeaderInfoResult(buf, WebcamUtil.c_JpegHeaderFixModeNone)


    @staticmethod
    def ApplyCachedJpegHeaderInfo(logger:logging.Logger, buf:Buffer, fixMode:int, app0IdentifierStart:int = 0) -> Buffer:
        try:
            if fixMode == WebcamUtil.c_JpegHeaderFixModeNone:
                return buf

            bufLen = len(buf)
            rawBuffer = buf.Get()
            if bufLen < 2 or rawBuffer[0] != 0xFF or rawBuffer[1] != 0xD8:
                return buf

            if fixMode == WebcamUtil.c_JpegHeaderFixModeSetApp0Identifier:
                app0SegmentStart = app0IdentifierStart - 4
                if (
                    app0SegmentStart < 2
                    or app0IdentifierStart + 5 > bufLen
                    or rawBuffer[app0SegmentStart] != 0xFF
                    or rawBuffer[app0SegmentStart+1] != 0xE0
                ):
                    return WebcamUtil.EnsureJpegHeaderInfo(logger, buf)
                if (
                    rawBuffer[app0IdentifierStart] == 0x4A
                    and rawBuffer[app0IdentifierStart+1] == 0x46
                    and rawBuffer[app0IdentifierStart+2] == 0x49
                    and rawBuffer[app0IdentifierStart+3] == 0x46
                    and rawBuffer[app0IdentifierStart+4] == 0
                ):
                    return buf
                editableBuffer = buf.ForceAsByteArray()
                editableBuffer[app0IdentifierStart:app0IdentifierStart+5] = b"JFIF\0"
                return buf

            if fixMode == WebcamUtil.c_JpegHeaderFixModeInsertApp0:
                # If a later frame already has the standard APP0 marker, don't add a duplicate.
                if (
                    bufLen >= 11
                    and rawBuffer[2] == 0xFF
                    and rawBuffer[3] == 0xE0
                    and rawBuffer[4] == 0x00
                    and rawBuffer[5] == 0x10
                    and rawBuffer[6] == 0x4A
                    and rawBuffer[7] == 0x46
                    and rawBuffer[8] == 0x49
                    and rawBuffer[9] == 0x46
                    and rawBuffer[10] == 0
                ):
                    return buf
                # If it has a non-standard APP0 marker, fall back to the full parser so it can patch instead.
                if bufLen >= 4 and rawBuffer[2] == 0xFF and rawBuffer[3] == 0xE0:
                    return WebcamUtil.EnsureJpegHeaderInfo(logger, buf)
                newBuffer = bytearray(bufLen + len(WebcamUtil.c_JfifApp0Header))
                newBuffer[0] = 0xFF
                newBuffer[1] = 0xD8
                app0End = 2 + len(WebcamUtil.c_JfifApp0Header)
                newBuffer[2:app0End] = WebcamUtil.c_JfifApp0Header
                newBuffer[app0End:] = rawBuffer[2:]
                return Buffer(newBuffer)

            return WebcamUtil.EnsureJpegHeaderInfo(logger, buf)
        except Exception as e:
            Sentry.OnException("WebcamUtil ApplyCachedJpegHeaderInfo failed to handle jpeg buffer", e)
            return buf
