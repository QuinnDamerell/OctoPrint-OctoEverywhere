# ruff: noqa: E402
import importlib.util
import ftplib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
import zlib
from unittest.mock import patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

from octoeverywhere.WebStream.uploadbody import MultipartFormUploadBody, UploadBody
from octoeverywhere.commandhandler import CommandHandler
from octoeverywhere.compression import CompressionContext
from octoeverywhere.filesystemcommands import FileSystemCommandHelper, FileSystemTreeBuilder
from octoeverywhere.interfaces import CommandResponse
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


class FakeFileCommandPlatform:
    def __init__(self) -> None:
        self.Args = None
        self.UsedFileBackedUploadBody = False
        self.BodyBytes = b""


    def ExecuteFileUpload(self, args, uploadBody):
        self.Args = args
        self.UsedFileBackedUploadBody = uploadBody.IsUsingFile
        with uploadBody.OpenForRequest() as requestBody:
            self.BodyBytes = requestBody.read() if hasattr(requestBody, "read") else bytes(requestBody)
        return CommandResponse.Success({
            "ok": True,
            "size": len(self.BodyBytes)
        })


class FakeUploadHttpResult:
    def __init__(self) -> None:
        self.StatusCode = 200
        self.FullBodyBuffer = None
        self.FreeCalled = False


    def ReadAllContentFromStreamResponse(self, logger) -> None:
        return None


    def Free(self) -> None:
        self.FreeCalled = True


class FakeBambuFtpServer:
    def __init__(self) -> None:
        self.Dirs = {""}
        self.Files = {}
        self.Connections = []


    def CreateClient(self):
        client = FakeBambuFtpClient(self)
        self.Connections.append(client)
        return client


class FakeBambuFtpClient:
    def __init__(self, server:FakeBambuFtpServer) -> None:
        self.Server = server
        self.Closed = False
        self.Passive = None


    def set_pasv(self, passive:bool) -> None:
        self.Passive = passive


    def connect(self, host, port, timeout=None) -> None:
        self.Host = host
        self.Port = port
        self.Timeout = timeout


    def login(self, user, passwd) -> None:
        self.User = user
        self.Password = passwd
        if passwd == "bad":
            raise ftplib.error_perm("530 Login incorrect.")


    def prot_p(self):
        return "200 Protection set to Private"


    def retrlines(self, command, callback) -> str:
        path = ""
        if command.startswith("LIST "):
            path = command[5:].strip("/")
        if path not in self.Server.Dirs:
            raise ftplib.error_perm("550 Directory not found.")
        for name, isDir, size in self._ListChildren(path):
            mode = "drwxr-xr-x" if isDir else "-rw-r--r--"
            callback(f"{mode} 1 owner group {size} Jan 02 2025 {name}")
        return "226 Directory send OK."


    def storbinary(self, command, reader, blocksize=8192):
        path = command[5:].strip("/")
        payload = bytearray()
        while True:
            chunk = reader.read(blocksize)
            if chunk is None or len(chunk) == 0:
                break
            payload.extend(chunk)
        parent = self._Parent(path)
        if parent not in self.Server.Dirs:
            raise ftplib.error_perm("550 Directory not found.")
        self.Server.Files[path] = bytes(payload)
        return "226 Transfer complete."


    def retrbinary(self, command, callback, blocksize=8192):
        path = command[5:].strip("/")
        if path not in self.Server.Files:
            raise ftplib.error_perm("550 File not found.")
        data = self.Server.Files[path]
        for offset in range(0, len(data), blocksize):
            callback(data[offset:offset + blocksize])
        return "226 Transfer complete."


    def delete(self, path):
        path = path.strip("/")
        if path not in self.Server.Files:
            raise ftplib.error_perm("550 File not found.")
        del self.Server.Files[path]
        return "250 Delete operation successful."


    def mkd(self, path):
        path = path.strip("/")
        if path in self.Server.Dirs:
            raise ftplib.error_perm("550 Directory already exists.")
        parent = self._Parent(path)
        if parent not in self.Server.Dirs:
            raise ftplib.error_perm("550 Parent not found.")
        self.Server.Dirs.add(path)
        return path


    def size(self, path):
        path = path.strip("/")
        if path not in self.Server.Files:
            raise ftplib.error_perm("550 File not found.")
        return len(self.Server.Files[path])


    def quit(self) -> None:
        self.Closed = True


    def close(self) -> None:
        self.Closed = True


    def _ListChildren(self, path):
        prefix = "" if len(path) == 0 else path.rstrip("/") + "/"
        seen = {}
        for directory in self.Server.Dirs:
            if len(directory) == 0 or not directory.startswith(prefix):
                continue
            remainder = directory[len(prefix):]
            if len(remainder) == 0 or "/" in remainder:
                continue
            seen[remainder] = (True, 0)
        for filePath, data in self.Server.Files.items():
            if not filePath.startswith(prefix):
                continue
            remainder = filePath[len(prefix):]
            if len(remainder) == 0 or "/" in remainder:
                continue
            seen[remainder] = (False, len(data))
        return [(name, isDir, size) for name, (isDir, size) in sorted(seen.items())]


    def _Parent(self, path):
        slash = path.rfind("/")
        if slash == -1:
            return ""
        return path[:slash]


class FakeBambuCommandResult:
    def __init__(self, result=None, connected=True, timeout=False) -> None:
        self.Result = result
        self.Connected = connected
        self.Timeout = timeout
        self.OtherError = None
        self.Ex = None


    def HasError(self) -> bool:
        return self.Result is None or self.Connected is False or self.Timeout is True


    def GetLoggingErrorStr(self) -> str:
        return "fake error"


class FakeBambuClientForCommands:
    def __init__(self) -> None:
        self.Commands = []


    def SendCommand(self, payload, timeoutSec=None, waitForResponse=True):
        self.Commands.append({
            "payload": payload,
            "timeoutSec": timeoutSec,
            "waitForResponse": waitForResponse,
        })
        return FakeBambuCommandResult({
            "request": {
                "topic": "device/sn/request",
                "payload": payload,
                "qos": 0,
            },
            "response": {
                "topic": "device/sn/report",
                "payload": {
                    "print": {
                        "command": payload["print"]["command"],
                        "result": "success",
                    }
                },
            },
        })


    def IsDisconnectDueToAuth(self) -> bool:
        return False


def LoadPlatformCommandHandlerModule(packageName:str, moduleName:str):
    aliasPackageName = "_tests_" + packageName
    fullName = aliasPackageName + "." + moduleName
    if fullName in sys.modules:
        return sys.modules[fullName]

    packagePath = Path(__file__).resolve().parents[1] / packageName
    if aliasPackageName not in sys.modules:
        package = types.ModuleType(aliasPackageName)
        package.__path__ = [str(packagePath)] # type: ignore[attr-defined]
        sys.modules[aliasPackageName] = package

    spec = importlib.util.spec_from_file_location(fullName, packagePath / (moduleName + ".py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load " + fullName)
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullName] = module
    spec.loader.exec_module(module)
    return module


class TestOctoWebStreamUploadBody(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("test-upload-body")
        self.compressionContext = CompressionContext(self.logger)


    def tearDown(self) -> None:
        self.compressionContext.__exit__(None, None, None)


    def _BuildFinalizedUploadBody(self, payload:bytes) -> UploadBody:
        body = UploadBody(self.logger, 1, len(payload), self.compressionContext, maxInMemoryBodyBytes=1024)
        self.addCleanup(body.Cleanup)
        body.AppendMessage(FakeWebStreamMsg(payload, isDone=True))
        body.Finalize()
        return body


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


    def test_compressed_upload_rejects_original_size_mismatch(self) -> None:
        original = b"abc123" * 100
        compressed = zlib.compress(original)
        body = UploadBody(self.logger, 1, 20, self.compressionContext, maxInMemoryBodyBytes=4096)
        self.addCleanup(body.Cleanup)

        body.AppendMessage(FakeWebStreamMsg(compressed, DataCompression.Zlib, 20, isDone=True))

        with self.assertRaisesRegex(Exception, "decompressed zlib chunk exceeded expected size"):
            body.Finalize()


    def test_failed_file_decompression_removes_intermediate_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempDir:
            body = UploadBody(self.logger, 1, None, self.compressionContext, maxInMemoryBodyBytes=1)

            def createTempFile():
                return tempfile.NamedTemporaryFile(prefix="oe-upload-", suffix=".tmp", dir=tempDir, mode="w+b", delete=False)

            try:
                with patch.object(body, "_CreateTempFile", side_effect=createTempFile):
                    body.AppendMessage(FakeWebStreamMsg(b"not-zlib-data", DataCompression.Zlib, 100, isDone=True))
                    self.assertEqual(len(os.listdir(tempDir)), 1)

                    with self.assertRaisesRegex(Exception, "zlib chunk"):
                        body.Finalize()

                    self.assertEqual(len(os.listdir(tempDir)), 1)
            finally:
                body.Cleanup()

            self.assertEqual(os.listdir(tempDir), [])


    def test_cleanup_waits_for_file_request_context_close(self) -> None:
        payload = b"cleanup-body-over-file-limit"
        body = UploadBody(self.logger, 1, len(payload), self.compressionContext, maxInMemoryBodyBytes=4)
        self.addCleanup(body.Cleanup)

        body.AppendMessage(FakeWebStreamMsg(payload, isDone=True))
        body.Finalize()

        context = body.OpenForRequest()
        filePath = context.FilePath
        try:
            self.assertIsNotNone(filePath)
            self.assertTrue(os.path.exists(filePath))
            body.Cleanup()
            self.assertTrue(os.path.exists(filePath))
        finally:
            context.Close()

        self.assertIsNotNone(filePath)
        self.assertFalse(os.path.exists(filePath))


    def test_append_after_cleanup_is_ignored(self) -> None:
        # If the stream is torn down (Cleanup) while more upload data is still arriving, the late append should
        # be dropped quietly rather than raising and resetting the whole connection.
        body = UploadBody(self.logger, 1, None, self.compressionContext, maxInMemoryBodyBytes=1024)
        self.addCleanup(body.Cleanup)

        body.AppendMessage(FakeWebStreamMsg(b"hello"))
        body.Cleanup()

        # This must not raise.
        body.AppendMessage(FakeWebStreamMsg(b"world", isDone=True))
        self.assertEqual(body.UploadBytesReceivedSoFar, len(b"hello"))


    def test_cleanup_during_append_defers_then_cleans_up(self) -> None:
        # Simulates the socket close path calling Cleanup() while an append is actively writing to the spill file
        # (these run on different threads in production). Cleanup() must not delete the file out from under the
        # in-flight write; it defers, and the append finishes the cleanup once the write completes.
        payload = b"x" * 64
        body = UploadBody(self.logger, 1, len(payload), self.compressionContext, maxInMemoryBodyBytes=8)
        self.addCleanup(body.Cleanup)
        self.assertTrue(body.IsUsingFile)

        rawPath = body._rawUploadFilePath
        self.assertIsNotNone(rawPath)
        self.assertTrue(os.path.exists(rawPath))

        class ReentrantCleanupMsg(FakeWebStreamMsg):
            def IsDataTransmissionDone(self) -> bool:
                # Fire a concurrent-style Cleanup() exactly while this append is in-flight.
                body.Cleanup()
                return True

        # The append must not raise even though Cleanup() ran mid-write...
        body.AppendMessage(ReentrantCleanupMsg(payload, isDone=True))

        # ...and the storage that Cleanup() deferred must be removed once the append finished.
        self.assertFalse(os.path.exists(rawPath))


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


    def test_raw_file_upload_command_does_not_parse_body_as_json(self) -> None:
        payload = b"\x00raw-gcode-body-not-json"
        body = UploadBody(self.logger, 1, len(payload), self.compressionContext, maxInMemoryBodyBytes=4)
        self.addCleanup(body.Cleanup)
        body.AppendMessage(FakeWebStreamMsg(payload, isDone=True))
        body.Finalize()
        self.assertTrue(body.IsUsingFile)

        platform = FakeFileCommandPlatform()
        handler = CommandHandler(self.logger, None, platform, None)
        context = FakeHttpInitialContext(CommandHandler.c_CommandHandlerPathPrefix + CommandHandler.c_FilesUploadCommand + "?path=gcode/folder/test.gcode&print=true")

        result = handler.HandleCommand(context, body)

        self.assertEqual(result.StatusCode, 200)
        self.assertIsNotNone(result.FullBodyBuffer)
        responseObj = json.loads(result.FullBodyBuffer.GetBytesLike().decode("utf-8"))
        self.assertEqual(responseObj["Status"], 200)
        self.assertTrue(responseObj["Result"]["ok"])
        self.assertEqual(responseObj["Result"]["size"], len(payload))
        self.assertEqual(platform.Args["path"], "gcode/folder/test.gcode")
        self.assertEqual(platform.Args["print"], "true")
        self.assertTrue(platform.UsedFileBackedUploadBody)
        self.assertEqual(platform.BodyBytes, payload)


    def test_octoprint_file_upload_forwards_backend_options(self) -> None:
        module = LoadPlatformCommandHandlerModule("octoprint_octoeverywhere", "octoprintcommandhandler")
        handler = module.OctoPrintCommandHandler(self.logger, None, None, None)
        handler._AddOctoPrintLocalAuth = lambda headers: None
        uploadBody = self._BuildFinalizedUploadBody(b"G1 X1\n")
        captured = {}

        def fakeMakeHttpCall(logger, path, pathType, method, headers, body=None, **kwargs):
            captured["path"] = path
            captured["method"] = method
            captured["fields"] = dict(body.Fields)
            return FakeUploadHttpResult()

        with patch.object(module.OctoHttpRequest, "MakeHttpCall", fakeMakeHttpCall):
            response = handler.ExecuteFileUpload({
                "path": "gcode/folder/a.gcode",
                "select": "true",
                "print": True,
            }, uploadBody)

        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(captured["path"], "/api/files/local")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["fields"], {
            "path": "folder",
        })
        self.assertEqual(response.ResultDict["VirtualPath"], "gcode/folder/a.gcode")
        self.assertEqual(response.ResultDict["PlatformPath"], "folder/a.gcode")
        self.assertEqual(response.ResultDict["SizeBytes"], len(b"G1 X1\n"))


    def test_moonraker_file_upload_forwards_backend_options(self) -> None:
        module = LoadPlatformCommandHandlerModule("moonraker_octoeverywhere", "moonrakercommandhandler")
        handler = module.MoonrakerCommandHandler(self.logger, None)
        handler._AddMoonrakerAuth = lambda headers: None
        uploadBody = self._BuildFinalizedUploadBody(b"G1 X1\n")
        captured = {}

        def fakeMakeHttpCall(logger, path, pathType, method, headers, body=None, **kwargs):
            captured["path"] = path
            captured["method"] = method
            captured["fields"] = dict(body.Fields)
            return FakeUploadHttpResult()

        with patch.object(module.OctoHttpRequest, "MakeHttpCall", fakeMakeHttpCall):
            response = handler.ExecuteFileUpload({
                "Path": "gcode/folder/a.gcode",
                "Print": "yes",
                "Checksum": "abc123",
            }, uploadBody)

        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(captured["path"], "/server/files/upload")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["fields"], {
            "root": "gcodes",
            "path": "folder",
        })
        self.assertEqual(response.ResultDict["VirtualPath"], "gcode/folder/a.gcode")
        self.assertEqual(response.ResultDict["PlatformPath"], "folder/a.gcode")
        self.assertEqual(response.ResultDict["SizeBytes"], len(b"G1 X1\n"))


    def test_multipart_form_upload_body_streams_file_backed_upload(self) -> None:
        payload = b"G1 X1 Y1\n" * 4
        body = UploadBody(self.logger, 1, len(payload), self.compressionContext, maxInMemoryBodyBytes=4)
        self.addCleanup(body.Cleanup)
        body.AppendMessage(FakeWebStreamMsg(payload, isDone=True))
        body.Finalize()
        self.assertTrue(body.IsUsingFile)

        multipart = MultipartFormUploadBody(self.logger, body, "test.gcode", {"path": "folder"}, boundary="boundary-test")
        context = multipart.OpenForRequest()
        try:
            reader = context.GetData()
            data = reader.read(11) + reader.read(7) + reader.read()
        finally:
            context.Close()

        self.assertEqual(len(data), multipart.GetContentLength())
        self.assertIn(b'Content-Disposition: form-data; name="path"', data)
        self.assertIn(b'Content-Disposition: form-data; name="file"; filename="test.gcode"', data)
        self.assertIn(payload, data)
        self.assertTrue(data.endswith(b"\r\n--boundary-test--\r\n"))


    def test_multipart_reader_reports_len_for_content_length(self) -> None:
        # requests/urllib3 derive Content-Length from len(body) via super_len(). The reader must report the full
        # multipart length so the upload is framed even on retries that send no caller-supplied Content-Length.
        payload = b"G1 X1 Y1\n" * 4
        body = UploadBody(self.logger, 1, len(payload), self.compressionContext, maxInMemoryBodyBytes=4)
        self.addCleanup(body.Cleanup)
        body.AppendMessage(FakeWebStreamMsg(payload, isDone=True))
        body.Finalize()

        multipart = MultipartFormUploadBody(self.logger, body, "test.gcode", {"path": "folder"}, boundary="boundary-test")
        context = multipart.OpenForRequest()
        try:
            reader = context.GetData()
            self.assertEqual(len(reader), multipart.GetContentLength())
        finally:
            context.Close()


    def test_file_tree_builder_creates_virtual_gcode_root(self) -> None:
        tree = FileSystemTreeBuilder.FromMoonrakerFileList([
            {"path": "folder/b.gcode", "size": 20, "modified": 2},
            {"path": "a.gcode", "size": 10, "modified": 1},
        ])

        rootChildren = tree["Root"]
        self.assertEqual(len(rootChildren), 1)
        gcodeRoot = rootChildren[0]
        self.assertEqual(gcodeRoot["Name"], "gcode")
        self.assertEqual(gcodeRoot["VirtualPath"], "gcode")
        self.assertEqual([c["Name"] for c in gcodeRoot["Children"]], ["folder", "a.gcode"])
        folder = gcodeRoot["Children"][0]
        self.assertEqual(folder["Children"][0]["VirtualPath"], "gcode/folder/b.gcode")
        self.assertEqual(folder["Children"][0]["SizeBytes"], 20)


    def test_send_command_http_parse_is_pascal_case_with_lowercase_fallback(self) -> None:
        # PascalCase is canonical; lowercase is still accepted for leniency.
        parsedPascal = CommandHandler.ParseHttpSendCommand({
            "Path": "/api/version",
            "Method": "post",
            "Headers": {"X-Test": "canonical"},
            "headers": {"X-Test": "lower"},
            "TimeoutSec": 42
        }, {"a": 1})
        self.assertNotIsInstance(parsedPascal, CommandResponse)
        self.assertEqual(parsedPascal.Path, "/api/version")   #type: ignore[union-attr]
        self.assertEqual(parsedPascal.Method, "POST")          #type: ignore[union-attr]
        self.assertEqual(parsedPascal.Headers["X-Test"], "canonical") #type: ignore[union-attr]
        self.assertIsNotNone(parsedPascal.BodyBytes)           #type: ignore[union-attr]
        self.assertEqual(parsedPascal.TimeoutSec, 42)           #type: ignore[union-attr]

        parsedLower = CommandHandler.ParseHttpSendCommand({"path": "/api/version"}, {})
        self.assertNotIsInstance(parsedLower, CommandResponse)
        self.assertEqual(parsedLower.Path, "/api/version")     #type: ignore[union-attr]
        self.assertEqual(parsedLower.Method, "GET")            #type: ignore[union-attr]
        self.assertEqual(parsedLower.TimeoutSec, 10)            #type: ignore[union-attr]

        missing = CommandHandler.ParseHttpSendCommand({}, {})
        self.assertIsInstance(missing, CommandResponse)

        invalidTimeout = CommandHandler.ParseHttpSendCommand({"Path": "/api/version", "TimeoutSec": "1.5"}, {})
        self.assertIsInstance(invalidTimeout, CommandResponse)


    def test_send_command_wait_for_response_parsing(self) -> None:
        # Default is wait. The flag is case-insensitive and accepts string/bool.
        ws = CommandHandler.ParseWebsocketSendCommand({}, {"Method": "printer.info"})
        self.assertTrue(ws.WaitForResponse)                    #type: ignore[union-attr]
        self.assertEqual(ws.Method, "printer.info")            #type: ignore[union-attr]
        self.assertEqual(ws.TimeoutSec, 10)                     #type: ignore[union-attr]

        wsNoWait = CommandHandler.ParseWebsocketSendCommand({"WaitForResponse": False, "TimeoutSec": "11"}, {"Cmd": 1})
        self.assertFalse(wsNoWait.WaitForResponse)             #type: ignore[union-attr]
        self.assertEqual(wsNoWait.Method, 1)                   #type: ignore[union-attr]
        self.assertEqual(wsNoWait.TimeoutSec, 11)               #type: ignore[union-attr]

        mqtt = CommandHandler.ParseMqttSendCommand({"waitForResponse": "false"}, {"Method": 5})
        self.assertFalse(mqtt.WaitForResponse)                 #type: ignore[union-attr]
        self.assertEqual(mqtt.Method, 5)                       #type: ignore[union-attr]
        self.assertEqual(mqtt.TimeoutSec, 10)                   #type: ignore[union-attr]


    def test_build_send_command_result_envelope_is_common(self) -> None:
        # Normal response.
        ok = CommandHandler.BuildSendCommandResult("websocket", {"Method": "m"}, {"x": 1})
        self.assertEqual(ok.StatusCode, 200)
        self.assertEqual(ok.ResultDict["TransportType"], "websocket")
        self.assertEqual(ok.ResultDict["Request"], {"Method": "m"})
        self.assertEqual(ok.ResultDict["Response"], {"x": 1})
        self.assertTrue(ok.ResultDict["ResponseReceived"])
        self.assertFalse(ok.ResultDict["IsError"])

        # Protocol error response.
        err = CommandHandler.BuildSendCommandResult("http", {"Path": "/x"}, {"StatusCode": 404}, isError=True)
        self.assertTrue(err.ResultDict["IsError"])
        self.assertEqual(err.ResultDict["Response"], {"StatusCode": 404})

        # Fire-and-forget keeps the same schema, with a null Response.
        forget = CommandHandler.BuildSendCommandResult("mqtt", {"Topic": "t"}, responseReceived=False, waitForResponse=False, timeoutSec=12)
        self.assertFalse(forget.ResultDict["ResponseReceived"])
        self.assertIsNone(forget.ResultDict["Response"])
        self.assertFalse(forget.ResultDict["IsError"])
        self.assertFalse(forget.ResultDict["WaitForResponse"])
        self.assertEqual(forget.ResultDict["TimeoutSec"], 12)


    def test_file_tree_includes_native_platform_path(self) -> None:
        moonraker = FileSystemTreeBuilder.FromMoonrakerFileList([{"path": "folder/b.gcode", "size": 20, "modified": 2}])
        fileNode = moonraker["Root"][0]["Children"][0]["Children"][0]
        self.assertEqual(fileNode["VirtualPath"], "gcode/folder/b.gcode")
        self.assertEqual(fileNode["PlatformPath"], "folder/b.gcode")
        self.assertEqual(fileNode["ModifiedTimeSec"], 2)

        octoprint = FileSystemTreeBuilder.FromOctoPrintFileList([{"type": "machinecode", "path": "sub/a.gcode", "size": 10, "date": 3}])
        opFile = octoprint["Root"][0]["Children"][0]["Children"][0]
        self.assertEqual(opFile["VirtualPath"], "gcode/sub/a.gcode")
        self.assertEqual(opFile["PlatformPath"], "sub/a.gcode")
        self.assertEqual(opFile["ModifiedTimeSec"], 3)


    def test_octoprint_file_tree_keeps_selected_metadata(self) -> None:
        octoprint = FileSystemTreeBuilder.FromOctoPrintFileList([{
            "type": "machinecode",
            "path": "sub/a.gcode",
            "size": 10,
            "date": 3,
            "hash": "abc",
            "origin": "local",
            "display": "A",
            "refs": {"resource": "/api/files/local/sub/a.gcode"}
        }])
        opFile = octoprint["Root"][0]["Children"][0]["Children"][0]
        self.assertEqual(opFile["Hash"], "abc")
        self.assertEqual(opFile["Origin"], "local")
        self.assertEqual(opFile["Display"], "A")
        self.assertNotIn("refs", opFile)


    def test_bambu_file_list_uses_ftps_tree_and_filters_printable_files(self) -> None:
        module = self._LoadBambuCommandHandlerModule()
        ftpServer = FakeBambuFtpServer()
        ftpServer.Dirs.update({"cache", "cache/sub", "image"})
        ftpServer.Files["root.3mf"] = b"root"
        ftpServer.Files["cache/test.gcode.3mf"] = b"123"
        ftpServer.Files["cache/sub/raw.gcode"] = b"abcde"
        ftpServer.Files["cache/notes.txt"] = b"not printable"
        ftpServer.Files["image/preview.jpg"] = b"not listed"

        def managerFactory():
            return module.BambuFileManager(self.logger, "192.168.1.40", "123456", ftpFactory=ftpServer.CreateClient)

        handler = module.BambuCommandHandler(self.logger, None, fileManagerFactory=managerFactory)
        response = handler.ExecuteFileList(None)

        self.assertEqual(response.StatusCode, 200)
        fileNodes = self._FlattenFileTree(response.ResultDict)
        virtualPaths = sorted([f["VirtualPath"] for f in fileNodes])
        self.assertEqual(virtualPaths, [
            "gcode/cache/sub/raw.gcode",
            "gcode/cache/test.gcode.3mf",
            "gcode/root.3mf",
        ])
        cacheFile = next(f for f in fileNodes if f["VirtualPath"] == "gcode/cache/test.gcode.3mf")
        self.assertEqual(cacheFile["PlatformPath"], "cache/test.gcode.3mf")
        self.assertEqual(cacheFile["SizeBytes"], 3)
        self.assertTrue(all(c.Passive for c in ftpServer.Connections))


    def test_bambu_file_upload_download_and_delete_use_ftps(self) -> None:
        module = self._LoadBambuCommandHandlerModule()
        ftpServer = FakeBambuFtpServer()
        uploadPayload = b"G1 X1 Y1\n"

        def managerFactory():
            return module.BambuFileManager(self.logger, "192.168.1.40", "123456", ftpFactory=ftpServer.CreateClient)

        handler = module.BambuCommandHandler(self.logger, None, fileManagerFactory=managerFactory)
        uploadBody = self._BuildFinalizedUploadBody(uploadPayload)
        uploadResponse = handler.ExecuteFileUpload({"Path": "gcode/cache/new file.gcode.3mf"}, uploadBody)

        self.assertEqual(uploadResponse.StatusCode, 200)
        self.assertEqual(ftpServer.Files["cache/new file.gcode.3mf"], uploadPayload)
        self.assertIn("cache", ftpServer.Dirs)
        self.assertEqual(uploadResponse.ResultDict["VirtualPath"], "gcode/cache/new file.gcode.3mf")
        self.assertEqual(uploadResponse.ResultDict["PlatformPath"], "cache/new file.gcode.3mf")

        downloadResult = handler.ExecuteFileDownload({"Path": "gcode/cache/new file.gcode.3mf"})
        self.assertEqual(downloadResult.StatusCode, 200)
        self.assertEqual(downloadResult.Headers["Content-Length"], str(len(uploadPayload)))
        downloaded = self._ReadCustomStream(downloadResult)
        self.assertEqual(downloaded, uploadPayload)

        deleteResponse = handler.ExecuteFileDelete({"Path": "gcode/cache/new file.gcode.3mf"})
        self.assertEqual(deleteResponse.StatusCode, 200)
        self.assertNotIn("cache/new file.gcode.3mf", ftpServer.Files)
        self.assertEqual(deleteResponse.ResultDict["PlatformPath"], "cache/new file.gcode.3mf")


    def test_bambu_file_upload_ignores_print_options(self) -> None:
        module = self._LoadBambuCommandHandlerModule()
        ftpServer = FakeBambuFtpServer()
        fakeClient = FakeBambuClientForCommands()

        def managerFactory():
            return module.BambuFileManager(self.logger, "192.168.1.40", "123456", ftpFactory=ftpServer.CreateClient)

        handler = module.BambuCommandHandler(self.logger, None, fileManagerFactory=managerFactory)
        uploadBody = self._BuildFinalizedUploadBody(b"3mf bytes")
        with patch.object(module.BambuClient, "Get", return_value=fakeClient):
            response = handler.ExecuteFileUpload({
                "Path": "gcode/folder/my file.gcode.3mf",
                "Print": "true",
                "Plate": "2",
                "UseAms": "true",
                "AmsMapping": "[0, -1]",
                "FlowCali": "true",
            }, uploadBody)

        self.assertEqual(response.StatusCode, 200)
        self.assertEqual(ftpServer.Files["folder/my file.gcode.3mf"], b"3mf bytes")
        self.assertNotIn("PrintStarted", response.ResultDict)
        self.assertEqual(fakeClient.Commands, [])


    def test_file_path_errors_are_short_and_actionable(self) -> None:
        _, missingError = FileSystemCommandHelper.ParsePathArg(None)
        self.assertEqual(missingError, "Missing Path. Provide a file path like 'gcode/<file>'.")

        _, rootError = FileSystemCommandHelper.ParsePathArg({"path": "models/test.gcode"})
        self.assertEqual(rootError, "Unsupported path root 'models'. Use 'gcode/<file>'.")

        errorResult = FileSystemCommandHelper.BuildRawError(400, "line one\n" + ("x" * 500), CommandHandler.c_FilesUploadCommand)
        self.assertIsNotNone(errorResult.FullBodyBuffer)
        errorObj = json.loads(errorResult.FullBodyBuffer.GetBytesLike().decode("utf-8"))
        self.assertLessEqual(len(errorObj["Error"]), FileSystemCommandHelper.c_ErrorMaxChars)
        self.assertNotIn("\n", errorObj["Error"])


    def _FlattenFileTree(self, tree):
        result = []

        def visit(node):
            if node.get("Type") == "file":
                result.append(node)
                return
            for child in node.get("Children", []):
                visit(child)

        for root in tree["Root"]:
            visit(root)
        return result


    def _LoadBambuCommandHandlerModule(self):
        aliasPackageName = "_tests_bambu_octoeverywhere"
        sys.modules.pop(aliasPackageName + ".bambucommandhandler", None)
        bambuClientModule = types.ModuleType(aliasPackageName + ".bambuclient")

        class StubBambuClient:
            @staticmethod
            def Get():
                return None

        bambuClientModule.BambuClient = StubBambuClient
        sys.modules[aliasPackageName + ".bambuclient"] = bambuClientModule
        return LoadPlatformCommandHandlerModule("bambu_octoeverywhere", "bambucommandhandler")


    def _ReadCustomStream(self, result) -> bytes:
        chunks = []
        callback = result.GetCustomBodyStreamCallback
        self.assertIsNotNone(callback)
        try:
            while True:
                chunk = callback()
                if chunk is None:
                    break
                chunks.append(bytes(chunk.GetBytesLike()))
        finally:
            closeCallback = result.GetCustomBodyStreamClosedCallback
            self.assertIsNotNone(closeCallback)
            closeCallback()
        return b"".join(chunks)


if __name__ == "__main__":
    unittest.main()
