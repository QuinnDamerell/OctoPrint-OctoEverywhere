"""Verify local identity requests and unexpected origin encodings over real HTTP."""

# pylint: disable=import-error,no-name-in-module,protected-access
# Generated FlatBuffers annotations do not describe their builder methods fully.
# pyright: reportUnknownMemberType=false

import gzip
import io
import logging
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Tuple
from unittest.mock import Mock, patch

import octoflatbuffers
import requests

from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.httpresult import HttpResult
from octoeverywhere.Proto import HttpHeader, HttpInitialContext
from octoeverywhere.Proto.PathTypes import PathTypes
from octoeverywhere.WebStream.octoheaderimpl import BaseProtocol, HeaderHelper


def GzipCompress(data:bytes) -> bytes:
    # gzip.compress gained mtime in PY3.8; GzipFile keeps fixtures deterministic on PY3.7 too.
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressor:
        compressor.write(data)
    return output.getvalue()


class EncodingFixtureHandler(BaseHTTPRequestHandler):
    payload = b"window.fixture = 'a cacheable frontend asset';\n" * 40
    compressed = GzipCompress(payload)
    requestHeaders:Dict[str, str] = {}

    def log_message(self, format:str, *args:object) -> None: #pylint: disable=redefined-builtin
        del format, args

    def do_HEAD(self) -> None:
        self.SendRepresentation()

    def do_GET(self) -> None:
        self.SendRepresentation()

    def SendRepresentation(self) -> None:
        EncodingFixtureHandler.requestHeaders = {name.lower(): value for name, value in self.headers.items()}
        if self.path == "/retry" and self.headers.get("X-Trigger-431"):
            self.send_response(431)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        acceptEncoding = self.headers.get("Accept-Encoding", "")
        useGzip = self.path.startswith("/forced-gzip/")
        for coding in acceptEncoding.split(","):
            parts = coding.strip().split(";")
            if parts[0] == "gzip":
                quality = next((float(part.strip()[2:]) for part in parts[1:] if part.strip().startswith("q=")), 1.0)
                useGzip = useGzip or quality > 0
        body = self.compressed if useGzip else self.payload
        etag = '"gzip-v1"' if useGzip else '"identity-v1"'
        status = 200
        contentRange = None
        if self.headers.get("If-None-Match") == etag:
            status = 304
        elif self.headers.get("Range") == "bytes=0-9" and self.headers.get("If-Range", etag) == etag:
            contentRange = f"bytes 0-9/{len(body)}"
            body = body[:10]
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", "application/javascript")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "public, max-age=3600")
        if useGzip:
            self.send_header("Content-Encoding", "gzip")
        if contentRange is not None:
            self.send_header("Content-Range", contentRange)
        self.end_headers()
        if self.command != "HEAD" and status != 304:
            self.wfile.write(body)


def MakeEncodingContext(headers:List[Tuple[str, str]], path:str) -> HttpInitialContext.HttpInitialContext:
    builder = octoflatbuffers.Builder(1024)
    offsets:List[int] = []
    for name, value in headers:
        nameOffset = builder.CreateString(name)
        valueOffset = builder.CreateString(value)
        HttpHeader.Start(builder)
        HttpHeader.AddKey(builder, nameOffset)
        HttpHeader.AddValue(builder, valueOffset)
        offsets.append(HttpHeader.End(builder))
    HttpInitialContext.StartHeadersVector(builder, len(offsets))
    for offset in reversed(offsets):
        builder.PrependUOffsetTRelative(offset)
    vector = builder.EndVector()
    host = builder.CreateString("fixture.octoeverywhere.io")
    pathOffset = builder.CreateString(path)
    HttpInitialContext.Start(builder)
    HttpInitialContext.AddOctoHost(builder, host)
    HttpInitialContext.AddPath(builder, pathOffset)
    HttpInitialContext.AddPathType(builder, PathTypes.Relative)
    HttpInitialContext.AddHeaders(builder, vector)
    builder.Finish(HttpInitialContext.End(builder))
    return HttpInitialContext.HttpInitialContext.GetRootAs(builder.Output())


class HttpEncodingNegotiationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), EncodingFixtureHandler)
        cls.serverThread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.serverThread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.serverThread.join(timeout=2)

    def setUp(self) -> None:
        self.logger = logging.getLogger("test-http-encoding-negotiation")
        self.session = requests.Session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)
        patches = [
            patch.object(OctoHttpRequest, "LocalHostAddress", "127.0.0.1"),
            patch.object(OctoHttpRequest, "LocalOctoPrintPort", self.server.server_port),
            patch.object(OctoHttpRequest, "LocalHostUseHttps", False),
            patch("octoeverywhere.octohttprequest.HttpSessions.GetSession", return_value=self.session),
            patch("octoeverywhere.octohttprequest.Compat.GetApiRouterHandler", return_value=None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def CallBrowser(self, headers:List[Tuple[str, str]], path:str="/frontend_latest/fixture.js", method:str="GET") -> HttpResult:
        context = MakeEncodingContext(headers, path)
        gathered = HeaderHelper.GatherRequestHeaders(self.logger, context, BaseProtocol.Http)
        result = OctoHttpRequest.MakeHttpCallOctoStreamHelper(self.logger, context, method, gathered)
        assert result is not None
        self.addCleanup(result.__exit__, None, None, None)
        return result

    def ReadRaw(self, result:HttpResult) -> bytes:
        response = result.ResponseForBodyRead
        assert response is not None
        self.assertFalse(response.raw.decode_content)
        return response.raw.read()

    def test_browser_requests_identity_and_preserves_origin_validator(self) -> None:
        encoding = "br;q=0, gzip;q=0.8, identity;q=0.2"
        result = self.CallBrowser([("aCcEpT-EnCoDiNg", encoding)])
        self.assertEqual(EncodingFixtureHandler.requestHeaders["accept-encoding"], "identity")
        self.assertNotIn("Content-Encoding", result.Headers)
        self.assertEqual(result.Headers["ETag"], '"identity-v1"')
        self.assertEqual(result.Headers["Vary"], "Accept-Encoding")
        self.assertEqual(result.Headers["Cache-Control"], "public, max-age=3600")
        self.assertEqual(self.ReadRaw(result), EncodingFixtureHandler.payload)

    def test_origin_ignoring_identity_preserves_encoded_bytes_and_metadata(self) -> None:
        result = self.CallBrowser([("Accept-Encoding", "br")], path="/forced-gzip/fixture.js")
        self.assertEqual(EncodingFixtureHandler.requestHeaders["accept-encoding"], "identity")
        self.assertEqual(result.StatusCode, 200)
        self.assertEqual(result.Headers["Content-Encoding"], "gzip")
        self.assertEqual(result.Headers["ETag"], '"gzip-v1"')
        self.assertEqual(result.Headers["Vary"], "Accept-Encoding")
        self.assertEqual(result.Headers["Cache-Control"], "public, max-age=3600")
        raw = self.ReadRaw(result)
        self.assertEqual(raw, EncodingFixtureHandler.compressed)
        self.assertEqual(gzip.decompress(raw), EncodingFixtureHandler.payload)

    def test_head_conditional_and_range_preserve_selected_representation(self) -> None:
        baseHeaders = [("Accept-Encoding", "gzip, identity;q=0.5")]
        for path, etag, payload in (("/fixture.js", '"identity-v1"', EncodingFixtureHandler.payload),
                                    ("/forced-gzip/fixture.js", '"gzip-v1"', EncodingFixtureHandler.compressed)):
            with self.subTest(path=path):
                head = self.CallBrowser(baseHeaders, path=path, method="HEAD")
                self.assertEqual(EncodingFixtureHandler.requestHeaders["accept-encoding"], "identity")
                self.assertEqual(head.Headers["ETag"], etag)
                self.assertEqual(head.Headers["Content-Length"], str(len(payload)))
                self.assertEqual(self.ReadRaw(head), b"")
                conditional = self.CallBrowser(baseHeaders + [("If-None-Match", etag)], path=path)
                self.assertEqual(EncodingFixtureHandler.requestHeaders["accept-encoding"], "identity")
                self.assertEqual(conditional.StatusCode, 304)
                self.assertEqual(conditional.Headers["ETag"], etag)
                self.assertEqual(self.ReadRaw(conditional), b"")
                ranged = self.CallBrowser(baseHeaders + [("Range", "bytes=0-9"), ("If-Range", etag)], path=path)
                self.assertEqual(EncodingFixtureHandler.requestHeaders["accept-encoding"], "identity")
                self.assertEqual(ranged.StatusCode, 206)
                self.assertEqual(ranged.Headers["Content-Range"], f"bytes 0-9/{len(payload)}")
                self.assertEqual(ranged.Headers.get("Content-Encoding"), "gzip" if path.startswith("/forced-gzip/") else None)
                self.assertEqual(self.ReadRaw(ranged), payload[:10])

    def test_internal_http_call_defaults_to_identity(self) -> None:
        for headers in (None, {"accept-encoding": "gzip", "ACCEPT-ENCODING": "br"}):
            with self.subTest(headers=headers):
                result = OctoHttpRequest.MakeHttpCall(self.logger, "/api/internal", PathTypes.Relative, "GET", headers)
                assert result is not None
                with result:
                    self.assertEqual(EncodingFixtureHandler.requestHeaders["accept-encoding"], "identity")
                    self.assertNotIn("Content-Encoding", result.Headers)
                    self.assertEqual(self.ReadRaw(result), EncodingFixtureHandler.payload)

    def test_platform_response_handler_paths_request_identity(self) -> None:
        for path in ("/", "/config.json", "/webcam/webrtc"):
            with self.subTest(path=path):
                result = self.CallBrowser([("Accept-Encoding", "gzip")], path=path)
                self.assertEqual(EncodingFixtureHandler.requestHeaders["accept-encoding"], "identity")
                self.assertEqual(self.ReadRaw(result), EncodingFixtureHandler.payload)

    def test_header_rejection_retry_still_requests_identity(self) -> None:
        self.session.headers["Accept-Encoding"] = "gzip"
        result = self.CallBrowser([("Accept-Encoding", "br"), ("X-Trigger-431", "yes")], path="/retry")
        self.assertEqual(result.StatusCode, 200)
        self.assertNotIn("x-trigger-431", EncodingFixtureHandler.requestHeaders)
        self.assertEqual(EncodingFixtureHandler.requestHeaders["accept-encoding"], "identity")
        self.assertEqual(self.ReadRaw(result), EncodingFixtureHandler.payload)

    def test_missing_nominated_empty_and_repeated_encoding(self) -> None:
        cases:List[Tuple[List[Tuple[str, str]], str]] = [
            ([], "identity"),
            ([("Accept-Encoding", "gzip"), ("Connection", "Accept-Encoding")], "identity"),
            ([("Accept-Encoding", "")], "identity"),
            ([("Accept-Encoding", "gzip;q=0"), ("accept-encoding", "identity")], "identity"),
        ]
        for headers, expected in cases:
            with self.subTest(expected=expected):
                result = self.CallBrowser(headers)
                self.assertEqual(EncodingFixtureHandler.requestHeaders["accept-encoding"], expected)
                self.assertNotIn("Content-Encoding", result.Headers)
                self.assertEqual(self.ReadRaw(result), EncodingFixtureHandler.payload)

    def test_platform_api_router_requests_identity(self) -> None:
        router = Mock()
        router.MapRelativePathToAbsolutePathIfNeeded.return_value = f"http://127.0.0.1:{self.server.server_port}/printer/info"
        with patch("octoeverywhere.octohttprequest.Compat.GetApiRouterHandler", return_value=router):
            result = self.CallBrowser([("Accept-Encoding", "gzip")], path="/printer/info")
            router.MapRelativePathToAbsolutePathIfNeeded.assert_called_once_with("/printer/info", "http://")
            self.assertEqual(EncodingFixtureHandler.requestHeaders["accept-encoding"], "identity")
            self.assertEqual(self.ReadRaw(result), EncodingFixtureHandler.payload)


if __name__ == "__main__":
    unittest.main()
