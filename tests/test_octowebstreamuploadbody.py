import logging
import os
import unittest
import zlib
from unittest.mock import patch

from octoeverywhere.WebStream.uploadbody import UploadBody
from octoeverywhere.commandhandler import CommandHandler
from octoeverywhere.compression import CompressionContext
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.Proto.DataCompression import DataCompression
from octoeverywhere.Proto.PathTypes import PathTypes


class FakeWebStreamMsg:
    def __init__(self, data:bytes, compressionType:int=DataCompression.None_, originalDataSize:int=0, isDone:bool=False) -> None:
        self._data = bytearray(data)
        self._compressionType = compressionType
        self._originalDataSize = originalDataSize
        self._isDone = isDone


    def DataLength(self) -> int:
        return len(self._data)


    def DataAsByteArray(self) -> bytearray:
        return self._data


    def DataCompression(self) -> int:
        return self._compressionType


    def OriginalDataSize(self) -> int:
        return self._originalDataSize


    def IsDataTransmissionDone(self) -> bool:
        return self._isDone


class FakeHttpInitialContext:
    def __init__(self, path:str) -> None:
        self._path = path.encode("utf-8")


    def Path(self) -> bytes:
        return self._path


    def PathType(self) -> int:
        return PathTypes.Relative


class FakeResponse:
    def __init__(self, statusCode:int) -> None:
        self.status_code = statusCode
        self.headers = {"Content-Length": "0"}


    def __enter__(self) -> "FakeResponse":
        return self


    def __exit__(self, t, v, tb) -> None:
        return None


class RecordingSession:
    def __init__(self, responses) -> None:
        self.Responses = list(responses)
        self.Calls = []


    def request(self, method, url, headers=None, data=None, timeout=None, allow_redirects=False, stream=True, verify=False):
        if hasattr(data, "read"):
            body = data.read()
        elif data is None:
            body = None
        else:
            body = bytes(data)

        self.Calls.append({
            "method": method,
            "url": url,
            "headers": headers,
            "body": body,
            "timeout": timeout,
            "allow_redirects": allow_redirects,
            "stream": stream,
            "verify": verify,
        })
        return self.Responses.pop(0)


class TestOctoWebStreamUploadBody(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("test-upload-body")
        self.compressionContext = CompressionContext(self.logger)


    def tearDown(self) -> None:
        self.compressionContext.__exit__(None, None, None)


    def _readRequestBody(self, body:UploadBody) -> bytes:
        with body.OpenForRequest() as requestBody:
            self.assertIsNotNone(requestBody)
            if isinstance(requestBody, (bytes, bytearray)):
                return bytes(requestBody)
            return requestBody.read()


    def test_small_uncompressed_upload_stays_in_memory(self) -> None:
        body = UploadBody(self.logger, 1, None, self.compressionContext, maxInMemoryBodyBytes=1024)
        self.addCleanup(body.Cleanup)

        body.AppendMessage(FakeWebStreamMsg(b"hello "))
        body.AppendMessage(FakeWebStreamMsg(b"world", isDone=True))
        body.Finalize()

        self.assertFalse(body.IsUsingFile)
        self.assertEqual(self._readRequestBody(body), b"hello world")
        bodyBuffer = body.GetBodyAsBuffer()
        self.assertIsNotNone(bodyBuffer)
        self.assertEqual(bytes(bodyBuffer.GetBytesLike()), b"hello world")


    def test_large_known_uncompressed_upload_uses_file_and_cleans_up(self) -> None:
        payload = b"hello world"
        body = UploadBody(self.logger, 1, len(payload), self.compressionContext, maxInMemoryBodyBytes=5)
        self.addCleanup(body.Cleanup)

        body.AppendMessage(FakeWebStreamMsg(b"hello "))
        body.AppendMessage(FakeWebStreamMsg(b"world", isDone=True))
        body.Finalize()

        self.assertTrue(body.IsUsingFile)
        context = body.OpenForRequest()
        try:
            requestBody = context.GetData()
            self.assertIsNotNone(requestBody)
            self.assertFalse(isinstance(requestBody, (bytes, bytearray)))
            self.assertEqual(requestBody.read(), payload)
            self.assertIsNotNone(context.FilePath)
            self.assertTrue(os.path.exists(context.FilePath))
            bodyBuffer = body.GetBodyAsBuffer()
            self.assertIsNotNone(bodyBuffer)
            self.assertEqual(bytes(bodyBuffer.GetBytesLike()), payload)
        finally:
            context.Close()

        filePath = context.FilePath
        body.Cleanup()
        self.assertIsNotNone(filePath)
        self.assertFalse(os.path.exists(filePath))


    def test_compressed_upload_decompresses_in_memory(self) -> None:
        original = b"abc123" * 100
        compressed = zlib.compress(original)
        body = UploadBody(self.logger, 1, len(original), self.compressionContext, maxInMemoryBodyBytes=4096)
        self.addCleanup(body.Cleanup)

        body.AppendMessage(FakeWebStreamMsg(compressed, DataCompression.Zlib, len(original), isDone=True))
        body.Finalize()

        self.assertFalse(body.IsUsingFile)
        self.assertEqual(self._readRequestBody(body), original)


    def test_large_compressed_upload_decompresses_to_file_and_cleans_raw_file(self) -> None:
        originalA = b"abc123" * 200
        originalB = b"xyz789" * 200
        compressedA = zlib.compress(originalA)
        compressedB = zlib.compress(originalB)
        body = UploadBody(self.logger, 1, len(originalA) + len(originalB), self.compressionContext, maxInMemoryBodyBytes=128)
        self.addCleanup(body.Cleanup)

        body.AppendMessage(FakeWebStreamMsg(compressedA, DataCompression.Zlib, len(originalA)))
        body.AppendMessage(FakeWebStreamMsg(compressedB, DataCompression.Zlib, len(originalB), isDone=True))
        body.Finalize()

        self.assertTrue(body.IsUsingFile)
        self.assertEqual(self._readRequestBody(body), originalA + originalB)

        rawFilePath = body._rawUploadFilePath
        context = body.OpenForRequest()
        finalFilePath = context.FilePath
        context.Close()
        self.assertIsNotNone(rawFilePath)
        self.assertIsNotNone(finalFilePath)
        self.assertTrue(os.path.exists(rawFilePath))
        self.assertTrue(os.path.exists(finalFilePath))

        body.Cleanup()
        self.assertFalse(os.path.exists(rawFilePath))
        self.assertFalse(os.path.exists(finalFilePath))


    def test_known_size_mismatch_fails_finalize(self) -> None:
        body = UploadBody(self.logger, 1, 10, self.compressionContext, maxInMemoryBodyBytes=1024)
        self.addCleanup(body.Cleanup)

        body.AppendMessage(FakeWebStreamMsg(b"short", isDone=True))

        with self.assertRaisesRegex(Exception, "haven't gotten all of the upload payload"):
            body.Finalize()


    def test_upload_larger_than_known_size_is_rejected(self) -> None:
        body = UploadBody(self.logger, 1, 3, self.compressionContext, maxInMemoryBodyBytes=1024)
        self.addCleanup(body.Cleanup)

        with self.assertRaisesRegex(Exception, "Too many bytes"):
            body.AppendMessage(FakeWebStreamMsg(b"toolong", isDone=True))


    def test_file_request_context_can_seek_to_start_for_retry(self) -> None:
        payload = b"retry-body"
        body = UploadBody(self.logger, 1, len(payload), self.compressionContext, maxInMemoryBodyBytes=4)
        self.addCleanup(body.Cleanup)

        body.AppendMessage(FakeWebStreamMsg(payload, isDone=True))
        body.Finalize()

        context = body.OpenForRequest()
        try:
            requestBody = context.GetData()
            self.assertEqual(requestBody.read(5), b"retry")
            context.SeekToStart()
            self.assertEqual(requestBody.read(), payload)
        finally:
            context.Close()


    def test_http_431_retry_rewinds_file_backed_upload(self) -> None:
        payload = b"retry-body-over-file-limit"
        body = UploadBody(self.logger, 1, len(payload), self.compressionContext, maxInMemoryBodyBytes=4)
        self.addCleanup(body.Cleanup)

        body.AppendMessage(FakeWebStreamMsg(payload, isDone=True))
        body.Finalize()
        session = RecordingSession([FakeResponse(431), FakeResponse(200)])

        with patch("octoeverywhere.octohttprequest.HttpSessions.GetSession", return_value=session):
            ret = OctoHttpRequest.MakeHttpCallAttempt(self.logger, "test", "POST", "http://example.local/api", {"X-Test": "1"}, body, None, False, None)

        self.assertTrue(ret.IsChainDone)
        self.assertIsNotNone(ret.Result)
        self.assertEqual(ret.Result.StatusCode, 200)
        self.assertEqual(len(session.Calls), 2)
        self.assertEqual(session.Calls[0]["body"], payload)
        self.assertEqual(session.Calls[1]["body"], payload)
        self.assertEqual(session.Calls[0]["headers"], {"X-Test": "1"})
        self.assertEqual(session.Calls[1]["headers"], {})
        ret.Result.Free()


    def test_command_path_parsing_allows_no_post_body(self) -> None:
        handler = CommandHandler(self.logger, None, None, None)
        context = FakeHttpInitialContext(CommandHandler.c_CommandHandlerPathPrefix + "proxy/mqtt?printerId=abc")

        commandPath, commandPathLower, jsonObj = handler._GetPathAndJsonArgs(context, None)

        self.assertEqual(commandPath, "proxy/mqtt?printerId=abc")
        self.assertEqual(commandPathLower, "proxy/mqtt?printerid=abc")
        self.assertEqual(jsonObj, {"printerid": "abc"})


    def test_command_path_parsing_reads_file_backed_post_body(self) -> None:
        body = UploadBody(self.logger, 1, None, self.compressionContext, maxInMemoryBodyBytes=4)
        self.addCleanup(body.Cleanup)
        body.AppendMessage(FakeWebStreamMsg(b'{"transportType":"http","request":{},"path":"/api/version"}', isDone=True))
        body.Finalize()
        handler = CommandHandler(self.logger, None, None, None)
        context = FakeHttpInitialContext(CommandHandler.c_CommandHandlerPathPrefix + "send-command")

        commandPath, commandPathLower, jsonObj = handler._GetPathAndJsonArgs(context, body)

        self.assertEqual(commandPath, "send-command")
        self.assertEqual(commandPathLower, "send-command")
        self.assertIsNotNone(jsonObj)
        self.assertEqual(jsonObj["transportType"], "http")
        self.assertEqual(jsonObj["path"], "/api/version")


if __name__ == "__main__":
    unittest.main()
