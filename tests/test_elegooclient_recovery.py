import json
import logging
import threading
import unittest
from unittest.mock import MagicMock, patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position,wrong-import-order,protected-access
import octowebsocket  # noqa: E402
from elegoo_octoeverywhere.elegooclient import ElegooClient, MsgWaitingContext  # noqa: E402
from octoeverywhere.buffer import Buffer  # noqa: E402
from octoeverywhere.interfaces import WebSocketOpCode  # noqa: E402


class TestElegooClientRecovery(unittest.TestCase):
    @staticmethod
    def _MakeClient():
        client = ElegooClient.__new__(ElegooClient)
        client.Logger = logging.getLogger("TestElegooClientRecovery")
        client.WebsocketMux = MagicMock()
        client._HandleStatusUpdate = MagicMock()
        client.RequestLock = threading.Lock()
        client.RequestPendingContexts = {}
        return client


    def test_discovery_without_ip_waits_and_does_not_report(self) -> None:
        class StopWorker(BaseException):
            pass

        client = self._MakeClient()
        client._GetIpForConnectionAttempt = MagicMock(return_value=None)
        client.ConsecutivelyFailedConnectionAttempts = 1
        client.SleepEvent = MagicMock()
        client.SleepEvent.wait.side_effect = StopWorker()
        with patch("elegoo_octoeverywhere.elegooclient.LocalWebApi.Get") as localApi, \
             patch("elegoo_octoeverywhere.elegooclient.Client") as websocket, \
             patch("elegoo_octoeverywhere.elegooclient.Sentry.OnException") as report:
            with self.assertRaises(StopWorker):
                client._ClientWorker()
            client.SleepEvent.wait.assert_called_once_with(5.0)
            localApi.return_value.SetPrinterConnectionState.assert_called_once_with(False)
            websocket.assert_not_called()
            report.assert_not_called()


    @staticmethod
    def _MakeCorruptFileList(requestId):
        # The control character is JSON-escaped, but the filename also contains raw invalid UTF-8.
        response = {"Topic": "sdcp/response/printer", "Data": {
            "RequestID": requestId, "Cmd": 258, "Data": {"FileList": [
                {"name": "/local/valid_été.gcode"},
                {"name": "/local/\u0010kQ0CORRUPTX@sample.gcode"},
            ]},
        }}
        raw = json.dumps(response, ensure_ascii=False).encode("utf-8").replace(b"CORRUPT", b"\x8b\x9d")
        return Buffer(raw)


    def test_corrupt_filename_completes_local_request_without_disconnecting(self) -> None:
        client = self._MakeClient()
        websocket = MagicMock()
        context = MsgWaitingContext("a" * 32)
        client.RequestPendingContexts[context.Id] = context
        with patch("elegoo_octoeverywhere.elegooclient.Sentry.OnException") as report:
            client._OnWsData(websocket, self._MakeCorruptFileList(context.Id), WebSocketOpCode.TEXT)
            report.assert_not_called()
        websocket.Close.assert_not_called()
        self.assertTrue(context.GetEvent().is_set())
        files = context.GetResult()["Data"]["Data"]["FileList"]
        self.assertEqual(files[0]["name"], "/local/valid_été.gcode")
        self.assertEqual(files[1]["name"], "/local/\u0010kQ0\ufffd\ufffdX@sample.gcode")
        client.WebsocketMux.OnIncomingMessage.assert_not_called()


    def test_corrupt_filename_mux_response_forwards_recovered_utf8(self) -> None:
        client = self._MakeClient()
        websocket = MagicMock()
        context = MsgWaitingContext("a" * 32, wsId=7)
        client.RequestPendingContexts[context.Id] = context
        with patch("elegoo_octoeverywhere.elegooclient.Sentry.OnException") as report:
            client._OnWsData(websocket, self._MakeCorruptFileList(context.Id), WebSocketOpCode.TEXT)
            report.assert_not_called()
        websocket.Close.assert_not_called()
        client.WebsocketMux.OnIncomingMessage.assert_called_once()
        wsId, buffer, opCode = client.WebsocketMux.OnIncomingMessage.call_args[0]
        self.assertEqual(wsId, 7)
        self.assertEqual(opCode, WebSocketOpCode.TEXT)
        response = json.loads(buffer.GetBytesLike().decode("utf-8"))
        self.assertEqual(response["Data"]["Data"]["FileList"][0]["name"], "/local/valid_été.gcode")
        self.assertEqual(response["Data"]["Data"]["FileList"][1]["name"], "/local/\u0010kQ0\ufffd\ufffdX@sample.gcode")
        self.assertNotIn(context.Id, client.RequestPendingContexts)


    def test_corrupt_broadcast_forwards_recovered_utf8(self) -> None:
        client = self._MakeClient()
        websocket = MagicMock()
        buffer = Buffer(b'{"Topic":"sdcp/status/printer","Status":{"name":"printer\xff"}}')
        client._OnWsData(websocket, buffer, WebSocketOpCode.TEXT)
        websocket.Close.assert_not_called()
        client._HandleStatusUpdate.assert_called_once_with({"name": "printer\ufffd"})
        client.WebsocketMux.OnIncomingMessage.assert_called_once()
        wsId, forwardedBuffer, opCode = client.WebsocketMux.OnIncomingMessage.call_args[0]
        self.assertIsNone(wsId)
        self.assertEqual(opCode, WebSocketOpCode.TEXT)
        self.assertEqual(json.loads(forwardedBuffer.GetBytesLike().decode("utf-8"))["Status"], {"name": "printer\ufffd"})


    def test_invalid_json_is_dropped_and_next_status_is_processed(self) -> None:
        for raw in (b"\xff", b'{"Topic":', b'{"Topic":"sdcp/status/printer","Status":{"name":"\x10\xff"}}'):
            with self.subTest(raw=raw):
                client = self._MakeClient()
                websocket = MagicMock()
                with patch("elegoo_octoeverywhere.elegooclient.Sentry.OnException") as report:
                    client._OnWsData(websocket, Buffer(raw), WebSocketOpCode.TEXT)
                    report.assert_not_called()
                websocket.Close.assert_not_called()
                client.WebsocketMux.OnIncomingMessage.assert_not_called()
                client._HandleStatusUpdate.assert_not_called()
                validBuffer = Buffer(b'{"Topic":"sdcp/status/printer","Status":{"name":"valid"}}')
                client._OnWsData(websocket, validBuffer, WebSocketOpCode.TEXT)
                client._HandleStatusUpdate.assert_called_once_with({"name": "valid"})
                client.WebsocketMux.OnIncomingMessage.assert_called_once_with(None, validBuffer, WebSocketOpCode.TEXT)


    def test_connection_closed_after_connect_uses_retry_delay(self) -> None:
        class StopWorker(BaseException):
            pass

        client = self._MakeClient()
        client._GetIpForConnectionAttempt = MagicMock(return_value="127.0.0.1")
        client.ConsecutivelyFailedConnectionAttempts = 3
        client.PortStr = "3030"
        client.SendRequest = MagicMock()
        client.SleepEvent = MagicMock()
        client.SleepEvent.wait.side_effect = StopWorker()
        websocket = MagicMock()
        def connectAndClose(**kwargs):
            client._OnWsConnect(websocket)
            websocket.Close()
        websocket.RunUntilClosed.side_effect = connectAndClose
        with patch("elegoo_octoeverywhere.elegooclient.LocalWebApi.Get"), \
             patch("elegoo_octoeverywhere.elegooclient.LocalIpHelper.SetConnectionTargetIpOverride"), \
             patch("elegoo_octoeverywhere.elegooclient.Client", return_value=websocket), \
             patch("elegoo_octoeverywhere.elegooclient.RepeatTimer"), \
             patch("elegoo_octoeverywhere.elegooclient.Sentry.OnException") as report:
            with self.assertRaises(StopWorker):
                client._ClientWorker()
            self.assertEqual(client.ConsecutivelyFailedConnectionAttempts, 0)
            client.SleepEvent.wait.assert_called_once_with(5.0)
            websocket.Close.assert_called_once()
            report.assert_not_called()


    def test_valid_message_is_forwarded_without_reencoding(self) -> None:
        client = self._MakeClient()
        status = {"name": "Imprimante été"}
        buffer = Buffer(json.dumps({"Topic": "sdcp/status/printer", "Status": status}, ensure_ascii=False).encode("utf-8"))
        client._OnWsData(MagicMock(), buffer, WebSocketOpCode.TEXT)
        client._HandleStatusUpdate.assert_called_once_with(status)
        client.WebsocketMux.OnIncomingMessage.assert_called_once_with(None, buffer, WebSocketOpCode.TEXT)


    def test_unexpected_handler_failure_is_reported_and_raw_message_forwarded(self) -> None:
        client = self._MakeClient()
        error = RuntimeError("unexpected handler bug")
        client._HandleStatusUpdate.side_effect = error
        buffer = Buffer(b'{"Topic":"sdcp/status/printer","Status":{}}')
        with patch("elegoo_octoeverywhere.elegooclient.Sentry.OnException") as report:
            client._OnWsData(MagicMock(), buffer, WebSocketOpCode.TEXT)
            report.assert_called_once_with("Failed to handle incoming Elegoo message.", error)
        client.WebsocketMux.OnIncomingMessage.assert_called_once_with(None, buffer, WebSocketOpCode.TEXT)


    def test_only_local_bad_gateway_handshake_is_filtered(self) -> None:
        client = self._MakeClient()
        with patch("elegoo_octoeverywhere.elegooclient.Sentry.OnException") as report:
            gatewayError = octowebsocket.WebSocketBadStatusException("Handshake status 502 Bad Gateway", 502)
            gatewayError.status_code = 502
            client._OnWsError(MagicMock(), gatewayError)
            report.assert_not_called()
            error = octowebsocket.WebSocketBadStatusException("Handshake status 403 Forbidden", 403)
            client._OnWsError(MagicMock(), error)
            report.assert_called_once_with("Elegoo printer websocket error.", error)


if __name__ == "__main__":
    unittest.main()
