from __future__ import annotations # Needed for PY 3.7.3 on the Elegoo Neptune 4 Plus, otherwise customBodyStreamClosedCallback:Optional[Callable[[],None]]=None breaks.
import logging
from typing import Any, Callable, Dict, List, Optional, Union

import requests
from requests.structures import CaseInsensitiveDict

from .buffer import Buffer
from .streamreadhelper import StreamReadHelper
from .Proto.DataCompression import DataCompression

# Easy to use types.
HttpResultOrNone = Union["HttpResult", None]

# This result class is a wrapper around the requests PY lib Response object.
# For the most part, it should abstract away what's needed from the Response object, so that an actual Response object isn't needed
# for all http calls. However, sometimes the actual Response object might be in this result object, because a ref to it needs to be held
# so the body stream can be read, assuming there's no full body buffer.
#
# There are three ways this class can contain a body to be used.
#       1) ResponseForBodyRead - If this is not None, then there's a requests. Response attached to this Result and it can be used to be read from.
#              Note, in this case, ideally the Result is used with a `with` keyword to cleanup when it's done.
#       2) FullBodyBuffer - If this is not None, then there's a fully read body buffer that should be used.
#              In this case, the size of the body is known, it's the size of the full body buffer. The size can't change.
#       3) CustomBodyStream - If this is not None, then there's a custom body stream that should be used.
#              This callback can be implemented by anything. The size is unknown and should continue until the callback returns None.
#                   customBodyStreamCallback() -> byteArray : Called to get more bytes. If None is returned, the stream is done.
#                   customBodyStreamClosedCallback() -> None : MUST BE CALLED when this Result object is closed, to clean up the stream.
class HttpResult():
    def __init__(self,
                    statusCode:int,
                    headers:Union[CaseInsensitiveDict[str], Dict[str, str]], #pyright: ignore[unsubscriptable-object] this is required for later PY versions.
                    url:str,
                    didFallback:bool,
                    fullBodyBuffer:Optional[Buffer]=None,
                    requestLibResponseObj:Optional[requests.Response]=None,
                    customBodyStreamCallback:Optional[Callable[[], Optional[Buffer]]]=None,
                    customBodyStreamClosedCallback:Optional[Callable[[],None]]=None
                    ):
        # Status code isn't a property because some things need to set it externally to the class. (Result.StatusCode = 302)
        self.StatusCode = statusCode
        self._url:str = url
        self._requestLibResponseObj = requestLibResponseObj
        self._didFallback:bool = didFallback
        self._fullBodyBuffer:Optional[Buffer] = None
        self._bodyCompressionType = DataCompression.None_
        self._fullBodyBufferPreCompressedSize:int = 0
        self._customBodyStreamCallback = customBodyStreamCallback
        self._customBodyStreamClosedCallback = customBodyStreamClosedCallback

        # Validate.
        if (self._customBodyStreamCallback is not None and self._customBodyStreamClosedCallback is None) or (self._customBodyStreamCallback is None and self._customBodyStreamClosedCallback is not None):
            raise Exception("Both the customBodyStreamCallback and customBodyStreamClosedCallback must be set!")

        # Set the buffer if we got one.
        if fullBodyBuffer is not None:
            self.SetFullBodyBuffer(fullBodyBuffer)

        # Always convert the headers to a CaseInsensitiveDict.
        if isinstance(headers, dict):
            # If the headers are a dict, we need to convert them to a CaseInsensitiveDict.
            # This is because the requests lib uses CaseInsensitiveDict for headers.
            headers = CaseInsensitiveDict(headers)
        self._headers = headers


    # Allows for a quick way to create a Result object with no body.
    @staticmethod
    def Error(statusCode:int, url:str, didFallback:bool=False) -> "HttpResult":
        # We must use a content length of 0 and set an empty body for the request to be handled correctly.
        headers = CaseInsensitiveDict()
        headers["Content-Length"] = "0"
        return HttpResult(statusCode, headers, url, didFallback, fullBodyBuffer=Buffer(bytearray()))


    # Allows for a quick way to create a Result object that is a redirect.
    @staticmethod
    def Redirect(url:str, didFallback:bool=False) -> "HttpResult":
        headers = CaseInsensitiveDict()
        headers["Location"] = url
        # We must use a content length of 0 and set an empty body for the request to be handled correctly.
        headers["Content-Length"] = "0"
        return HttpResult(302, headers, url, didFallback, fullBodyBuffer=Buffer(bytearray()))


    # Builds a Result object from a requests.Response object.
    @staticmethod
    def BuildFromRequestLibResponse(response:requests.Response, url:str, isFallback:bool=False) -> "HttpResult":
        return HttpResult(response.status_code, response.headers, url, isFallback, requestLibResponseObj=response)


    @property
    def Headers(self) -> CaseInsensitiveDict[str]: #pyright: ignore[unsubscriptable-object] this is required for later PY versions.
        return self._headers


    @property
    def Url(self) -> str:
        return self._url


    @property
    def DidFallback(self) -> bool:
        return self._didFallback


    # This should only be used for reading the http stream body and it might be None
    # If this Result was created without one.
    @property
    def ResponseForBodyRead(self) -> Optional[requests.Response]:
        return self._requestLibResponseObj


    @property
    def FullBodyBuffer(self) -> Optional[Buffer]:
        # Defaults to None
        return self._fullBodyBuffer


    @property
    def BodyBufferCompressionType(self) -> int:
        # Defaults to None
        return self._bodyCompressionType


    @property
    def BodyBufferPreCompressSize(self) -> int:
        # There must be a buffer
        if self._fullBodyBuffer is None:
            return 0
        return self._fullBodyBufferPreCompressedSize


    @property
    def GetCustomBodyStreamCallback(self) -> Optional[Callable[[], Optional[Buffer]]]:
        # This callback can return None, which indicates the stream is done or there was an error.
        return self._customBodyStreamCallback


    @property
    def GetCustomBodyStreamClosedCallback(self) -> Optional[Callable[[], None]]:
        return self._customBodyStreamClosedCallback


    # Note the buffer can be bytes or bytearray object!
    # A bytes object is more efficient, but bytearray can be edited.
    def SetFullBodyBuffer(self, buffer:Buffer, compressionType:int=DataCompression.None_, preCompressedSize:int = 0) -> None:
        self._fullBodyBuffer = buffer
        self._bodyCompressionType = compressionType
        self._fullBodyBufferPreCompressedSize = preCompressedSize
        if compressionType != DataCompression.None_ and preCompressedSize <= 0:
            raise Exception("The pre-compression full size must be set if the buffer is compressed.")


    # It's important we clear all of the vars that are set above.
    # This is used by the system that updates the request object with a 304 if the cache headers match.
    def ClearFullBodyBuffer(self) -> None:
        self._fullBodyBuffer = None
        self._bodyCompressionType = DataCompression.None_
        self._fullBodyBufferPreCompressedSize = 0


    # Since most things use request Stream=True, this is a helpful util that will read the entire
    # content of a request and return it. Note if the request has no defined length, this will read
    # as long as the stream will go.
    # This function will not throw on failures, it will read as much as it can and then set the buffer.
    # On a complete failure, the buffer will be set to None, so that should be checked.
    def ReadAllContentFromStreamResponse(self, logger:logging.Logger, maxBodySizeBytes:Optional[int]=None) -> None:
        # Ensure we have a stream to read.
        if self._requestLibResponseObj is None:
            raise Exception("ReadAllContentFromStreamResponse was called on a result with no request lib Response object.")
        # It's more efficient to gather the data in a single buffer, and append together at the end.
        buffers:List[Union[bytes, bytearray]] = []

        # In the past, we used iter_content, but it has a lot of overhead and also doesn't read all available data, it will only read a chunk if the transfer encoding is chunked.
        # This isn't great because it's slow and also we don't need to reach each chunk, process it, just to dump it in a buffer and read another.
        #
        # For more comments, read doBodyRead, but using read is way more efficient.
        # The only other thing to note is that read will allocate the full buffer size passed, even if only some of it is filled.
        try:
            # If we have a content length, we can use that to read more efficiently
            # And if the underlying stream supports readinto, we can use that to avoid some allocations and copies.
            useReadInto = StreamReadHelper.CanTryReadInto(self._requestLibResponseObj.raw)
            contentLengthStr = self._requestLibResponseObj.headers.get("Content-Length", None)
            if contentLengthStr is not None:
                contentLength = int(contentLengthStr)
                if maxBodySizeBytes is not None and contentLength > maxBodySizeBytes:
                    logger.warning("ReadAllContentFromStreamResponse refused to read %d bytes because the max is %d bytes.", contentLength, maxBodySizeBytes)
                    return
                if contentLength > 0:
                    contentBuffer = bytearray(contentLength)
                    # Read the full content length.
                    contentBytesRead, useReadInto = StreamReadHelper.ReadIntoByteArrayFull(self._requestLibResponseObj.raw, contentBuffer, 0, contentLength, useReadInto)
                    if contentBytesRead > 0:
                        # If we got less than the content length, trim the buffer to what we got.
                        if contentBytesRead < contentLength:
                            del contentBuffer[contentBytesRead:]
                        buffers.append(contentBuffer)
                    # If we got the full content length, we can set the buffer and return early.
                    if contentBytesRead >= contentLength:
                        self.SetFullBodyBuffer(Buffer(contentBuffer))
                        return
                    logger.warning(f"ReadAllContentFromStreamResponse: We expected to read {contentLength} bytes based on the Content-Length header, but only read {contentBytesRead} bytes.")

            # Ideally we use the content size, but if we can't we use our default.
            # The default size is tuned to fit about one 1080 jpeg image.
            # Since this function is mostly used for snapshots, that's a good default.
            perReadSizeBytes = 490 * 1024
            totalBytesRead = sum(len(p) for p in buffers)

            while True:
                # Read data
                buffer, useReadInto = StreamReadHelper.ReadBuffer(self._requestLibResponseObj.raw, perReadSizeBytes, useReadInto)

                # Check if we are done.
                if buffer is None or len(buffer) == 0:
                    # Break out when we are done.
                    break

                if maxBodySizeBytes is not None and totalBytesRead + len(buffer) > maxBodySizeBytes:
                    logger.warning("ReadAllContentFromStreamResponse stopped reading because the body exceeded %d bytes.", maxBodySizeBytes)
                    buffers = []
                    return

                # If we aren't done, append the buffer.
                buffers.append(buffer.GetBytesLike())
                totalBytesRead += len(buffer)
        except Exception as e:
            bufferLength = sum(len(p) for p in buffers)
            lengthStr = "[buffer is None]" if bufferLength == 0 else str(bufferLength)
            logger.warning(f"ReadAllContentFromStreamResponse got an exception. We will return the current buffer length of {lengthStr}, exception: {e}")

        # Ensure we got something, as after this callers will expect an object to be there.
        buffer:Optional[Buffer] = None
        if len(buffers) == 1:
            buffer = Buffer(buffers[0])
        elif len(buffers) > 0:
            buffer = Buffer(b''.join(buffers))
        else:
            buffer = Buffer(bytearray())
        self.SetFullBodyBuffer(buffer)


    # Creates a shallow replay copy of this result. FullBodyBuffer is shared, but response status and headers can be safely mutated by the caller.
    # This is mostly used to transfer a response from the requests lib that might be holding a socket open to a result that's untied and just has the response body, headers, etc.
    def CreateReplayCopy(self) -> "HttpResult":
        result = HttpResult(self.StatusCode, CaseInsensitiveDict(self.Headers), self.Url, self.DidFallback)
        if self.FullBodyBuffer is not None:
            result.SetFullBodyBuffer(self.FullBodyBuffer, self.BodyBufferCompressionType, self.BodyBufferPreCompressSize)
        return result


    # This is the same as calling __exit__, but it can be called manually if not using a with statement.
    # After this is called, the object should not be used anymore, as the body stream is closed and the full body buffer is cleared.
    def Free(self, t:Any=None, v:Any=None, tb:Any=None) -> None:
        # If we have a request lib response object, we should close it to free the underlying socket.
        if self._requestLibResponseObj is not None:
            self._requestLibResponseObj.__exit__(t, v, tb)
        # If we have a custom body stream closed callback, we should call it to free the underlying stream.
        if self._customBodyStreamClosedCallback is not None:
            self._customBodyStreamClosedCallback()
        # Clear the full body buffer to free memory.
        self.ClearFullBodyBuffer()


    # We need to support the with keyword incase we have an actual Response object.
    def __enter__(self):
        if self._requestLibResponseObj is not None:
            self._requestLibResponseObj.__enter__()
        return self


    # We need to support the with keyword incase we have an actual Response object.
    def __exit__(self, t:Any, v:Any, tb:Any):
        self.Free(t, v, tb)
