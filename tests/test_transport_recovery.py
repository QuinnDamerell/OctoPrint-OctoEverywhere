import logging
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from octoeverywhere.buffer import Buffer
from octoeverywhere.compression import Compression, CompressionContext
from octoeverywhere.Proto.DataCompression import DataCompression
from octoeverywhere.Proto.PathTypes import PathTypes
from octoeverywhere.WebStream.octowebstreamhttphelper import OctoWebStreamHttpHelper
from octoeverywhere.WebStream.octowebstreamwshelper import OctoWebStreamWsHelper
from octoeverywhere.websocketimpl import Client


class TestTransportRecovery(unittest.TestCase):
    def test_error_callback_thread_failure_still_closes_socket(self):
        callbacks = []
        def onError(ws, error):
            callbacks.append("error")
        def onClose(ws):
            callbacks.append("close")
        client = Client("ws://unused.invalid", onWsError=onError, onWsClose=onClose)
        client.hasDeferredCloseCallbackDueToPendingErrorCallback = True
        threadError = RuntimeError("can't start new thread")
        originalError = ConnectionRefusedError("Printer is offline")
        def report(message, error):
            self.assertIs(error, threadError)
            self.assertTrue(client.isClosed)
            self.assertIsNone(client.Ws)
            callbacks.append("report")
        with patch("octoeverywhere.websocketimpl.threading.Thread.start", side_effect=threadError), \
             patch("octoeverywhere.websocketimpl.Sentry.OnException", side_effect=report) as onException:
            client.handleWsError(originalError)
        self.assertEqual(callbacks, ["error", "close", "report"])
        onException.assert_called_once()
        self.assertFalse(client.hasPendingErrorCallbackFire)
        self.assertTrue(client.isClosed)
        self.assertIsNone(client.Ws)

    def test_close_waits_for_compression_flush(self):
        context = CompressionContext(logging.getLogger("test-compression-close"))
        entered = threading.Event()
        release = threading.Event()
        closeStarted = threading.Event()
        closeDone = threading.Event()
        results = []
        errors = []
        pool = Mock()

        class Writer:
            def write(self, data):
                context.write(b"compressed")
            def flush(self):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("Test did not release flush")
            def __exit__(self, *args):
                pass

        context.Compressor = Mock()
        context.StreamWriter = Writer()
        def compress():
            try:
                results.append(context.Compress(Buffer(b"payload")))
            except Exception as e:
                errors.append(e)
        def close():
            closeStarted.set()
            context.__exit__(None, None, None)
            closeDone.set()
        worker = threading.Thread(target=compress, daemon=True)
        closer = threading.Thread(target=close, daemon=True)
        with patch.object(Compression, "Get", return_value=pool):
            try:
                worker.start()
                self.assertTrue(entered.wait(2))
                closer.start()
                self.assertTrue(closeStarted.wait(2))
                self.assertFalse(closeDone.wait(0.05))
                pool.ReturnZStandardCompressor.assert_not_called()
            finally:
                release.set()
                worker.join(2)
                if closer.ident is not None:
                    closer.join(2)
            self.assertTrue(closeDone.is_set())
            self.assertEqual(errors, [])
            self.assertEqual(results[0].Bytes.Get(), b"compressed")
            pool.ReturnZStandardCompressor.assert_called_once()

    def test_close_waits_for_single_message_decompression(self):
        context = CompressionContext(logging.getLogger("test-decompression-close"))
        entered = threading.Event()
        release = threading.Event()
        closeDone = threading.Event()
        results = []
        pool = Mock()
        def decompress(data):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("Test did not release decompression")
            return b"payload"
        decompressor = Mock()
        decompressor.decompress.side_effect = decompress
        pool.RentZStandardDecompressor.return_value = decompressor
        worker = threading.Thread(target=lambda: results.append(context.Decompress(Buffer(b"compressed"), 7, True)), daemon=True)
        def close():
            context.__exit__(None, None, None)
            closeDone.set()
        closer = threading.Thread(target=close, daemon=True)
        with patch.object(Compression, "Get", return_value=pool):
            try:
                worker.start()
                self.assertTrue(entered.wait(2))
                closer.start()
                self.assertFalse(closeDone.wait(0.05))
                pool.ReturnZStandardDecompressor.assert_not_called()
            finally:
                release.set()
                worker.join(2)
                if closer.ident is not None:
                    closer.join(2)
            self.assertTrue(closeDone.is_set())
            self.assertEqual(results[0].Get(), b"payload")
            pool.ReturnZStandardDecompressor.assert_called_once_with(decompressor)

    def _MakeHttpHelper(self):
        helper = OctoWebStreamHttpHelper(1, logging.getLogger("test-upload-close"), Mock(), SimpleNamespace(FullStreamDataSize=lambda: 4), 0)
        self.addCleanup(helper.UploadBody.Cleanup)
        self.addCleanup(helper.CompressionContext.__exit__, None, None, None)
        return helper

    def _MakeUploadMessage(self):
        return SimpleNamespace(DataLength=lambda: 4, DataCompression=lambda: DataCompression.None_, DataAsByteArray=lambda: bytearray(b"data"), IsDataTransmissionDone=lambda: True)

    def test_close_during_finalize_does_not_execute_request(self):
        helper = self._MakeHttpHelper()
        finalizeMemoryBody = helper.UploadBody._FinalizeMemoryBody
        def closeDuringFinalize():
            helper.Close()
            finalizeMemoryBody()
        helper.executeHttpRequest = Mock()
        with patch.object(helper.UploadBody, "_FinalizeMemoryBody", side_effect=closeDuringFinalize):
            self.assertTrue(helper.IncomingServerMessage(self._MakeUploadMessage()))
        helper.executeHttpRequest.assert_not_called()
        self.assertTrue(helper.CompressionContext.IsClosed)
        self.assertIsNone(helper.UploadBody._bodyBuffer)

    def test_close_before_open_for_request_keeps_upload_alive(self):
        helper = self._MakeHttpHelper()
        def execute():
            helper.Close()
            with helper.UploadBody.OpenForRequest() as data:
                self.assertEqual(data, b"data")
        helper.executeHttpRequest = execute
        self.assertTrue(helper.IncomingServerMessage(self._MakeUploadMessage()))
        self.assertIsNone(helper.UploadBody._bodyBuffer)

    def test_late_upload_message_after_close_is_ignored(self):
        helper = self._MakeHttpHelper()
        helper.Close()
        helper.executeHttpRequest = Mock()
        self.assertTrue(helper.IncomingServerMessage(self._MakeUploadMessage()))
        helper.executeHttpRequest.assert_not_called()

    def test_websocket_close_during_decompression_ends_stream(self):
        helper = OctoWebStreamWsHelper.__new__(OctoWebStreamWsHelper)
        helper.IsClosed = False
        helper.IsWsObjOpened = True
        helper.IsWsObjClosed = False
        helper.CompressionContext = Mock()
        message = SimpleNamespace(DataAsByteArray=lambda: bytearray(b"compressed"), DataCompression=lambda: DataCompression.ZStandard, OriginalDataSize=lambda: 4)
        def decompress(*args):
            helper.IsClosed = True
            raise Exception("The compression context is closed, we can't decompress data")
        with patch.object(Compression, "Get", return_value=SimpleNamespace(Decompress=decompress)):
            self.assertTrue(helper.IncomingServerMessage(message))

    def _MakeWsHelperForProvider(self, path=b"/websocket", pathType=PathTypes.Relative):
        helper = OctoWebStreamWsHelper.__new__(OctoWebStreamWsHelper)
        helper.Id = 1
        helper.Logger = logging.getLogger("test-ws-provider")
        helper.ConnectionAttempt = 0
        helper.IsUsingRelayProvider = False
        helper.ResolvedLocalHostnameUrl = None
        helper.HttpInitialContext = SimpleNamespace(Path=lambda: path, PathType=lambda: pathType)
        helper.Headers = {}
        helper.SubProtocolList = []
        return helper

    def test_declined_relay_provider_uses_normal_socket_and_retries(self):
        helper = self._MakeWsHelperForProvider()
        provider = Mock()
        provider.GetWebsocketObject.return_value = None
        commandHandler = Mock()
        commandHandler.IsCommandRequest.return_value = False
        with patch("octoeverywhere.WebStream.octowebstreamwshelper.Compat.GetRelayWebsocketProvider", return_value=provider), \
             patch("octoeverywhere.WebStream.octowebstreamwshelper.Compat.GetApiRouterHandler", return_value=None), \
             patch("octoeverywhere.WebStream.octowebstreamwshelper.CommandHandler.Get", return_value=commandHandler), \
             patch("octoeverywhere.WebStream.octowebstreamwshelper.Client") as client:
            self.assertIs(helper._GetWebsocketObject(), client.return_value)
            self.assertIs(helper._GetWebsocketObject(), client.return_value)
        self.assertEqual(client.call_count, 2)
        provider.GetWebsocketObject.assert_called_once()

    def test_declined_command_provider_does_not_fall_back_to_http(self):
        helper = self._MakeWsHelperForProvider()
        provider = Mock()
        provider.GetWebsocketObject.return_value = None
        commandHandler = Mock()
        commandHandler.IsCommandRequest.return_value = True
        commandHandler.HandleWebsocketCommand.return_value = None
        with patch("octoeverywhere.WebStream.octowebstreamwshelper.Compat.GetRelayWebsocketProvider", return_value=provider), \
             patch("octoeverywhere.WebStream.octowebstreamwshelper.CommandHandler.Get", return_value=commandHandler), \
             patch("octoeverywhere.WebStream.octowebstreamwshelper.Client") as client:
            self.assertIsNone(helper._GetWebsocketObject())
            client.assert_not_called()

    def test_declined_relay_provider_allows_absolute_socket(self):
        helper = self._MakeWsHelperForProvider(b"wss://printer.example/websocket", PathTypes.Absolute)
        provider = Mock()
        provider.GetWebsocketObject.return_value = None
        commandHandler = Mock()
        commandHandler.IsCommandRequest.return_value = False
        mdns = Mock()
        mdns.TryToResolveIfLocalHostnameFound.return_value = None
        with patch("octoeverywhere.WebStream.octowebstreamwshelper.Compat.GetRelayWebsocketProvider", return_value=provider), \
             patch("octoeverywhere.WebStream.octowebstreamwshelper.CommandHandler.Get", return_value=commandHandler), \
             patch("octoeverywhere.WebStream.octowebstreamwshelper.MDns.Get", return_value=mdns), \
             patch("octoeverywhere.WebStream.octowebstreamwshelper.Client") as client:
            self.assertIs(helper._GetWebsocketObject(), client.return_value)
        self.assertEqual(client.call_args.kwargs["url"], "wss://printer.example/websocket")

    def test_selected_relay_provider_failure_does_not_fall_back(self):
        helper = self._MakeWsHelperForProvider()
        provider = Mock()
        with patch("octoeverywhere.WebStream.octowebstreamwshelper.Compat.GetRelayWebsocketProvider", return_value=provider), \
             patch("octoeverywhere.WebStream.octowebstreamwshelper.Client") as client:
            self.assertIs(helper._GetWebsocketObject(), provider.GetWebsocketObject.return_value)
            self.assertIsNone(helper._GetWebsocketObject())
            client.assert_not_called()

    def test_constructor_rejection_closes_only_request_stream(self):
        webStream = Mock()
        context = SimpleNamespace(PathType=lambda: PathTypes.Relative)
        with patch.object(OctoWebStreamWsHelper, "AttemptConnection", return_value=False), \
             patch("octoeverywhere.WebStream.octowebstreamwshelper.OctoHttpRequest.GetDisableHttpRelay", return_value=False), \
             patch("octoeverywhere.WebStream.octowebstreamwshelper.HeaderHelper.GatherWebsocketRequestHeaders", return_value={}), \
             patch("octoeverywhere.WebStream.octowebstreamwshelper.HeaderHelper.GetWebSocketSubProtocols", return_value=[]):
            helper = OctoWebStreamWsHelper(1, logging.getLogger("test-ws-provider"), webStream, SimpleNamespace(HttpInitialContext=lambda: context), 0)
        helper.CompressionContext.__exit__(None, None, None)
        webStream.SetClosedDueToFailedRequestConnection.assert_called_once()
        webStream.Close.assert_called_once()
