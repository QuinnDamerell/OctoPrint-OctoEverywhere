"""Exercise real HTTP response framing and header preservation through the relay."""

# pylint: disable=import-error,no-name-in-module,protected-access
# Generated FlatBuffers annotations do not describe their builder methods fully.
# pyright: reportUnknownMemberType=false

import gzip
import io
import logging
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Dict, List, Tuple
from unittest.mock import Mock, patch

import octoflatbuffers
import requests
from urllib3 import HTTPHeaderDict

from octoeverywhere.buffer import Buffer
from octoeverywhere.httpheaderpolicy import HttpHeaderPolicy
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.httpresult import HttpResult
from octoeverywhere.octostreammsgbuilder import OctoStreamMsgBuilder
from octoeverywhere.Proto import HttpHeader, HttpInitialContext, OctoStreamMessage, WebStreamMsg
from octoeverywhere.Proto.DataCompression import DataCompression
from octoeverywhere.WebStream.octoheaderimpl import BaseProtocol, HeaderHelper
from octoeverywhere.WebStream.octowebstreamhttphelper import OctoWebStreamHttpHelper


def GzipCompress(data:bytes) -> bytes:
    # gzip.compress gained mtime in PY3.8; GzipFile keeps fixtures deterministic on PY3.7 too.
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressor:
        compressor.write(data)
    return output.getvalue()


class HeaderFixtureHandler(BaseHTTPRequestHandler):
    payload = b'{"tiles":["/api/map_tiles/vector/{z}/{x}/{y}.mvt"]}'
    compressed = GzipCompress(payload)
    multipart = GzipCompress(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 3\r\n\r\nabc\r\n--frame--\r\n")
    cookies = ["first=1; Expires=Wed, 21 Oct 2030 07:28:00 GMT; Path=/", "second=2; Path=/"]
    requestHeaders:Dict[str, str] = {}
    requestBody = b""

    def log_message(self, format:str, *args:object) -> None: #pylint: disable=redefined-builtin
        del format, args

    def do_GET(self) -> None:
        if self.path.startswith("/conditional/"):
            self.SendConditional(int(self.path.rsplit("/", 1)[1]))
            return
        if self.path == "/encoded-multipart":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(self.multipart)))
            self.end_headers()
            self.wfile.write(self.multipart)
            return
        if self.path == "/not-modified":
            self.SendNoBody(304)
            return
        if self.path.startswith("/invalid-length/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", self.path.rsplit("/", 1)[1])
            self.end_headers()
            self.wfile.write(b"hello")
            return
        if self.path == "/folded":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("X-Folded", "first\r\n  second")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Vary", "Origin")
        self.send_header("Expires", "Wed, 21 Oct 2030 07:28:00 GMT")
        for cookie in self.cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("X-Proxy-Feature", "end-to-end")
        self.send_header("X-Legacy-Text", "caf\u00e9")
        self.send_header("X-Private", "hop-only")
        self.send_header("Connection", "X-Private, Keep-Alive")
        self.send_header("Keep-Alive", "timeout=5")
        self.send_header("Alt-Svc", 'h3=":443"')
        if self.path in ("/chunked", "/chunked-with-length"):
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Trailer", "X-Checksum")
            if self.path == "/chunked-with-length":
                self.send_header("Content-Length", "1")
        elif self.path != "/unknown-length":
            self.send_header("Content-Length", str(len(self.compressed)))
            if self.path == "/duplicate-length":
                self.send_header("Content-Length", str(len(self.compressed)))
        self.end_headers()
        if self.path in ("/chunked", "/chunked-with-length"):
            for chunk in (self.compressed[:11], self.compressed[11:]):
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.write(b"0\r\nX-Checksum: trailer-only\r\n\r\n")
        else:
            self.wfile.write(self.compressed)

    def do_HEAD(self) -> None:
        self.SendNoBody(200)

    def SendNoBody(self, status:int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "8192")
        self.send_header("Content-Encoding", "gzip")
        self.end_headers()

    def do_POST(self) -> None:
        HeaderFixtureHandler.requestHeaders = dict(self.headers.items())
        HeaderFixtureHandler.requestBody = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path.startswith("/conditional/"):
            self.SendConditional(int(self.path.rsplit("/", 1)[1]))
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def SendConditional(self, status:int) -> None:
        body = b"origin response body"
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"same"')
        self.send_header("Last-Modified", "Wed, 21 Oct 2030 07:28:00 GMT")
        self.end_headers()
        self.wfile.write(body)


def MakeContext(headers:List[Tuple[str, str]], method:str="GET") -> HttpInitialContext.HttpInitialContext:
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
    methodOffset = builder.CreateString(method)
    path = builder.CreateString("/")
    HttpInitialContext.Start(builder)
    HttpInitialContext.AddOctoHost(builder, host)
    HttpInitialContext.AddMethod(builder, methodOffset)
    HttpInitialContext.AddPath(builder, path)
    HttpInitialContext.AddHeaders(builder, vector)
    builder.Finish(HttpInitialContext.End(builder))
    return HttpInitialContext.HttpInitialContext.GetRootAs(builder.Output())


class HttpHeaderRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), HeaderFixtureHandler)
        cls.serverThread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.serverThread.start()
        cls.baseUrl = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.serverThread.join(timeout=2)

    def setUp(self) -> None:
        self.logger = logging.getLogger("test-http-header-relay")
        self.helper = OctoWebStreamHttpHelper.__new__(OctoWebStreamHttpHelper)
        self.helper.Id = 1
        self.helper.Logger = self.logger
        self.helper.BodyReadUseReadInto = True
        self.helper.BodyReadContentFallbackOffset = 0

    def WireHeaders(self, result:HttpResult) -> List[Tuple[str, str]]:
        builder = octoflatbuffers.Builder(1024)
        vector = self.helper.buildHeaderVector(builder, result)
        HttpInitialContext.Start(builder)
        if vector is not None:
            HttpInitialContext.AddHeaders(builder, vector)
        builder.Finish(HttpInitialContext.End(builder))
        context = HttpInitialContext.HttpInitialContext.GetRootAs(builder.Output())
        headers:List[Tuple[str, str]] = []
        for i in range(context.HeadersLength()):
            header = context.Headers(i)
            assert header is not None
            name = OctoStreamMsgBuilder.BytesToString(header.Key())
            value = OctoStreamMsgBuilder.BytesToString(header.Value())
            assert name is not None and value is not None
            headers.append((name, value))
        return headers

    def ExecuteResult(self, result:HttpResult, method:str, headers:List[Tuple[str, str]], handler:Mock, useCompressionPolicy:bool=False) -> WebStreamMsg.WebStreamMsg:
        context = MakeContext(headers, method)
        stream = Mock()
        logger = Mock(spec=logging.Logger)
        logger.isEnabledFor.return_value = False
        helper = OctoWebStreamHttpHelper(1, logger, stream, SimpleNamespace(
            FullStreamDataSize=lambda: 0, HttpInitialContext=lambda: context), 0) #pyright: ignore[reportArgumentType]
        self.addCleanup(helper.UploadBody.Cleanup)
        self.addCleanup(helper.CompressionContext.__exit__, None, None, None)
        with patch("octoeverywhere.WebStream.octowebstreamhttphelper.CommandHandler.Get",
                   return_value=SimpleNamespace(IsCommandRequest=lambda _: False)), \
             patch("octoeverywhere.WebStream.octowebstreamhttphelper.WebcamHelper.Get",
                   return_value=SimpleNamespace(IsSnapshotOrWebcamStreamOracleRequest=lambda *_: False)), \
             patch("octoeverywhere.WebStream.octowebstreamhttphelper.Compat.GetSlipstream", return_value=None), \
             patch("octoeverywhere.WebStream.octowebstreamhttphelper.Compat.GetLocalAuth", return_value=None), \
             patch("octoeverywhere.WebStream.octowebstreamhttphelper.Compat.GetRelayWebcamStreamDetector", return_value=None), \
             patch("octoeverywhere.WebStream.octowebstreamhttphelper.OctoHttpRequest.MakeHttpCallOctoStreamHelper", return_value=result), \
             patch("octoeverywhere.WebStream.octowebstreamhttphelper.Compat.GetWebRequestResponseHandler", return_value=handler), \
             patch.object(helper, "shouldCompressBody", side_effect=helper.shouldCompressBody if useCompressionPolicy else None, return_value=False):
            helper.executeHttpRequest()
        logger.warning.assert_not_called()
        logger.error.assert_not_called()
        stream.SendToOctoStream.assert_called_once()
        buffer, start, _, isLast, _ = stream.SendToOctoStream.call_args[0]
        self.assertTrue(isLast)
        envelope = OctoStreamMessage.OctoStreamMessage.GetRootAs(buffer.GetBytesLike(), start + 4)
        table = envelope.Context()
        assert table is not None
        message = WebStreamMsg.WebStreamMsg()
        message.Init(table.Bytes, table.Pos)
        return message

    def test_real_gzip_response_stays_encoded_through_stream_and_full_body_reads(self) -> None:
        for path in ("/length", "/unknown-length", "/chunked", "/duplicate-length", "/chunked-with-length"):
            for buffered in (False, True):
                with self.subTest(path=path, buffered=buffered):
                    with requests.get(self.baseUrl + path, headers={"Accept-Encoding": "identity"}, stream=True, timeout=2) as response:
                        result = HttpResult.BuildFromRequestLibResponse(response, self.baseUrl + path)
                        if buffered:
                            result.ReadAllContentFromStreamResponse(self.logger)
                            assert result.FullBodyBuffer is not None
                            body = bytes(result.FullBodyBuffer.GetBytesLike())
                        else:
                            chunks:List[bytes] = []
                            self.helper.BodyReadContentFallbackOffset = 0
                            while True:
                                target = bytearray(11)
                                count = self.helper.doBodyReadInto(result, target, 0, len(target))
                                if count == 0:
                                    break
                                chunks.append(bytes(target[:count]))
                            body = b"".join(chunks)
                        self.assertEqual(body, HeaderFixtureHandler.compressed)
                        self.assertEqual(gzip.decompress(body), HeaderFixtureHandler.payload)
                        headers = self.WireHeaders(result)
                        self.assertIn(("Content-Encoding", "gzip"), headers)
                        self.assertEqual([value for name, value in headers if name.lower() == "set-cookie"], HeaderFixtureHandler.cookies)
                        self.assertEqual([value for name, value in headers if name.lower() == "vary"], ["Accept-Encoding", "Origin"])
                        self.assertIn(("X-Proxy-Feature", "end-to-end"), headers)
                        self.assertEqual(response.raw.headers["X-Legacy-Text"], "caf\u00e9")
                        self.assertIn(("X-Legacy-Text", "caf\u00e9"), headers)
                        self.assertIn(("Expires", "Wed, 21 Oct 2030 07:28:00 GMT"), headers)
                        excluded = HttpHeaderPolicy.c_HopByHopHeaders | {"x-private", "x-checksum"}
                        self.assertFalse(any(name.lower() in excluded for name, _ in headers))
                        if path in ("/chunked", "/chunked-with-length", "/unknown-length"):
                            self.assertNotIn("Content-Length", result.Headers)
                        else:
                            self.assertEqual(result.Headers.getlist("Content-Length"), [str(len(HeaderFixtureHandler.compressed))])

    def test_replay_header_copy_keeps_duplicates_and_is_independently_mutable(self) -> None:
        headers = HTTPHeaderDict()
        for cookie in HeaderFixtureHandler.cookies:
            headers.add("Set-Cookie", cookie)
        result = HttpResult(200, headers, "/", False, fullBodyBuffer=Buffer(b"body"))
        replay = result.CreateReplayCopy()
        self.assertEqual(replay.Headers.getlist("set-cookie"), HeaderFixtureHandler.cookies)
        replay.Headers["Set-Cookie"] = "replacement=1"
        self.assertEqual(replay.Headers.getlist("set-cookie"), ["replacement=1"])
        self.assertEqual(result.Headers.getlist("set-cookie"), HeaderFixtureHandler.cookies)
        del replay.Headers["SET-cookie"]
        self.assertNotIn("set-cookie", replay.Headers)

    def test_encoded_http_body_skips_tunnel_compression_and_preserves_protocol_flags(self) -> None:
        encodedBody = GzipCompress(bytes(range(256)) * 20)
        self.assertGreater(len(encodedBody), 200)
        for encoding in ("gzip", "br", "deflate", "gzip, br"):
            result = HttpResult(200, {"Content-Encoding": encoding}, "/", False)
            self.assertFalse(self.helper.shouldCompressBody("application/octet-stream", result, len(encodedBody)))
        for encoding in ("", "identity"):
            result = HttpResult(200, {"Content-Encoding": encoding}, "/", False)
            self.assertTrue(self.helper.shouldCompressBody("application/octet-stream", result, len(encodedBody)))

        for precompressed in (False, True):
            with self.subTest(precompressed=precompressed):
                result = HttpResult(200, {"Content-Type": "application/octet-stream", "Content-Encoding": "gzip",
                                         "Content-Length": str(len(encodedBody)), "ETag": '"gzip-origin"'}, "/", False)
                data = zlib.compress(encodedBody) if precompressed else encodedBody
                result.SetFullBodyBuffer(Buffer(data), DataCompression.Zlib if precompressed else DataCompression.None_,
                                         len(encodedBody) if precompressed else 0)
                handler = Mock()
                handler.CheckIfResponseNeedsToBeHandled.return_value = None
                with patch("octoeverywhere.WebStream.octowebstreamhttphelper.Compression.Get",
                           side_effect=AssertionError("Encoded bytes must not be compressed again")):
                    message = self.ExecuteResult(result, "GET", [], handler, useCompressionPolicy=True)
                self.assertEqual(bytes(message.DataAsByteArray()), data)
                self.assertEqual(message.DataCompression(), DataCompression.Zlib if precompressed else DataCompression.None_)
                self.assertEqual(message.OriginalDataSize(), len(encodedBody) if precompressed else 0)
                self.assertEqual(message.FullStreamDataSize(), len(encodedBody))
                self.assertEqual(result.Headers["Content-Encoding"], "gzip")
                self.assertEqual(result.Headers["Content-Length"], str(len(encodedBody)))
                self.assertEqual(result.Headers["ETag"], '"gzip-origin"')

    def test_request_hops_are_removed_and_requests_recomputes_upload_framing(self) -> None:
        context = MakeContext([
            ("X-Private", "hop-only"), ("Connection", "X-Private, Keep-Alive"),
            ("Keep-Alive", "timeout=5"), ("TE", "trailers"), ("Trailer", "X-Checksum"),
            ("Transfer-Encoding", "chunked"), ("Upgrade", "other-protocol"),
            ("Proxy-Authorization", "fixture"), ("Content-Length", "999"),
            ("Content-Type", "application/octet-stream"), ("Content-Encoding", "gzip"),
            ("X-Proxy-Feature", "keep"), ("Accept-Encoding", "br"), ("X-Legacy-Text", "caf\u00e9"),
        ])
        headers = HeaderHelper.GatherRequestHeaders(self.logger, context, BaseProtocol.Http)
        self.assertNotIn("Content-Length", headers)
        self.assertFalse(any(name.lower() in HttpHeaderPolicy.c_HopByHopHeaders | {"x-private"} for name in headers))
        self.assertEqual(headers["Accept-Encoding"], "identity")
        with requests.post(self.baseUrl, headers=headers, data=HeaderFixtureHandler.compressed, timeout=2) as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(HeaderFixtureHandler.requestBody, HeaderFixtureHandler.compressed)
        self.assertEqual(HeaderFixtureHandler.requestHeaders["Content-Length"], str(len(HeaderFixtureHandler.compressed)))
        self.assertEqual(HeaderFixtureHandler.requestHeaders["Content-Encoding"], "gzip")
        self.assertEqual(HeaderFixtureHandler.requestHeaders["X-Proxy-Feature"], "keep")
        self.assertEqual(HeaderFixtureHandler.requestHeaders["X-Legacy-Text"], "caf\u00e9")

    def test_invalid_fields_are_rejected_without_losing_other_headers(self) -> None:
        context = MakeContext([("X-Good", "value"), ("Bad Name", "value"), ("X-Bad", "one\r\ntwo"), ("X-Unencodable", "\u0100")])
        headers = HeaderHelper.GatherRequestHeaders(self.logger, context, BaseProtocol.Http)
        self.assertEqual(headers["X-Good"], "value")
        self.assertNotIn("Bad Name", headers)
        self.assertNotIn("X-Bad", headers)
        self.assertNotIn("X-Unencodable", headers)
        result = HttpResult(200, {"X-Good": "value", "Bad Name": "value", "X-Bad": "one\r\ntwo", "X-Unencodable": "\u0100"}, "/", False)
        self.assertEqual(self.WireHeaders(result), [("X-Good", "value")])

    def test_representation_length_is_preserved_as_metadata_for_head_and_304(self) -> None:
        for method, path in (("HEAD", "/"), ("GET", "/not-modified")):
            for buffered in (False, True):
                with self.subTest(method=method, buffered=buffered):
                    with requests.request(method, self.baseUrl + path, stream=True, timeout=2) as response:
                        result = HttpResult.BuildFromRequestLibResponse(response, self.baseUrl + path)
                        if buffered:
                            result.ReadAllContentFromStreamResponse(self.logger)
                            assert result.FullBodyBuffer is not None
                            self.assertEqual(len(result.FullBodyBuffer), 0)
                        else:
                            self.assertEqual(self.helper.doBodyReadInto(result, bytearray(11), 0, 11), 0)
                        self.assertIn(("Content-Length", "8192"), self.WireHeaders(result))
                        self.assertIn(("Content-Encoding", "gzip"), self.WireHeaders(result))

    def test_bodyless_responses_send_zero_relay_payload_and_keep_representation_length(self) -> None:
        for method, path in (("HEAD", "/"), ("GET", "/not-modified")):
            for buffered in (False, True):
                with self.subTest(method=method, buffered=buffered):
                    with requests.request(method, self.baseUrl + path, stream=True, timeout=2) as response:
                        result = HttpResult.BuildFromRequestLibResponse(response, self.baseUrl + path)
                        if buffered:
                            with patch.object(self.logger, "warning") as warning:
                                result.ReadAllContentFromStreamResponse(self.logger, maxBodySizeBytes=1)
                            warning.assert_not_called()
                        handler = Mock()
                        message = self.ExecuteResult(result, method, [], handler)
                        self.assertEqual(message.FullStreamDataSize(), 0)
                        self.assertEqual(message.DataLength(), 0)
                        context = message.HttpInitialContext()
                        assert context is not None
                        fields = []
                        for i in range(context.HeadersLength()):
                            field = context.Headers(i)
                            assert field is not None
                            fields.append((OctoStreamMsgBuilder.BytesToString(field.Key()), OctoStreamMsgBuilder.BytesToString(field.Value())))
                        self.assertIn(("Content-Length", "8192"), fields)
                        handler.CheckIfResponseNeedsToBeHandled.assert_not_called()

        for status in (101, 103, 204, 205):
            with self.subTest(status=status):
                result = HttpResult(status, {"Content-Length": "8192"}, "/", False, fullBodyBuffer=Buffer(b"must not send"))
                handler = Mock()
                message = self.ExecuteResult(result, "GET", [], handler)
                self.assertEqual(message.FullStreamDataSize(), 0)
                self.assertEqual(message.DataLength(), 0)
                self.assertNotIn("Content-Length", result.Headers)
                handler.CheckIfResponseNeedsToBeHandled.assert_not_called()

    def test_request_no_transform_skips_response_rewriter_during_relay_execution(self) -> None:
        body = b"<html><head></head><body>unchanged</body></html>"
        result = HttpResult(200, {"Content-Type": "text/html", "ETag": '"original"'}, "/", False, fullBodyBuffer=Buffer(body))
        handler = Mock()
        message = self.ExecuteResult(result, "GET", [("Cache-Control", "private, No-Transform")], handler)
        handler.CheckIfResponseNeedsToBeHandled.assert_not_called()
        self.assertEqual(bytes(message.DataAsByteArray()), body)
        self.assertEqual(message.FullStreamDataSize(), len(body))
        self.assertEqual(result.Headers["ETag"], '"original"')

    def test_live_origin_ignoring_validators_gets_nginx_not_modified(self) -> None:
        # The fixture always answers with a full body. Like nginx with proxy_cache, a matching
        # GET becomes a 304; other statuses and methods keep the origin's response.
        headers = [("If-None-Match", '"same"'), ("If-Modified-Since", "Wed, 21 Oct 2030 07:28:00 GMT")]
        for method, status in (("GET", 200), ("GET", 412), ("GET", 404), ("POST", 200)):
            with self.subTest(method=method, status=status):
                url = f"{self.baseUrl}/conditional/{status}"
                with requests.request(method, url, headers=dict(headers), stream=True, timeout=2) as response:
                    result = HttpResult.BuildFromRequestLibResponse(response, url)
                    handler = Mock()
                    handler.CheckIfResponseNeedsToBeHandled.return_value = None
                    message = self.ExecuteResult(result, method, headers, handler)
                    if method == "GET" and status == 200:
                        self.assertEqual(message.StatusCode(), 304)
                        self.assertEqual(message.DataLength(), 0)
                        self.assertEqual(message.FullStreamDataSize(), 0)
                        context = message.HttpInitialContext()
                        assert context is not None
                        names = [OctoStreamMsgBuilder.BytesToString(context.Headers(i).Key()) for i in range(context.HeadersLength())] #pyright: ignore[reportOptionalMemberAccess]
                        self.assertIn("ETag", names)
                        self.assertIn("Last-Modified", names)
                        self.assertNotIn("Content-Type", names)
                        self.assertNotIn("Content-Length", names)
                        # Only the rewrite exclusion consulted the handler; the body was never read.
                        handler.CheckIfResponseNeedsToBeHandled.assert_called_once()
                        handler.HandleResponse.assert_not_called()
                    else:
                        self.assertEqual(message.StatusCode(), status)
                        self.assertEqual(bytes(message.DataAsByteArray()), b"origin response body")

    def test_rewritable_response_is_never_answered_with_a_synthetic_304(self) -> None:
        # A page cached while it wasn't rewritten (for example, while marked no-transform) still
        # carries the origin's ETag. Once the page is rewritable, a 304 would keep that copy.
        headers = [("If-None-Match", '"same"'), ("If-Modified-Since", "Wed, 21 Oct 2030 07:28:00 GMT")]
        url = f"{self.baseUrl}/conditional/200"
        with requests.get(url, headers=dict(headers), stream=True, timeout=2) as response:
            result = HttpResult.BuildFromRequestLibResponse(response, url)
            handler = Mock()
            handler.CheckIfResponseNeedsToBeHandled.return_value = object()
            handler.HandleResponse.side_effect = lambda context, httpResult, body: body
            message = self.ExecuteResult(result, "GET", headers, handler)
            self.assertEqual(message.StatusCode(), 200)
            self.assertEqual(bytes(message.DataAsByteArray()), b"origin response body")
            handler.HandleResponse.assert_called_once()

    def test_not_modified_rules_follow_nginx(self) -> None:
        lastModified = "Wed, 21 Oct 2030 07:28:00 GMT"

        def Origin(*extra:Tuple[str, str]) -> HTTPHeaderDict:
            headers = HTTPHeaderDict()
            headers.add("ETag", '"v1"')
            headers.add("Last-Modified", lastModified)
            for name, value in extra:
                headers.add(name, value)
            return headers

        cases:List[Tuple[str, str, Dict[str, str], int, HTTPHeaderDict, bool]] = [
            ("strong tag", "GET", {"If-None-Match": '"v1"'}, 200, Origin(), True),
            ("HEAD", "HEAD", {"If-None-Match": '"v1"'}, 200, Origin(), True),
            ("tag weakened by the service", "GET", {"If-None-Match": 'W/"v1"'}, 200, Origin(), True),
            ("tag list", "GET", {"If-None-Match": '"other", W/"v1"'}, 200, Origin(), True),
            ("wildcard", "GET", {"If-None-Match": "*"}, 200, Origin(), True),
            ("lowercase field name", "GET", {"if-none-match": '"v1"'}, 200, Origin(), True),
            ("exact date", "GET", {"If-Modified-Since": lastModified}, 200, Origin(), True),
            ("same instant in asctime form", "GET", {"If-Modified-Since": "Mon Oct 21 07:28:00 2030"}, 200, Origin(), True),
            ("both validators match", "GET", {"If-None-Match": '"v1"', "If-Modified-Since": lastModified}, 200, Origin(), True),
            ("prefix of another tag", "GET", {"If-None-Match": '"v10"'}, 200, Origin(), False),
            ("different tag", "GET", {"If-None-Match": '"v2"'}, 200, Origin(), False),
            ("later date is not exact", "GET", {"If-Modified-Since": "Thu, 22 Oct 2030 07:28:00 GMT"}, 200, Origin(), False),
            ("unparseable date", "GET", {"If-Modified-Since": "yesterday"}, 200, Origin(), False),
            ("tag matches but date does not", "GET", {"If-None-Match": '"v1"', "If-Modified-Since": "Thu, 22 Oct 2030 07:28:00 GMT"}, 200, Origin(), False),
            ("no validators", "GET", {}, 200, Origin(), False),
            ("POST", "POST", {"If-None-Match": '"v1"'}, 200, Origin(), False),
            ("non-200 status", "GET", {"If-None-Match": '"v1"'}, 201, Origin(), False),
            ("If-Match defers to origin", "GET", {"If-None-Match": '"v1"', "If-Match": '"v1"'}, 200, Origin(), False),
            ("If-Unmodified-Since defers to origin", "GET", {"If-None-Match": '"v1"', "If-Unmodified-Since": lastModified}, 200, Origin(), False),
            ("repeated ETag is ambiguous", "GET", {"If-None-Match": '"v1"'}, 200, Origin(("ETag", '"v2"')), False),
            ("date without Last-Modified", "GET", {"If-Modified-Since": lastModified}, 200, HTTPHeaderDict({"ETag": '"v1"'}), False),
            # Signed responses: the 304 changes the status and fields a signature can cover.
            ("Signature", "GET", {"If-None-Match": '"v1"'}, 200, Origin(("Signature", "sig1=:fixture:")), False),
            ("Signature-Input", "GET", {"If-None-Match": '"v1"'}, 200, Origin(("Signature-Input", 'sig1=("@status")')), False),
            # Deliberately broader than nginx, which only converts responses it would cache. A matching
            # validator is correct whatever the cache directives say, and the relay stores nothing.
            ("Set-Cookie", "GET", {"If-None-Match": '"v1"'}, 200, Origin(("Set-Cookie", "a=1")), True),
            ("Vary star", "GET", {"If-None-Match": '"v1"'}, 200, Origin(("Vary", "Origin, *")), True),
            ("no-store with a wildcard", "GET", {"If-None-Match": "*"}, 200, Origin(("Cache-Control", "no-store")), True),
            ("no-cache", "GET", {"If-None-Match": '"v1"'}, 200, Origin(("Cache-Control", "no-cache")), True),
            ("private", "GET", {"If-None-Match": '"v1"'}, 200, Origin(("Cache-Control", "private, max-age=60")), True),
            ("max-age=0", "GET", {"If-None-Match": '"v1"'}, 200, Origin(("Cache-Control", "public, max-age=0")), True),
            ("past Expires", "GET", {"If-None-Match": '"v1"'}, 200, Origin(("Expires", "Thu, 01 Jan 1970 00:00:00 GMT")), True),
        ]
        for name, method, requestHeaders, status, responseHeaders, expected in cases:
            with self.subTest(name):
                self.assertEqual(HttpHeaderPolicy.IsNotModified(method, requestHeaders, status, responseHeaders), expected)

        weakOrigin = HTTPHeaderDict({"ETag": 'W/"v1"'})
        self.assertTrue(HttpHeaderPolicy.IsNotModified("GET", {"If-None-Match": '"v1"'}, 200, weakOrigin))

        result = HttpResult(200, {"Content-Type": "text/css", "Content-Length": "4", "Accept-Ranges": "bytes",
                                  "Content-Encoding": "gzip", "ETag": '"v1"', "Cache-Control": "max-age=60",
                                  "Vary": "Accept-Encoding", "Set-Cookie": "a=1"}, "/", False, fullBodyBuffer=Buffer(b"body"))
        result.ConvertToNotModified()
        self.assertEqual(result.StatusCode, 304)
        self.assertIsNone(result.FullBodyBuffer)
        self.assertEqual(sorted(name for name, _ in result.Headers.items()), ["Cache-Control", "ETag", "Set-Cookie", "Vary"])

    def test_unframeable_origin_response_is_a_502_for_that_request_only(self) -> None:
        # nginx answers an invalid upstream Content-Length with 502. Raising instead would reach the
        # web stream thread and disconnect the whole tunnel.
        session = requests.Session()
        session.trust_env = False
        self.addCleanup(session.close)
        with patch("octoeverywhere.octohttprequest.HttpSessions.GetSession", return_value=session):
            for value in ("abc", "-1", "+5", "5.0"):
                with self.subTest(value=value):
                    attempt = OctoHttpRequest.MakeHttpCallAttempt(self.logger, "Main request", "GET", f"{self.baseUrl}/invalid-length/{value}",
                                                              {"Accept-Encoding": "identity"}, None, None, False, None)
                    self.assertTrue(attempt.IsChainDone)
                    assert attempt.Result is not None
                    self.assertEqual(attempt.Result.StatusCode, 502)
                    self.assertEqual(attempt.Result.Headers["Content-Length"], "0")

    def test_folded_response_field_is_unfolded_instead_of_dropped(self) -> None:
        url = self.baseUrl + "/folded"
        with requests.get(url, stream=True, timeout=2) as response:
            result = HttpResult.BuildFromRequestLibResponse(response, url)
            self.assertIn(("X-Folded", "first second"), self.WireHeaders(result))

    def test_tunnel_compression_covers_cloudflare_types(self) -> None:
        result = HttpResult(200, {}, "/", False)
        for contentType in ("application/x-protobuf", "font/ttf", "image/x-icon", "application/wasm",
                            "application/manifest+json", "image/svg+xml; charset=utf-8", "application/vnd.ms-fontobject"):
            with self.subTest(contentType=contentType):
                self.assertTrue(self.helper.shouldCompressBody(contentType, result, 4096))
        for contentType in ("image/png", "font/woff2", "video/mp4", "application/zip"):
            with self.subTest(contentType=contentType):
                self.assertFalse(self.helper.shouldCompressBody(contentType, result, 4096))

    def test_encoded_multipart_bypasses_decoded_boundary_parser(self) -> None:
        url = self.baseUrl + "/encoded-multipart"
        with requests.get(url, stream=True, timeout=2) as response:
            result = HttpResult.BuildFromRequestLibResponse(response, url)
            handler = Mock()
            handler.CheckIfResponseNeedsToBeHandled.return_value = None
            with patch.object(OctoWebStreamHttpHelper, "readStreamChunk", side_effect=AssertionError("Encoded body must stay raw")) as parse:
                message = self.ExecuteResult(result, "GET", [], handler)
            parse.assert_not_called()
            self.assertEqual(bytes(message.DataAsByteArray()), HeaderFixtureHandler.multipart)
            self.assertEqual(message.FullStreamDataSize(), len(HeaderFixtureHandler.multipart))
            self.assertEqual(result.Headers["Content-Encoding"], "gzip")

    def test_response_no_transform_skips_rewriter_setup_before_streaming(self) -> None:
        chunks = [Buffer(b"data: unmodified\n\n"), None]
        closed = Mock()
        result = HttpResult(200, {"Content-Type": "text/event-stream", "Cache-Control": "no-transform"},
                            "/map", False, customBodyStreamCallback=lambda: chunks.pop(0), customBodyStreamClosedCallback=closed)
        handler = Mock()
        context = MakeContext([])
        stream = Mock()
        helper = OctoWebStreamHttpHelper(1, self.logger, stream, SimpleNamespace(
            FullStreamDataSize=lambda: 0, HttpInitialContext=lambda: context), 0) #pyright: ignore[reportArgumentType]
        self.addCleanup(helper.UploadBody.Cleanup)
        self.addCleanup(helper.CompressionContext.__exit__, None, None, None)
        with patch("octoeverywhere.WebStream.octowebstreamhttphelper.CommandHandler.Get",
                   return_value=SimpleNamespace(IsCommandRequest=lambda _: False)), \
             patch("octoeverywhere.WebStream.octowebstreamhttphelper.WebcamHelper.Get",
                   return_value=SimpleNamespace(IsSnapshotOrWebcamStreamOracleRequest=lambda *_: False)), \
             patch("octoeverywhere.WebStream.octowebstreamhttphelper.Compat.GetSlipstream", return_value=None), \
             patch("octoeverywhere.WebStream.octowebstreamhttphelper.Compat.GetLocalAuth", return_value=None), \
             patch("octoeverywhere.WebStream.octowebstreamhttphelper.Compat.GetRelayWebcamStreamDetector", return_value=None), \
             patch("octoeverywhere.WebStream.octowebstreamhttphelper.OctoHttpRequest.MakeHttpCallOctoStreamHelper", return_value=result), \
             patch("octoeverywhere.WebStream.octowebstreamhttphelper.Compat.GetWebRequestResponseHandler", return_value=handler), \
             patch.object(helper, "shouldCompressBody", return_value=False):
            helper.executeHttpRequest()
        handler.CheckIfResponseNeedsToBeHandled.assert_not_called()
        self.assertEqual(stream.SendToOctoStream.call_count, 2)
        closed.assert_called_once()

    def test_websocket_header_and_subprotocol_handling_is_unchanged(self) -> None:
        context = MakeContext([("Connection", "Upgrade"), ("Upgrade", "websocket"),
                               ("Cookie", "session=fixture"), ("User-Agent", "fixture"),
                               ("Sec-WebSocket-Protocol", "first,second")])
        headers = HeaderHelper.GatherWebsocketRequestHeaders(self.logger, context)
        self.assertEqual(headers, {"Cookie": "session=fixture"})
        self.assertEqual(HeaderHelper.GetWebSocketSubProtocols(self.logger, context), ["first", "second"])


if __name__ == "__main__":
    unittest.main()
