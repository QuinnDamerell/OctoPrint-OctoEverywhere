import json
import logging
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as mqtt

from linux_host.config import Config
from linux_host.localwebapi import LocalWebApi

from octoeverywhere.localip import LocalIpHelper
from octoeverywhere.mqttmux.localclient import LocalPluginClient
from octoeverywhere.mqttmux.mux import (
    MqttConnectionContext as MuxConnectionContext,
    MqttUpstreamMux,
)
from octoeverywhere.mqttmux.muxregistry import MqttMuxRegistry
from octoeverywhere.mqttmux.types import ConnAckReturnCode, MqttMessage, SubToken
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.repeattimer import RepeatTimer
from octoeverywhere.sentry import Sentry

from .elegoocc2discovery import ElegooCc2Discovery, ElegooCc2DiscoveryResult
from .elegoocc2models import PrinterAttributes, PrinterState
from .interfaces import IFileManager, IStateTranslator


# Stable registry key for the mux. Each host process serves one Elegoo printer
# today, but using a vendor-prefixed key (rather than the SN, which may be
# unknown at startup) keeps lookups simple for the relay/local-broker code.
_MUX_KEY = "elegoo-cc2"


class ResponseMsg:

    ELEGOO_CMD_ERROR_GENERIC = 88880001
    OE_ERROR_MQTT_NOT_CONNECTED = 99990001
    OE_ERROR_TIMEOUT = 99990002
    OE_ERROR_EXCEPTION = 99990003
    OE_ERROR_MIN = OE_ERROR_MQTT_NOT_CONNECTED
    OE_ERROR_MAX = OE_ERROR_EXCEPTION

    def __init__(self, resultObj:Optional[Dict[str, Any]], errorCode:int=0, errorStr:Optional[str]=None) -> None:
        self.Result = resultObj
        self.ErrorCode = errorCode
        self.ErrorStr = errorStr
        if self.ErrorCode == ResponseMsg.OE_ERROR_TIMEOUT:
            self.ErrorStr = "Timeout waiting for Elegoo CC2 MQTT response."
        if self.ErrorCode == ResponseMsg.OE_ERROR_MQTT_NOT_CONNECTED:
            self.ErrorStr = "No active MQTT connection."
        if self.ErrorCode == ResponseMsg.ELEGOO_CMD_ERROR_GENERIC:
            self.ErrorStr = "Printer responded with a failed command result."

    def HasError(self) -> bool:
        return self.ErrorCode != 0

    def GetErrorCode(self) -> int:
        return self.ErrorCode

    def IsErrorCodeOeError(self) -> bool:
        return self.ErrorCode >= ResponseMsg.OE_ERROR_MIN and self.ErrorCode <= ResponseMsg.OE_ERROR_MAX

    def GetErrorStr(self) -> Optional[str]:
        return self.ErrorStr

    def GetLoggingErrorStr(self) -> str:
        return str(self.ErrorCode) + " - " + str(self.ErrorStr)

    def GetResult(self) -> Optional[Dict[str, Any]]:
        return self.Result


class MqttWaitingContext:

    def __init__(self, msgId:int) -> None:
        self.Id = msgId
        self.WaitEvent = threading.Event()
        self.Result:Optional[Dict[str, Any]] = None
        self.ErrorCode:int = 0
        self.ErrorMessage:Optional[str] = None

    def GetEvent(self) -> threading.Event:
        return self.WaitEvent

    def SetResultAndEvent(self, result:Optional[Dict[str, Any]], errorCode:int=0, errorMessage:Optional[str]=None) -> None:
        self.Result = result
        self.ErrorCode = errorCode
        self.ErrorMessage = errorMessage
        self.WaitEvent.set()

    def SetSocketClosed(self) -> None:
        self.Result = None
        self.ErrorCode = ResponseMsg.OE_ERROR_MQTT_NOT_CONNECTED
        self.ErrorMessage = "MQTT connection closed."
        self.WaitEvent.set()


class Cc2ConnectionContext:
    def __init__(self, ipOrHostname:str, portStr:str, serialNumber:str, accessCode:str) -> None:
        self.IpOrHostname = ipOrHostname
        self.PortStr = portStr
        self.SerialNumber = serialNumber
        self.AccessCode = accessCode


# Here's a really good overview of the elegoo centauri carbon 2 MQTT protocol
# https://github.com/danielcherubini/elegoo-homeassistant/blob/main/docs/CC2_PROTOCOL.md
#
# As of the mqttmux refactor this class no longer owns a paho connection. It
# constructs an MqttUpstreamMux for the Elegoo printer and attaches a
# LocalPluginClient to drive the CC2-specific protocol on top:
#   1. CONNECT to the broker (mux).
#   2. Subscribe to elegoo/{SN}/{requestId}/register_response.
#   3. Publish a register request to elegoo/{SN}/api_register.
#   4. On register-OK, subscribe to api_status and api_response.
#   5. Once both are SUBACK'd, the registration is finalized and the heartbeat
#      ping/pong (PING -> PONG) keeps the link alive.
#
# All of this is driven by one worker thread per upstream connection; the
# heartbeat runs continuously and is a no-op when not registered.
class ElegooCc2Client:

    RequestTimeoutSec = 10.0
    RegistrationTimeoutSec = 10.0
    PongTimeoutSec = 65.0
    HeartbeatIntervalSec = 10.0
    DefaultAccessCode = "123456"

    _Instance:"ElegooCc2Client" = None #pyright: ignore[reportAssignmentType]

    MqttMessageDebugging = False


    @staticmethod
    def Init(logger:logging.Logger, config:Config, pluginId:str, pluginVersion:str, stateTranslator:IStateTranslator, fileManager:IFileManager) -> None:
        ElegooCc2Client._Instance = ElegooCc2Client(logger, config, pluginId, pluginVersion, stateTranslator, fileManager)


    @staticmethod
    def Get() -> "ElegooCc2Client":
        return ElegooCc2Client._Instance


    def __init__(self, logger:logging.Logger, config:Config, pluginId:str, pluginVersion:str, stateTranslator:IStateTranslator, fileManager:IFileManager) -> None:
        self.Logger = logger
        self.Config = config
        self.PluginId = pluginId
        self.PluginVersion = pluginVersion
        self.StateTranslator = stateTranslator
        self.FileManager = fileManager

        # SendRequest correlation table (msgId -> waiter).
        self.RequestLock = threading.Lock()
        self.RequestPendingContexts:Dict[int, MqttWaitingContext] = {}
        self.NextRequestId = random.randint(1000, 100000)

        # Connection / registration state. Guarded by StateLock unless noted.
        self.StateLock = threading.Lock()
        self.CurrentConnectionContext:Optional[Cc2ConnectionContext] = None
        self.CurrentClientId:Optional[str] = None
        self.RegisterRequestId:Optional[str] = None
        self.ConnectionGeneration = 0
        self.MqttRegistered = False
        self.ConnectionFinalized = False
        self.LastConnectionFailedDueToTooManyClients = False
        self.LastPongTimeSec = 0.0
        self.WebsocketConnectionIp:Optional[str] = None
        # Sub tokens held only while connected; cleared on disconnect so the
        # mux's reconnect-replay doesn't re-issue subs against the old topic
        # strings (which embed the now-stale client id).
        self._register_response_token:Optional[SubToken] = None
        self._status_token:Optional[SubToken] = None
        self._response_token:Optional[SubToken] = None
        # Backoff counters for the connection-context provider.
        self.ConsecutivelyFailedConnectionAttempts = 0
        self.ConsecutivelyFailedConnectionAttemptsSinceSearch = 0
        self.HasDoneNetScanSincePluginStart = False

        # Printer state. Touches happen on the report callback (paho thread).
        self.State:Optional[PrinterState] = None
        self.Attributes:Optional[PrinterAttributes] = None
        self.FullStatus:Dict[str, Any] = {}
        self.LastStatusId:Optional[int] = None
        self.MissedStatusCounter = 0

        # Required config.
        self.PortStr = config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, "1883")
        if self.PortStr is None:
            self.PortStr = "1883"
        self.AccessCode = config.GetStr(Config.SectionElegoo, Config.ElegooCc2AccessCode, ElegooCc2Client.DefaultAccessCode)
        if self.AccessCode is None:
            self.AccessCode = ElegooCc2Client.DefaultAccessCode
        self.SerialNumber = config.GetStr(Config.SectionElegoo, Config.ElegooCc2PrinterSn, None)

        ipOrHostname = config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        if ipOrHostname is not None and len(ipOrHostname) > 0:
            OctoHttpRequest.SetLocalHostAddress(ipOrHostname)
        OctoHttpRequest.SetLocalOctoPrintPort(80)
        OctoHttpRequest.SetLocalHttpProxyIsHttps(False)
        OctoHttpRequest.SetLocalHttpProxyPort(80)

        # Build mux + local client.
        # The mux is the one actual MQTT connection to the printer, all others share it.
        self._mux = MqttUpstreamMux(
            logger=logger,
            printer_key=_MUX_KEY,
            connection_context_provider=self._BuildConnectionContext,
            subscribe_timeout_sec=15.0,
            publish_timeout_sec=20.0,
            backoff_min_sec=5.0,
            backoff_max_sec=60.0,
        )
        MqttMuxRegistry.Register(_MUX_KEY, self._mux)

        self.Client = LocalPluginClient(logger, self._mux)
        self.Client.Start()
        self.Client.OnConnected(self._OnUpstreamConnected)
        self.Client.OnDisconnected(self._OnUpstreamDisconnected)

        # Heartbeat ticks every 10s. It's idempotent when not registered, so
        # we let it run for the lifetime of the process.
        self._heartbeat = RepeatTimer(self.Logger, "ElegooCc2MqttHeartbeat", ElegooCc2Client.HeartbeatIntervalSec, self._HeartbeatTick)
        self._heartbeat.start()

        self._mux.Start()


    def GetState(self) -> Optional[PrinterState]:
        if self.State is None:
            self._mux.WakeReconnect()
            return None
        return self.State


    def GetAttributes(self) -> Optional[PrinterAttributes]:
        return self.Attributes


    def IsMqttConnected(self) -> bool:
        with self.StateLock:
            registered = self.MqttRegistered
        if not registered:
            self._mux.WakeReconnect()
        return registered


    def IsDisconnectDueToTooManyClients(self) -> bool:
        return self.LastConnectionFailedDueToTooManyClients


    # Exposes the shared MqttUpstreamMux so hosts can wire downstream surfaces
    # (local TCP broker, etc.) against it directly.
    def GetMux(self) -> MqttUpstreamMux:
        return self._mux


    # Returns an auth-check function the local TCP broker can use to verify
    # incoming MQTT CONNECT credentials against whatever the printer currently
    # requires (Elegoo CC2 always uses username "elegoo" + the access code).
    # Reads the live state per CONNECT so a runtime access-code change is
    # picked up without restarting the broker.
    def GetBrokerAuthCheck(self) -> Callable[[Optional[str], Optional[bytes]], int]:
        def _check(username: Optional[str], password: Optional[bytes]) -> int:
            with self.StateLock:
                ctx = self.CurrentConnectionContext
                fallback_access_code = self.AccessCode
            expected_access_code = ctx.AccessCode if ctx is not None else fallback_access_code
            if expected_access_code is None:
                # No credentials known yet; reject for safety.
                return ConnAckReturnCode.NOT_AUTHORIZED
            if username != "elegoo":
                return ConnAckReturnCode.BAD_USERNAME_OR_PASSWORD
            expected_pw_bytes = expected_access_code.encode("utf-8")
            if password != expected_pw_bytes:
                return ConnAckReturnCode.BAD_USERNAME_OR_PASSWORD
            return ConnAckReturnCode.ACCEPTED
        return _check


    def SendEnableWebcamCommand(self, waitForResponse:bool=True) -> ResponseMsg:
        return self.SendRequest(1042, {"enable": True}, waitForResponse=waitForResponse)


    def SendFrontendPopupMsg(self, title:str, text:str, msgType:str, actionText:Optional[str], actionLink:Optional[str], showForSec:int, onlyShowIfLoadedViaOeBool:bool) -> None:
        # There is no local CC2 browser socket to inject into yet. Keep this as
        # a no-op so the host popup interface remains compatible.
        self.Logger.debug("Elegoo CC2 frontend popup requested: %s - %s", title, text)


    def SendRequest(self, method:int, params:Optional[Dict[str, Any]]=None, waitForResponse:bool=True, timeoutSec:Optional[float]=None) -> ResponseMsg:
        if params is None:
            params = {}

        with self.StateLock:
            registered = self.MqttRegistered
        if not registered:
            self._mux.WakeReconnect()
            return ResponseMsg(None, ResponseMsg.OE_ERROR_MQTT_NOT_CONNECTED)

        requestId = self._GetNextRequestId()
        waitContext:Optional[MqttWaitingContext] = None
        if waitForResponse:
            waitContext = MqttWaitingContext(requestId)
            with self.RequestLock:
                self.RequestPendingContexts[requestId] = waitContext

        try:
            obj = {"id": requestId, "method": method, "params": params}
            if not self._PublishToRequestTopic(obj):
                return ResponseMsg(None, ResponseMsg.OE_ERROR_MQTT_NOT_CONNECTED)
            if not waitForResponse:
                return ResponseMsg(None)
            if waitContext is None:
                raise Exception("Missing wait context.")
            if timeoutSec is None:
                timeoutSec = ElegooCc2Client.RequestTimeoutSec
            waitContext.GetEvent().wait(timeoutSec)
            if waitContext.ErrorCode != 0:
                return ResponseMsg(waitContext.Result, waitContext.ErrorCode, waitContext.ErrorMessage)
            if waitContext.Result is None:
                self.Logger.info(f"Elegoo CC2 client timeout while waiting for request. {requestId}")
                return ResponseMsg(None, ResponseMsg.OE_ERROR_TIMEOUT)
            return ResponseMsg(waitContext.Result)
        except Exception as e:
            Sentry.OnException("Elegoo CC2 MQTT request failed to send.", e)
            return ResponseMsg(None, ResponseMsg.OE_ERROR_EXCEPTION, str(e))
        finally:
            if waitForResponse:
                with self.RequestLock:
                    self.RequestPendingContexts.pop(requestId, None)


    # Fired when the main MQTT mux connection to the printer is established.
    def _OnUpstreamConnected(self) -> None:
        self.Logger.info("Elegoo CC2 upstream connected, beginning registration.")
        threading.Thread(target=self._RegistrationWorker, name="ElegooCc2Register", daemon=True).start()


    # Fired when the main MQTT mux connection to the printer is lost (after all retries).
    def _OnUpstreamDisconnected(self) -> None:
        self.Logger.warning("Elegoo CC2 printer connection lost. We will try to reconnect in a few seconds.")
        # Fail every pending command waiter so callers don't hang on the
        # request-response correlation map.
        with self.RequestLock:
            for waiter in list(self.RequestPendingContexts.values()):
                waiter.SetSocketClosed()
        # Drop our subscription tokens so the mux's auto-replay-on-reconnect
        # doesn't re-issue subs against the now-stale client id.
        self._ClearPerConnectionState(was_registered_before=self._WasRegisteredAndReset())


    def _WasRegisteredAndReset(self) -> bool:
        with self.StateLock:
            self.ConnectionGeneration += 1
            was = self.MqttRegistered
            self.MqttRegistered = False
            self.ConnectionFinalized = False
            return was


    def _ClearPerConnectionState(self, was_registered_before:bool) -> None:
        with self.StateLock:
            tokens = [self._register_response_token, self._status_token, self._response_token]
            self._register_response_token = None
            self._status_token = None
            self._response_token = None
            self.RegisterRequestId = None
            self.LastPongTimeSec = 0.0
            self.LastStatusId = None
            self.MissedStatusCounter = 0
            self.State = None
            self.Attributes = None
            self.FullStatus = {}
        for tok in tokens:
            if tok is None:
                continue
            try:
                self.Client.Unsubscribe(tok)
            except Exception as e:
                self.Logger.debug("Elegoo CC2 unsubscribe on disconnect raised: %s", e)
        try:
            LocalWebApi.Get().SetPrinterConnectionState(False)
        except Exception as e:
            self.Logger.debug("LocalWebApi notify (disconnect) failed: %s", e)
        try:
            self.StateTranslator.OnConnectionLost(was_registered_before)
        except Exception as e:
            Sentry.OnException("Elegoo CC2 StateTranslator.OnConnectionLost raised", e)


    def _RegistrationWorker(self) -> None:
        try:
            with self.StateLock:
                sn = self.SerialNumber
                client_id = self.CurrentClientId
                generation = self.ConnectionGeneration
                if sn is None or client_id is None:
                    # Couldn't have connected without these; if they're gone,
                    # something tore us down between connect and now.
                    return
            register_request_id = f"{client_id}_req"
            with self.StateLock:
                if generation != self.ConnectionGeneration:
                    return
                self.RegisterRequestId = register_request_id
                # Clear stale "too many clients" flag; will be set again by the
                # register-response handler if it's still the case.
                self.LastConnectionFailedDueToTooManyClients = False

            register_response_topic = f"elegoo/{sn}/{register_request_id}/register_response"
            register_topic = f"elegoo/{sn}/api_register"
            # (Status and response topics are subscribed only after register
            # succeeds; see _SubscribeToStatusAndResponseTopics.)

            # 1) Subscribe to register_response BEFORE sending the register
            #    request so we don't race the response.
            tok_register = self.Client.Subscribe(
                register_response_topic,
                0,
                lambda msg, gen=generation: self._OnRegisterResponseMessage(msg, gen),
            )
            if tok_register is None:
                self.Logger.warning("Elegoo CC2 failed to subscribe to register response topic; forcing reconnect.")
                self._mux.ForceReconnect()
                return
            with self.StateLock:
                if generation != self.ConnectionGeneration:
                    try:
                        self.Client.Unsubscribe(tok_register)
                    except Exception as e:
                        self.Logger.debug("Elegoo CC2 stale register-response unsubscribe raised: %s", e)
                    return
                self._register_response_token = tok_register

            # 2) Publish the register request.
            register_payload = json.dumps({"client_id": client_id, "request_id": register_request_id})
            if not self.Client.Publish(register_topic, register_payload, qos=0):
                self.Logger.warning("Elegoo CC2 failed to publish the register request; forcing reconnect.")
                self._mux.ForceReconnect()
                return

            # 3) Wait for the register response by polling the per-connection
            #    state. The callback flips MqttRegistered once status + response
            #    subs are in place. Bound by RegistrationTimeoutSec.
            deadline = time.time() + ElegooCc2Client.RegistrationTimeoutSec
            registered = False
            while time.time() < deadline:
                with self.StateLock:
                    if generation != self.ConnectionGeneration:
                        return
                    registered = self.MqttRegistered
                    # If the response callback already disconnected us (e.g.
                    # "too many clients") then ForceReconnect was called and
                    # we should bail out.
                    if not self._mux.IsUpstreamConnected():
                        return
                if registered:
                    break
                time.sleep(0.1)
            if not registered:
                self.Logger.warning("Elegoo CC2 registration timed out; forcing reconnect.")
                self._mux.ForceReconnect()
                return

            # 4) Registered. Request the printer's initial state.
            with self.StateLock:
                self.LastPongTimeSec = time.time()
                self.ConsecutivelyFailedConnectionAttempts = 0
                self.ConsecutivelyFailedConnectionAttemptsSinceSearch = 0
            try:
                LocalWebApi.Get().SetPrinterConnectionState(True)
            except Exception as e:
                self.Logger.debug("LocalWebApi notify (connected) failed: %s", e)
            self.Logger.info("Elegoo CC2 MQTT client is registered and subscribed.")
            self.SendRequest(1001, waitForResponse=False)
            self.SendRequest(1002, waitForResponse=False)

        except Exception as e:
            Sentry.OnException("Elegoo CC2 registration worker raised", e)
            self._mux.ForceReconnect()


    def _SubscribeToStatusAndResponseTopics(self, generation:int) -> None:
        with self.StateLock:
            if generation != self.ConnectionGeneration:
                return
            sn = self.SerialNumber
            client_id = self.CurrentClientId
            if sn is None or client_id is None:
                return
        status_topic = f"elegoo/{sn}/api_status"
        response_topic = f"elegoo/{sn}/{client_id}/api_response"

        status_tok = self.Client.Subscribe(
            status_topic, 0,
            lambda msg, gen=generation: self._OnStatusMessage(msg, gen),
        )
        if status_tok is None:
            self.Logger.warning("Elegoo CC2 failed to subscribe to status topic; forcing reconnect.")
            self._mux.ForceReconnect()
            return
        response_tok = self.Client.Subscribe(
            response_topic, 0,
            lambda msg, gen=generation: self._OnResponseMessage(msg, gen),
        )
        if response_tok is None:
            self.Logger.warning("Elegoo CC2 failed to subscribe to response topic; forcing reconnect.")
            try:
                self.Client.Unsubscribe(status_tok)
            except Exception as e:
                self.Logger.debug("Elegoo CC2 status unsubscribe after response-sub failure raised: %s", e)
            self._mux.ForceReconnect()
            return
        tokens:List[SubToken] = []
        with self.StateLock:
            if generation != self.ConnectionGeneration:
                tokens = [status_tok, response_tok]
            else:
                tokens = []
                self._status_token = status_tok
                self._response_token = response_tok
                self.MqttRegistered = True
        for tok in tokens:
            try:
                self.Client.Unsubscribe(tok)
            except Exception as e:
                self.Logger.debug("Elegoo CC2 stale status/response unsubscribe raised: %s", e)


    def _OnRegisterResponseMessage(self, mqtt_msg: MqttMessage, generation:int) -> None:
        try:
            with self.StateLock:
                if generation != self.ConnectionGeneration:
                    return
            msg = json.loads(mqtt_msg.payload)
            if msg is None:
                return
            result = msg.get("result", None)
            error_message = str(msg.get("error", "fail")).lower()
            if error_message != "ok":
                if "too many" in error_message:
                    self.LastConnectionFailedDueToTooManyClients = True
                    self.Logger.warning("Elegoo CC2 registration failed because too many clients are connected.")
                else:
                    self.Logger.error("Elegoo CC2 registration failed. Error: %s Result: %s", error_message, result)
                self._mux.ForceReconnect()
                return
            # Spawn the status/response subscribe step on a separate worker so
            # we don't block paho's loop thread (Subscribe blocks for SUBACK).
            threading.Thread(target=self._SubscribeToStatusAndResponseTopics, args=(generation,),
                             name="ElegooCc2PostRegister", daemon=True).start()
        except Exception as e:
            Sentry.OnException("Elegoo CC2 register response handler raised", e)
            self._mux.ForceReconnect()


    def _OnStatusMessage(self, mqtt_msg: MqttMessage, generation:int) -> None:
        try:
            with self.StateLock:
                if generation != self.ConnectionGeneration:
                    return
            msg = json.loads(mqtt_msg.payload)
            if msg is None:
                return
            if ElegooCc2Client.MqttMessageDebugging and self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug("Incoming Elegoo CC2 Status:\r\n%s", json.dumps(msg, indent=3))
            self._HandleStatusMessage(msg)
        except Exception as e:
            Sentry.OnException(f"Failed to handle Elegoo CC2 status message. `{mqtt_msg.payload!r}`", e)


    def _OnResponseMessage(self, mqtt_msg: MqttMessage, generation:int) -> None:
        try:
            with self.StateLock:
                if generation != self.ConnectionGeneration:
                    return
            msg = json.loads(mqtt_msg.payload)
            if msg is None:
                return
            if ElegooCc2Client.MqttMessageDebugging and self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug("Incoming Elegoo CC2 Response:\r\n%s", json.dumps(msg, indent=3))
            self._HandleResponseMessage(msg)
        except Exception as e:
            Sentry.OnException(f"Failed to handle Elegoo CC2 response message. `{mqtt_msg.payload!r}`", e)


    def _HandleResponseMessage(self, msg:Dict[str, Any]) -> None:
        if msg.get("type", None) == "PONG":
            with self.StateLock:
                self.LastPongTimeSec = time.time()
            return

        method = msg.get("method", None)
        rawResultObj = msg.get("result", None)
        resultObj:Optional[Dict[str, Any]] = None
        if isinstance(rawResultObj, dict):
            resultObj = cast(Dict[str, Any], rawResultObj)
        if resultObj is not None and int(resultObj.get("error_code", 0)) == 0:
            if method == 1001:
                self._HandleAttributesUpdate(resultObj)
            elif method == 1002:
                self._HandleStatusResult(resultObj, self.State is None)

        msgId = msg.get("id", None)
        if msgId is None:
            return
        msgIdInt = int(msgId)
        with self.RequestLock:
            context = self.RequestPendingContexts.get(msgIdInt, None)
            if context is None:
                return
            error = msg.get("error", None)
            if isinstance(error, dict):
                context.SetResultAndEvent(resultObj if isinstance(resultObj, dict) else None, ResponseMsg.ELEGOO_CMD_ERROR_GENERIC, str(error))
            elif isinstance(resultObj, dict) and int(resultObj.get("error_code", 0)) != 0:
                context.SetResultAndEvent(resultObj, ResponseMsg.ELEGOO_CMD_ERROR_GENERIC, str(resultObj.get("error_msg", "Printer command failed.")))
            else:
                context.SetResultAndEvent(resultObj if resultObj is not None else {})


    def _HandleStatusMessage(self, msg:Dict[str, Any]) -> None:
        method = msg.get("method", None)
        if method != 6000:
            return
        statusId = msg.get("status_id", msg.get("id", None))
        if statusId is not None:
            statusIdInt = int(statusId)
            if self.LastStatusId is not None and statusIdInt != self.LastStatusId + 1:
                self.MissedStatusCounter += 1
                self.Logger.debug("Elegoo CC2 missed a status update. Last: %s New: %s Missed Count: %s",
                                  self.LastStatusId, statusIdInt, self.MissedStatusCounter)
                if self.MissedStatusCounter >= 5:
                    self.MissedStatusCounter = 0
                    self.Logger.info("Elegoo CC2 missed several status updates, requesting a full status sync.")
                    self.SendRequest(1002, waitForResponse=False)
            else:
                self.MissedStatusCounter = 0
            self.LastStatusId = statusIdInt
        resultObj = msg.get("result", None)
        if isinstance(resultObj, dict):
            self._HandleStatusResult(resultObj, False)


    def _HandleStatusResult(self, status:Dict[str, Any], isFirstFullSyncResponse:bool) -> None:
        self._DeepMerge(self.FullStatus, status)
        isFirstStateUpdate = self.State is None
        try:
            if self.State is None:
                s = PrinterState(self.Logger)
                s.OnUpdate(self.FullStatus)
                self.State = s
                self.Logger.debug("Elegoo CC2 printer state object created.")
            else:
                self.State.OnUpdate(self.FullStatus)
        except Exception as e:
            Sentry.OnException("Failed to update Elegoo CC2 printer state object", e)
        if self.State is None:
            self.Logger.warning("Elegoo CC2 client finalized but we don't have a state object.")
            return
        self.StateTranslator.OnStatusUpdate(self.State, isFirstFullSyncResponse or isFirstStateUpdate)


    def _HandleAttributesUpdate(self, attributes:Dict[str, Any]) -> None:
        try:
            if self.Attributes is None:
                s = PrinterAttributes(self.Logger)
                s.OnUpdate(attributes)
                self.Attributes = s
                self.Logger.debug("Elegoo CC2 printer attributes object created.")
            else:
                self.Attributes.OnUpdate(attributes)
        except Exception as e:
            Sentry.OnException("Failed to update Elegoo CC2 printer attributes object", e)

        with self.StateLock:
            if self.ConnectionFinalized:
                return
            self.ConnectionFinalized = True
            wsConIp = self.WebsocketConnectionIp
        if wsConIp is None:
            self.Logger.error("Elegoo CC2 client finalized but we don't have a connection IP.")
        else:
            OctoHttpRequest.SetLocalHostAddress(wsConIp)
            self.Config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, wsConIp)

        if self.Attributes is not None and self.Attributes.SerialNumber is not None:
            if self.SerialNumber is None:
                self.SerialNumber = self.Attributes.SerialNumber
                self.Config.SetStr(Config.SectionElegoo, Config.ElegooCc2PrinterSn, self.SerialNumber)
            elif self.SerialNumber != self.Attributes.SerialNumber:
                self.Logger.error("Elegoo CC2 serial number mismatch. Expected: %s Got: %s",
                                  self.SerialNumber, self.Attributes.SerialNumber)

        self.FileManager.Sync()
        self.Logger.info("Elegoo CC2 client connection fully connected.")


    def _PublishToRequestTopic(self, obj:Dict[str, Any]) -> bool:
        try:
            if not self.Client.IsConnected():
                self.Logger.info("Failed to publish Elegoo CC2 command because we aren't connected.")
                self._mux.WakeReconnect()
                return False
            sn = self.SerialNumber
            client_id = self.CurrentClientId
            if sn is None or client_id is None:
                return False
            if ElegooCc2Client.MqttMessageDebugging and self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug("Outgoing Elegoo CC2 Message:\r\n%s", json.dumps(obj, indent=3))
            return self.Client.Publish(f"elegoo/{sn}/{client_id}/api_request", json.dumps(obj), qos=0)
        except Exception as e:
            Sentry.OnException("Failed to publish message to Elegoo CC2 printer.", e)
            return False


    def _HeartbeatTick(self) -> None:
        with self.StateLock:
            registered = self.MqttRegistered
            last_pong = self.LastPongTimeSec
        if not registered:
            return
        try:
            if last_pong > 0 and time.time() - last_pong > ElegooCc2Client.PongTimeoutSec:
                self.Logger.warning("Elegoo CC2 heartbeat timed out, disconnecting.")
                self._mux.ForceReconnect()
                return
            # Don't block on QoS-0 publish ack; if it fails the next tick
            # picks up the missing PONG.
            sn = self.SerialNumber
            client_id = self.CurrentClientId
            if sn is None or client_id is None:
                return
            self.Client.Publish(f"elegoo/{sn}/{client_id}/api_request",
                                  json.dumps({"type": "PING"}), qos=0)
        except Exception as e:
            Sentry.OnException("Elegoo CC2 heartbeat failed.", e)
            self._mux.ForceReconnect()


    def _BuildConnectionContext(self) -> MuxConnectionContext:
        with self.StateLock:
            self.ConsecutivelyFailedConnectionAttempts += 1
            self.ConsecutivelyFailedConnectionAttemptsSinceSearch += 1

        configIpOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        serialNumber = self.Config.GetStr(Config.SectionElegoo, Config.ElegooCc2PrinterSn, self.SerialNumber)
        accessCode = self.Config.GetStr(Config.SectionElegoo, Config.ElegooCc2AccessCode, self.AccessCode)
        if accessCode is None:
            accessCode = ElegooCc2Client.DefaultAccessCode
        self.AccessCode = accessCode

        # If we don't have the serial number, search for it now.
        # Most setups don't pass this at first, so it's easier for the user, and then on first run we get and bind to it.
        if serialNumber is None or len(serialNumber) == 0:
            discovery:Optional[ElegooCc2DiscoveryResult] = None
            if configIpOrHostname is not None and len(configIpOrHostname) > 0:
                results = ElegooCc2Discovery.Discover(self.Logger, configIpOrHostname, timeoutSec=3.0)
                if len(results) > 0:
                    discovery = results[0]
            else:
                results = ElegooCc2Discovery.Discover(self.Logger, None, timeoutSec=3.0)
                if len(results) == 1:
                    discovery = results[0]
            if discovery is None or discovery.SerialNumber is None:
                raise Exception("Missing Elegoo CC2 serial number and discovery did not find exactly one printer.")
            serialNumber = discovery.SerialNumber
            configIpOrHostname = discovery.Ip
            self.Config.SetStr(Config.SectionElegoo, Config.ElegooCc2PrinterSn, serialNumber)
            self.Config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, discovery.Ip)
            self.Logger.info("Discovered Elegoo CC2 printer %s at %s.", serialNumber, discovery.Ip)

        # Now that we have all that we need, detect if we should do a search.
        doSearch = False
        with self.StateLock:
            # If the plugin has just started and we are failing to connect, do a search quicker.
            if self.HasDoneNetScanSincePluginStart is False and self.ConsecutivelyFailedConnectionAttemptsSinceSearch > 1:
                self.HasDoneNetScanSincePluginStart = True
                doSearch = True
            elif self.ConsecutivelyFailedConnectionAttemptsSinceSearch > 6:
                doSearch = True
        if doSearch:
            self.Logger.info("Multiple failed connection attempts to Elegoo CC2 printer. Running discovery again...")
            self.ConsecutivelyFailedConnectionAttemptsSinceSearch = 0
            results = ElegooCc2Discovery.Discover(self.Logger, None, timeoutSec=3.0)
            if serialNumber is None or len(serialNumber) == 0:
                self.Logger.info(f"We found {len(results)} Elegoo CC2 printers on the network during discovery. But have no set serial number, so we can't auto rediscover.")
            else:
                for r in results:
                    if r.SerialNumber == serialNumber:
                        self.Logger.info(f"We found the Elegoo CC2 printer with the correct serial number {serialNumber} at {r.Ip}. Updating config and trying to connect there.")
                        configIpOrHostname = r.Ip
                        self.Config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, r.Ip)
                        break
                else:
                    self.Logger.info(f"We found {len(results)} Elegoo CC2 printers on the network during discovery. But none had the correct serial number {serialNumber}.")

        if configIpOrHostname is None or len(configIpOrHostname) == 0:
            raise Exception("An IP address or hostname must be provided in the config for Elegoo CC2 Connect.")
        if self.PortStr is None:
            raise Exception("A port must be provided in the config for Elegoo CC2 Connect.")

        # Generate a fresh per-connection client id and cache state the
        # registration handshake will use for topic construction.
        client_id = self._GenerateClientId()
        ctx = Cc2ConnectionContext(configIpOrHostname, self.PortStr, serialNumber, accessCode)
        with self.StateLock:
            self.SerialNumber = serialNumber
            self.CurrentClientId = client_id
            self.CurrentConnectionContext = ctx
            self.WebsocketConnectionIp = configIpOrHostname
            self.ConnectionGeneration += 1

        LocalIpHelper.SetConnectionTargetIpOverride(configIpOrHostname)
        OctoHttpRequest.SetLocalHostAddress(configIpOrHostname)
        self.Logger.info(f"Trying to connect to Elegoo CC2 printer at {configIpOrHostname}:{self.PortStr}...")

        return MuxConnectionContext(
            host=configIpOrHostname,
            port=int(self.PortStr),
            username="elegoo",
            password=accessCode,
            client_id=client_id,
            use_tls=False,
            transport="tcp",
            keep_alive_sec=30,
        )


    def _WaitForCurrentConnectionContext(self, isClosed:Callable[[], bool]) -> Optional[Cc2ConnectionContext]:
        attempt = 0
        while isClosed() is False:
            attempt += 1
            with self.StateLock:
                context = self.CurrentConnectionContext
            if context is not None:
                return context
            if attempt > 10:
                return None
            self._mux.WakeReconnect()
            time.sleep(1.5 * attempt)
        return None


    def _GetNextRequestId(self) -> int:
        with self.RequestLock:
            self.NextRequestId += 1
            return self.NextRequestId


    def _GenerateClientId(self) -> str:
        return f"1_PC_{random.randint(1000, 9999)}"


    def _DeepMerge(self, destination:Dict[str, Any], source:Dict[str, Any]) -> None:
        for k, v in source.items():
            if isinstance(v, dict):
                existing = destination.get(k, None)
                if isinstance(existing, dict):
                    self._DeepMerge(existing, v)
                else:
                    destination[k] = dict(v)
            else:
                destination[k] = v
