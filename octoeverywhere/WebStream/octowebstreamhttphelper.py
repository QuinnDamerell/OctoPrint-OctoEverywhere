import time
import logging
import threading

import requests
import urllib3
import octoflatbuffers

from .octoheaderimpl import HeaderHelper
from .octoheaderimpl import BaseProtocol
from ..octohttprequest import OctoHttpRequest
from ..octostreammsgbuilder import OctoStreamMsgBuilder
from ..Webcam.webcamhelper import WebcamHelper
from ..commandhandler import CommandHandler
from ..compression import Compression, CompressionContext
from ..sentry import Sentry
from ..compat import Compat
from ..Proto import HttpHeader
from ..Proto import WebStreamMsg
from ..Proto import MessageContext
from ..Proto import HttpInitialContext
from ..Proto import DataCompression
from ..Proto import OeAuthAllowed
from ..Proto.PathTypes import PathTypes

# A wrapper that allows us to pass around a ref to the per message builder object.
class MsgBuilderContext:

    # From testing, beyond the dynamic size of the send buffer, the rest of the overhead is from 500-2000 bytes.
    # This overhead does include some dynamic things, like the return headers, and other random values.
    # It's way better to be over the size than under, since if we are under the buffer will resize, double in size, and do a copy.
    # Thus, we allocate 10k bytes for the overhead, which should be more than enough.
    c_MsgStreamOverheadSize = 1024 * 10

    def __init__(self):
        self.Builder:octoflatbuffers.Builder = None

    def CreateBuilder(self, knownBodySizeBytes = 0):
        self.Builder = octoflatbuffers.Builder(knownBodySizeBytes + self.c_MsgStreamOverheadSize)


#
# A helper object that handles http request for the web stream system.
#
# The helper can close the stream by calling close directly on the WebStream object
# or by returning true from `IncomingServerMessage`
#
class OctoWebStreamHttpHelper:

    # Called by the main socket thread so this should be quick!
    def __init__(self, streamId, logger:logging.Logger, webStream, webStreamOpenMsg:WebStreamMsg.WebStreamMsg, openedTime):
        self.Id = streamId
        self.Logger = logger
        self.WebStream = webStream
        self.WebStreamOpenMsg = webStreamOpenMsg
        self.IsClosed = False
        self.OpenedTime = openedTime
        self.CompressionContext = CompressionContext(self.Logger)

        # Vars for response reading
        self.BodyReadTempBuffer:bytearray = None
        self.ChunkedBodyHasNoContentLengthHeaders = False
        self.CompressionType:DataCompression.DataCompression = None
        self.CompressionTimeSec = -1
        self.MissingBoundaryWarningCounter = 0
        self.IsUsingFullBodyBuffer = False
        self.IsUsingCustomBodyStreamCallbacks = False

        # If this doesn't not equal None, it means we know how much data to expect.
        self.KnownFullStreamUploadSizeBytes = None
        self.UploadBytesReceivedSoFar = 0
        self.UploadBuffer = None

        # Unknown body size chunk reader
        # If this is not None, we are doing the unknown body read. Then the rest of the body reads must use this same system.
        self.UnknownBodyChunkReadContext:UnknownBodyChunkReadContext = None

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
        # Set the flag so all of the looping http operations will stop.
        self.IsClosed = True

        # Important! If we are doing a unknown chunk read, we need to set the wait event to unblock the stream read thread.
        # This will cause the thread to wake up, it will see the IsClosed flag, return, and then allow the web request to close,
        # which will end the unknown body read thread.
        if self.UnknownBodyChunkReadContext is not None:
            # Call set under lock, to ensure the other thread doesn't clear it without us seeing it.
            with self.UnknownBodyChunkReadContext.BufferLock:
                self.UnknownBodyChunkReadContext.BufferDataReadyEvent.set()


    # Called when a new message has arrived for this stream from the server.
    # This function should throw on critical errors, that will reset the connection.
    # Returning true will case the websocket to close on return.
    def IncomingServerMessage(self, webStreamMsg:WebStreamMsg.WebStreamMsg):

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

            # Do the request. This will block this thread until it's done and the entire response is sent.
            # We want to make sure we destroy the compression context after this returns, no matter what.
            with self.CompressionContext:
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

        # Before we handle the request, see if this is a webcam stream request we need to handle specially.
        if Compat.HasRelayWebcamStreamDetector():
            relativeOrAbsolutePath = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Path())
            # If needed, this will update the send headers to make it look like an oracle stream or snapshot request.
            Compat.GetRelayWebcamStreamDetector().OnIncomingRelayRequest(relativeOrAbsolutePath, sendHeaders)

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
            # Note we must always allow absolute paths, since these can be services like Spoolman or OctoFarm.
            if OctoHttpRequest.GetDisableHttpRelay() and httpInitialContext.PathType() != PathTypes.Absolute:
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
            if isFromCache is False:
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
                if octoHttpResult.BodyBufferCompressionType != DataCompression.DataCompression.None_:
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
            contentLength:int = None
            # We will also look for the content type, and look for a boundary string if there is one
            # The boundary stream is used for webcam streams, and it's an ideal place to package and send each frame
            boundaryStr:str = None
            # Pull out the content type value, so we can use it to figure out if we want to compress this data or not
            contentTypeLower:str =None
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

            # If the content length is known, tell the compression system, which will help performance.
            if contentLength is not None:
                self.CompressionContext.SetTotalCompressedSizeOfData(contentLength)

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
                # In the past we started the message here, but the problem is we don't really know how large to make it.
                # So instead, we build this context and let the body read function tell us how much data it read.
                builderContext = MsgBuilderContext()

                # Unless we are skipping the body read, do it now.
                # If there's a 304, we might have a body, but we don't want to read it.
                # If the response is 204, there will be no content, so don't bother.
                if octoHttpResult.StatusCode == 304 or octoHttpResult.StatusCode == 204:
                    # Use zero read defaults.
                    nonCompressedBodyReadSize = 0
                    lastBodyReadLength = 0
                    dataOffset = None
                    # Note that compressBody will be set to false in the special case below.
                else:
                    # Start by reading data from the response.
                    # This function will return a read length of 0 and a null data offset if there's nothing to read.
                    # Otherwise, it will return the length of the read data and the data offset in the buffer.
                    nonCompressedBodyReadSize, lastBodyReadLength, dataOffset = self.readContentFromBodyAndMakeDataVector(builderContext, octoHttpResult, boundaryStr, compressBody, contentTypeLower, contentLength, responseHandlerContext)
                contentReadBytes += lastBodyReadLength
                nonCompressedContentReadSizeBytes += nonCompressedBodyReadSize

                # Ensure that the build was created by now. In most cases it's created with the body read, but in other cases where there's no body, we create it now.
                if builderContext.Builder is None:
                    builderContext.CreateBuilder()

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
                if dataOffset is not None and contentLength is not None and nonCompressedContentReadSizeBytes < contentLength:
                    # This might happen if the connection closes unexpectedly before the transfer is done.
                    self.Logger.warn(self.getLogMsgPrefix()+f" we expected a fixed length response, but the body read completed before we read it all. cl:{contentLength}, got:{nonCompressedContentReadSizeBytes} {uri}")

                # Check if this is the last message.
                # This is the last message if...
                #  - The data offset is ever None, this means we have read the entire body as far as the request system is concerned.
                #  - We have an expected length and we have hit it or gone over it.
                isLastMessage = dataOffset is None or (contentLength is not None and nonCompressedContentReadSizeBytes >= contentLength)

                # Special Case - If this request has no body, we need to make sure we the `compressBody` flag is set to false.
                # For example, if this request is not 200 but has no content, compressBody might be set but we didn't read any body, so we didn't compress anything,
                # and thus self.CompressionType will not be set.
                if isLastMessage and nonCompressedContentReadSizeBytes == 0:
                    self.Logger.debug(self.getLogMsgPrefix()+" read no body so we will turned off the compressBody flag.")
                    compressBody = False

                # If this is the first response in the stream, we need to send the initial http context and status code.
                httpInitialContextOffset = None
                statusCode = None
                if isFirstResponse is True:
                    # Set the status code, so it's sent.
                    statusCode = octoHttpResult.StatusCode

                    # Gather the headers, if there are any. This will return None if there are no headers to send.
                    headerVectorOffset = self.buildHeaderVector(builderContext.Builder, octoHttpResult)

                    # Build the initial context. We should always send a http initial context on the first response,
                    # even if there are no headers in t.
                    HttpInitialContext.Start(builderContext.Builder)
                    if headerVectorOffset is not None:
                        HttpInitialContext.AddHeaders(builderContext.Builder, headerVectorOffset)
                    httpInitialContextOffset = HttpInitialContext.End(builderContext.Builder)

                # Now build the return message
                WebStreamMsg.Start(builderContext.Builder)
                WebStreamMsg.AddStreamId(builderContext.Builder, self.Id)
                # Indicate this message has data, even if it's just the initial http context (because there's no data for this request)
                WebStreamMsg.AddIsControlFlagsOnly(builderContext.Builder, False)
                if statusCode is not None:
                    WebStreamMsg.AddStatusCode(builderContext.Builder, statusCode)
                if dataOffset is not None:
                    WebStreamMsg.AddData(builderContext.Builder, dataOffset)
                if httpInitialContextOffset is not None:
                    # This should always be not null for the first response.
                    WebStreamMsg.AddHttpInitialContext(builderContext.Builder, httpInitialContextOffset)
                if isFirstResponse is True and contentLength is not None:
                    # Only on the first response, if we know the full size, set it.
                    WebStreamMsg.AddFullStreamDataSize(builderContext.Builder, contentLength)
                if compressBody:
                    # If we are compressing, we need to add what we are using and what the original size was.
                    if self.CompressionType is None:
                        raise Exception("The body of this message should be compressed but not compression type is set.")
                    WebStreamMsg.AddDataCompression(builderContext.Builder, self.CompressionType)
                    WebStreamMsg.AddOriginalDataSize(builderContext.Builder, nonCompressedBodyReadSize)
                if isLastMessage:
                    # If this is the last message because we know the body is all
                    # sent, indicate that the data stream is done and send the close message.
                    WebStreamMsg.AddIsDataTransmissionDone(builderContext.Builder, True)
                    WebStreamMsg.AddIsCloseMsg(builderContext.Builder, True)
                if self.MultipartReadsPerSecond != 0:
                    # If this is a multipart stream (webcam streaming), every 1 second a value will be dumped into MultipartReadsPerSecond
                    # when it's there, we want to send it to the server for telemetry, and then zero it out.
                    if self.Logger.isEnabledFor(logging.DEBUG):
                        self.Logger.debug(f"Multipart Stats; reads per second: {str(self.MultipartReadsPerSecond)}, body read high water mark {str(format(self.BodyReadTimeHighWaterMarkSec*1000.0, '.2f'))}ms, socket write high water mark {str(format(self.ServiceUploadTimeHighWaterMarkSec*1000.0, '.2f'))}ms")
                    if self.MultipartReadsPerSecond > 255 or self.MultipartReadsPerSecond < 0:
                        self.Logger.warn("self.MultipartReadsPerSecond is larger than uint8. "+str(self.MultipartReadsPerSecond))
                        self.MultipartReadsPerSecond  = 255
                    WebStreamMsg.AddMultipartReadsPerSecond(builderContext.Builder, self.MultipartReadsPerSecond)
                    self.MultipartReadsPerSecond = 0
                    # Also attach the other stats.
                    bodyReadTimeHighWaterMarkMs = int(self.BodyReadTimeHighWaterMarkSec * 1000.0)
                    self.BodyReadTimeHighWaterMarkSec = 0.0
                    if bodyReadTimeHighWaterMarkMs > 65535 or bodyReadTimeHighWaterMarkMs < 0:
                        bodyReadTimeHighWaterMarkMs  = 65535
                    WebStreamMsg.AddBodyReadTimeHighWaterMarkMs(builderContext.Builder, bodyReadTimeHighWaterMarkMs)

                    serviceUploadTimeHighWaterMarkMs = int(self.ServiceUploadTimeHighWaterMarkSec * 1000.0)
                    self.ServiceUploadTimeHighWaterMarkSec = 0.0
                    if serviceUploadTimeHighWaterMarkMs > 65535 or serviceUploadTimeHighWaterMarkMs < 0:
                        serviceUploadTimeHighWaterMarkMs  = 65535
                    WebStreamMsg.AddSocketSendTimeHighWaterMarkMs(builderContext.Builder, serviceUploadTimeHighWaterMarkMs)

                webStreamMsgOffset = WebStreamMsg.End(builderContext.Builder)

                # Wrap in the OctoStreamMsg and finalize.
                buffer, msgStartOffsetBytes, msgSizeBytes = OctoStreamMsgBuilder.CreateOctoStreamMsgAndFinalize(builderContext.Builder, MessageContext.MessageContext.WebStreamMsg, webStreamMsgOffset)

                # Send the message.
                # If this is the last, we need to make sure to set that we have set the closed flag.
                serviceSendStartSec = time.time()
                self.WebStream.SendToOctoStream(buffer, msgStartOffsetBytes, msgSizeBytes, isLastMessage, True)
                thisServiceSendTimeSec = time.time() - serviceSendStartSec
                self.ServiceUploadTimeSec += thisServiceSendTimeSec
                if thisServiceSendTimeSec > self.ServiceUploadTimeHighWaterMarkSec:
                    self.ServiceUploadTimeHighWaterMarkSec = thisServiceSendTimeSec

                # Do a debug check to see if our pre-allocated flatbuffer size was too small.
                # If this fires often, we should increase the c_MsgStreamOverheadSize size.
                finalFullBufferBytes = len(buffer)
                if finalFullBufferBytes > lastBodyReadLength + builderContext.c_MsgStreamOverheadSize and self.Logger.isEnabledFor(logging.DEBUG):
                    delta = msgSizeBytes - (lastBodyReadLength + builderContext.c_MsgStreamOverheadSize)
                    self.Logger.warn(f"The flatbuffer internal buffer had to be resized from the guess we set. Flatbuffer full buffer size: {finalFullBufferBytes}, last body read length: {lastBodyReadLength}; overage delta: {delta}")

                # Clear this flag
                isFirstResponse = False
                messageCount += 1

            # Log about it - only if debug is enabled. Otherwise, we don't want to waste time making the log string.
            responseWriteDone = time.time()
            if self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug(self.getLogMsgPrefix() + method+" [upload:"+str(format(requestExecutionStart - self.OpenedTime, '.3f'))+"s; request_exe:"+str(format(requestExecutionEnd - requestExecutionStart, '.3f'))+"s; send:"+str(format(responseWriteDone - requestExecutionEnd, '.3f'))+"s; body_read:"+str(format(self.BodyReadTimeSec, '.3f'))+"s; compress:"+str(format(self.CompressionTimeSec, '.3f'))+"s; octo_stream_upload:"+str(format(self.ServiceUploadTimeSec, '.3f'))+"s] size:("+str(nonCompressedContentReadSizeBytes)+"->"+str(contentReadBytes)+") compressed:"+str(compressBody)+" msgcount:"+str(messageCount)+" microreads:"+str(self.UnknownBodyChunkReadContext is not None)+" type:"+str(contentTypeLower)+" status:"+str(octoHttpResult.StatusCode)+" cached:"+str(isFromCache)+" for " + uri)


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
            # This will do a copy and set the copy as the upload buffer.
            self.UploadBuffer = self.UploadBuffer[0:self.UploadBytesReceivedSoFar]


    def copyUploadDataFromMsg(self, webStreamMsg:WebStreamMsg.WebStreamMsg):
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
    def decompressBufferIfNeeded(self, webStreamMsg:WebStreamMsg.WebStreamMsg) -> bytearray:
        # Get the compression type.
        compressionType = webStreamMsg.DataCompression()
        dataByteArray = webStreamMsg.DataAsByteArray()
        if compressionType is DataCompression.DataCompression.None_:
            return dataByteArray
        # It's compressed, decompress it.
        return Compression.Get().Decompress(self.CompressionContext, dataByteArray, webStreamMsg.OriginalDataSize(), webStreamMsg.IsDataTransmissionDone(), compressionType)


    def checkForNotModifiedCacheAndUpdateResponseIfSo(self, sentHeaders, octoHttpResult:OctoHttpRequest.Result):
        # Check if the sent headers have any conditional http headers.
        requestEtag = None
        requestModifiedDate = None
        for key in sentHeaders:
            keyLower = key.lower()
            if keyLower == "if-modified-since":
                requestModifiedDate = sentHeaders[key]
            if keyLower == "if-none-match":
                requestEtag = sentHeaders[key]
                # If the request etag starts with the weak indicator, remove it
                if requestEtag.startswith("W/"):
                    requestEtag = requestEtag[2:]

        # If there were none found, there's nothing do to.
        if requestEtag is None and requestModifiedDate is None:
            return

        # Look through the response headers
        responseEtag = None
        responseModifiedDate = None
        headers = octoHttpResult.Headers
        for key in headers:
            keyLower = key.lower()
            if keyLower == "etag":
                responseEtag = headers[key]
            if keyLower == "last-modified":
                responseModifiedDate = headers[key]
            if responseEtag is not None and responseModifiedDate is not None:
                break

        # See if there are any matches.
        # If we have both values, both must match.
        convertTo304 = False
        # If we have both, both must match
        if requestEtag is not None and requestModifiedDate is not None:
            if responseEtag is not None and responseModifiedDate is not None and requestEtag == responseEtag and requestModifiedDate == responseModifiedDate:
                convertTo304 = True
        # If we only have the date, see if it matches
        elif requestModifiedDate is not None:
            if responseModifiedDate is not None and requestModifiedDate == responseModifiedDate:
                convertTo304 = True
        # If we only have the etag, see if it matches
        elif requestEtag is not None:
            if responseEtag is not None and requestEtag == responseEtag:
                convertTo304 = True

        # Check if we have something to do.
        if convertTo304 is False:
            return

        # Convert the response.
        self.updateResponseFor304(octoHttpResult)


    def updateResponseFor304(self, octoHttpResult:OctoHttpRequest.Result):
        self.Logger.info(f"Converting request for {octoHttpResult.Url} {octoHttpResult.StatusCode} to a 304.")
        # First of all, update the status code.
        octoHttpResult.StatusCode = 304
        # Next, if this was a cached result or a result that has a full body buffer, we need to clear it.
        octoHttpResult.ClearFullBodyBuffer()
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
    def shouldCompressBody(self, contentTypeLower:str, octoHttpResult:OctoHttpRequest.Result, contentLengthOpt:int):
        # Compression isn't too expensive in terms of cpu cost but for text, it drastically
        # cuts the size down (ike a 75% reduction.) So we are quite liberal with our compression.

        # If there is a full body buffer and and it's already compressed, always return true.
        # This ensures the message is flagged correctly for compression and the body reading system
        # will also read the flag and skip the compression.
        if octoHttpResult.BodyBufferCompressionType != DataCompression.DataCompression.None_:
            return True

        # Make sure we have a known length and it's not too small to compress.
        if contentLengthOpt is not None and contentLengthOpt < Compression.MinSizeToCompress:
            return False

        # If we don't know what this is, we don't want to compress it.
        # Compressing the body of a compressed thing will make it larger and takes a good amount of time,
        # so we don't want to waste time on it.
        if contentTypeLower is None:
            return False

        # We will compress...
        #   - Any thing that has text/ in it
        #   - Anything that says it's javascript
        #   - Anything that says it's json
        #   - Anything that's xml
        #   - Anything that's svg
        #   - Anything that's a application/octet-stream - moonraker sends unknown file types as these.
        return (contentTypeLower.find("text/") != -1 or contentTypeLower.find("javascript") != -1
                or contentTypeLower.find("json") != -1 or contentTypeLower.find("xml") != -1
                or contentTypeLower.find("svg") != -1 or contentTypeLower.find("application/octet-stream") != -1)


    # Reads data from the response body, puts it in a data vector, and returns the offset.
    # If the body has been fully read, this should return ogLen == 0, len = 0, and offset == None
    # The read style depends on the presence of the boundary string existing.
    def readContentFromBodyAndMakeDataVector(self, builderContext:MsgBuilderContext, octoHttpResult:OctoHttpRequest.Result, boundaryStr_opt, shouldCompress, contentTypeLower_NoneIfNotKnown:str, contentLength_NoneIfNotKnown:int, responseHandlerContext):
        # This is the max size each body read will be. Since we are making local calls, most of the time we will always get this full amount as long as theres more body to read.
        # This size is a little under the max read buffer on the server, allowing the server to handle the buffers with no copies.
        #
        # 3/24/24 - We did a lot of direct download testing to tweak this buffer size and the server read size, these were the best values able to hit about 223mbps.
        # With the current values, the majority of the time is spent sending the data on the websocket.
        #
        # But NOTE! This size is the actual size that will be allocated for the read buffer (in the stream class) and then the buffer is sliced by how much
        # is read. So we can't make this value too large, or we will be allocating big buffers.
        # This is 490kb
        defaultBodyReadSizeBytes = 490 * 1024

        # If we are going to compress this read, use a much higher number. Since most of what we compress is text,
        # and that text usually compresses down to 25% of the og size, we will use a x4 multiplier.
        # We do want to make sure this value isn't too big, because we dont want to allocate a huge buffer on low memory systems.
        if shouldCompress:
            defaultBodyReadSizeBytes = defaultBodyReadSizeBytes * 4

        # Finally check if we know the content length of the request. If we do, we will set the buffer to be exactly that value.
        # This is a lot more efficient, because we only allocate a buffer the exact size we need for the request.
        # But we want to limit the max size of the buffer, so we don't allocate a huge buffer for a large request.
        if contentLength_NoneIfNotKnown is not None and contentLength_NoneIfNotKnown < defaultBodyReadSizeBytes:
            defaultBodyReadSizeBytes = contentLength_NoneIfNotKnown

        # Some requests like snapshot requests will already have a fully read body. In this case we use the existing body buffer instead of reading from the body.
        finalDataBuffer = None
        finalDataBufferMv_CanBeNone = None
        try:
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
                    # If we get a length, we have a buffer to use.
                    if readLength != 0:
                        # We create a memory view from the buffer, which is a zero copy operation and zero copy slicing.
                        # This allows us to pass the buffer around without copying it, but we do have to be sure to release the
                        # memory views when we are done.
                        finalDataBufferMv_CanBeNone = memoryview(self.BodyReadTempBuffer)
                        finalDataBuffer = finalDataBufferMv_CanBeNone[0:readLength]
                else:
                    if self.UnknownBodyChunkReadContext is not None or (responseHandlerContext is None and self.shouldDoUnknownBodyChunkRead(contentTypeLower_NoneIfNotKnown, contentLength_NoneIfNotKnown)):
                        # According to the HTTP 1.1 spec, if there's no content length and no boundary string, then the body is chunk based transfer encoding.
                        # Note that once we do on read as an unknown body size chunk read, we need to always do it, since there's a thread reading the body.
                        finalDataBuffer = self.doUnknownBodyChunkRead(octoHttpResult)
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
                                defaultBodyReadSizeBytes = 1024 * 1024 * 2
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
                    # Important! If the response handler has edited the buffer, we need to update the content length to match the new size.
                    # This is safe to do because currently we always read the entire buffer for a responseHandlerContext into one buffer, thus there's only one read, and this is the read.
                    # The function that calls readContentFromBodyAndMakeDataVector will correct the content header length in the main class, but we must update the encryption context
                    # otherwise the zstandard lib encryption will fail.
                    self.CompressionContext.SetTotalCompressedSizeOfData(len(finalDataBuffer))

            # If we were asked to compress, do it
            originalBufferSize = len(finalDataBuffer)

            # Check to see if this was a full body buffer, if it was already compressed.
            if octoHttpResult.BodyBufferCompressionType != DataCompression.DataCompression.None_:
                # The full body buffer was already compressed and set, so update the other compression values.
                originalBufferSize = octoHttpResult.BodyBufferPreCompressSize
                if self.CompressionType is not None:
                    raise Exception(f"The BodyBufferCompressionType tried to be set but the compression was already set! It is {self.CompressionType} and now tried to be {octoHttpResult.BodyBufferCompressionType}")
                self.CompressionType = octoHttpResult.BodyBufferCompressionType

            # Otherwise, check if we should compress
            elif shouldCompress:
                compressionResult = Compression.Get().Compress(self.CompressionContext, finalDataBuffer)
                finalDataBuffer = compressionResult.Bytes
                # Init and update the total compression time if needed.
                if self.CompressionTimeSec < 0:
                    self.CompressionTimeSec = 0
                self.CompressionTimeSec += compressionResult.CompressionTimeSec
                # Set the compression type, this should only be set once and can't change.
                if self.CompressionType is None:
                    self.CompressionType = compressionResult.CompressionType
                elif self.CompressionType != compressionResult.CompressionType:
                    raise Exception(f"The data compression has changed mid stream! It was {self.CompressionType} and now tried to be {compressionResult.CompressionType}")

            # We have a data buffer and we know how large it will be.
            # Since this buffer is the majority of the flatbuffer message, we use it to create the initial size of the flatbuffer.
            # This is important, because if the buffer is too small, it will double the size in a loop until it's big enough, which is silly.
            # So ideally we use the size of the body buffer we will actually send, and add enough overhead to contain the rest of the msg data.
            finalDataBufferSizeBytes = len(finalDataBuffer)
            builderContext.CreateBuilder(finalDataBufferSizeBytes)

            return (originalBufferSize, len(finalDataBuffer), builderContext.Builder.CreateByteVector(finalDataBuffer))
        finally:
            # If we used a memory view, release it.
            # This also means that the finalDataBuffer is a memory view.
            if finalDataBufferMv_CanBeNone is not None:
                finalDataBuffer.release()
                finalDataBufferMv_CanBeNone.release()


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


    def doBodyRead(self, octoHttpResult:OctoHttpRequest.Result, readSize:int):
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
            #
            # Note, whatever size we pass in will be allocated as a buffer, filled, and then sliced.
            # So if we pass in a huge value, we will get a big buffer allocated.
            # So if we know the size, we should use it, so that the buffer allocated it the same amount that's returned.
            # Also note, any improvements made here should be updated in ReadAllContentFromStreamResponse as well!
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


    def doUnknownBodyChunkReadThread(self):
        try:
            if self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug(f"{self.getLogMsgPrefix()}Starting chunk read thread.")

            # Get the Request response object.
            response = self.UnknownBodyChunkReadContext.HttpResult.ResponseForBodyRead
            if response is None:
                raise Exception("doUnknownBodyChunkReadThread was called with a result that has not Response object to read from.")

            # Loop until the stream is closed.
            # Remember we use the raw stream read, because it will read and entire chunk and return it as soon as it's ready.
            # BUG - response.raw.stream doesn't close when we close the http request from our side. (but it says it should?)
            # If server we are calling closes it shutdown correctly, but if our server drops the connection it will not close.
            # So what happens is that the stream will timeout from the httprequest.MakeHttpCallAttempt timeout, or when it gets a new chunk it will end.
            # That's not great, but it's not super common, so it's fine.
            gen = response.raw.stream(amt=None)
            for i in gen:
                # When we have a new buffer, add it to the list under lock.
                with self.UnknownBodyChunkReadContext.BufferLock:
                    self.UnknownBodyChunkReadContext.BufferList.append(i)
                    # Call set under lock, to ensure the other thread doesn't clear it without us seeing it.
                    self.UnknownBodyChunkReadContext.BufferDataReadyEvent.set()

            # When the loop exits, the body read is complete and the stream is closed.

        except Exception as e:
            # If the web stream is already closed, don't bother logging the exception.
            # These exceptions happen for use cases as above, where stream() doesn't close in time and such.
            # Note the exception can be a timeout, but it can also be a "doesn't have a read" function error bc if the socket gets data the lib will try to call read on a fp that's closed and set to None. :/
            if self.IsClosed is False:
                Sentry.Exception(self.getLogMsgPrefix()+ " exception thrown in doUnknownBodyChunkReadThread", e)
        finally:
            # Ensure we always set this flag, so the web stream will know the body read is done.
            self.UnknownBodyChunkReadContext.ReadComplete = True

            # Set the event to break the stream read wait, so it will shutdown.
            # Call set under lock, to ensure the other thread doesn't clear it without us seeing it.
            with self.UnknownBodyChunkReadContext.BufferLock:
                self.UnknownBodyChunkReadContext.BufferDataReadyEvent.set()

            try:
                if self.Logger.isEnabledFor(logging.DEBUG):
                    self.Logger.debug(f"{self.getLogMsgPrefix()}Exiting chunk read thread.")
            except Exception:
                pass


    # This function should be used if there's no content length and there's no boundary string.
    # In that case, the HTTP1.1 standard says the body content must be chunk based transfer encoded, which is what this function does.
    # Most of the time these HTTP calls are for streams, like an event stream, log stream, etc.
    #
    # In the past we had two problems with this:
    #   1) A body read() will block until the size requested is full, which means we can't stream chunks as they come in.
    #   2) We then tired to do micro reads to build a buffer but it allowed us to return if there was enough. But this still failed because read still needs to fill the buffer,
    #      so if we wanted to read the last 5 bytes of the buffer but set our size to 10, it would block until the final 5 bytes were read.
    #
    # So, the right way to do this is with response.raw.stream().
    # This will read each chunk as it comes in, and return each complete chunk. This is the same way a web browser will handle the data, where it won't handle the data until the entire
    # chunk is read. So there's no need to stream sub-chunks.
    #
    # There's still a problem with stream, which is it will block until the next chunk is ready. But for us, once we have data, we want to send it. Thus we must spin off a thread to do
    # the stream reading, and then transfer the buffer. Also stream() is a generator, so it can't be re-entered.
    #
    def doUnknownBodyChunkRead(self, httpResult:OctoHttpRequest.Result):

        # Even though we read complete chunks as they come in, we might want to buffer smaller chunks up
        # before sending them so the compression and stream is more efficient.
        # This does need to be small, because we wan't reading this min time period back to back,
        # we are reading a chunk, doing all of the send logic, and then spinning back to here.
        # So if we set this at exactly 16.6 for a 60fps stream, for example, we will fall behind.
        minBufferBuildTimeSec = 0.010 # 10ms

        # Just as a sanity check, we will define the max amount of time we will wait for one chunk.
        # This will make sure we don't get stuck in a loop if there are any bugs.
        maxChunkReadTimeSec = 20 * 60 * 60 # 20 hours

        # If this is the first time, setup the unknown body read info.
        # Once this is defined, this body read method must be used for the rest of the request.
        if self.UnknownBodyChunkReadContext is None:
            context = UnknownBodyChunkReadContext(httpResult)
            context.Thread = threading.Thread(target=self.doUnknownBodyChunkReadThread)
            self.UnknownBodyChunkReadContext = context
            context.Thread.start()

        try:
            startSec = time.time()
            chunkBufferList = None

            # Since we will always sleep for at least the min time, there's no need to do work until the min time is meet.
            # If we did do the loop, we would just end up spinning and sleeping again.
            time.sleep(minBufferBuildTimeSec)

            # Try to read a chunk or wait for the read to be done.
            # Only try to read while the stream is open.
            while self.IsClosed is False:

                # First, sanity check we haven't been running forever.
                now = time.time()
                if now - startSec > maxChunkReadTimeSec:
                    raise Exception(f"doUnknownBodyChunkRead has been waiting for a chunk for {maxChunkReadTimeSec} seconds. This is an error.")

                # Next, check if there are any new buffers to read.
                with self.UnknownBodyChunkReadContext.BufferLock:
                    if len(self.UnknownBodyChunkReadContext.BufferList) > 0:
                        # If there's new chunks, grab them all and reset the buffer list.
                        if chunkBufferList is None:
                            chunkBufferList = self.UnknownBodyChunkReadContext.BufferList
                        else:
                            chunkBufferList += self.UnknownBodyChunkReadContext.BufferList
                        self.UnknownBodyChunkReadContext.BufferList = []
                        # Clear the event under lock, so we don't miss a new set.
                        self.UnknownBodyChunkReadContext.BufferDataReadyEvent.clear()

                # If we got some chunks, see if we are past the min chunk read time or if the chunk stream is complete.
                if chunkBufferList is not None and now - startSec > minBufferBuildTimeSec:
                    break

                # Finally, AFTER we checked if we have new buffers, check is the read is done.
                # Note we have to do this after we grab any new buffers in the list, because we can have pending chunks from before the stream is closed.
                if self.UnknownBodyChunkReadContext.ReadComplete:
                    break

                # If we don't have a chunk, wait on the event until we have something.
                # This will return when there's new chunks ready, ReadComplete is set, or it hits a timeout.
                self.UnknownBodyChunkReadContext.BufferDataReadyEvent.wait(maxChunkReadTimeSec)

            # If we broke out of the loop and we have no chunks to send, we are done.
            if chunkBufferList is None:
                return None

            # Append all of the chunks together and return the buffer!
            # Optimize for the single chunk scenario.
            if len(chunkBufferList) == 1:
                return chunkBufferList[0]

            # Find the final buffer length.
            totalLength = sum(len(b) for b in chunkBufferList)

            # Allocate a buffer to hold all of the chunks.
            finalBuffer = bytearray(totalLength)
            offset = 0
            for buffer in chunkBufferList:
                view = memoryview(buffer)
                with view:
                    finalBuffer[offset:offset + len(view)] = view
                    offset += len(view)

            # Sanity check
            if len(finalBuffer) != totalLength:
                raise Exception(f"Final appended buffer was {len(finalBuffer)} but it should have been {totalLength}")

            # Return!
            return finalBuffer

        except Exception as e:
            Sentry.Exception(self.getLogMsgPrefix()+ " exception thrown in doUnknownBodySizeRead. Ending body read.", e)
            return None


    # Based on the content length and the content type, determine if we should do a doUnknownBodySizeRead read.
    # Read doUnknownBodySizeRead about why we need to use it, but since it's not efficient, we only want to use it when we know we should.
    def shouldDoUnknownBodyChunkRead(self, contentTypeLower_CanBeNone:str, contentLengthLower_CanBeNone:int):

        # If this is set, we are already doing a unknown body chunk read, so we must keep doing it.
        if self.UnknownBodyChunkReadContext is not None:
            return True

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
        # This isn't used at all right now.
        pass
        # Allow anything above Normal priority to always execute
        # if self.WebStreamOpenMsg.MsgPriority() < MessagePriority.MessagePriority.Normal:
        #     return
        # # Otherwise, we want to block for a bit if there's a high pri stream processing.
        # self.WebStream.BlockIfHighPriStreamActive()

    # Formatting helper.
    def _FormatFloat(self, value:float) -> str:
        return str(format(value, '.3f'))


# Used to capture the context of the unknown body read thread.
class UnknownBodyChunkReadContext:

    def __init__(self, httpResult:OctoHttpRequest.Result) -> None:
        self.HttpResult = httpResult
        self.Thread:threading.Thread = None
        self.BufferLock = threading.Lock()
        self.BufferDataReadyEvent = threading.Event()

        # We use a list so we can efficiently append all of the pending buffers at once when they are being sent.
        self.BufferList = []

        # Set to true when the read is done either from the end of the body or an error.
        # Once true, it will never read again, but we do need to process the BufferList
        self.ReadComplete = False
