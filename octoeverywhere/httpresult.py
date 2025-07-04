from __future__ import annotations # Needed for PY 3.7.3 on the Elegoo Neptune 4 Plus, otherwise customBodyStreamClosedCallback:Optional[Callable[[],None]]=None breaks.
import logging
from typing import Any, Callable, Dict, Optional, Union

import requests
from requests.structures import CaseInsensitiveDict

from .buffer import Buffer
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
                    customBodyStreamCallback:Optional[Callable[[], Buffer]]=None,
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
    def GetCustomBodyStreamCallback(self) -> Optional[Callable[[], Buffer]]:
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
    def ReadAllContentFromStreamResponse(self, logger:logging.Logger) -> None:
        # Ensure we have a stream to read.
        if self._requestLibResponseObj is None:
            raise Exception("ReadAllContentFromStreamResponse was called on a result with no request lib Response object.")
        # It's more efficient to gather the data in a single buffer, and append together at the end.
        buffers:list[bytes | bytearray] = []

        # In the past, we used iter_content, but it has a lot of overhead and also doesn't read all available data, it will only read a chunk if the transfer encoding is chunked.
        # This isn't great because it's slow and also we don't need to reach each chunk, process it, just to dump it in a buffer and read another.
        #
        # For more comments, read doBodyRead, but using read is way more efficient.
        # The only other thing to note is that read will allocate the full buffer size passed, even if only some of it is filled.
        try:
            # Ideally we use the content size, but if we can't we use our default.
            # The default size is tuned to fit about one 1080 jpeg image.
            # Since this function is mostly used for snapshots, that's a good default.
            perReadSizeBytes = 490 * 1024
            contentLengthStr = self._requestLibResponseObj.headers.get("Content-Length", None)
            if contentLengthStr is not None:
                perReadSizeBytes = int(contentLengthStr)

            while True:
                # Read data
                data = self._requestLibResponseObj.raw.read(perReadSizeBytes)

                # Check if we are done.
                if data is None or len(data) == 0:
                    # This is weird, but there can be lingering data in response.content, so add that if there is any.
                    # See doBodyRead for more details.
                    if len(self._requestLibResponseObj.content) > 0:
                        buffers.append(self._requestLibResponseObj.content)
                    # Break out when we are done.
                    break

                # If we aren't done, append the buffer.
                buffers.append(data)
        except Exception as e:
            bufferLength = sum(len(p) for p in buffers)
            lengthStr = "[buffer is None]" if bufferLength == 0 else str(bufferLength)
            logger.warning(f"ReadAllContentFromStreamResponse got an exception. We will return the current buffer length of {lengthStr}, exception: {e}")

        # Ensure we got something, as after this callers will expect an object to be there.
        buffer:Buffer = Buffer(b''.join(buffers)) if len(buffers) > 0 else Buffer(bytearray())
        self.SetFullBodyBuffer(buffer)


    # We need to support the with keyword incase we have an actual Response object.
    def __enter__(self):
        if self._requestLibResponseObj is not None:
            self._requestLibResponseObj.__enter__()
        return self


    # We need to support the with keyword incase we have an actual Response object.
    def __exit__(self, t:Any, v:Any, tb:Any):
        if self._requestLibResponseObj is not None:
            self._requestLibResponseObj.__exit__(t, v, tb)
        if self._customBodyStreamClosedCallback is not None:
            self._customBodyStreamClosedCallback()
