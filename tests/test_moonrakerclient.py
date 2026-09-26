import logging
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position,protected-access
from moonraker_octoeverywhere.moonrakerclient import MoonrakerClient, MoonrakerCompat  # noqa: E402
from moonraker_octoeverywhere.jsonrpcresponse import JsonRpcResponse  # noqa: E402
from moonraker_octoeverywhere.moonrakerwebcamhelper import MoonrakerWebcamHelper  # noqa: E402
from octoeverywhere.buffer import Buffer  # noqa: E402
from linux_host.config import Config  # noqa: E402


class FakeConfig:
    def __init__(self, moonrakerApiKey=None) -> None:
        self.MoonrakerApiKey = moonrakerApiKey


    def GetFloatRequired(self, section, key, defaultValue):
        return defaultValue


    def GetStr(self, section, key, defaultValue, keepInConfigIfNone=False):
        if section == Config.MoonrakerSection and key == Config.MoonrakerApiKey:
            return self.MoonrakerApiKey
        return defaultValue


class FakeCredentialManager:
    def __init__(self, apiKey=None, oneshotToken=None) -> None:
        self.ApiKey = apiKey
        self.OneshotToken = oneshotToken
        self.ApiKeyCalls = 0
        self.OneshotTokenCalls = []


    def TryToGetApiKey(self):
        self.ApiKeyCalls += 1
        return self.ApiKey


    def TryToGetOneshotToken(self, apiKey=None):
        self.OneshotTokenCalls.append(apiKey)
        return self.OneshotToken


class FakeMoonrakerCompatEvents:
    def __init__(self) -> None:
        self.Paused = []
        self.Errors = []
        self.Disconnects = []


    def OnPrintPaused(self, printStats=None, supplementalMessage=None) -> None:
        self.Paused.append((printStats, supplementalMessage))


    def OnPrintError(self, printStats=None) -> None:
        self.Errors.append(printStats)


    def OnPrintProgress(self, progress) -> None:
        return None


    def KlippyDisconnectedOrShutdown(self, platformErrorCode=None, error=None) -> None:
        self.Disconnects.append((platformErrorCode, error))


class FakeNotificationHandler:
    def __init__(self) -> None:
        self.Paused = []
        self.Errors = []


    def OnPaused(self, fileName=None, platformErrorCode=None, error=None) -> None:
        self.Paused.append((fileName, platformErrorCode, error))


    def OnError(self, error, platformErrorCode=None) -> None:
        self.Errors.append((error, platformErrorCode))


class TestMoonrakerClient(unittest.TestCase):
    @staticmethod
    def _MakeRpcClient():
        client = MoonrakerClient.__new__(MoonrakerClient)
        client.Logger = logging.getLogger("TestMoonrakerClient")
        client.WebSocketLock = threading.Lock()
        client.WebSocketConnected = True
        client.WebSocketGeneration = 4
        client.JsonRpcIdLock = threading.Lock()
        client.JsonRpcIdCounter = 0
        client.JsonRpcWaitingContexts = {}

        class FakeWebSocket:
            def __init__(self):
                self.Sent = []
                self.LockWasHeld = []

            def Send(self, buffer, isData=True):
                # The connection must remain protected from checking its generation
                # until the request has been handed to this specific websocket.
                acquired = client.WebSocketLock.acquire(blocking=False)
                self.LockWasHeld.append(not acquired)
                if acquired:
                    client.WebSocketLock.release()
                request = json.loads(buffer.GetBytesLike())
                self.Sent.append(request)
                if "id" in request:
                    client.JsonRpcWaitingContexts[request["id"]].SetResultAndEvent({"result": "ok"})

        client.WebSocket = FakeWebSocket()
        return client


    @staticmethod
    def _MakeAuthClient(startupApiKey=None, configApiKey=None):
        # Don't start the worker thread or build the real compat and notification handler.
        with patch("moonraker_octoeverywhere.moonrakerclient.threading.Thread"), patch("moonraker_octoeverywhere.moonrakerclient.MoonrakerCompat"):
            return MoonrakerClient(logging.getLogger("TestMoonrakerClient"), FakeConfig(configApiKey), None, startupApiKey, "printerId", None, "1.0.0")


    def test_startup_api_key_is_used_over_config_api_key(self) -> None:
        client = self._MakeAuthClient(startupApiKey="startupKey", configApiKey="configKey")
        self.assertEqual(client.MoonrakerApiKey, "startupKey")
        self.assertTrue(client.IsMoonrakerApiKeyExplicitlySet)


    def test_config_api_key_is_used_without_startup_api_key(self) -> None:
        client = self._MakeAuthClient(configApiKey="configKey")
        self.assertEqual(client.MoonrakerApiKey, "configKey")
        self.assertTrue(client.IsMoonrakerApiKeyExplicitlySet)


    def test_no_api_key_is_not_explicitly_set(self) -> None:
        client = self._MakeAuthClient()
        self.assertIsNone(client.MoonrakerApiKey)
        self.assertFalse(client.IsMoonrakerApiKeyExplicitlySet)


    def test_auth_uses_unix_socket_api_key_when_not_explicitly_set(self) -> None:
        client = self._MakeAuthClient()
        credentialManager = FakeCredentialManager(apiKey="socketKey")
        with patch("moonraker_octoeverywhere.moonrakerclient.MoonrakerCredentialManager.Get", return_value=credentialManager):
            self.assertTrue(client._TryToGetWebsocketAuth())
        self.assertEqual(client.MoonrakerApiKey, "socketKey")
        self.assertEqual(credentialManager.ApiKeyCalls, 1)
        self.assertEqual(credentialManager.OneshotTokenCalls, [])


    def test_auth_keeps_explicit_api_key_and_skips_unix_socket(self) -> None:
        client = self._MakeAuthClient(startupApiKey="startupKey")
        credentialManager = FakeCredentialManager(apiKey="socketKey", oneshotToken="token")
        with patch("moonraker_octoeverywhere.moonrakerclient.MoonrakerCredentialManager.Get", return_value=credentialManager):
            self.assertTrue(client._TryToGetWebsocketAuth())
        self.assertEqual(client.MoonrakerApiKey, "startupKey")
        self.assertEqual(client.OneshotToken, "token")
        self.assertEqual(credentialManager.ApiKeyCalls, 0)
        self.assertEqual(credentialManager.OneshotTokenCalls, ["startupKey"])


    def test_auth_fails_when_explicit_api_key_is_rejected(self) -> None:
        client = self._MakeAuthClient(configApiKey="badKey")
        credentialManager = FakeCredentialManager(apiKey="socketKey")
        with patch("moonraker_octoeverywhere.moonrakerclient.MoonrakerCredentialManager.Get", return_value=credentialManager):
            self.assertFalse(client._TryToGetWebsocketAuth())
        self.assertEqual(client.MoonrakerApiKey, "badKey")
        self.assertEqual(credentialManager.ApiKeyCalls, 0)


    def test_rpc_rejects_stale_connection_generation_without_sending(self) -> None:
        client = self._MakeRpcClient()
        response = client.SendJsonRpcRequest("printer.gcode.script", {"script": "M83"}, expectedConnectionGeneration=3)
        self.assertEqual(response.GetErrorCode(), JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED)
        self.assertEqual(client.WebSocket.Sent, [])
        self.assertEqual(client.JsonRpcWaitingContexts, {})


    def test_rpc_allows_matching_and_unspecified_generations(self) -> None:
        client = self._MakeRpcClient()
        response = client.SendJsonRpcRequest("printer.gcode.script", {"script": "M83"}, expectedConnectionGeneration=4)
        self.assertFalse(response.HasError())
        self.assertEqual(response.GetSimpleResult(), "ok")
        self.assertFalse(client.SendJsonRpcRequest("printer.info").HasError())
        self.assertFalse(client.SendJsonRpcRequest("printer.info", waitForResponse=False).HasError())
        self.assertEqual(len(client.WebSocket.Sent), 3)
        self.assertEqual(client.WebSocket.LockWasHeld, [True, True, True])
        self.assertEqual(client.JsonRpcWaitingContexts, {})


    def test_generation_is_checked_after_acquiring_send_lock(self) -> None:
        client = self._MakeRpcClient()
        results = []
        started = threading.Event()

        def send_request():
            started.set()
            results.append(client.SendJsonRpcRequest("printer.gcode.script", {"script": "M83"}, expectedConnectionGeneration=4))

        with client.WebSocketLock:
            worker = threading.Thread(target=send_request)
            worker.start()
            self.assertTrue(started.wait(1))
            # Simulate reconnect while the caller is waiting to acquire the send lock.
            client.WebSocketGeneration = 5
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].GetErrorCode(), JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED)
        self.assertEqual(client.WebSocket.Sent, [])
        self.assertEqual(client.JsonRpcWaitingContexts, {})


    def test_connection_generation_changes_on_open_and_is_omitted_while_disconnected(self) -> None:
        client = self._MakeRpcClient()
        self.assertEqual(client.GetConnectionGeneration(), 4)
        client.WebSocketConnected = False
        self.assertIsNone(client.GetConnectionGeneration())
        with patch("moonraker_octoeverywhere.moonrakerclient.threading.Thread"):
            client._OnWsOpened(client.WebSocket)
        self.assertEqual(client.GetConnectionGeneration(), 5)
        response = client.SendJsonRpcRequest("printer.gcode.script", {"script": "M83"}, expectedConnectionGeneration=4)
        self.assertEqual(response.GetErrorCode(), JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED)
        self.assertEqual(client.WebSocket.Sent, [])


    def test_status_updates_route_pause_and_error_states(self) -> None:
        compat = FakeMoonrakerCompatEvents()
        client = MoonrakerClient.__new__(MoonrakerClient)
        client.MoonrakerCompat = compat

        pausedStats = {"state": "paused", "exception": {"message": "Filament stuck"}}
        client._OnWsNonResponseMessage({
            "method": "notify_status_update",
            "params": [
                {"print_stats": pausedStats},
                {"display_status": {"message": "Check filament path"}},
            ],
        })
        errorStats = {"state": "error", "message": "Printer stopped"}
        client._OnWsNonResponseMessage({
            "method": "notify_status_update",
            "params": [{"print_stats": errorStats}],
        })

        self.assertEqual(compat.Paused, [(pausedStats, "Check filament path")])
        self.assertEqual(compat.Errors, [errorStats])


    def test_shutdown_uses_cached_webhooks_state_message(self) -> None:
        compat = FakeMoonrakerCompatEvents()
        client = MoonrakerClient.__new__(MoonrakerClient)
        client.Logger = logging.getLogger("TestMoonrakerClient")
        client.MoonrakerCompat = compat
        client.LastWebhooksState = None
        client.LastWebhooksStateMessage = None
        client.JsonRpcIdLock = threading.Lock()
        client.JsonRpcWaitingContexts = {}
        client.NonResponseMsgQueue = queue.Queue()
        client.WebSocketDebugProfiler = None
        client._RestartWebsocket = lambda: None

        client._onWsData(None, Buffer(b'''{
            "jsonrpc": "2.0",
            "method": "notify_status_update",
            "params": [{
                "webhooks": {
                    "state": "shutdown",
                    "state_message": "MCU 'mcu' shutdown: Timer too close\\nOnce the underlying issue is corrected, restart."
                }
            }]
        }'''), None)
        client._onWsData(None, Buffer(b'''{
            "jsonrpc": "2.0",
            "method": "notify_klippy_shutdown"
        }'''), None)

        self.assertEqual(compat.Disconnects, [
            ("klippy_shutdown", "MCU 'mcu' shutdown: Timer too close"),
        ])


    def test_pause_and_error_use_full_u1_exception_details(self) -> None:
        fullStats = {
            "filename": "file.gcode",
            "state": "paused",
            "total_duration": 10.0,
            "print_duration": 9.0,
            "message": "",
            "exception": {
                "level": 2,
                "id": 525,
                "index": 1,
                "code": 7,
                "message": "Filament feed blocked",
            },
        }
        handler = FakeNotificationHandler()
        compat = MoonrakerCompat.__new__(MoonrakerCompat)
        compat.Logger = logging.getLogger("TestMoonrakerClient")
        compat.IsReadyToProcessNotifications = True
        compat.NotificationHandler = handler
        compat._GetCurrentPrintStats = lambda: fullStats

        compat.OnPrintPaused({"state": "paused"})
        fullStats["state"] = "error"
        fullStats["exception"]["level"] = 3
        compat.OnPrintError({"state": "error"})

        self.assertEqual(handler.Paused, [
            ("file.gcode", "0002-0525-0001-0007", "Filament feed blocked"),
        ])
        self.assertEqual(handler.Errors, [
            ("Filament feed blocked", "0003-0525-0001-0007"),
        ])


class TestMoonrakerStartupRecovery(unittest.TestCase):
    def _ReadConfig(self, contents):
        client = MoonrakerClient.__new__(MoonrakerClient)
        client.Logger = MagicMock()
        with tempfile.TemporaryDirectory() as tempDir:
            configPath = Path(tempDir) / "moonraker.conf"
            configPath.write_text(contents, encoding="utf-8")
            client.MoonrakerConfigFilePath = str(configPath)
            with patch("moonraker_octoeverywhere.moonrakerclient.Compat.IsCompanionMode", return_value=False), \
                 patch("moonraker_octoeverywhere.moonrakerclient.Sentry.OnException") as report:
                result = client.GetMoonrakerHostAndPortFromConfig()
                report.assert_not_called()
        return result, client.Logger


    def test_port_with_inline_comments_and_host_all(self) -> None:
        for comment in ("# printer port", "; printer port"):
            with self.subTest(comment=comment):
                result, _ = self._ReadConfig("[server]\nhost: all\nport: 7126 " + comment + "\n")
                self.assertEqual(result, ("127.0.0.1", 7126))


    def test_invalid_port_uses_default_with_configuration_warning(self) -> None:
        for port in ("%PORT%", "invalid", "0", "65536", "-1", ""):
            with self.subTest(port=port):
                result, logger = self._ReadConfig("[server]\nhost: 127.0.0.2\nport: " + port + "\n")
                self.assertEqual(result, ("127.0.0.2", 7125))
                logger.warning.assert_called_once()


    def test_manual_parse_only_uses_server_section(self) -> None:
        result, _ = self._ReadConfig("[broken\n[other]\nhost: wrong\nport: 99\n[server]\nhost: 127.0.0.2\nport: 7127 # comment\n")
        self.assertEqual(result, ("127.0.0.2", 7127))


    def test_manual_parse_ignores_host_without_a_value_separator(self) -> None:
        result, _ = self._ReadConfig("[broken\n[server]\nhost\nport: 7127\n")
        self.assertEqual(result, ("0.0.0.0", 7127))


    def test_webcam_waits_for_client_initialization_without_resetting_settings(self) -> None:
        helper = MoonrakerWebcamHelper.__new__(MoonrakerWebcamHelper)
        helper.Logger = MagicMock()
        helper._TryToFindWebcamFromApi = MagicMock(return_value=True)
        helper._ResetValuesToDefaults = MagicMock()
        with patch("moonraker_octoeverywhere.moonrakerwebcamhelper.MoonrakerClient.Get", return_value=None), \
             patch("moonraker_octoeverywhere.moonrakerwebcamhelper.Sentry.OnException") as report:
            helper._DoAutoSettingsUpdate()
            helper._TryToFindWebcamFromApi.assert_not_called()
            helper._ResetValuesToDefaults.assert_not_called()
            report.assert_not_called()
        with patch("moonraker_octoeverywhere.moonrakerwebcamhelper.MoonrakerClient.Get", return_value=object()):
            helper._DoAutoSettingsUpdate()
            helper._TryToFindWebcamFromApi.assert_called_once()


if __name__ == "__main__":
    unittest.main()
