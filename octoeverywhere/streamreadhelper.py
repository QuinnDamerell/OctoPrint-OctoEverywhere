from typing import Any, Optional, Tuple

from .buffer import Buffer


# A helper to do more efficient readinto streams.
class StreamReadHelper:

    # A helper to check if we can use readinto on the underlying stream.
    @staticmethod
    def CanTryReadInto(rawResponse:Any) -> bool:
        return callable(getattr(rawResponse, "readinto", None))


    # This is the most ideal way to read, where there's an existing bytearray buffer that we can read into, and the underlying stream supports readinto.
    # This will continue reading until the requested number of bytes is read or the end of the stream is reached.
    @staticmethod
    def ReadIntoByteArrayFull(rawResponse:Any, targetBuffer:bytearray, offset:int, readSize:int, useReadInto:bool = True) -> Tuple[int, bool]:
        totalBytesRead = 0
        while totalBytesRead < readSize:
            bytesRead, useReadInto = StreamReadHelper.ReadIntoByteArray(rawResponse, targetBuffer, offset + totalBytesRead, readSize - totalBytesRead, useReadInto)
            if bytesRead == 0:
                break
            totalBytesRead += bytesRead
        return totalBytesRead, useReadInto


    # This is the most ideal way to read, where there's an existing bytearray buffer that we can read into, and the underlying stream supports readinto.
    # This will only do one read and will return up to the number of bytes requested.
    @staticmethod
    def ReadIntoByteArray(rawResponse:Any, targetBuffer:bytearray, offset:int, readSize:int, useReadInto:bool = True) -> Tuple[int, bool]:
        if readSize <= 0:
            return 0, useReadInto

        # Try to use readinto if we can, but if we can't, just use read.
        if useReadInto:
            readInto = getattr(rawResponse, "readinto", None)
            if callable(readInto):
                with memoryview(targetBuffer) as bufferView:
                    with bufferView[offset:offset + readSize] as targetView:
                        try:
                            bytesRead: Optional[int] = readInto(targetView) #pyright: ignore[reportAssignmentType]
                            if bytesRead is None:
                                return 0, True
                            return bytesRead, True
                        except (AttributeError, NotImplementedError, TypeError):
                            useReadInto = False
            else:
                useReadInto = False

        # If we can't use readinto, just use read and copy the data into the target buffer.
        data = rawResponse.read(readSize)
        if data is None or len(data) == 0:
            return 0, useReadInto
        bytesRead = len(data)
        targetBuffer[offset:offset + bytesRead] = data
        return bytesRead, useReadInto


    # This isn't much better than a normal read. The only advantage of using readinto here is that we return the data in a bytearray instead of a bytes, which is a little more flexible.
    @staticmethod
    def ReadBuffer(rawResponse:Any, readSize:int, useReadInto:bool = True) -> Tuple[Optional[Buffer], bool]:
        if readSize <= 0:
            return Buffer(bytearray()), useReadInto

        # Try to use readinto if we can, but if we can't, just use read.
        if useReadInto:
            readInto = getattr(rawResponse, "readinto", None)
            if callable(readInto):
                targetBuffer = bytearray(readSize)
                bytesRead, useReadInto = StreamReadHelper.ReadIntoByteArray(rawResponse, targetBuffer, 0, readSize, useReadInto)
                if bytesRead == 0:
                    return None, useReadInto
                if bytesRead < readSize:
                    del targetBuffer[bytesRead:]
                return Buffer(targetBuffer), useReadInto
            useReadInto = False

        # If we can't use readinto, just use read and return a buffer.
        data = rawResponse.read(readSize)
        if data is None or len(data) == 0:
            return None, useReadInto
        return Buffer(data), useReadInto


    @staticmethod
    def RecvInto(socketObj:Any, targetBuffer:bytearray, offset:int, readSize:int) -> int:
        if readSize <= 0:
            return 0
        bufferView = memoryview(targetBuffer)
        targetView = bufferView[offset:offset + readSize]
        try:
            return int(socketObj.recv_into(targetView, readSize))
        finally:
            targetView.release()
            bufferView.release()
