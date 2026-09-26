import json
import logging
import unittest
from unittest.mock import MagicMock, patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position,wrong-import-order,protected-access
import octowebsocket  # noqa: E402
from elegoo_octoeverywhere.elegooclient import ElegooClient  # noqa: E402
from octoeverywhere.buffer import Buffer  # noqa: E402
from octoeverywhere.interfaces import WebSocketOpCode  # noqa: E402


class TestElegooClientRecovery(unittest.TestCase):
    @staticmethod
    def _MakeClient():
        client = ElegooClient.__new__(ElegooClient)
        client.Logger = logging.getLogger("TestElegooClientRecovery")
        client.WebsocketMux = MagicMock()
        client._HandleStatusUpdate = MagicMock()
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


    def test_invalid_utf8_reconnects_without_forwarding_corrupt_data(self) -> None:
        client = self._MakeClient()
        websocket = MagicMock()
        with patch("elegoo_octoeverywhere.elegooclient.Sentry.OnException") as report:
            client._OnWsData(websocket, Buffer(b"\xff"), WebSocketOpCode.TEXT)
            report.assert_not_called()
        websocket.Close.assert_called_once()
        client.WebsocketMux.OnIncomingMessage.assert_not_called()


    def test_malformed_first_message_after_connect_keeps_minimum_retry_delay(self) -> None:
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
        def connectAndReceive(**kwargs):
            client._OnWsConnect(websocket)
            client._OnWsData(websocket, Buffer(b"\xff"), WebSocketOpCode.TEXT)
        websocket.RunUntilClosed.side_effect = connectAndReceive
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
