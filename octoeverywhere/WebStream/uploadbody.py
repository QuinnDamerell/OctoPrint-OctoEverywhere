from enum import Enum
import logging
import os
import threading
import uuid
import zlib
import tempfile
from urllib.parse import quote
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Union

from ..memorymanager import MemoryManager
from ..Proto.DataCompression import DataCompression
from ..buffer import Buffer, BufferOrNone, ByteLike
from ..compression import Compression, CompressionContext

UploadBodyOrNone = Union[None, "UploadBody"]
UploadBodyBufferOrNone = Union[BufferOrNone, "UploadBody"]
UploadTypesOrNone = Union[None, "UploadBody", "MultipartFormUploadBody"]
UploadTypesBufferOrNone = Union[BufferOrNone, "UploadBody", "MultipartFormUploadBody"]
BufferedReaderBytesOrNone = Union[BinaryIO, ByteLike, None]


class MultipartFormUploadBodyReadContext:
    def __init__(self, logger:Any, uploadBody:"UploadBody", prefix:bytes, suffix:bytes) -> None:
        self.Logger = logger
        self.UploadBody = uploadBody
        self.Prefix = prefix
        self.Suffix = suffix
        self.UploadBodyContext:Optional[UploadBodyReadContext] = None
        self.Reader:Optional[MultipartFormUploadBodyReader] = None


    def GetData(self) -> "MultipartFormUploadBodyReader":
        if self.Reader is not None:
            return self.Reader
        self.UploadBodyContext = self.UploadBody.OpenForRequest()
        uploadData = self.UploadBodyContext.GetData()
        if uploadData is None:
            raise Exception("files-upload body stream is empty after opening upload data.")
        self.Reader = MultipartFormUploadBodyReader(self.Prefix, uploadData, self.Suffix, self.UploadBody.UploadBytesReceivedSoFar)
        return self.Reader


    def SeekToStart(self) -> None:
        if self.UploadBodyContext is not None:
            self.UploadBodyContext.SeekToStart()
        if self.Reader is not None:
            self.Reader.SeekToStart()


    def Close(self) -> None:
        self.Logger.debug("MultipartFormUploadBodyReadContext closed.")
        if self.UploadBodyContext is not None:
            self.UploadBodyContext.Close()
            self.UploadBodyContext = None
        self.Reader = None


    def __enter__(self) -> "MultipartFormUploadBodyReader":
        return self.GetData()


    def __exit__(self, t:Any, v:Any, tb:Any) -> None:
        self.Close()


class MultipartFormUploadBodyReader:
    def __init__(self, prefix:bytes, uploadData:Union[BinaryIO, ByteLike], suffix:bytes, uploadDataLen:int) -> None:
        self._prefix = prefix
        self._uploadData = uploadData
        self._suffix = suffix
        self._uploadDataLen = uploadDataLen
        self._position = 0
        self._prefixOffset = 0
        self._uploadBytesRead = 0
        self._suffixOffset = 0


    # Reports the full multipart body length (prefix + upload + suffix). This lets requests/urllib3 derive the
    # Content-Length directly from the body object via super_len(), so the upload stays correctly framed even on
    # the 431 retry path, which re-sends the request with no caller-supplied headers (and thus no Content-Length).
    # super_len() subtracts tell() from this value, so we always return the full length here.
    def __len__(self) -> int:
        return len(self._prefix) + self._uploadDataLen + len(self._suffix)


    def read(self, size:Optional[int]=-1) -> bytes:
        if size is None or size < 0:
            chunks:List[bytes] = []
            while True:
                chunk = self.read(1024 * 1024)
                if len(chunk) == 0:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

        if size == 0:
            return b""

        chunks = []
        bytesRemaining = size
        while bytesRemaining > 0:
            chunk = self._ReadNextChunk(bytesRemaining)
            if len(chunk) == 0:
                break
            chunks.append(chunk)
            bytesRemaining -= len(chunk)
            self._position += len(chunk)
        if len(chunks) == 0:
            return b""
        if len(chunks) == 1:
            return chunks[0]
        return b"".join(chunks)


    def tell(self) -> int:
        return self._position


    def SeekToStart(self) -> None:
        self._position = 0
        self._prefixOffset = 0
        self._uploadBytesRead = 0
        self._suffixOffset = 0
        seekFunc = getattr(self._uploadData, "seek", None)
        if callable(seekFunc):
            seekFunc(0)


    def _ReadNextChunk(self, maxBytes:int) -> bytes:
        if self._prefixOffset < len(self._prefix):
            end = min(len(self._prefix), self._prefixOffset + maxBytes)
            chunk = self._prefix[self._prefixOffset:end]
            self._prefixOffset = end
            return chunk

        if isinstance(self._uploadData, (bytes, bytearray)):
            if self._uploadBytesRead < len(self._uploadData):
                end = min(len(self._uploadData), self._uploadBytesRead + maxBytes)
                chunk = bytes(self._uploadData[self._uploadBytesRead:end])
                self._uploadBytesRead = end
                return chunk
        else:
            chunk = self._uploadData.read(maxBytes)
            if chunk is not None and len(chunk) > 0:
                self._uploadBytesRead += len(chunk)
                return chunk

        if self._suffixOffset < len(self._suffix):
            end = min(len(self._suffix), self._suffixOffset + maxBytes)
            chunk = self._suffix[self._suffixOffset:end]
            self._suffixOffset = end
            return chunk

        return b""


class MultipartFormUploadBody:
    def __init__(self, logger:Any, uploadBody:"UploadBody", fileName:str, fields:Optional[Dict[str, str]]=None, fileFieldName:str="file", fileContentType:str="application/octet-stream", boundary:Optional[str]=None) -> None:
        self.Logger = logger
        self.UploadBody = uploadBody
        self.FileName = fileName
        self.Fields = fields if fields is not None else {}
        self.FileFieldName = fileFieldName
        self.FileContentType = fileContentType
        self.Boundary = boundary if boundary is not None else "oe-upload-" + uuid.uuid4().hex
        self._prefix = self._BuildPrefix()
        self._suffix = ("\r\n--" + self.Boundary + "--\r\n").encode("utf-8")
        self.ContentLength = len(self._prefix) + self.UploadBody.UploadBytesReceivedSoFar + len(self._suffix)


    def GetContentType(self) -> str:
        return "multipart/form-data; boundary=" + self.Boundary


    def GetContentLength(self) -> int:
        return self.ContentLength


    def OpenForRequest(self) -> MultipartFormUploadBodyReadContext:
        return MultipartFormUploadBodyReadContext(self.Logger, self.UploadBody, self._prefix, self._suffix)


    def _BuildPrefix(self) -> bytes:
        parts:List[bytes] = []
        for key, value in self.Fields.items():
            parts.append(("--" + self.Boundary + "\r\n").encode("utf-8"))
            parts.append(self._BuildContentDisposition(key).encode("utf-8"))
            parts.append(b"\r\n\r\n")
            parts.append(str(value).encode("utf-8"))
            parts.append(b"\r\n")

        parts.append(("--" + self.Boundary + "\r\n").encode("utf-8"))
        parts.append(self._BuildContentDisposition(self.FileFieldName, self.FileName).encode("utf-8"))
        parts.append(("\r\nContent-Type: " + self.FileContentType + "\r\n\r\n").encode("utf-8"))
        return b"".join(parts)


    def _BuildContentDisposition(self, name:str, fileName:Optional[str]=None) -> str:
        safeName = self._HeaderQuote(name)
        if fileName is None:
            return f'Content-Disposition: form-data; name="{safeName}"'
        fallbackFileName = self._HeaderQuote(self._AsciiFallback(fileName))
        encodedFileName = quote(fileName.encode("utf-8"))
        return f'Content-Disposition: form-data; name="{safeName}"; filename="{fallbackFileName}"; filename*=utf-8\'\'{encodedFileName}'


    def _HeaderQuote(self, value:str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


    def _AsciiFallback(self, value:str) -> str:
        chars:List[str] = []
        for ch in value:
            if ord(ch) >= 32 and ord(ch) < 127 and ch not in {'"', "\\"}:
                chars.append(ch)
            else:
                chars.append("_")
        fallback = "".join(chars)
        return fallback if len(fallback) > 0 else "upload.gcode"


class _UploadChunk:
    def __init__(self, offset:int, sizeBytes:int, originalSizeBytes:int, compressionType:int, isLastMessage:bool, memoryBuffer:Optional[Buffer]=None) -> None:
        self.Offset = offset
        self.SizeBytes = sizeBytes
        self.OriginalSizeBytes = originalSizeBytes
        self.CompressionType = compressionType
        self.IsLastMessage = isLastMessage
        self.MemoryBuffer = memoryBuffer


# Returned from OpenForRequest to keep the lifetime of the file opened.
class UploadBodyReadContext:
    def __init__(self, logger:Any, buffer:Optional[Buffer], filePath:Optional[str], onClose:Optional[Callable[[], None]]=None) -> None:
        self.Logger = logger
        self.Buffer = buffer
        self.FilePath = filePath
        self.File:Optional[BinaryIO] = None
        self._onClose = onClose
        self._isClosed = False

    def GetData(self) -> BufferedReaderBytesOrNone:
        if self._isClosed:
            raise Exception("UploadBodyReadContext tried to get data after it was closed.")
        if self.Buffer is not None:
            self.Logger.debug("UploadBodyReadContext is using in-memory buffer for request data.")
            return self.Buffer.GetBytesLike()
        if self.FilePath is not None:
            if self.File is not None:
                return self.File
            self.Logger.debug("UploadBodyReadContext is opening file for request data. path:%s", str(self.FilePath))
            self.File = open(self.FilePath, "rb")  #pylint: disable=consider-using-with
            return self.File
        raise Exception("UploadBodyReadContext was opened with no buffer or file.")


    def SeekToStart(self) -> None:
        if self.File is not None:
            self.File.seek(0)


    def Close(self) -> None:
        if self._isClosed:
            return
        closeException:Optional[Exception] = None
        try:
            self.Logger.debug("UploadBodyReadContext closed.")
            if self.File is not None:
                self.File.close()
                self.File = None
        except Exception as e:
            closeException = e
        finally:
            self._isClosed = True
            if self._onClose is not None:
                self._onClose()
                self._onClose = None
        if closeException is not None:
            raise closeException


    def __enter__(self) -> BufferedReaderBytesOrNone:
        return self.GetData()


    def __exit__(self, t:Any, v:Any, tb:Any) -> None:
        self.Close()


class UploadBodyState(Enum):
    Building = 1
    Finalizing = 2
    Finalized = 3
    CleanedUp = 4


class UploadBody:
    # Disk copy/decompression chunk size. Keep this bounded independently of the HTTP read sizes.
    c_FileCopyBufferSizeBytes = 1024 * 1024


    def __init__(self, logger:Any, streamId:int, knownFullUploadSizeBytes:Optional[int], compressionContext:CompressionContext, maxInMemoryBodyBytes:Optional[int]=None) -> None:
        self.Logger = logger
        self.StreamId = streamId
        self.KnownFullUploadSizeBytes = knownFullUploadSizeBytes
        self.CompressionContext = compressionContext
        self.MaxInMemoryBodyBytes = maxInMemoryBodyBytes if maxInMemoryBodyBytes is not None else MemoryManager.OctoWebStreamHttpHelper_MaxUploadBufferSizeBytes

        self._lock = threading.Lock()
        self._storageCleanupLock = threading.Lock()
        self._state = UploadBodyState.Building
        self.UploadBytesReceivedSoFar = 0
        self._chunks:List[_UploadChunk] = []
        self._usingFile = False
        self._hasCompressedChunks = False
        self._bodyBuffer:Optional[Buffer] = None
        self._bodyFilePath:Optional[str] = None

        self._rawUploadFile:Optional[BinaryIO] = None
        self._rawUploadFilePath:Optional[str] = None
        self._activeReadContextCount = 0
        # Tracks an in-progress AppendMessage() so a concurrent Cleanup() (which can be called from the socket
        # close path on a different thread) defers tearing down the storage we're actively writing to.
        self._activeAppendCount = 0

        if self.KnownFullUploadSizeBytes is not None and self.KnownFullUploadSizeBytes > self.MaxInMemoryBodyBytes:
            self._SwitchToFile("known upload size exceeds in-memory limit")


    @property
    def IsUsingFile(self) -> bool:
        return self._usingFile


    @property
    def HasData(self) -> bool:
        return self.UploadBytesReceivedSoFar > 0


    def AppendMessage(self, webStreamMsg:Any) -> None:
        if self._state == UploadBodyState.CleanedUp:
            # The stream was cleaned up (e.g. closed mid-upload) while data was still arriving. Drop it quietly.
            self._Debug("ignoring upload data because the body was already cleaned up.")
            return
        if self._state != UploadBodyState.Building:
            raise Exception("OctoWebStreamUploadBody tried to append after finalize.")

        rawDataLen = webStreamMsg.DataLength()
        if rawDataLen <= 0:
            self._Warn("is waiting on upload data but got a message with no data.")
            return

        compressionType = webStreamMsg.DataCompression()
        originalSizeBytes = rawDataLen
        if compressionType != DataCompression.None_:
            originalSizeBytes = int(webStreamMsg.OriginalDataSize())
            if originalSizeBytes <= 0:
                raise Exception("Compressed upload message had no original data size.")
            self._hasCompressedChunks = True

        projectedUploadBytes = self.UploadBytesReceivedSoFar + originalSizeBytes
        if self.KnownFullUploadSizeBytes is not None and projectedUploadBytes > self.KnownFullUploadSizeBytes:
            self._Warn("received more bytes than it was expecting for the upload. thisMsg:"+str(originalSizeBytes)+"; so far:"+str(self.UploadBytesReceivedSoFar) + "; expected:"+str(self.KnownFullUploadSizeBytes))
            raise Exception("Too many bytes received for http upload buffer")

        rawData = Buffer(webStreamMsg.DataAsByteArray())

        # Mark an append as in-progress under the lock so a concurrent Cleanup() (which can be called from the
        # socket close path on a different thread) won't tear down the storage we're about to write to. We do the
        # actual write outside the lock so we never block the close path on disk IO - instead, Cleanup() defers the
        # storage cleanup back to us, and we run it here once the write finishes. This mirrors how active read
        # contexts are tracked for the request-time file handle.
        with self._lock:
            if self._state == UploadBodyState.CleanedUp:
                self._Debug("ignoring upload data because the body was cleaned up while receiving.")
                return
            if self._state != UploadBodyState.Building:
                raise Exception("OctoWebStreamUploadBody tried to append after finalize.")
            self._activeAppendCount += 1

        try:
            if self._usingFile is False and projectedUploadBytes > self.MaxInMemoryBodyBytes:
                self._SwitchToFile("upload exceeded in-memory limit")

            if self._usingFile:
                self._AppendRawDataToFile(rawData, originalSizeBytes, compressionType, webStreamMsg.IsDataTransmissionDone())
            else:
                self._chunks.append(_UploadChunk(0, rawDataLen, originalSizeBytes, compressionType, webStreamMsg.IsDataTransmissionDone(), rawData))

            self.UploadBytesReceivedSoFar = projectedUploadBytes
            self._Debug("received upload data chunk. using file: %s; bytes in this msg: %s; total received so far: %s; known full size: %s", str(self._usingFile), originalSizeBytes, self.UploadBytesReceivedSoFar, self.KnownFullUploadSizeBytes)
        finally:
            cleanupNow = False
            with self._lock:
                self._activeAppendCount -= 1
                # If Cleanup() ran while we were writing, it deferred to us. Now that the write is done and nothing
                # else is using the storage, do the cleanup it skipped.
                cleanupNow = self._state == UploadBodyState.CleanedUp and self._activeAppendCount == 0 and self._activeReadContextCount == 0
            if cleanupNow:
                self._CleanupStorage()


    def Finalize(self) -> None:
        needsCleanup = False
        try:
            with self._lock:
                if self._state != UploadBodyState.Building:
                    return

                if self.KnownFullUploadSizeBytes is not None and self.UploadBytesReceivedSoFar != self.KnownFullUploadSizeBytes:
                    raise Exception("Http request tried to execute, but we haven't gotten all of the upload payload. Total:"+str(self.KnownFullUploadSizeBytes)+"; rec so far:"+str(self.UploadBytesReceivedSoFar))

                if self.UploadBytesReceivedSoFar == 0:
                    self._state = UploadBodyState.Finalized
                    return

                self._state = UploadBodyState.Finalizing

            self._Debug("UploadBody doing a finalize with data. Total bytes received: %d, chunks: %d", self.UploadBytesReceivedSoFar, len(self._chunks))
            if self._usingFile:
                self._FinalizeFileBody()
            else:
                self._FinalizeMemoryBody()

            with self._lock:
                if self._state == UploadBodyState.CleanedUp:
                    needsCleanup = True
                else:
                    self._state = UploadBodyState.Finalized
        except Exception:
            with self._lock:
                if self._state == UploadBodyState.CleanedUp:
                    needsCleanup = True
                elif self._state == UploadBodyState.Finalizing:
                    self._state = UploadBodyState.Building
            if needsCleanup:
                self._CleanupStorage()
            raise

        if needsCleanup:
            self._CleanupStorage()


    def OpenForRequest(self) -> UploadBodyReadContext:
        onClose:Optional[Callable[[], None]] = None
        with self._lock:
            if self._state != UploadBodyState.Finalized:
                raise Exception("OctoWebStreamUploadBody is not in a finalized state before opening it for a request.")
            if self._bodyBuffer is not None or self._bodyFilePath is not None:
                self._activeReadContextCount += 1
                onClose = self._OnReadContextClosed
        return UploadBodyReadContext(self.Logger, self._bodyBuffer, self._bodyFilePath, onClose)


    def GetBodyAsBuffer(self) -> Optional[Buffer]:
        if self._state != UploadBodyState.Finalized:
            raise Exception("OctoWebStreamUploadBody is not in a finalized state before getting the body buffer.")
        if self.UploadBytesReceivedSoFar == 0:
            return None
        if self._bodyFilePath is not None:
            with open(self._bodyFilePath, "rb") as f:
                return Buffer(f.read())
        return self._bodyBuffer


    def Cleanup(self) -> None:
        with self._lock:
            if self._state == UploadBodyState.CleanedUp:
                cleanupNow = self._activeReadContextCount == 0 and self._activeAppendCount == 0 and self._HasStorage()
                if cleanupNow is False:
                    return
            else:
                # Defer the storage cleanup if a request read or an append is in-flight; whoever finishes last will
                # run it (see _OnReadContextClosed and the AppendMessage finally), so we never free storage out from
                # under an active reader/writer or block this (possibly close-path) thread on disk IO.
                cleanupNow = self._state != UploadBodyState.Finalizing and self._activeReadContextCount == 0 and self._activeAppendCount == 0
            self._state = UploadBodyState.CleanedUp

        if cleanupNow is False:
            return
        self._CleanupStorage()


    def _CleanupStorage(self) -> None:
        with self._storageCleanupLock:
            if self.HasData is True:
                self._Debug("UploadBody cleaning up. Total bytes received: %d, chunks: %d", self.UploadBytesReceivedSoFar, len(self._chunks))

            if self._rawUploadFile is not None:
                self._rawUploadFile.close()
                self._rawUploadFile = None

            rawUploadFilePath = self._rawUploadFilePath
            bodyFilePath = self._bodyFilePath

            rawFileDeleted = self._DeleteFileIfExists(rawUploadFilePath)
            if rawFileDeleted:
                self._rawUploadFilePath = None

            if bodyFilePath == rawUploadFilePath:
                if rawFileDeleted:
                    self._bodyFilePath = None
            else:
                bodyFileDeleted = self._DeleteFileIfExists(bodyFilePath)
                if bodyFileDeleted:
                    self._bodyFilePath = None

            self._bodyBuffer = None
            self._chunks = []


    def _AppendRawDataToFile(self, rawData:Buffer, originalSizeBytes:int, compressionType:int, isLastMessage:bool) -> None:
        if self._rawUploadFile is None:
            raise Exception("OctoWebStreamUploadBody tried to write to a raw upload file before it was opened.")
        offset = self._rawUploadFile.tell()
        data = rawData.Get()
        self._rawUploadFile.write(data)
        self._chunks.append(_UploadChunk(offset, len(rawData), originalSizeBytes, compressionType, isLastMessage))


    def _FinalizeMemoryBody(self) -> None:
        if len(self._chunks) == 0:
            self._bodyBuffer = None
            return

        if self._hasCompressedChunks is False:
            if len(self._chunks) == 1:
                self._Debug("UploadBody has a single in-memory chunk with no compression, so we can use it directly without copying.")
                self._bodyBuffer = self._chunks[0].MemoryBuffer
                return

            target = bytearray(self.UploadBytesReceivedSoFar)
            writeOffset = 0
            for chunk in self._chunks:
                if chunk.MemoryBuffer is None:
                    raise Exception("OctoWebStreamUploadBody memory chunk was missing its buffer.")
                chunkData = chunk.MemoryBuffer.Get()
                target[writeOffset:writeOffset+len(chunk.MemoryBuffer)] = chunkData
                writeOffset += len(chunk.MemoryBuffer)
            self._bodyBuffer = Buffer(target)
            self._Debug("UploadBody had multiple in-memory chunks with no compression, so we concatenated them into a single buffer. chunks: %d; total bytes: %d", len(self._chunks), self.UploadBytesReceivedSoFar)
            return

        target = bytearray(self.UploadBytesReceivedSoFar)
        writeOffset = 0
        for chunk in self._chunks:
            if chunk.MemoryBuffer is None:
                raise Exception("OctoWebStreamUploadBody memory chunk was missing its buffer.")
            data = self._GetDecompressedChunk(chunk, chunk.MemoryBuffer)
            target[writeOffset:writeOffset+len(data)] = data.Get()
            writeOffset += len(data)

        if writeOffset != self.UploadBytesReceivedSoFar:
            raise Exception("OctoWebStreamUploadBody decompressed memory size mismatch. expected:"+str(self.UploadBytesReceivedSoFar)+"; actual:"+str(writeOffset))
        self._Debug("UploadBody had multiple in-memory chunks with compression, so we decompressed and concatenated them into a single buffer. chunks: %d; total bytes: %d", len(self._chunks), self.UploadBytesReceivedSoFar)
        self._bodyBuffer = Buffer(target)


    def _FinalizeFileBody(self) -> None:
        if self._rawUploadFile is None or self._rawUploadFilePath is None:
            raise Exception("OctoWebStreamUploadBody file finalize called without a raw upload file.")

        self._rawUploadFile.flush()
        self._rawUploadFile.close()
        self._rawUploadFile = None

        if self._hasCompressedChunks is False:
            self._bodyFilePath = self._rawUploadFilePath
            self._Debug("UploadBody had no compression, so we can use the raw upload file directly. path: %s", str(self._bodyFilePath))
            return

        finalFile = self._CreateTempFile()
        finalFilePath = finalFile.name
        totalWritten = 0
        success = False
        try:
            try:
                with open(self._rawUploadFilePath, "rb") as rawFile:
                    for chunk in self._chunks:
                        rawFile.seek(chunk.Offset)
                        if chunk.CompressionType == DataCompression.None_:
                            totalWritten += self._CopyBytes(rawFile, finalFile, chunk.SizeBytes)
                            continue

                        rawBytes = rawFile.read(chunk.SizeBytes)
                        if len(rawBytes) != chunk.SizeBytes:
                            raise Exception("OctoWebStreamUploadBody failed to read the full compressed upload chunk from disk.")
                        data = self._GetDecompressedChunk(chunk, Buffer(rawBytes))
                        finalFile.write(data.Get())
                        totalWritten += len(data)
                finalFile.flush()
            finally:
                finalFile.close()

            if totalWritten != self.UploadBytesReceivedSoFar:
                raise Exception("OctoWebStreamUploadBody decompressed file size mismatch. expected:"+str(self.UploadBytesReceivedSoFar)+"; actual:"+str(totalWritten))

            self._Debug("UploadBody had compressed chunks, so we decompressed them into a final file. chunks: %d; total bytes: %d; final path: %s", len(self._chunks), self.UploadBytesReceivedSoFar, str(finalFilePath))
            self._bodyFilePath = finalFilePath
            success = True
        finally:
            if success is False:
                self._DeleteFileIfExists(finalFilePath)


    def _GetDecompressedChunk(self, chunk:_UploadChunk, rawData:Buffer) -> Buffer:
        if chunk.CompressionType == DataCompression.None_:
            return rawData
        if chunk.CompressionType == DataCompression.Zlib:
            return self._ValidateDecompressedChunkSize(chunk, self._DecompressZlibChunk(chunk, rawData))
        if chunk.CompressionType == DataCompression.ZStandard:
            return self._ValidateDecompressedChunkSize(chunk, Compression.Get().Decompress(self.CompressionContext, rawData, chunk.OriginalSizeBytes, chunk.IsLastMessage, chunk.CompressionType))
        raise Exception("Unknown upload data compression type: " + str(chunk.CompressionType))


    def _DecompressZlibChunk(self, chunk:_UploadChunk, rawData:Buffer) -> Buffer:
        decompressor = zlib.decompressobj()
        maxOutputSizeBytes = chunk.OriginalSizeBytes
        try:
            data = decompressor.decompress(rawData.Get(), maxOutputSizeBytes + 1)
            if len(data) <= maxOutputSizeBytes:
                data += decompressor.flush(maxOutputSizeBytes + 1 - len(data))
            if len(data) > maxOutputSizeBytes:
                raise Exception("OctoWebStreamUploadBody decompressed zlib chunk exceeded expected size. expected:"+str(maxOutputSizeBytes)+"; actual at least:"+str(len(data)))
            if decompressor.eof is False:
                raise Exception("OctoWebStreamUploadBody zlib chunk did not finish within the expected output size.")
            if len(decompressor.unused_data) > 0:
                raise Exception("OctoWebStreamUploadBody zlib chunk had trailing compressed data.")
        except Exception as e:
            raise Exception("OctoWebStreamUploadBody failed to decompress zlib chunk: "+str(e)) from e
        return Buffer(data)


    def _ValidateDecompressedChunkSize(self, chunk:_UploadChunk, data:Buffer) -> Buffer:
        if len(data) != chunk.OriginalSizeBytes:
            raise Exception("OctoWebStreamUploadBody decompressed chunk size mismatch. expected:"+str(chunk.OriginalSizeBytes)+"; actual:"+str(len(data)))
        return data


    def _CopyBytes(self, source:BinaryIO, target:BinaryIO, bytesToCopy:int) -> int:
        bytesRemaining = bytesToCopy
        totalCopied = 0
        while bytesRemaining > 0:
            readSize = min(bytesRemaining, UploadBody.c_FileCopyBufferSizeBytes)
            data = source.read(readSize)
            if len(data) == 0:
                raise Exception("OctoWebStreamUploadBody hit EOF while copying upload data.")
            target.write(data)
            bytesRemaining -= len(data)
            totalCopied += len(data)
        return totalCopied


    def _SwitchToFile(self, reason:str) -> None:
        if self._usingFile:
            return

        rawUploadFile = self._CreateTempFile()
        self._rawUploadFile = rawUploadFile
        self._rawUploadFilePath = rawUploadFile.name
        for chunk in self._chunks:
            if chunk.MemoryBuffer is None:
                raise Exception("OctoWebStreamUploadBody tried to spill a memory chunk with no buffer.")
            chunk.Offset = rawUploadFile.tell()
            rawUploadFile.write(chunk.MemoryBuffer.Get())
            chunk.MemoryBuffer = None

        self._usingFile = True
        self._Info("switched upload buffering to disk because " + reason + ". limit:"+str(self.MaxInMemoryBodyBytes)+"; received:"+str(self.UploadBytesReceivedSoFar)+"; known:"+str(self.KnownFullUploadSizeBytes))


    def _CreateTempFile(self) -> Any:
        tempDir = None
        try:
            compression = Compression.Get()
            if compression is not None:
                tempDir = compression.LocalFileStoragePath
        except Exception:
            tempDir = None

        if tempDir is not None:
            tempDir = os.path.join(tempDir, "octowebstream_uploads")
            os.makedirs(tempDir, exist_ok=True)

        return tempfile.NamedTemporaryFile(prefix="oe-upload-", suffix=".tmp", dir=tempDir, mode="w+b", delete=False)


    def _DeleteFileIfExists(self, filePath:Optional[str]) -> bool:
        if filePath is None:
            return True
        try:
            if os.path.exists(filePath):
                os.remove(filePath)
            return True
        except Exception as e:
            self._Warn("failed to delete upload temp file. path:"+str(filePath)+" error:"+str(e))
        return False


    def _OnReadContextClosed(self) -> None:
        cleanupNow = False
        with self._lock:
            if self._activeReadContextCount > 0:
                self._activeReadContextCount -= 1
            else:
                self._Warn("read context closed but no active read contexts were tracked.")
            cleanupNow = self._state == UploadBodyState.CleanedUp and self._activeReadContextCount == 0
        if cleanupNow:
            self._CleanupStorage()


    def _HasStorage(self) -> bool:
        return self._rawUploadFile is not None or self._rawUploadFilePath is not None or self._bodyFilePath is not None or self._bodyBuffer is not None or len(self._chunks) > 0


    def _LogPrefix(self) -> str:
        return "Web Stream http - upload body - ["+str(self.StreamId)+"]"


    def _Debug(self, message:str, *args:Any) -> None:
        if self.Logger.isEnabledFor(logging.DEBUG) is False:
            return
        self.Logger.debug(self._LogPrefix() + " " + message, *args)


    def _Info(self, message:str, *args:Any) -> None:
        if self.Logger.isEnabledFor(logging.INFO) is False:
            return
        self.Logger.info(self._LogPrefix() + " " + message, *args)


    def _Warn(self, message:str, *args:Any) -> None:
        if self.Logger.isEnabledFor(logging.WARNING) is False:
            return
        self.Logger.warning(self._LogPrefix() + " " + message, *args)
