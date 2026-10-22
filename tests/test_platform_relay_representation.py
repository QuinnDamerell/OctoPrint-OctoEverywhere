import importlib
import io
import json
import logging
import os
import sys
import threading
import types
import unittest
import zlib
from unittest.mock import Mock, patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

from moonraker_octoeverywhere.webrequestresponsehandler import MoonrakerWebRequestResponseHandler, ResponseHandlerContext  # noqa: E402
from octoeverywhere.buffer import Buffer  # noqa: E402
from octoeverywhere.compression import CompressionResult  # noqa: E402
from octoeverywhere.httpresult import HttpResult  # noqa: E402
from octoeverywhere.Proto.DataCompression import DataCompression  # noqa: E402
from octoeverywhere.Proto.PathTypes import PathTypes  # noqa: E402
from octoeverywhere.Webcam.webcamhelper import WebcamHelper  # noqa: E402
from octoeverywhere.Webcam.webcamutil import WebcamUtil  # noqa: E402


def _ImportOctoPrintSlipstream():
    # Import the actual cache in an isolated package so these tests don't need OctoPrint's plugin
    # bootstrap or authentication runtime, and don't change other tests' OctoPrint modules.
    packageName = "_relay_test_octoprint"
    package = types.ModuleType(packageName)
    package.__path__ = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "octoprint_octoeverywhere")]
    localAuth = types.ModuleType(packageName + ".localauth")
    localAuth.LocalAuth = Mock()
    with patch.dict(sys.modules, {packageName: package, packageName + ".localauth": localAuth}):
        return importlib.import_module(packageName + ".slipstream")


OctoPrintSlipstreamModule = _ImportOctoPrintSlipstream()
ElegooSlipstreamModule = importlib.import_module("elegoo_octoeverywhere.slipstream")


class TestPlatformRelayRepresentation(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("platform-relay-representation")
        self.handler = MoonrakerWebRequestResponseHandler(self.logger)
        self.context = ResponseHandlerContext(ResponseHandlerContext.MainsailConfig)


    def _Result(self, body, extraHeaders=None, status=200):
        headers = {
            "Content-Length": str(len(body)),
            "ETag": '"origin"',
            "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            "Content-MD5": "original-md5",
            "Digest": "original-digest",
            "Content-Digest": "original-content-digest",
            "Repr-Digest": "original-repr-digest",
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=60",
        }
        headers.update(extraHeaders or {})
        return HttpResult(status, headers, "/config.json", False, fullBodyBuffer=Buffer(body))


    def _AssertRewrittenMetadata(self, result, body):
        for name in ("ETag", "Last-Modified", "Content-MD5", "Digest", "Content-Digest", "Repr-Digest", "Accept-Ranges"):
            self.assertNotIn(name, result.Headers)
        self.assertEqual(result.Headers["Content-Length"], str(len(body)))
        self.assertEqual(result.Headers["Cache-Control"], "public, max-age=60")


    def test_mainsail_rewrite_invalidates_origin_representation_metadata(self):
        body = b'{"instancesDB":"browser","instances":[{"hostname":"printer"}],"port":80}'
        result = self._Result(body, {"Content-Type": "application/json"})
        rewritten = self.handler.HandleResponse(self.context, result, result.FullBodyBuffer)
        config = json.loads(rewritten.GetBytesLike())
        self.assertEqual(config["instancesDB"], "moonraker")
        self.assertEqual(config["instances"], [])
        self.assertEqual(config["port"], 443)
        self._AssertRewrittenMetadata(result, rewritten)


    def test_mainsail_identical_bytes_keep_validators_and_digests(self):
        body = json.dumps({"instancesDB": "moonraker", "instances": [], "port": 443}, indent=4).encode()
        result = self._Result(body)
        before = result.Headers.copy()
        rewritten = self.handler.HandleResponse(self.context, result, result.FullBodyBuffer)
        self.assertEqual(rewritten.GetBytesLike(), body)
        self.assertEqual(result.Headers, before)


    def test_mainsail_guarded_representations_are_not_parsed(self):
        body = b'{"instancesDB":"browser"}'
        cases = [(status, {}) for status in (204, 206, 304, 404)]
        cases += [(200, headers) for headers in (
            {"Content-Encoding": "gzip"}, {"Content-Encoding": "br"},
            {"Content-Encoding": "identity, gzip"}, {"Content-Encoding": "future-encoding"},
            {"Content-Range": "bytes 0-19/40"}, {"Cache-Control": "public, No-Transform"},
            {"Signature": "sig=:abc:"}, {"Signature-Input": 'sig=("@status")'},
        )]
        for status, headers in cases:
            with self.subTest(status=status, headers=headers):
                result = self._Result(body, headers, status)
                before = result.Headers.copy()
                with patch.object(self.handler, "_HandleMainsailConfig") as rewrite:
                    returned = self.handler.HandleResponse(self.context, result, result.FullBodyBuffer)
                self.assertIs(returned, result.FullBodyBuffer)
                self.assertEqual(returned.GetBytesLike(), body)
                self.assertEqual(result.Headers, before)
                rewrite.assert_not_called()


    def _WebcamHelper(self):
        helper = WebcamHelper.__new__(WebcamHelper)
        helper.Logger = self.logger
        return helper


    def test_jpeg_inserted_and_in_place_repairs_invalidate_metadata(self):
        missingApp0 = b"\xff\xd8\xff\xda\x00\x02image"
        wrongIdentifier = b"\xff\xd8\xff\xe0\x00\x10XXXXX\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xda\x00\x02image"
        for body in (missingApp0, wrongIdentifier):
            for bufferBody in (body, bytearray(body)):
                with self.subTest(body=body, bufferType=type(bufferBody)):
                    result = self._Result(bufferBody, {"Content-Type": "image/jpeg"})
                    returned = self._WebcamHelper()._EnsureJpegHeaderInfo(result)
                    self.assertIs(returned, result)
                    self.assertNotEqual(result.FullBodyBuffer.GetBytesLike(), body)
                    self.assertEqual(result.FullBodyBuffer.GetBytesLike()[6:11], b"JFIF\x00")
                    self._AssertRewrittenMetadata(result, result.FullBodyBuffer)


    def test_unchanged_jpeg_keeps_representation_metadata(self):
        body = b"\xff\xd8" + WebcamUtil.c_JfifApp0Header + b"\xff\xda\x00\x02image"
        result = self._Result(body)
        before = result.Headers.copy()
        self._WebcamHelper()._EnsureJpegHeaderInfo(result)
        self.assertEqual(result.FullBodyBuffer.GetBytesLike(), body)
        self.assertEqual(result.Headers, before)


    def test_jpeg_guards_do_not_repair_origin_bytes(self):
        cases = [(status, {}) for status in (204, 206, 304, 404)]
        cases += [(200, headers) for headers in (
            {"Content-Encoding": "gzip"}, {"Content-Encoding": "future-encoding"},
            {"Content-Range": "bytes 0-9/100"}, {"Cache-Control": "no-transform"},
            {"Signature": "sig=:abc:"}, {"Signature-Input": 'sig=("@status")'},
        )]
        for status, headers in cases:
            with self.subTest(status=status, headers=headers):
                result = self._Result(b"snapshot", headers, status)
                before = result.Headers.copy()
                with patch.object(WebcamUtil, "EnsureJpegHeaderInfoWithDetails") as repair:
                    returned = self._WebcamHelper()._EnsureJpegHeaderInfo(result)
                self.assertIs(returned, result)
                self.assertEqual(result.Headers, before)
                repair.assert_not_called()


    def test_guarded_snapshots_still_return_fully_buffered_bytes(self):
        body = b"\xff\xd8\xff\xda\x00\x02image"
        for extraHeaders in ({"Cache-Control": "no-transform"}, {"Signature": "sig=:abc:"}, {"Content-Encoding": "gzip"}):
            with self.subTest(headers=extraHeaders):
                headers = {"Content-Length": str(len(body)), "ETag": '"original"'}
                headers.update(extraHeaders)
                response = Mock(raw=io.BytesIO(body))
                result = HttpResult(200, headers, "/snapshot", False, requestLibResponseObj=response)
                with patch.object(WebcamUtil, "EnsureJpegHeaderInfoWithDetails") as repair:
                    returned = self._WebcamHelper()._EnsureJpegHeaderInfo(result)
                self.assertIs(returned, result)
                self.assertEqual(result.FullBodyBuffer.GetBytesLike(), body)
                self.assertEqual(result.Headers["ETag"], '"original"')
                repair.assert_not_called()


    def _Cache(self, module):
        cache = module.Slipstream.__new__(module.Slipstream)
        cache.Logger = self.logger
        cache.Lock = threading.Lock()
        cache.Cache = {}
        cache.UpdateCache = Mock()
        return cache


    def test_slipstream_leaves_encoded_partial_and_signed_responses_to_live_relay(self):
        for module in (OctoPrintSlipstreamModule, ElegooSlipstreamModule):
            for headers in (
                {"Content-Encoding": "gzip"}, {"Content-Encoding": "br"},
                {"Content-Encoding": "future-encoding"}, {"Content-Range": "bytes 0-9/100"},
                {"Signature": "sig=:abc:"}, {"Signature-Input": 'sig=("cache-control")'},
            ):
                with self.subTest(module=module.__name__, headers=headers):
                    cache = self._Cache(module)
                    result = self._Result(b"encoded body", headers)
                    cache.Cache["/asset.js"] = self._Result(b"stale")
                    with patch.object(module.OctoHttpRequest, "MakeHttpCall", return_value=result), \
                         patch.object(result, "ReadAllContentFromStreamResponse") as read, \
                         patch.object(module.Compression, "Get") as compression:
                        self.assertIsNone(cache._GetCacheReadyOctoHttpResult("/asset.js"))
                    read.assert_not_called()
                    compression.assert_not_called()
                    self.assertNotIn("/asset.js", cache.Cache)


    def test_slipstream_preserves_http_metadata_and_tunnel_compression_flags(self):
        body = b"var content = 'cached';" * 10
        compressed = zlib.compress(body)
        for module in (OctoPrintSlipstreamModule, ElegooSlipstreamModule):
            with self.subTest(module=module.__name__):
                cache = self._Cache(module)
                result = self._Result(body, {"Content-Type": "application/javascript", "Cache-Control": "private, no-transform"})
                compressor = Mock()
                compressor.Compress.return_value = CompressionResult(Buffer(compressed), 0.0, DataCompression.Zlib, len(body))
                with patch.object(module.OctoHttpRequest, "MakeHttpCall", return_value=result), \
                     patch.object(result, "ReadAllContentFromStreamResponse"), \
                     patch.object(module.Compression, "Get", return_value=compressor):
                    cached = cache._GetCacheReadyOctoHttpResult("/asset.js")
                self.assertIsNotNone(cached)
                self.assertEqual(cached.Headers["Cache-Control"], "private, no-transform")
                self.assertEqual(cached.Headers["ETag"], '"origin"')
                self.assertEqual(cached.Headers["Digest"], "original-digest")
                self.assertEqual(cached.Headers["Content-Length"], str(len(body)))
                self.assertNotIn("Content-Encoding", cached.Headers)
                self.assertEqual(cached.BodyBufferCompressionType, DataCompression.Zlib)
                self.assertEqual(cached.BodyBufferPreCompressSize, len(body))
                self.assertEqual(zlib.decompress(cached.FullBodyBuffer.GetBytesLike()), body)
                replay = cached.CreateReplayCopy()
                replay.Headers.pop("ETag")
                self.assertEqual(cached.Headers["ETag"], '"origin"')


    def test_elegoo_keeps_browser_cache_default_when_origin_has_no_policy(self):
        module = ElegooSlipstreamModule
        cache = self._Cache(module)
        body = b"var content = 'cached';" * 10
        result = self._Result(body)
        result.Headers.pop("Cache-Control")
        compressor = Mock()
        compressor.Compress.return_value = CompressionResult(Buffer(zlib.compress(body)), 0.0, DataCompression.Zlib, len(body))
        with patch.object(module.OctoHttpRequest, "MakeHttpCall", return_value=result), \
             patch.object(result, "ReadAllContentFromStreamResponse"), \
             patch.object(module.Compression, "Get", return_value=compressor):
            cached = cache._GetCacheReadyOctoHttpResult("/asset.js")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.Headers["Cache-Control"], "max-age=3600")


    def _Request(self, method="GET", headerNames=()):
        headers = [Mock(Key=Mock(return_value=name.encode())) for name in headerNames]
        return Mock(Method=Mock(return_value=method.encode()), Path=Mock(return_value=b"/asset.js"),
                    PathType=Mock(return_value=PathTypes.Relative), HeadersLength=Mock(return_value=len(headers)),
                    Headers=Mock(side_effect=lambda index: headers[index]))


    def test_slipstream_bypasses_non_get_ranges_and_origin_preconditions(self):
        for module in (OctoPrintSlipstreamModule, ElegooSlipstreamModule):
            cache = self._Cache(module)
            original = self._Result(b"cached")
            cache.Cache["/asset.js"] = original
            for method in ("POST", "HEAD", "PUT", "DELETE"):
                with self.subTest(module=module.__name__, method=method):
                    self.assertIsNone(cache.GetCachedOctoHttpResult(self._Request(method)))
            for name in ("Range", "If-Range", "If-Match", "If-Unmodified-Since"):
                with self.subTest(module=module.__name__, header=name):
                    self.assertIsNone(cache.GetCachedOctoHttpResult(self._Request(headerNames=(name,))))
            for names in ((), ("If-None-Match",), ("If-Modified-Since",)):
                with self.subTest(module=module.__name__, headers=names):
                    replay = cache.GetCachedOctoHttpResult(self._Request(headerNames=names))
                    self.assertIsNotNone(replay)
                    self.assertIsNot(replay, original)
                    self.assertEqual(replay.FullBodyBuffer.GetBytesLike(), b"cached")


if __name__ == "__main__":
    unittest.main()
