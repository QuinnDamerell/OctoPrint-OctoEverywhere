# namespace: WebStream

import time
import zlib
import logging

import requests
import urllib3

from .octoheaderimpl import HeaderHelper
from .octoheaderimpl import BaseProtocol
from ..octohttprequest import OctoHttpRequest
from ..octostreammsgbuilder import OctoStreamMsgBuilder
from ..webcamhelper import WebcamHelper
from ..commandhandler import CommandHandler
from ..sentry import Sentry
from ..compat import Compat
from ..Proto import HttpHeader
from ..Proto import WebStreamMsg
from ..Proto import MessageContext
from ..Proto import HttpInitialContext
from ..Proto import DataCompression
from ..Proto import MessagePriority
from ..Proto import OeAuthAllowed

#
# A helper object that handles http request for the web stream system.
#
# The helper can close the stream by calling close directly on the WebStream object
# or by returning true from `IncomingServerMessage`
#
class OctoWebStreamHttpHelper:

    # Called by the main socket thread so this should be quick!
    def __init__(self, streamId, logger:logging.Logger, webStream, webStreamOpenMsg, openedTime):
        self.Id = streamId
        self.Logger = logger
        self.WebStream = webStream
        self.WebStreamOpenMsg = webStreamOpenMsg
        self.IsClosed = False
        self.OpenedTime = openedTime

        # Vars for response reading
        self.BodyReadTempBuffer = None
        self.ChunkedBodyHasNoContentLengthHeaders = False
        self.CompressionTimeSec = -1
        self.MissingBoundaryWarningCounter = 0
        self.IsUsingFullBodyBuffer = False
        self.IsUsingCustomBodyStreamCallbacks = False

        # If this doesn't not equal None, it means we know how much data to expect.
        self.KnownFullStreamUploadSizeBytes = None
        self.UploadBytesReceivedSoFar = 0
        self.UploadBuffer = None

        # Micro body read stuff.
        self.IsDoingMicroBodyReads = False
        self.IsFirstMicroBodyRead = True

        # Perf stats
        self.BodyReadTimeSec = 0.0
        self.ServiceUploadTimeSec = 0.0
        self.BodyReadTimeHighWaterMarkSec = 0.0
        self.ServiceUploadTimeHighWaterMarkSec = 0.0

        # Used to keep track of multipart read rates, aka webcam streaming fps.
        # A value of 0 means there's no current read rate.
        self.MultipartReadsPerSecond = 0
        self.MultipartReadsPerSecondCounter = 0
        self.MultipartReadTimestampSec = 0.0

        # In the open message, this value might exist, which would indicate
        # we know the full data size of the data that's being uploaded.
        # If it doesn't exist, either there is no upload payload or we don't
        # know how large the payload is.
        fullStreamUploadSize = webStreamOpenMsg.FullStreamDataSize()
        if fullStreamUploadSize > 0:
            self.KnownFullStreamUploadSizeBytes = fullStreamUploadSize


    # When close is called, all http operations should be shutdown.
    # Called by the main socket thread so this should be quick!
    def Close(self):
        self.IsClosed = True


    # Called when a new message has arrived for this stream from the server.
    # This function should throw on critical errors, that will reset the connection.
    # Returning true will case the websocket to close on return.
    def IncomingServerMessage(self, webStreamMsg):

        # Note this is called on a single thread and will always handle messages
        # in order as they were sent.

        # This http call might have data sent to us in multiple messages.
        # If this message has data, put it into our buffer.
        if webStreamMsg.DataLength() > 0:
            # Copy this upload data from the message.
            self.copyUploadDataFromMsg(webStreamMsg)

        # If the data is done flag is set, that indicates that
        # the full upload buffer has been transmitted.
        if webStreamMsg.IsDataTransmissionDone():
            # If we didn't know the upload size, we need to finalize it now
            self.finalizeUnknownUploadSizeIfNeeded()

            # Do the request. This will block this thread until it's done and the
            # entire response is sent.
            self.executeHttpRequest()

            # Return true since this stream is now done
            return True

        # Return false since there should be more to this stream.
        return False


    # This function either needs to throw (which will restart the entire connection)
    # or return a WebStreamMsg, or close the web stream. Otherwise the server will be waiting for it
    # for until it hits a timeout.
    # For errors
    #   - If it's a octo protocol error or missing protocol data, throw to take down the entire OctoStream connection
    #   - For request errors, this logic should close the stream without sending back a response, which will make the server
    #     generate an error.
    def executeHttpRequest(self):
        requestExecutionStart = time.time()

        # Validate
        if self.WebStreamOpenMsg is None:
            raise Exception("ExecuteHttpRequest but there is no open message")
        # Make sure if there was a defined upload size, we have all of the data.
        if self.KnownFullStreamUploadSizeBytes is not None:
            if self.UploadBytesReceivedSoFar != self.KnownFullStreamUploadSizeBytes:
                raise Exception("Http request tried to execute, but we haven't gotten all of the upload payload. Total:"+str(self.KnownFullStreamUploadSizeBytes)+"; rec so far:"+str(self.UploadBytesReceivedSoFar))

        # Get the initial context
        httpInitialContext = self.WebStreamOpenMsg.HttpInitialContext()
        if httpInitialContext is None:
            self.Logger.error(self.getLogMsgPrefix()+ " request open message had no initial context.")
            raise Exception("Http request open message had no initial context")

        # Setup the headers
        sendHeaders = HeaderHelper.GatherRequestHeaders(self.Logger, httpInitialContext, BaseProtocol.Http)

        # Figure out if this is a special OctoEverywhere Auth call.
        isOeAuthCall = httpInitialContext.UseOctoeverywhereAuth() == OeAuthAllowed.OeAuthAllowed.Allow
        if isOeAuthCall and Compat.HasLocalAuth():
            # If so and this platform supports local auth, add the auth header.
            Compat.GetLocalAuth().AddAuthHeader(sendHeaders)

        # Find the method
        method = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Method())
        if method is None:
            self.Logger.error(self.getLogMsgPrefix()+" request had a None method type.")
            raise Exception("Http request had a None method type")

        # Before we make the request, make sure we shouldn't defer for a high pri request
        self.checkForDelayIfNotHighPri()

        # Check for some special case requests before we handle the request as normal.
        #
        # 1) An oracle snapshot or webcam stream request. In this case the WebCamHelper class will handle the request.
        # 2) If the request is a OctoStreamCommand, the CommandHandler will handle the request.
        # 3) Finally, check if the request is cached in Slipstream.
        octoHttpResult = None
        isFromCache = False
        if WebcamHelper.Get().IsSnapshotOrWebcamStreamOracleRequest(sendHeaders):
            octoHttpResult = WebcamHelper.Get().MakeSnapshotOrWebcamStreamRequest(httpInitialContext, method, sendHeaders, self.UploadBuffer)
        # If this is a special command for OctoEverywhere, we handle it differently.
        elif CommandHandler.Get().IsCommandRequest(httpInitialContext):
            # This HandleCommand wil return a mock  OctoHttpResult, including a full mock response object.
            octoHttpResult = CommandHandler.Get().HandleCommand(httpInitialContext, self.UploadBuffer)
        else:
            # This is a normal web request, first ensure they are allowed.
            if OctoHttpRequest.GetDisableHttpRelay():
                self.Logger.warn("OctoWebStreamHttpHelper got a request but the http relay is disabled.")
                self.WebStream.SetClosedDueToFailedRequestConnection()
                self.WebStream.Close()
                return

            # For all web requests, check our in memory read-to-go cache.
            # If available, this will return the object. On a miss it will return None
            if Compat.HasSlipstream():
                octoHttpResult = Compat.GetSlipstream().GetCachedOctoHttpResult(httpInitialContext)

            # Check if we got a cache hit.
            if octoHttpResult is not None:
                isFromCache = True
            else:
                # If we don't have a valid result yet, do the normal http path.
                octoHttpResult = OctoHttpRequest.MakeHttpCallOctoStreamHelper(self.Logger, httpInitialContext, method, sendHeaders, self.UploadBuffer)


        # If None is returned, it failed.
        # Since the request failed, we want to just close the stream, since it's not a protocol failure.
        if octoHttpResult is None:
            path = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Path())
            self.Logger.warn(self.getLogMsgPrefix() + " failed to make http request. octoHttpResult was None; url:"+str(path))
            self.WebStream.SetClosedDueToFailedRequestConnection()
            self.WebStream.Close()
            return

        # On success, unpack the result.
        uri = octoHttpResult.Url
        requestExecutionEnd = time.time()

        # Now that we have a valid response, use a with block to ensure no matter what it gets closed when we leave.
        # This is important since we use the stream flag, otherwise close() will not get called and the connection will remain open.
        # Note that close() could throw in bad cases, but that's ok because this function is allowed to throw on errors and the octostream will be cleaned up.
        with octoHttpResult:

            # As a caching technique, if the request has the correct modified headers and the response has them as well, send back a 304,
            # which indicates the body hasn't been modified and we can save the bandwidth by not sending it.
            # We need to do this before we process the response headers.
            # This function will check if we want to do a 304 return and update the request correctly.
            self.checkForNotModifiedCacheAndUpdateResponseIfSo(sendHeaders, octoHttpResult)


            # Before we check the headers, check if we are using a full body buffer.
            # If we are using a full body buffer, we need to ensure the content header is set. This will do a few things:
            #   - It will make the request more efficient since we can allocate the fully know buffer size.
            #   - It will make the send loop more efficient, since we know we are only sending one big chunk of data.
            c_contentLengthHeaderKeyLower = "content-length"
            if octoHttpResult.FullBodyBuffer is not None:
                # We set this flag so other parts of this class that need to know if we are using it or not
                # this way we only have one check that enables or disables it.
                self.IsUsingFullBodyBuffer = True

                # Figure out the size of the fully body buffer.
                # Note that if the buffer is compressed, we need to use the OG size of the buffer, which is stored in the result.
                # The FULL buffer size must be set in the content-length, not the compressed size, since the compression is just for our link, it's decompressed when the
                # message is unpacked.
                fullContentBufferSize = len(octoHttpResult.FullBodyBuffer)
                if octoHttpResult.IsBodyBufferZlibCompressed:
                    fullContentBufferSize = octoHttpResult.BodyBufferPreCompressSize

                # See what the current header is (if there is one). If it's set, it should match.
                if c_contentLengthHeaderKeyLower in octoHttpResult.Headers:
                    curHeaderLen = int(octoHttpResult.Headers[c_contentLengthHeaderKeyLower])
                    if curHeaderLen != fullContentBufferSize:
                        self.Logger.error(f"Request {uri} had a content length set ({curHeaderLen}) but its different from the full body length size: {fullContentBufferSize}")

                # Ensure the header is set to the current buffer size.
                octoHttpResult.Headers[c_contentLengthHeaderKeyLower] = str(fullContentBufferSize)

            # Next, check if this response is using a custom body callback
            if octoHttpResult.GetCustomBodyStreamCallback is not None:
                # We set this flag so other parts of this class that need to know if we are using it or not
                # this way we only have one check that enables or disables it.
                self.IsUsingCustomBodyStreamCallbacks = True


            # Look at the headers to see what kind of response we are dealing with.
            # See if we find a content length, for http request that are streams, there is no content length.
            contentLength = None
            # We will also look for the content type, and look for a boundary string if there is one
            # The boundary stream is used for webcam streams, and it's an ideal place to package and send each frame
            boundaryStr = None
            # Pull out the content type value, so we can use it to figure out if we want to compress this data or not
            contentTypeLower =None
            headers = octoHttpResult.Headers
            for name, value in headers.items():
                nameLower = name.lower()

                if nameLower == c_contentLengthHeaderKeyLower:
                    contentLength = int(value)

                elif nameLower == "content-type":
                    contentTypeLower = value.lower()

                    # Look for a boundary string, something like this: `multipart/x-mixed-replace;boundary=boundarydonotcross`
                    indexOfBoundaryStart = contentTypeLower.find('boundary=')
                    if indexOfBoundaryStart != -1:
                        # Move past the string we found
                        indexOfBoundaryStart += len('boundary=')
                        # We should find a boundary, use the original case to parse it out.
                        boundaryStr = value[indexOfBoundaryStart:].strip()
                        if len(boundaryStr) == 0:
                            self.Logger.error("We found a boundary stream, but didn't find the boundary string. "+ contentTypeLower)
                            continue

                elif nameLower == "location":
                    # We have noticed that some proxy servers aren't setup correctly to forward the x-forwarded-for and such headers.
                    # So when the web server responds back with a 301 or 302, the location header might not have the correct hostname, instead an ip like 127.0.0.1.
                    octoHttpResult.Headers[name] = HeaderHelper.CorrectLocationResponseHeaderIfNeeded(self.Logger, uri, value, sendHeaders)

            # We also look at the content-type to determine if we should add compression to this request or not.
            # general rule of thumb is that compression is quite cheap but really helps with text, so we should compress when we
            # can.
            compressBody = self.shouldCompressBody(contentTypeLower, octoHttpResult, contentLength)

            # Since streams with unknown content-lengths can run for a while, report now when we start one.
            # If the status code is 304 or 204, we don't expect content.
            if self.Logger.isEnabledFor(logging.DEBUG) and contentLength is None and octoHttpResult.StatusCode != 304 and octoHttpResult.StatusCode != 204:
                self.Logger.debug(self.getLogMsgPrefix() + "STARTING " + method+" [upload:"+str(format(requestExecutionStart - self.OpenedTime, '.3f'))+"s; request_exe:"+str(format(requestExecutionEnd - requestExecutionStart, '.3f'))+"s; ] type:"+str(contentTypeLower)+" status:"+str(octoHttpResult.StatusCode)+" for " + uri)

            # Check for a response handler and if we have one, check if it might want to edit the response of this call.
            # If so, it will return a context object. If not, it will return None.
            responseHandlerContext = None
            if Compat.HasWebRequestResponseHandler():
                responseHandlerContext = Compat.GetWebRequestResponseHandler().CheckIfResponseNeedsToBeHandled(uri)

            # Setup a loop to read the stream and push it out in multiple messages.
            contentReadBytes = 0
            nonCompressedContentReadSizeBytes = 0
            isFirstResponse = True
            isLastMessage = False
            messageCount = 0
            # Continue as long as the stream isn't closed and we haven't sent the close message.
            # We don't check th body read sizes here, because we don't want to duplicate that logic check.
            while self.IsClosed is False and isLastMessage is False:

                # Before we process the response, make sure we shouldn't defer for a high pri request
                self.checkForDelayIfNotHighPri()

                # This is an interesting check. If we are spinning to deliver a http body, and we detect that what we are compressing
                # is larger than the OG body, we will disable compression for all future messages. We do this because any files that's already
                # compressed (video, audio, images, or files) will be the same after compression but with overhead added.
                # We take a big time hit applying the compression, which is usually offset by the size reduction, but if that's not the case, disable it.
                # If the compressed stream size (contentReadBytes) is larger than  90% of the original stream size(nonCompressedContentReadSizeBytes), stop compression.
                if compressBody and contentReadBytes != 0 and nonCompressedContentReadSizeBytes != 0 and contentReadBytes > nonCompressedContentReadSizeBytes * 0.9:
                    compressBody = False
                    self.Logger.info(f"We detected that the compression being applied to this stream was inefficient, so we are disabling compression. Compression: {float(contentReadBytes)/float(nonCompressedContentReadSizeBytes)} URL: {uri}")

                # Prepare a response.
                # TODO - We should start the buffer at something that's likely to not need expanding for most requests.
                builder = OctoStreamMsgBuilder.CreateBuffer(20000)

                # Unless we are skipping the body read, do it now.
                # If there's a 304, we might have a body, but we don't want to read it.
                # If the response is 204, there will be no content, so don't bother.
                if octoHttpResult.StatusCode == 304 or octoHttpResult.StatusCode == 204:
                    # Use zero read defaults.
                    nonCompressedBodyReadSize = 0
                    lastBodyReadLength = 0
                    dataOffset = None
                else:
                    # Start by reading data from the response.
                    # This function will return a read length of 0 and a null data offset if there's nothing to read.
                    # Otherwise, it will return the length of the read data and the data offset in the buffer.
                    nonCompressedBodyReadSize, lastBodyReadLength, dataOffset = self.readContentFromBodyAndMakeDataVector(builder, octoHttpResult, boundaryStr, compressBody, contentTypeLower, contentLength, responseHandlerContext)
                contentReadBytes += lastBodyReadLength
                nonCompressedContentReadSizeBytes += nonCompressedBodyReadSize

                # Special Case - If this request was handled by the Web Request Response Handler, the body buffer might have been edited.
                # We need to update the content length for the message, so it's sent correctly in the OctoStream response.
                # Since we know we read the entire file at once, this should be the first message, which means updating it now
                # works. This is a little hacky, there could be a better way to do this.
                if responseHandlerContext is not None and contentLength is not None:
                    if isFirstResponse is False:
                        self.Logger.error("We edited the response and need to update the request content length but this isn't the first request?")
                    # Always update the content length, because the new size could be smaller or larger than the original.
                    contentLength = nonCompressedBodyReadSize

                # Since this operation can take a while, check if we closed.
                if self.IsClosed:
                    break

                # Validate.
                if contentLength is not None and nonCompressedContentReadSizeBytes > contentLength:
                    self.Logger.warn(self.getLogMsgPrefix()+" the http stream read more data than the content length indicated.")
                if dataOffset is None and contentLength is not None and nonCompressedContentReadSizeBytes < contentLength:
                    # This might happen if the connection closes unexpectedly before the transfer is done.
                    self.Logger.warn(self.getLogMsgPrefix()+" we expected a fixed length response, but the body read completed before we read it all.")

                # Check if this is the last message.
                # This is the last message if...
                #  - The data offset is ever None, this means we have read the entire body as far as the request system is concerned.
                #  - We have an expected length and we have hit it or gone over it.
                isLastMessage = dataOffset is None or (contentLength is not None and nonCompressedContentReadSizeBytes >= contentLength)

                # If this is the first response in the stream, we need to send the initial http context and status code.
                httpInitialContextOffset = None
                statusCode = None
                if isFirstResponse is True:
                    # Set the status code, so it's sent.
                    statusCode = octoHttpResult.StatusCode

                    # Gather the headers, if there are any. This will return None if there are no headers to send.
                    headerVectorOffset = self.buildHeaderVector(builder, octoHttpResult)

                    # Build the initial context. We should always send a http initial context on the first response,
                    # even if there are no headers in t.
                    HttpInitialContext.Start(builder)
                    if headerVectorOffset is not None:
                        HttpInitialContext.AddHeaders(builder, headerVectorOffset)
                    httpInitialContextOffset = HttpInitialContext.End(builder)

                # Now build the return message
                WebStreamMsg.Start(builder)
                WebStreamMsg.AddStreamId(builder, self.Id)
                # Indicate this message has data, even if it's just the initial http context (because there's no data for this request)
                WebStreamMsg.AddIsControlFlagsOnly(builder, False)
                if statusCode is not None:
                    WebStreamMsg.AddStatusCode(builder, statusCode)
                if dataOffset is not None:
                    WebStreamMsg.AddData(builder, dataOffset)
                if httpInitialContextOffset is not None:
                    # This should always be not null for the first response.
                    WebStreamMsg.AddHttpInitialContext(builder, httpInitialContextOffset)
                if isFirstResponse is True and contentLength is not None:
                    # Only on the first response, if we know the full size, set it.
                    WebStreamMsg.AddFullStreamDataSize(builder, contentLength)
                if compressBody:
                    # If we are compressing, we need to add what we are using and what the original size was.
                    WebStreamMsg.AddDataCompression(builder, DataCompression.DataCompression.Zlib)
                    WebStreamMsg.AddOriginalDataSize(builder, nonCompressedBodyReadSize)
                if isLastMessage:
                    # If this is the last message because we know the body is all
                    # sent, indicate that the data stream is done and send the close message.
                    WebStreamMsg.AddIsDataTransmissionDone(builder, True)
                    WebStreamMsg.AddIsCloseMsg(builder, True)
                if self.MultipartReadsPerSecond != 0:
                    # If this is a multipart stream (webcam streaming), every 1 second a value will be dumped into MultipartReadsPerSecond
                    # when it's there, we want to send it to the server for telemetry, and then zero it out.
                    if self.Logger.isEnabledFor(logging.DEBUG):
                        self.Logger.debug(f"Multipart Stats; reads per second: {str(self.MultipartReadsPerSecond)}, body read high water mark {str(format(self.BodyReadTimeHighWaterMarkSec*1000.0, '.2f'))}ms, socket write high water mark {str(format(self.ServiceUploadTimeHighWaterMarkSec*1000.0, '.2f'))}ms")
                    if self.MultipartReadsPerSecond > 255 or self.MultipartReadsPerSecond < 0:
                        self.Logger.warn("self.MultipartReadsPerSecond is larger than uint8. "+str(self.MultipartReadsPerSecond))
                        self.MultipartReadsPerSecond  = 255
                    WebStreamMsg.AddMultipartReadsPerSecond(builder, self.MultipartReadsPerSecond)
                    self.MultipartReadsPerSecond = 0
                    # Also attach the other stats.
                    bodyReadTimeHighWaterMarkMs = int(self.BodyReadTimeHighWaterMarkSec * 1000.0)
                    self.BodyReadTimeHighWaterMarkSec = 0.0
                    if bodyReadTimeHighWaterMarkMs > 65535 or bodyReadTimeHighWaterMarkMs < 0:
                        bodyReadTimeHighWaterMarkMs  = 65535
                    WebStreamMsg.AddBodyReadTimeHighWaterMarkMs(builder, bodyReadTimeHighWaterMarkMs)

                    serviceUploadTimeHighWaterMarkMs = int(self.ServiceUploadTimeHighWaterMarkSec * 1000.0)
                    self.ServiceUploadTimeHighWaterMarkSec = 0.0
                    if serviceUploadTimeHighWaterMarkMs > 65535 or serviceUploadTimeHighWaterMarkMs < 0:
                        serviceUploadTimeHighWaterMarkMs  = 65535
                    WebStreamMsg.AddSocketSendTimeHighWaterMarkMs(builder, serviceUploadTimeHighWaterMarkMs)

                webStreamMsgOffset = WebStreamMsg.End(builder)

                # Wrap in the OctoStreamMsg and finalize.
                outputBuf = OctoStreamMsgBuilder.CreateOctoStreamMsgAndFinalize(builder, MessageContext.MessageContext.WebStreamMsg, webStreamMsgOffset)

                # Send the message.
                # If this is the last, we need to make sure to set that we have set the closed flag.
                serviceSendStartSec = time.time()
                self.WebStream.SendToOctoStream(outputBuf, isLastMessage, True)
                thisServiceSendTimeSec = time.time() - serviceSendStartSec
                self.ServiceUploadTimeSec += thisServiceSendTimeSec
                if thisServiceSendTimeSec > self.ServiceUploadTimeHighWaterMarkSec:
                    self.ServiceUploadTimeHighWaterMarkSec = thisServiceSendTimeSec

                # Clear this flag
                isFirstResponse = False
                messageCount += 1

            # Log about it - only if debug is enabled. Otherwise, we don't want to waste time making the log string.
            responseWriteDone = time.time()
            if self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug(self.getLogMsgPrefix() + method+" [upload:"+str(format(requestExecutionStart - self.OpenedTime, '.3f'))+"s; request_exe:"+str(format(requestExecutionEnd - requestExecutionStart, '.3f'))+"s; send:"+str(format(responseWriteDone - requestExecutionEnd, '.3f'))+"s; body_read:"+str(format(self.BodyReadTimeSec, '.3f'))+"s; compress:"+str(format(self.CompressionTimeSec, '.3f'))+"s; octo_stream_upload:"+str(format(self.ServiceUploadTimeSec, '.3f'))+"s] size:("+str(nonCompressedContentReadSizeBytes)+"->"+str(contentReadBytes)+") compressed:"+str(compressBody)+" msgcount:"+str(messageCount)+" microreads:"+str(self.IsDoingMicroBodyReads)+" type:"+str(contentTypeLower)+" status:"+str(octoHttpResult.StatusCode)+" cached:"+str(isFromCache)+" for " + uri)


    def buildHeaderVector(self, builder, octoHttpResult:OctoHttpRequest.Result):
        # Gather up the headers to return.
        headerTableOffsets = []
        headers = octoHttpResult.Headers
        for name, value in headers.items():
            nameLower = name.lower()

            # Since we send the entire result as one non-encoded
            # payload we want to drop this header. Otherwise the server might emit it to
            # the client, when it actually doesn't match what the server sends to the client.
            # Note: Typically, if the OctoPrint web server sent something chunk encoded,
            # our web server will also send it to the client via chunk encoding. But it will handle
            # that on it's own and set the header accordingly.
            if nameLower == "transfer-encoding":
                continue
            # Don't send this easter egg.
            if nameLower == "x-clacks-overhead":
                continue

            # Allocate strings
            keyOffset = builder.CreateString(name)
            valueOffset = builder.CreateString(value)
            # Create the header table
            HttpHeader.Start(builder)
            HttpHeader.AddKey(builder, keyOffset)
            HttpHeader.AddValue(builder, valueOffset)
            headerTableOffsets.append(HttpHeader.End(builder))

        # Check if there were any headers, if not, return null so we don't set the vector.
        if len(headerTableOffsets) == 0:
            return None

        # Build the heaver vector
        HttpInitialContext.StartHeadersVector(builder, len(headerTableOffsets))
        for offset in headerTableOffsets:
            # This function was very hard to find, I eventually found an example in the
            # py samples in the flatbuffer repo.
            builder.PrependUOffsetTRelative(offset)
        return builder.EndVector()


    def finalizeUnknownUploadSizeIfNeeded(self):
        # Check if we are in the state where we have an upload buffer, but don't know the size.
        # If we don't know the full upload buffer size, the UploadBuffer will be larger the actual size
        # since we allocate extra room on it to try to reduce allocations.
        # Note we only do this if the final size is unknown. If the final size is known but doesn't
        # match how much we have, that's an error that will be thrown later.
        if self.UploadBuffer is not None and self.KnownFullStreamUploadSizeBytes is None:
            # Trim the buffer to the final size that we received.
            self.UploadBuffer = self.UploadBuffer[0:self.UploadBytesReceivedSoFar]


    def copyUploadDataFromMsg(self, webStreamMsg):
        # Check how much data this message has in it.
        # This size is the size of the full buffer, which is decompressed size if the data is compressed.
        thisMessageDataLen = webStreamMsg.DataLength()
        if thisMessageDataLen <= 0:
            self.Logger.warn(self.getLogMsgPrefix() + " is waiting on upload data but got a message with no data. ")
            return

        # Most uploads have very small payloads that come in single messages.
        # In that case we don't need to allocate a buffer to build up the data
        # and instead we will shortcut using the data buffer that this message is
        # already using
        # IF we don't have a buffer and we know the full size and this message is the full size
        # just use this buffer.
        if self.UploadBuffer is None and self.KnownFullStreamUploadSizeBytes is not None and self.KnownFullStreamUploadSizeBytes == thisMessageDataLen:
            # This is the only message with data, just use it's buffer.
            # I -believe- this doesn't copy the buffer and just makes a view of it.
            # That's the ideal case, because this message buffer will stay around since
            # the http will execute on this same stack.
            self.UploadBuffer = self.decompressBufferIfNeeded(webStreamMsg)
            self.UploadBytesReceivedSoFar = len(self.UploadBuffer)
            # Done!
            return

        # NOTE: We can't do this! Since we try to compress all of the things right now, for already compressed things it will add a little overhead!
        # The full upload size will be the same size as we expect, but the compression will make the payload larger.
        # If we know the upload size, make sure this doesn't exceeded it.
        # if self.KnownFullStreamUploadSizeBytes is not None and thisMessageDataLen + self.UploadBytesReceivedSoFar > self.KnownFullStreamUploadSizeBytes:
        #     self.Logger.warn(self.getLogMsgPrefix() + " received more bytes than it was expecting for the upload. thisMsg:"+str(thisMessageDataLen)+"; so far:"+str(self.UploadBytesReceivedSoFar) + "; expected:"+str(self.KnownFullStreamUploadSizeBytes))

        # Make sure the array has been allocated and it's still large enough.
        if self.UploadBuffer is None or thisMessageDataLen + self.UploadBytesReceivedSoFar > len(self.UploadBuffer):
            newBufferSizeBytes = 0
            if self.KnownFullStreamUploadSizeBytes is not None:
                # We know exactly how much to allocate
                newBufferSizeBytes = self.KnownFullStreamUploadSizeBytes
            else:
                # If we don't know the size, allocate this message plus the current size, plus some buffer (50kb).
                newBufferSizeBytes = thisMessageDataLen + self.UploadBytesReceivedSoFar + 1204 * 50

            # If there's a buffer, grab it since we need to copy it over
            oldBuffer = self.UploadBuffer

            # Allocate the new buffer
            self.UploadBuffer = bytearray(newBufferSizeBytes)

            # Copy over anything that existed before
            if oldBuffer is not None:
                # This will copy the old buffer into the front of the new buffer.
                self.UploadBuffer[0:len(oldBuffer)] = oldBuffer

        # We are ready to copy the new data now.
        # Get a slice of the buffer to avoid a the copy, since we copy on the next step anyways.
        buf = self.decompressBufferIfNeeded(webStreamMsg)

        # Now that we have the original size of the body back, check to make sure it's not too much.
        if self.KnownFullStreamUploadSizeBytes is not None and len(buf) + self.UploadBytesReceivedSoFar > self.KnownFullStreamUploadSizeBytes:
            self.Logger.warn(self.getLogMsgPrefix() + " received more bytes than it was expecting for the upload. thisMsg:"+str(len(buf))+"; so far:"+str(self.UploadBytesReceivedSoFar) + "; expected:"+str(self.KnownFullStreamUploadSizeBytes))
            raise Exception("Too many bytes received for http upload buffer")

        # Append the data into the main buffer.
        pos = self.UploadBytesReceivedSoFar
        self.UploadBuffer[pos:pos+len(buf)] = buf
        self.UploadBytesReceivedSoFar += len(buf)


    # A helper, given a web stream message returns it's data buffer, decompressed if needed.
    def decompressBufferIfNeeded(self, webStreamMsg):
        if webStreamMsg.DataCompression() == DataCompression.DataCompression.Brotli:
            raise Exception("decompressBufferIfNeeded Failed - Brotli decompression not possible.")
        elif webStreamMsg.DataCompression() == DataCompression.DataCompression.Zlib:
            return zlib.decompress(webStreamMsg.DataAsByteArray())
        else:
            return webStreamMsg.DataAsByteArray()


    def checkForNotModifiedCacheAndUpdateResponseIfSo(self, sentHeaders, octoHttpResult:OctoHttpRequest.Result):
        # Check if the sent headers have any conditional http headers.
        etag = None
        modifiedDate = None
        for key in sentHeaders:
            keyLower = key.lower()
            if keyLower == "if-modified-since":
                modifiedDate = sentHeaders[key]
            if keyLower == "if-none-match":
                etag = sentHeaders[key]

        # If there were none found, there's nothing do to.
        if etag is None and modifiedDate is None:
            return

        # Look through the response headers
        headers = octoHttpResult.Headers
        for key in headers:
            keyLower = key.lower()
            if etag is not None and keyLower == "etag":
                # Both have etags, check them.
                # If the request etag starts with the weak indicator, remove it
                if etag.startswith("W/"):
                    etag = etag[2:]
                # Check for an exact match.
                if etag == headers[key]:
                    self.updateResponseFor304(octoHttpResult)
                    return
            if modifiedDate is not None and keyLower == "last-modified":
                # There are actual ways to parse and compare these,
                # But for now we will just do exact matches.
                if modifiedDate == headers[key]:
                    self.updateResponseFor304(octoHttpResult)
                    return


    def updateResponseFor304(self, octoHttpResult:OctoHttpRequest.Result):
        # First of all, update the status code.
        octoHttpResult.StatusCode = 304
        # Remove any headers we don't want to send. Including some of these seems to trip up some browsers.
        # However, there are some we must send...
        # Quote - Note that the server generating a 304 response MUST generate any of the following header fields that would have been sent in a 200 (OK) response to the same request: Cache-Control, Content-Location, Date, ETag, Expires, and Vary.
        #         https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/If-None-Match
        removeHeaders = []
        for key in octoHttpResult.Headers:
            keyLower = key.lower()
            if keyLower == "content-length":
                removeHeaders.append(key)
            if keyLower == "content-type":
                removeHeaders.append(key)
        for key in removeHeaders:
            del octoHttpResult.Headers[key]


    def getLogMsgPrefix(self):
        return "Web Stream http ["+str(self.Id)+"] "


    # Based on the content-type header, this determines if we would apply compression or not.
    # Returns true or false
    def shouldCompressBody(self, contentTypeLower, octoHttpResult, contentLengthOpt):
        # Compression isn't too expensive in terms of cpu cost but for text, it drastically
        # cuts the size down (ike a 75% reduction.) So we are quite liberal with our compression.

        # From testing, we have found that compressing anything smaller than ~200 bytes has not effect
        # thus it's not worth doing (it actually makes it slightly larger)
        if contentLengthOpt is not None and contentLengthOpt < 200:
            return False

        # If we don't know what this is, we don't want to compress it.
        # Compressing the body of a compressed thing will make it larger and takes a good amount of time,
        # so we don't want to waste time on it.
        if contentTypeLower is None:
            return False

        # If there is a full body buffer and and it's already compressed, always return true.
        # This ensures the message is flagged correctly for compression and the body reading system
        # will also read the flag and skip the compression.
        if octoHttpResult.IsBodyBufferZlibCompressed:
            return True

        # We will compress...
        #   - Any thing that has text/ in it
        #   - Anything that says it's javascript
        #   - Anything that says it's json
        #   - Anything that's xml
        #   - Anything that's svg
        return (contentTypeLower.find("text/") != -1 or contentTypeLower.find("javascript") != -1
                or contentTypeLower.find("json") != -1 or contentTypeLower.find("xml") != -1
                or contentTypeLower.find("svg") != -1)


    # Reads data from the response body, puts it in a data vector, and returns the offset.
    # If the body has been fully read, this should return ogLen == 0, len = 0, and offset == None
    # The read style depends on the presence of the boundary string existing.
    def readContentFromBodyAndMakeDataVector(self, builder, octoHttpResult:OctoHttpRequest.Result, boundaryStr_opt, shouldCompress, contentTypeLower_NoneIfNotKnown, contentLength_NoneIfNotKnown, responseHandlerContext):
        # This is the max size each body read will be. Since we are making local calls, most of the time we will always get this full amount as long as theres more body to read.
        # This size is a little under the max read buffer on the server, allowing the server to handle the buffers with no copies.
        #
        # 3/24/24 - We did a lot of direct download testing to tweak this buffer size and the server read size, these were the best values able to hit about 223mpbs.
        # With the current values, the majority of the time is spent sending the data on the websocket.
        defaultBodyReadSizeBytes = 490 * 1024

        # If we are going to compress this read, use a much higher number. Since most of what we compress is text,
        # and that text usually compresses down to 25% of the og size, we will use a x4 multiplier.
        if shouldCompress:
            defaultBodyReadSizeBytes = defaultBodyReadSizeBytes * 4

        # Some requests like snapshot requests will already have a fully read body. In this case we use the existing body buffer instead of reading from the body.
        finalDataBuffer = None
        bodyReadStartSec = time.time()
        if self.IsUsingFullBodyBuffer:
            # In this case, the entire buffer and size are known, so we get them all in one go.
            finalDataBuffer = octoHttpResult.FullBodyBuffer
        elif self.IsUsingCustomBodyStreamCallbacks:
            # In this case we just call this callback, and send whatever it sends. Note that even if this is a boundary stream, we just send back what it sends.
            # If None is returned, we are done.
            finalDataBuffer = octoHttpResult.GetCustomBodyStreamCallback()
        else:
            # If the boundary string exist and is not empty, we will use it to try to read the data.
            # Unless the self.ChunkedBodyHasNoContentLengthHeaders flag has been set, which indicate we have read the body has chunks
            # and failed to find any content length headers. In that case, we will just read fixed sized chunks.
            if self.ChunkedBodyHasNoContentLengthHeaders is False and boundaryStr_opt is not None and len(boundaryStr_opt) != 0:
                # Try to read a single boundary chunk
                readLength = self.readStreamChunk(octoHttpResult, boundaryStr_opt)
                # If we get a length, set the final buffer using the temp buffer.
                # This isn't a copy, just a reference to a subset of the buffer.
                if readLength != 0:
                    finalDataBuffer = self.BodyReadTempBuffer[0:readLength]
            else:
                if responseHandlerContext is None and self.shouldDoUnknownBodySizeRead(contentTypeLower_NoneIfNotKnown, contentLength_NoneIfNotKnown):
                    # If we don't know the content length AND there is no boundary string, this request is probably a event stream of some sort.
                    # We have to use this special read function, because doBodyRead will block until the full buffer is filled, which might take a long time
                    # for a number of streamed messages to fill it up. This special function does micro reads on the socket until a time limit is hit, and then
                    # returns what was received.
                    self.IsDoingMicroBodyReads = True
                    finalDataBuffer = self.doUnknownBodySizeRead(octoHttpResult)
                else:
                    # If there is no boundary string, but we know the content length, it's safe to just read.
                    # This will block until either the full defaultBodyReadSizeBytes is read or the full request has been received.
                    # If this returns None, we hit a read timeout or the stream is done, so we are done.

                    # If this request will be handled by the a response handler, we need to load the full body into one buffer.
                    if responseHandlerContext:
                        # We have to be careful with the size, because on some platforms (like the K1) whatever size we pass it will try to allocate
                        # into one buffer. If we know the context length, us it. Otherwise, set something that's reasonably large.
                        if contentLength_NoneIfNotKnown is not None:
                            defaultBodyReadSizeBytes = contentLength_NoneIfNotKnown
                        else:
                            # Use a 2mb buffer.
                            defaultBodyReadSizeBytes = 1024 * 1024 * 1024 * 2
                    finalDataBuffer = self.doBodyRead(octoHttpResult, defaultBodyReadSizeBytes)

        # Keep track of read times.
        thisBodyReadTimeSec = time.time() - bodyReadStartSec
        self.BodyReadTimeSec += thisBodyReadTimeSec
        if thisBodyReadTimeSec > self.BodyReadTimeHighWaterMarkSec:
            self.BodyReadTimeHighWaterMarkSec = thisBodyReadTimeSec

        # If the final data buffer has been set to None, it means the body is not empty
        if finalDataBuffer is None:
            # Return empty to indicate the body has been fully read.
            return (0, 0, None)

        # Before we do any compression, check if there is a response handler context, meaning there's a response handler that
        # might want to edit the body buffer before it's compressed.
        if responseHandlerContext:
            if contentLength_NoneIfNotKnown is not None and len(finalDataBuffer) != contentLength_NoneIfNotKnown:
                self.Logger.error("We detected the read of the web request response handler message, but the buffer size doesn't match the content length.")
            else:
                # If we have the compat handler, give it the buffer before we finalize the size, as it might want to edit the buffer.
                if Compat.HasWebRequestResponseHandler():
                    finalDataBuffer = Compat.GetWebRequestResponseHandler().HandleResponse(responseHandlerContext, octoHttpResult, finalDataBuffer)

        # If we were asked to compress, do it
        originalBufferSize = len(finalDataBuffer)

        # Check to see if this was a full body buffer, if it was already compressed.
        if octoHttpResult.IsBodyBufferZlibCompressed:
            # If so, use pre compress size it's supplies.
            # And skip compression since it's already done.
            originalBufferSize = octoHttpResult.BodyBufferPreCompressSize
        # Otherwise, check if we should compress
        elif shouldCompress:
            # Some setups can't install brotli since it requires gcc and c++ to compile native code.
            # zlib is part of PY so all plugins us it. Right now it's not worth the tradeoff from testing to enable brotli.
            #
            # After a good amount of testing, we found that a compression level of 3 is a good tradeoff for both.
            # For small to medium size files zlib can actually be better. Brotli starts to be much better in terms of speed
            # and compression for larger files. But for now given the file sizes we use here, it's not worth it.
            #
            # Here's a good quick benchmark on a large js file (4mb)
            #2021-12-17 22:37:22,258 - octoprint.plugins.octoeverywhere - INFO - zlib level: 0 time:9.43207740784 size: 815175 og:815104
            #2021-12-17 22:37:22,319 - octoprint.plugins.octoeverywhere - INFO - zlib level: 1 time:58.7220191956 size: 273923 og:815104
            #2021-12-17 22:37:22,383 - octoprint.plugins.octoeverywhere - INFO - zlib level: 2 time:61.7210865021 size: 263366 og:815104
            #2021-12-17 22:37:22,453 - octoprint.plugins.octoeverywhere - INFO - zlib level: 3 time:69.3519115448 size: 256257 og:815104
            #2021-12-17 22:37:22,537 - octoprint.plugins.octoeverywhere - INFO - zlib level: 4 time:81.6609859467 size: 239928 og:815104
            #2021-12-17 22:37:22,650 - octoprint.plugins.octoeverywhere - INFO - zlib level: 5 time:110.955953598 size: 231844 og:815104
            #2021-12-17 22:37:22,803 - octoprint.plugins.octoeverywhere - INFO - zlib level: 6 time:150.192022324 size: 229684 og:815104
            #2021-12-17 22:37:22,972 - octoprint.plugins.octoeverywhere - INFO - zlib level: 7 time:166.711091995 size: 229118 og:815104
            #2021-12-17 22:37:23,196 - octoprint.plugins.octoeverywhere - INFO - zlib level: 8 time:221.390962601 size: 228784 og:815104
            #2021-12-17 22:37:23,442 - octoprint.plugins.octoeverywhere - INFO - zlib level: 9 time:244.188070297 size: 228737 og:815104
            #2021-12-17 22:37:23,477 - octoprint.plugins.octoeverywhere - INFO - brotli level: 0 time:31.9409370422 size: 280540 og:815104
            #2021-12-17 22:37:23,536 - octoprint.plugins.octoeverywhere - INFO - brotli level: 1 time:56.2720298767 size: 267581 og:815104
            #2021-12-17 22:37:23,611 - octoprint.plugins.octoeverywhere - INFO - brotli level: 2 time:72.9219913483 size: 245109 og:815104
            #2021-12-17 22:37:23,703 - octoprint.plugins.octoeverywhere - INFO - brotli level: 3 time:86.4551067352 size: 241472 og:815104
            #2021-12-17 22:37:23,874 - octoprint.plugins.octoeverywhere - INFO - brotli level: 4 time:169.479846954 size: 235446 og:815104
            #2021-12-17 22:37:24,125 - octoprint.plugins.octoeverywhere - INFO - brotli level: 5 time:248.244047165 size: 219928 og:815104
            #2021-12-17 22:37:24,451 - octoprint.plugins.octoeverywhere - INFO - brotli level: 6 time:321.651935577 size: 217598 og:815104
            #2021-12-17 22:37:24,848 - octoprint.plugins.octoeverywhere - INFO - brotli level: 7 time:395.76292038 size: 216307 og:815104
            #2021-12-17 22:37:25,334 - octoprint.plugins.octoeverywhere - INFO - brotli level: 8 time:483.689785004 size: 215660 og:815104
            #2021-12-17 22:37:25,973 - octoprint.plugins.octoeverywhere - INFO - brotli level: 9 time:637.011051178 size: 214962 og:815104
            #2021-12-17 22:37:30,395 - octoprint.plugins.octoeverywhere - INFO - brotli level: 10 time:4420.00603676 size: 202474 og:815104
            #2021-12-17 22:37:40,826 - octoprint.plugins.octoeverywhere - INFO - brotli level: 11 time:10429.7590256 size: 198538 og:815104
            # Here's a more average size file
            #2021-12-17 22:45:06,278 - octoprint.plugins.octoeverywhere - INFO - zlib level: 0 time:1.84893608093 size: 13514 og:13503
            #2021-12-17 22:45:06,291 - octoprint.plugins.octoeverywhere - INFO - zlib level: 1 time:1.37400627136 size: 5647 og:13503
            #2021-12-17 22:45:06,298 - octoprint.plugins.octoeverywhere - INFO - zlib level: 2 time:3.87191772461 size: 5550 og:13503
            #2021-12-17 22:45:06,301 - octoprint.plugins.octoeverywhere - INFO - zlib level: 3 time:1.43599510193 size: 5498 og:13503
            #2021-12-17 22:45:06,304 - octoprint.plugins.octoeverywhere - INFO - zlib level: 4 time:1.70516967773 size: 5306 og:13503
            #2021-12-17 22:45:06,308 - octoprint.plugins.octoeverywhere - INFO - zlib level: 5 time:2.17819213867 size: 5227 og:13503
            #2021-12-17 22:45:06,312 - octoprint.plugins.octoeverywhere - INFO - zlib level: 6 time:2.08187103271 size: 5217 og:13503
            #2021-12-17 22:45:06,316 - octoprint.plugins.octoeverywhere - INFO - zlib level: 7 time:2.29096412659 size: 5218 og:13503
            #2021-12-17 22:45:06,320 - octoprint.plugins.octoeverywhere - INFO - zlib level: 8 time:2.12597846985 size: 5218 og:13503
            #2021-12-17 22:45:06,324 - octoprint.plugins.octoeverywhere - INFO - zlib level: 9 time:2.29811668396 size: 5218 og:13503
            #2021-12-17 22:45:06,327 - octoprint.plugins.octoeverywhere - INFO - brotli level: 0 time:1.26886367798 size: 5877 og:13503
            #2021-12-17 22:45:06,330 - octoprint.plugins.octoeverywhere - INFO - brotli level: 1 time:1.18708610535 size: 5828 og:13503
            #2021-12-17 22:45:06,334 - octoprint.plugins.octoeverywhere - INFO - brotli level: 2 time:1.77407264709 size: 5479 og:13503
            #2021-12-17 22:45:06,339 - octoprint.plugins.octoeverywhere - INFO - brotli level: 3 time:2.63094902039 size: 5418 og:13503
            #2021-12-17 22:45:06,345 - octoprint.plugins.octoeverywhere - INFO - brotli level: 4 time:4.88996505737 size: 5335 og:13503
            #2021-12-17 22:45:06,354 - octoprint.plugins.octoeverywhere - INFO - brotli level: 5 time:6.34503364563 size: 5007 og:13503
            #2021-12-17 22:45:06,364 - octoprint.plugins.octoeverywhere - INFO - brotli level: 6 time:8.3749294281 size: 5003 og:13503
            #2021-12-17 22:45:06,384 - octoprint.plugins.octoeverywhere - INFO - brotli level: 7 time:18.7141895294 size: 4994 og:13503
            #2021-12-17 22:45:06,411 - octoprint.plugins.octoeverywhere - INFO - brotli level: 8 time:25.6741046906 size: 4994 og:13503
            #2021-12-17 22:45:06,447 - octoprint.plugins.octoeverywhere - INFO - brotli level: 9 time:33.3149433136 size: 4989 og:13503
            #2021-12-17 22:45:06,499 - octoprint.plugins.octoeverywhere - INFO - brotli level: 10 time:50.5220890045 size: 4609 og:13503
            #2021-12-17 22:45:06,636 - octoprint.plugins.octoeverywhere - INFO - brotli level: 11 time:135.287046432 size: 4503 og:13503

            start = time.time()
            finalDataBuffer = zlib.compress(finalDataBuffer, 3)
            if self.CompressionTimeSec == -1:
                self.CompressionTimeSec = 0
            self.CompressionTimeSec += (time.time() - start)

        # We have a data buffer, so write it into the builder and return the offset.
        return (originalBufferSize, len(finalDataBuffer), builder.CreateByteVector(finalDataBuffer))

    # Reads a single chunk from the http response.
    # This function uses the BodyReadTempBuffer to store the data.
    # Returns the read size, 0 if the body read is complete.
    def readStreamChunk(self, octoHttpResult:OctoHttpRequest.Result, boundaryStr):
        frameSize = 0
        headerSize = 0
        foundContentLength = False

        # If the temp array isn't setup, do it now.
        if self.BodyReadTempBuffer is None:
            self.BodyReadTempBuffer = bytearray(10*1024)

        # Note. OctoPrint webcam streams have content-length headers in each chunk. However, the standard
        # says it's not required. So if we can find them use them, but if not we will set the
        # ChunkedBodyHasNoContentLengthHeaders so that future body reads don't attempt to find the headers again.

        # First, we need to see if we can find the content length header.
        # We will keep grabbing more and more data in loop until we find the header
        # If we can't find it after our max size, we declare it's not there (which is fine)
        # and will use a different read method going forward.
        c_maxHeaderSearchSizeBytes = 5 * 1024
        tempBufferFilledSize = 0
        try:
            # Loop until found or we have hit the search limit.
            while foundContentLength is False and tempBufferFilledSize < c_maxHeaderSearchSizeBytes:
                # Read a small chunk to try to read the header
                # We want to read enough that hopefully we get all of the headers, but not so much that
                # we accidentally read two boundary messages at once.
                # 3/24/24 - After a lot of testing, it seems most times we get the full headers in 120 chars.
                # So we will target that much, hoping we can do one read and get them.
                headerBuffer = self.doBodyRead(octoHttpResult, 120)

                # If this returns 0, the body read is complete
                if headerBuffer is None:
                    # We should return the length of the buffer we have read so far.
                    return tempBufferFilledSize

                # Add the header buffer to the temp output
                self.BodyReadTempBuffer[tempBufferFilledSize:tempBufferFilledSize+len(headerBuffer)] = headerBuffer
                tempBufferFilledSize += len(headerBuffer)

                # Convert the entire buffer read so far into a string for parsing.
                # We must use the decode function here, not just str(), because in py3 str() will make a "ToString" the object,
                # and not actually return us the contents of the buffer as a string.
                headerStr = self.BodyReadTempBuffer[:tempBufferFilledSize].decode(errors="ignore")

                # Validate the headers starts with what we expect.
                # According the the RFC, the boundary should start with the boundary string or '--' + boundary string.
                # However, we have also seen \r\n--<str> and also no boundary string for the first frame as well. So this might fire once or twice, and that's fine.
                # These are in order of how common they are, for perf.
                if headerStr.startswith("--"+boundaryStr) is False and headerStr.startswith(boundaryStr) is False and headerStr.startswith("\r\n--"+boundaryStr) is False:
                    # Always report the first time we find this, otherwise, report only occasionally.
                    if self.MissingBoundaryWarningCounter % 120 == 0:
                        # Trim the string to print it.
                        outputStr = headerStr
                        if len(outputStr) > 40:
                            outputStr = outputStr[:40]
                        self.Logger.warn("We read a web stream body frame, but it didn't start with the expected boundary header. expected:'"+boundaryStr+"' got:^^"+outputStr+"^^")
                    self.MissingBoundaryWarningCounter += 1

                # Find out how long the headers are. The \r\n\r\n sequence ends the headers.
                endOfAllHeadersMatch = "\r\n\r\n"
                endOfHeaderMatch = "\r\n"

                # Try to find headers
                # This logic checks for errors, and if found, don't stop the logic because of them
                # This will cause us to loop again and read more. The reason for this is since we read random
                # chunks, we could read a chunk that splits the content-length header in half, which would cause errors.
                # So we just allow the system to keep reading until we hit the limit, because the next read would then have the full
                # header we are looking for.
                headerSize = headerStr.find(endOfAllHeadersMatch)
                if headerSize != -1:
                    # We found at least some headers!

                    # Add 4 bytes for the \r\n\r\n end of header sequence. Also add two bytes for the \r\n at the end of this boundary chunk.
                    headerSize += 4 + 2

                    # Split out the headers
                    headers = headerStr.split(endOfHeaderMatch)
                    for header in headers:
                        if header.lower().startswith("content-length"):
                            # We found the content-length header!
                            p = header.split(':')
                            if len(p) == 2:
                                frameSize = int(p[1].strip())
                                foundContentLength = True
                            break

        except Exception as e:
            Sentry.Exception(self.getLogMsgPrefix()+ " exception thrown in http stream chunk reader", e)
            return 0

        # Check if we found a content length header
        if foundContentLength is False:
            # It ok if we didn't find it, since it's not required for boundary chunks
            # In this case, we will set the flag so future reads don't try again.
            self.ChunkedBodyHasNoContentLengthHeaders = True
            # And return the length of whatever we read
            return tempBufferFilledSize

        # We have a content-length!
        # Compute how much more we need to read.
        toRead = (frameSize + headerSize) - tempBufferFilledSize
        if toRead < 0:
            # Oops. This means we read into the next chunk.
            # TODO - we could update this logic to correct itself by appending this chunk data
            # on the next chunk, but as it stands it won't work.
            # So just put the stream into the no content-length mode and return what we read.
            self.Logger.error(self.getLogMsgPrefix()+ " http stream to read size is less than 0. FrameSize:"+str(frameSize) + " HeaderSize:"+str(headerSize) + " Read:"+str(tempBufferFilledSize))
            self.ChunkedBodyHasNoContentLengthHeaders = True
            return tempBufferFilledSize

        # Read the remainder of the chunk.
        if toRead > 0:
            data = self.doBodyRead(octoHttpResult, toRead)

            # If we hit the end of the body, return how much we read already.
            if data is None:
                return tempBufferFilledSize

            # Warn if twe didn't read it all
            if len(data) != toRead:
                self.Logger.warn(self.getLogMsgPrefix()+" while reading a boundary chunk, doBodyRead didn't return the full size we requested.")

            # Copy this data into the temp buffer
            self.BodyReadTempBuffer[tempBufferFilledSize:tempBufferFilledSize+len(data)] = data
            tempBufferFilledSize += len(data)

        # Update our read rate. This is a metric we send along in the stream if the it's a multipart stream, to know how fast we are reading it.
        # Basically for webcams streamed via http, it's the frame rate.
        nowSec = time.time()
        if self.MultipartReadTimestampSec == 0:
            # This is the first read of the stream, so setup the timer.
            self.MultipartReadTimestampSec = nowSec + 1.0

        # Check if we have gone into the next time slice.
        isFirstIncrement = True
        while self.MultipartReadTimestampSec < nowSec:
            # Increment by a fixed amount, to keep the FPS steady.
            self.MultipartReadTimestampSec += 1.0
            # Dump the counter value into the main stored value. This will be picked up by the message creation process and sent to the server.
            # Note if this spins multiple times, it will be zeroed out. That would mean there's a more than 1s gap in reading.
            if isFirstIncrement is False and self.MultipartReadsPerSecond == 0:
                self.Logger.warn("Multipart read per second stats hit a period where 0 reads happened for more than second.")
            self.MultipartReadsPerSecond = self.MissingBoundaryWarningCounter
            self.MissingBoundaryWarningCounter = 0
            isFirstIncrement = False

        # Now increment our counter, to account for the frame we just processed.
        self.MissingBoundaryWarningCounter += 1

        # Finally, return how much we put into the temp buffer!
        return tempBufferFilledSize


    def doBodyRead(self, octoHttpResult:OctoHttpRequest.Result, readSize):
        try:
            # Ensure there's an actual requests lib Response object to read from
            response = octoHttpResult.ResponseForBodyRead
            if response is None:
                raise Exception("doBodyRead was called with a result that has not Response object to read from.")

            # In the past we used the iter_content and such streaming function calls, but the calls had a few issues.
            #  1) They use a generator system where they yield data buffers. The generator has to remain referenced or the connection closes.
            #  2) Since the generator could only be created once, the chunk size was set on the first creation and couldn't be used.
            #
            # Thus in our case, we want to just read the response raw. This is what the chunk logic does under the hood anyways, so this path
            # is more direct and should be more efficient.
            data = response.raw.read(readSize)

            # If we got a data buffer return it.
            if data is not None and len(data) > 0:
                return data

            # Data will return b"" (aka empty buffer) when...
            #   1) The stream is closed and all of the data is consumed
            #   2) OR there was an error and there's a body but it can't be streamed.
            # Thus, when data returns empty, we need to check if there's content. If content != null we need to send it back.
            content = response.content
            if len(content) > 0:
                if len(content) > readSize:
                    self.Logger.warn("Http request has non-streamed content but it's larger than the requested readSize. Returning anyways.")
                return content

            # Otherwise we are done, return None to end the octostream.
            return None

        except requests.exceptions.ChunkedEncodingError as _:
            # This shouldn't happen now that we don't use the iter_content read, but it doesn't hurt.
            return None
        except requests.exceptions.StreamConsumedError as _:
            # When this exception is thrown, it means the entire body has been read.
            return None
        except urllib3.exceptions.ReadTimeoutError as _:
            # Fired then the read times out, this should just close the stream.
            # TODO - this will leave this stream with an incomplete body size, we should indicate that to the server.
            return None
        except Exception as e:
            # There doesn't seem to be an exception type for this one, so we will just catch it like this.
            if "IncompleteRead" in str(e):
                # Don't do the entire sentry exception print, since it's too long.
                self.Logger.warn("doBodyRead failed with an IncompleteRead, so the stream is done.")
                return None
            Sentry.Exception(self.getLogMsgPrefix()+ " exception thrown in doBodyRead. Ending body read.", e)
            return None

    # This is similar to doBodyRead, but it allows us to send chunks of the body over time.
    # The problem is for requests where the content length isn't known AND there's no boundary string, response.raw.read(size) will block
    # until the full amount of data requested is read. That doesn't work for things like event streams, because there's no boundary string and the full length is unknown,
    # but we want to stream the data as it arrives to us. To make doBodyRead efficient, we request a large read buffer, so if the event stream contains many small messages,
    # doBodyRead will block until it accumulates enough messages to fill the full buffer and send it.
    #
    # For normal requests with known content lengths, response.raw.read will read full buffers until the full content is known to be done, and then will return the final subset buffer,
    # so they can't get blocked like streaming event can. So if the event stream isn't sending data often, this can get stuck while waiting for the final bytes of a message.
    #
    # Event streams are an important fallback for OctoPrint, and is also what OctoFarm uses to stream instead of websockets.
    #
    # Thus, this function does many small reads (which isn't as efficient) but builds them into a larger buffer that's time limited. In this way, we ensure we are still streaming
    # messages every x amount of time, but we also don't stream super small data packets.
    #
    # Note this logic will always have a "one message" latency due to the way we get blocked on the socket. Even though we read small amounts, there's no way to know the full message
    # length, and thus there's no way to ask for only the remainder of the message. Thus, the end of the message will usually always get put into a pending read, but that read will block
    # until the buffer is filled the rest of the way with the n+1 message. Unfortunately that means we are always a message or so behind in the stream. Without being able to do a non-blocking request read,
    # there's no way to work around this.
    #
    # Ideally if we could just peak at the pending data length without blocking, we could do this much more efficiently.
    def doUnknownBodySizeRead(self, octoHttpResult:OctoHttpRequest.Result):

        # How much we will micro read, this needs to be quite small, to prevent getting "stuck" between messages.
        microReadSizeBytes = 300

        # How long we will build one big buffer before returning, in seconds.
        # Note the first read will get double this time.
        maxBufferBuildTimeSec = 0.050 #(50ms)

        # Vars
        buildReads = 0
        buffer = bytearray(2 * 1024)
        bufferSize = 0
        try:
            startSec = time.time()
            while True:
                # Do a small read, which will block until the full (small) size is read.
                # If nothing shows up to be read, this will wait until the http request read timeout expires, and then will return None.
                currentReadBuffer = self.doBodyRead(octoHttpResult, microReadSizeBytes)

                # If None is returned, we are done. Return the current buffer or None.
                if currentReadBuffer is None:
                    break

                # Copy into the existing buffer.
                buffer[bufferSize:bufferSize+len(currentReadBuffer)] = currentReadBuffer
                bufferSize += len(currentReadBuffer)
                buildReads += 1

                # We have noticed for some systems (like OctoFarm) the first read takes a while for the event stream to get
                # going, and then it gets data. The problem here is we unblock with the first chunk of data and then we are at our time
                # limit and return. Instead, it's more ideal to allow one more time limit so we can read the full message and then return it.
                if self.IsFirstMicroBodyRead:
                    self.IsFirstMicroBodyRead = False
                    startSec = time.time()

                # Check if it's time to be done.
                if time.time() - startSec > maxBufferBuildTimeSec:
                    break

            # If we broke out, it's time to return what we have.
            # If we didn't read anything, we want to return none, to indicate we are done or there was a read timeout.
            if bufferSize == 0:
                return None

            # Return the subset of the buffer we filled.
            return buffer[0:bufferSize]

        except Exception as e:
            Sentry.Exception(self.getLogMsgPrefix()+ " exception thrown in doUnknownBodySizeRead. Ending body read.", e)
            return None


    # Based on the content length and the content type, determine if we should do a doUnknownBodySizeRead read.
    # Read doUnknownBodySizeRead about why we need to use it, but since it's not efficient, we only want to use it when we know we should.
    def shouldDoUnknownBodySizeRead(self, contentTypeLower_CanBeNone, contentLengthLower_CanBeNone):
        # If there's a known content length, there's no need to do this, because the normal read will fill the requested buffer size
        # but return the remainder subset immediately when the full buffer is read.
        if contentLengthLower_CanBeNone is not None:
            return False

        # If we didn't get a content type, default to true since we don't know what this is.
        if contentTypeLower_CanBeNone is None:
            return True

        # mjpegstreamer doesn't return a content type for snapshots (which is annoying) so if we know the content is a single image, don't stream it, allow the full
        # buffer read to read it in one bit.
        if contentTypeLower_CanBeNone == "image/jpeg":
            return False

        # Otherwise, default to true
        return True


    # To speed up page load, we will defer lower pri requests while higher priority requests
    # are executing.
    def checkForDelayIfNotHighPri(self):
        # Allow anything above Normal priority to always execute
        if self.WebStreamOpenMsg.MsgPriority() < MessagePriority.MessagePriority.Normal:
            return
        # Otherwise, we want to block for a bit if there's a high pri stream processing.
        self.WebStream.BlockIfHighPriStreamActive()

    # Formatting helper.
    def _FormatFloat(self, value:float) -> str:
        return str(format(value, '.3f'))
