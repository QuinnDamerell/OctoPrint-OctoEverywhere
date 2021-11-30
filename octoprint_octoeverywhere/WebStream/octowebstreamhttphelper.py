# namespace: WebStream

import requests
import sys
import time
import zlib

import brotli

from .octoheaderimpl import HeaderHelper
from ..octohttprequest import OctoHttpRequest
from ..octostreammsgbuilder import OctoStreamMsgBuilder
from ..Proto import HttpHeader
from ..Proto import WebStreamMsg
from ..Proto import MessageContext
from ..Proto import HttpInitialContext
from ..Proto import DataCompression
from ..Proto import MessagePriority

#
# A helper object that handles http request for the web stream system.
#
# The helper can close the stream by calling close directly on the WebStream object
# or by returning true from `IncomingServerMessage`
#
class OctoWebStreamHttpHelper:


    # Called by the main socket thread so this should be quick!
    def __init__(self, id, logger, webStream, webStreamOpenMsg, openedTime):
        self.Id = id
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

        # If this doesn't not equal None, it means we know how much data to expect.
        self.KnownFullStreamUploadSizeBytes = None
        self.UploadBytesReceivedSoFar = 0
        self.UploadBuffer = None

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
        # If this messsage has data, put it into our buffer.
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
        if self.WebStreamOpenMsg == None:
            raise Exception("ExecuteHttpRequest but there is no open message")
        # Make sure if there was a defined upload size, we have all of the data.
        if self.KnownFullStreamUploadSizeBytes != None:
            if self.UploadBytesReceivedSoFar != self.KnownFullStreamUploadSizeBytes:
                raise Exception("Http request tried to execute, but we haven't gotten all of the upload payload.")

        # Get the initial context
        httpInitialContext = self.WebStreamOpenMsg.HttpInitialContext()    
        if httpInitialContext == None:
            self.Logger.error(self.getLogMsgPrefix()+ " request open message had no initial context.")
            raise Exception("Http request open message had no initial context")

        # Setup the headers
        sendHeaders = HeaderHelper.GatherRequestHeaders(httpInitialContext, self.Logger)

        # Find the method
        method = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Method())
        if method == None:
            self.Logger.error(self.getLogMsgPrefix()+" request had a None method type.")
            raise Exception("Http request had a None method type")

        # Before we make the request, make sure we shouldn't defer for a high pri request
        self.checkForDelayIfNotHighPri()

        # Make the http request.
        httpResult = OctoHttpRequest.MakeHttpCall(self.Logger, httpInitialContext, method, sendHeaders, self.UploadBuffer, True)

        # If None is returned, it failed.
        # Since the request failed, we want to just close the stream, since it's not a protcol failure.
        if httpResult == None:
            self.Logger.warn(self.getLogMsgPrefix() + " failed to make http request. httpResult was None")
            self.WebStream.Close()
            return

        # On success, unpack the result.
        response = httpResult.Result
        uri = httpResult.Url
        requestExecutionEnd = time.time()

        # If there's no response, it means we failed to connect to whatever the request was trying to connect to.
        # Since the request failed, we want to just close the stream, since it's not a protcol failure.
        if response == None:
            self.Logger.warn(self.getLogMsgPrefix() + " failed to make http request. There was no response.")
            self.WebStream.Close()
            return

        # Look at the headers to see what kind of response we are dealing with.
        # See if we find a content length, for http request that are streams, there is no content length.
        contentLength = None
        # We will also look for the content type, and look for a boundary string if there is one
        # The boundary stream is used for webcam streams, and it's an ideal place to package and send each frame
        boundaryStr = None
        # Pull out the content type value, so we can use it to figure out if we want to compress this data or not  
        contentTypeLower =None
        for name in response.headers:
            nameLower = name.lower()

            if nameLower == "content-length":
                contentLength = int(response.headers[name])

            elif nameLower == "content-type":
                contentTypeLower = response.headers[name].lower()

                # Look for a boundary string, something like this: `multipart/x-mixed-replace;boundary=boundarydonotcross`
                indexOfBoundaryStart = contentTypeLower.find('boundary=')
                if indexOfBoundaryStart != -1:
                    # Move past the string we found
                    indexOfBoundaryStart += len('boundary=')
                    # We should find a boundary, use the original case to parse it out.
                    boundaryStr = response.headers[name][indexOfBoundaryStart:].strip()
                    if len(boundaryStr) == 0:
                        self.Logger.error("We found a boundary stream, but didn't find the boundary string. "+ contentTypeLower)
                        continue
        
        # We also look at the content-type to determine if we should add compression to this request or not.
        # general rule of thumb is that compression is quite cheap but really helps with text, so we should compress when we
        # can.        
        compressBody = self.shouldCompressBody(contentTypeLower, contentLength)

        # Since streams with unknown content-lengths can run for a while, report now when we start one.
        if contentLength == None:
            self.Logger.info(self.getLogMsgPrefix() + "STARTING " + method+" [upload:"+str(format(requestExecutionStart - self.OpenedTime, '.3f'))+"s; request_exe:"+str(format(requestExecutionEnd - requestExecutionStart, '.3f'))+"s; ] type:"+str(contentTypeLower)+" status:"+str(response.status_code)+" for " + uri)

        # Setup a loop to read the stream and push it out in multiple messages.
        contentReadBytes = 0
        nonCompressedContentReadSizeBytes = 0
        isFirstResponse = True
        isLastMessage = False
        messageCount = 0        
        # Continue as long as the stream isn't closed and we haven't sent the close message.
        # We don't check th body read sizes here, because we don't want to duplicate that logic check.
        while self.IsClosed == False and isLastMessage == False:

            # Before we process the response, make sure we shouldn't defer for a high pri request
            self.checkForDelayIfNotHighPri()

            # Prepare a response.
            # TODO - We should start the buffer at something that's likely to not need expanding for most requests.
            builder = OctoStreamMsgBuilder.CreateBuffer(20000)

            # Start by reading data from the response. 
            # This function will return a read length of 0 and a null data offset if there's nothing to read.
            # Otherwise, it will return the length of the read data and the data offset in the buffer.
            nonCompressedBodyReadSize, lastBodyReadLength, dataOffset = self.readContentFromBodyAndMakeDataVector(builder, response, boundaryStr, compressBody)
            contentReadBytes += lastBodyReadLength
            nonCompressedContentReadSizeBytes += nonCompressedBodyReadSize

            # Since this operation can take a while, check if we closed.
            if self.IsClosed:
                break

            # Validate.
            if contentLength != None and nonCompressedContentReadSizeBytes > contentLength:
                self.Logger.warn(self.getLogMsgPrefix()+" the http stream read more data than the content length indicated.")
            if dataOffset == None and contentLength != None and nonCompressedContentReadSizeBytes < contentLength:
                # This might happen if the connection closes unexpectedly before the transfer is done.
                self.Logger.warn(self.getLogMsgPrefix()+" we expected a fixed length response, but the body read completed before we read it all.")

            # Check if this is the last message.
            # This is the last message if...
            #  - The data offset is ever None, this means we have read the entire body as far as the request system is concerned.
            #  - We have an expected length and we have hit it or gone over it.
            isLastMessage = dataOffset == None or (contentLength != None and nonCompressedContentReadSizeBytes >= contentLength)

            # If this is the first response in the stream, we need to send the initial http context and status code.
            httpInitialContextOffset = None
            statusCode = None
            if isFirstResponse == True:
                # Set the status code, so it's sent.
                statusCode = response.status_code

                # Gather the headers, if there are any. This will return None if there are no headers to send.
                headerVectorOffset = self.buildHeaderVector(builder, response)

                # Build the initial context. We should always send a http initial context on the first response,
                # even if there are no headers in t.
                HttpInitialContext.Start(builder)
                if headerVectorOffset != None:
                    HttpInitialContext.AddHeaders(builder, headerVectorOffset)
                httpInitialContextOffset = HttpInitialContext.End(builder)

            # Now build the return message
            WebStreamMsg.Start(builder)
            WebStreamMsg.AddStreamId(builder, self.Id)
            # Indicate this message has data, even if it's just the initial http context (because there's no data for this request)
            WebStreamMsg.AddIsControlFlagsOnly(builder, False)
            if statusCode != None:
                WebStreamMsg.AddStatusCode(builder, statusCode)
            if dataOffset != None:
                WebStreamMsg.AddData(builder, dataOffset)
            if httpInitialContextOffset != None:
                # This should always be not null for the first response.
                WebStreamMsg.AddHttpInitialContext(builder, httpInitialContextOffset)
            if isFirstResponse == True and contentLength != None:
                # Only on the first response, if we know the full size, set it.
                WebStreamMsg.AddFullStreamDataSize(builder, contentLength)
            if compressBody:
                # If we are compressing, we need to add what we are using and what the original size was.
                WebStreamMsg.AddDataCompression(builder, DataCompression.DataCompression.Brotli)
                WebStreamMsg.AddOriginalDataSize(builder, nonCompressedBodyReadSize)
            if isLastMessage:
                # If this is the last message because we know the body is all
                # sent, indicate that the data stream is done and send the close message.
                WebStreamMsg.AddIsDataTransmissionDone(builder, True)
                WebStreamMsg.AddIsCloseMsg(builder, True)
            webStreamMsgOffset = WebStreamMsg.End(builder)

            # Wrap in the OctoStreamMsg and finalize.
            outputBuf = OctoStreamMsgBuilder.CreateOctoStreamMsgAndFinalize(builder, MessageContext.MessageContext.WebStreamMsg, webStreamMsgOffset)

            # Send the message.
            # If this is the last, we need to make sure to set that we have set the closed flag.
            self.WebStream.SendToOctoStream(outputBuf, isLastMessage, True)

            # Clear this flag
            isFirstResponse = False
            messageCount += 1

        # Log about it.
        resposneWriteDone = time.time() 
        self.Logger.info(self.getLogMsgPrefix() + method+" [upload:"+str(format(requestExecutionStart - self.OpenedTime, '.3f'))+"s; request_exe:"+str(format(requestExecutionEnd - requestExecutionStart, '.3f'))+"s; compress:"+str(format(self.CompressionTimeSec, '.3f'))+"s send:"+str(format(resposneWriteDone - requestExecutionEnd, '.3f'))+"s] size:("+str(nonCompressedContentReadSizeBytes)+"->"+str(contentReadBytes)+") compressed:"+str(compressBody)+" msgcount:"+str(messageCount)+" type:"+str(contentTypeLower)+" status:"+str(response.status_code)+" for " + uri)



    def buildHeaderVector(self, builder, response):
        # Gather up the headers to return.
        headerTableOffsets = []        
        for name in response.headers:
            nameLower = name.lower()

            # Since we send the entire result as one non-encoded
            # payload we want to drop this header. Otherwise the server might emit it to 
            # the client, when it actually doesn't match what the server sends to the client.
            # Note: Typically, if the OctoPrint web server sent something chunk encoded, 
            # our web server will also send it to the client via chunk encoding. But it will handle
            # that on it's own and set the header accordingly.
            if nameLower == "transfer-encoding":
                continue

            # Allocate strings
            keyOffset = builder.CreateString(name)
            valueOffset = builder.CreateString(response.headers[name])
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
        if self.UploadBuffer != None and self.KnownFullStreamUploadSizeBytes == None:
            # Trim the buffer to the final size that we received.
            self.UploadBuffer = self.UploadBuffer[0:self.UploadBytesReceivedSoFar]


    def copyUploadDataFromMsg(self, webStreamMsg):
        # Check how much data this message has in it. 
        # This size is the size of the full buffer, which is decompressed sizevif the data is compressed.
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
        if self.UploadBuffer == None and self.KnownFullStreamUploadSizeBytes != None and self.KnownFullStreamUploadSizeBytes == thisMessageDataLen:
            # This is the only message with data, just use it's buffer.
            # I -believe- this doesn't copy the buffer and just makes a view of it.
            # That's the ideal case, because this message buffer will stay around since
            # the http will excute on this same stack.
            self.UploadBuffer = self.decompressBufferIfNeeded(webStreamMsg)
            self.UploadBytesReceivedSoFar = len(self.UploadBuffer)
            # Done!
            return

        # If we know the upload size, make sure this doesn't exceeded it.
        if self.KnownFullStreamUploadSizeBytes != None and thisMessageDataLen + self.UploadBytesReceivedSoFar > self.KnownFullStreamUploadSizeBytes:
            self.Logger.warn(self.getLogMsgPrefix() + " received more bytes than it was expecting for the upload. thisMsg:"+str(thisMessageDataLen)+"; so far:"+str(self.UploadBytesReceivedSoFar) + "; expected:"+str(self.KnownFullStreamUploadSizeBytes))
            raise Exception("Too many bytes received for http upload buffer")

        # Make sure the array has been allocated and it's still large enough.
        if self.UploadBuffer == None or thisMessageDataLen + self.UploadBytesReceivedSoFar > len(self.UploadBuffer):
            newBufferSizeBytes = 0
            if self.KnownFullStreamUploadSizeBytes != None:
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
            if oldBuffer != None:
                # This will copy the old buffer into the front of the new buffer.
                self.UploadBuffer[0:len(oldBuffer)] = oldBuffer

        # We are ready to copy the new data now.
        # Get a slice of the buffer to avoid a the copy, since we copy on the next step anyways.
        buf = self.decompressBufferIfNeeded(webStreamMsg)

        # Append the data into the main buffer.
        pos = self.UploadBytesReceivedSoFar
        self.UploadBuffer[pos:pos+len(buf)] = buf        
        self.UploadBytesReceivedSoFar += len(buf)

    
    # A helper, given a web stream message returns it's data buffer, decompressed if needed.
    def decompressBufferIfNeeded(self, webStreamMsg):
        if webStreamMsg.DataCompression() == DataCompression.DataCompression.Brotli:
            return brotli.decompress(webStreamMsg.DataAsByteArray())
        else:
            return webStreamMsg.DataAsByteArray()


    def getLogMsgPrefix(self):
        return "Web Stream http ["+str(self.Id)+"] "


    # Based on the content-type header, this determins if we would apply compression or not.
    # Returns true or false
    def shouldCompressBody(self, contentTypeLower, contentLengthOpt):
        # Compression isn't too expensive in terms of cpu cost but for text, it drastically
        # cuts the size down (ike a 75% reduction.) So we are quite liberal with our compression.

        # From testing, we have found that compressing anything smaller than ~200 bytes has not effect
        # thus it's not worth doing (it actually makes it slightly larger)
        if contentLengthOpt != None and contentLengthOpt < 200:
            return False

        # If we don't know what this is, might as well compress it.
        if contentTypeLower == None:
            return True

        # We will compress...
        #   - Any thing that has text/ in it
        #   - Anything that says it's javascript
        #   - Anything that says it's json
        #   - Anything that's xml
        #   - Anything that's svg
        #   - Anything that's octet-stream. (we do this because some of the .less files don't get set as text correctly)
        return (contentTypeLower.find("text/") != -1 or contentTypeLower.find("javascript") != -1 
                or contentTypeLower.find("json") != -1 or contentTypeLower.find("xml") != -1
                or contentTypeLower.find("svg") != -1 or contentTypeLower.find("octet-stream") != -1)
        

    # Reads data from the response body, puts it in a data vector, and returns the offset.
    # If the body has been fully read, this should return ogLen == 0, len = 0, and offset == None
    # The read style depends on the presense of the boundary string existing.
    def readContentFromBodyAndMakeDataVector(self, builder, response, boundaryStr_opt, shouldCompress):
        # This is the max size each body read will be. Since we are making local calls, most of the time
        # we will always get this full amount as long as theres more body to read.
        # Note that this amount is larger than a single read of the websocket on the server. After some testing
        # we found the transfer was most efficient if we sent larger message sizes, because it could saturate the tcp link better
        c_defaultBodyReadSizeBytes = 199 * 1024
        # If we are going to compress this read, use a much higher number. Since most of what we compress is text,
        # and that text ususally compresses down to 25% of the og size, we will use a x4 multiplier. 
        if shouldCompress:
            c_defaultBodyReadSizeBytes = c_defaultBodyReadSizeBytes * 4

        # If the boundary string exist and is not empty, we will use it to try to read the data.
        # Unless the self.ChunkedBodyHasNoContentLengthHeaders flag has been set, which indicate we have read the body has chunks
        # and failed to find any content length headers. In that case, we will just read fixed sized chunks.
        finalDataBuffer = None
        if self.ChunkedBodyHasNoContentLengthHeaders == False and boundaryStr_opt != None and len(boundaryStr_opt) != 0:
            # Try to read a single boundary chunk
            readLength = self.readStreamChunk(response, boundaryStr_opt)
            # If we get a length, set the final buffer using the temp buffer.
            # This isn't a copy, just a reference to a subset of the buffer.
            if readLength != 0:
                finalDataBuffer = self.BodyReadTempBuffer[0:readLength]
        else:
            # If there is no boundary string, we will just read as much as possible up to our limit
            # If this returns None, we are done.
            finalDataBuffer = self.doBodyRead(response, c_defaultBodyReadSizeBytes)

        # If the final data buffer has been set to None, it means the body is not empty
        if finalDataBuffer == None:
            # Return empty to indicate the body has been fully read.
            return (0, 0, None)

        # If we were asked to compress, do it
        originalBufferSize = len(finalDataBuffer)
        if shouldCompress:
            # After a good amount of testing, it looks like quality 0 (the lowest works best)
            # The difference in compression is about 2-5% between quality 0 and 8, yet the difference in
            # compression time is about 2-5ms for quality 0, and 15-32ms for 8. (on a pi4)
            # brotli is also just better enough to use over gzip.
            start = time.time()
            finalDataBuffer = brotli.compress(finalDataBuffer, mode=brotli.MODE_TEXT, quality=0)
            if self.CompressionTimeSec == -1:
                self.CompressionTimeSec = 0
            self.CompressionTimeSec += (time.time() - start)

        # We have a data buffer, so write it into the builder and return the offset.
        return (originalBufferSize, len(finalDataBuffer), builder.CreateByteVector(finalDataBuffer))

    # Reads a single chunk from the http response.
    # This function uses the BodyReadTempBuffer to store the data.
    # Returns the read size, 0 if the body read is complete.
    def readStreamChunk(self, response, boundaryStr):
        frameSize = 0
        headerSize = 0
        foundContentLength = False

        # If the temp array isn't setup, do it now.
        if self.BodyReadTempBuffer == None:
            self.BodyReadTempBuffer = bytearray(10*1024)

        # Note. OctoPrint webcam streams have content-length headers in each chunk. However, the standard
        # says it's not required. So if we can find them use them, but if not we will set the 
        # ChunkedBodyHasNoContentLengthHeaders so that future body reads don't attempt to find the headers again.

        # First, we need to see if we can find the content length header.
        # We will keep grabbing more and more data in loop until we find the header
        # If we can't find it after our max size, we declare it's not there (which is fine)
        # and will use a differnet read method going forward.
        c_maxHeaderSearchSizeBytes = 5 * 1024
        tempBufferFilledSize = 0
        try: 
            # Loop until found or we have hit the search limit.
            while foundContentLength == False and tempBufferFilledSize < c_maxHeaderSearchSizeBytes:
                # Read a small chunk to try to read the header
                # We want to read enough that hopefully we get all of the headers, but not so much that
                # we accidentally read two boundary messages at once.
                headerBuffer = self.doBodyRead(response, 300)

                # If this returns 0, the body read is complete
                if headerBuffer == None:
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
                if headerStr.startswith("--"+boundaryStr) == False and headerStr.startswith(boundaryStr) == False:
                    # Always report the first time we find this, otherwise, report only occassionally.
                    if self.MissingBoundaryWarningCounter % 200 == 0:
                        self.Logger.warn("We read a web stream body frame, but it didn't start with the expected boundary header. expected:'"+boundaryStr+"' got:^^"+headerStr+"^^")
                    self.MissingBoundaryWarningCounter += 1                    

                # Find out how long the headers are. The \r\n\r\n sequence ends the headers.
                endOfAllHeadersMatch = "\r\n\r\n"
                endOfHeaderMatch = "\r\n"

                # Try to find headers
                # This logic checks for errors, and if found, don't stop the logic because of them
                # This will cause us to loop again and read more. The reason for this is since we read random 
                # chunks, we could read a chunk that splits the contet-length header in half, which would cause errors.
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
                                # TODO - there's a bug here, if the content length value could be truncated by
                                # the end of the chunk we read. So really we should check there's a /r/n after this
                                # value, to make sure we have the full int.
                                frameSize = int(p[1].strip())
                                foundContentLength = True
                            break
       
        except Exception as e:
            self.Logger.error(self.getLogMsgPrefix()+ " exception thrown in http stream chunk reader "+str(e))
            return 0      

        # Check if we found a content length header
        if foundContentLength == False:
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

        # Read the reaminader of the chunk.
        if toRead > 0:
            data = self.doBodyRead(response, toRead)

            # If we hit the end of the body, return how much we read already.
            if data == None:
                return tempBufferFilledSize

            # Warn if twe didn't read it all
            if len(data) != toRead:
                self.Logger.warn(self.getLogMsgPrefix()+" while reading a boundary chunk, doBodyRead didn't return the full size we requested.")

            # Copy this data into the temp buffer
            self.BodyReadTempBuffer[tempBufferFilledSize:tempBufferFilledSize+len(data)] = data
            tempBufferFilledSize += len(data)    

        # Finally, return how much we put into the temp buffer!
        return tempBufferFilledSize


    def doBodyRead(self, response, readSize):
        try:
            # This won't always read the full size if it's not all here yet.
            # But when running over localhost, this ususally always gets the full size asked for.
            for data in response.iter_content(chunk_size=readSize):
                # Skip keepalives
                if data:         
                    return data
        except requests.exceptions.StreamConsumedError as _:
            # When this exception is thrown, it means the entire body has been read.
            return None    

    # To speed up page load, we will defer lower pri requests while higher priority requests
    # are executing.
    def checkForDelayIfNotHighPri(self):
        # Allow anything above Normal priority to always execute
        if self.WebStreamOpenMsg.MsgPriority() < MessagePriority.MessagePriority.Normal:
            return
        # Otherwise, we want to block for a bit if there's a high pri stream processing.
        self.WebStream.BlockIfHighPriStreamActive()

