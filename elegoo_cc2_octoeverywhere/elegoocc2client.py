import json
import logging
import random
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as mqtt

from linux_host.config import Config
from linux_host.localwebapi import LocalWebApi

from octoeverywhere.localip import LocalIpHelper
from octoeverywhere.mqttwebsocketproxy import MqttConnectionContext
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.repeattimer import RepeatTimer
from octoeverywhere.sentry import Sentry

from .elegoocc2discovery import ElegooCc2Discovery
from .elegoocc2models import PrinterAttributes, PrinterState
from .interfaces import IFileManager, IStateTranslator


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
class ElegooCc2Client:

    RequestTimeoutSec = 10.0
    RegistrationTimeoutSec = 10.0
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

        self.RequestLock = threading.Lock()
        self.RequestPendingContexts:Dict[int, MqttWaitingContext] = {}
        self.NextRequestId = random.randint(1000, 100000)

        self.Client:Optional[mqtt.Client] = None
        self.MqttConnected = False
        self.MqttRegistered = False
        self.ConnectionFinalized = False
        self.LastConnectionFailedDueToTooManyClients = False
        self.SleepEvent = threading.Event()
        self.CurrentConnectionContext:Optional[Cc2ConnectionContext] = None
        self.CurrentClientId:Optional[str] = None
        self.RegisterRequestId:Optional[str] = None
        self.RegisterSubscribeMid:Optional[int] = None
        self.StatusSubscribeMid:Optional[int] = None
        self.ResponseSubscribeMid:Optional[int] = None
        self.StatusSubscribed = False
        self.ResponseSubscribed = False
        self.WebsocketConnectionIp:Optional[str] = None
        self.ConsecutivelyFailedConnectionAttempts = 0
        self.ConsecutivelyFailedConnectionAttemptsSinceSearch = 0
        self.HasDoneNetScanSincePluginStart = False
        self.LastStatusId:Optional[int] = None
        self.MissedStatusCounter = 0
        self.LastPongTimeSec = 0.0

        self.State:Optional[PrinterState] = None
        self.Attributes:Optional[PrinterAttributes] = None
        self.FullStatus:Dict[str, Any] = {}
        self._CleanupStateOnDisconnect()

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

        t = threading.Thread(target=self._ClientWorker, name="ElegooCc2Client")
        t.start()


    def GetState(self) -> Optional[PrinterState]:
        if self.State is None:
            self.SleepEvent.set()
            return None
        return self.State


    def GetAttributes(self) -> Optional[PrinterAttributes]:
        return self.Attributes


    def IsMqttConnected(self) -> bool:
        if self.MqttRegistered is False:
            self.SleepEvent.set()
        return self.MqttRegistered


    def IsDisconnectDueToTooManyClients(self) -> bool:
        return self.LastConnectionFailedDueToTooManyClients


    def GetMqttProxyConnectionContext(self, args:Optional[Dict[str, Any]], isClosed:Callable[[], bool]) -> Optional[MqttConnectionContext]:
        connectionContext = self._WaitForCurrentConnectionContext(isClosed)
        if connectionContext is None:
            return None

        clientId = self._GenerateClientId()
        websocketPath = "/"
        accessCode = connectionContext.AccessCode
        if args is not None:
            clientIdArg = args.get("client_id", args.get("clientId", None))
            if isinstance(clientIdArg, str) and len(clientIdArg) > 0:
                clientId = clientIdArg
            accessCodeArg = args.get("access_code", args.get("password", None))
            if isinstance(accessCodeArg, str) and len(accessCodeArg) > 0:
                accessCode = accessCodeArg
            websocketPathArg = args.get("websocket_path", args.get("path", None))
            if isinstance(websocketPathArg, str) and len(websocketPathArg) > 0:
                websocketPath = websocketPathArg

        return MqttConnectionContext(
            connectionContext.IpOrHostname,
            "9001",
            "elegoo",
            accessCode,
            clientId=clientId,
            transport="websockets",
            keepAliveSec=60,
            websocketPath=websocketPath
        )


    def SendEnableWebcamCommand(self, waitForResponse:bool=True) -> ResponseMsg:
        return self.SendRequest(1042, {"enable": True}, waitForResponse=waitForResponse)


    def SendFrontendPopupMsg(self, title:str, text:str, msgType:str, actionText:Optional[str], actionLink:Optional[str], showForSec:int, onlyShowIfLoadedViaOeBool:bool) -> None:
        # There is no local CC2 browser socket to inject into yet. Keep this as a no-op so the host popup
        # interface remains compatible with the other printer hosts.
        self.Logger.debug("Elegoo CC2 frontend popup requested: %s - %s", title, text)


    def SendRequest(self, method:int, params:Optional[Dict[str, Any]]=None, waitForResponse:bool=True, timeoutSec:Optional[float]=None) -> ResponseMsg:
        if params is None:
            params = {}

        if self.MqttRegistered is False or self.Client is None:
            self.SleepEvent.set()
            return ResponseMsg(None, ResponseMsg.OE_ERROR_MQTT_NOT_CONNECTED)

        requestId = self._GetNextRequestId()
        waitContext:Optional[MqttWaitingContext] = None
        if waitForResponse:
            waitContext = MqttWaitingContext(requestId)
            with self.RequestLock:
                self.RequestPendingContexts[requestId] = waitContext

        try:
            obj = {
                "id": requestId,
                "method": method,
                "params": params
            }
            if self._PublishRequest(obj) is False:
                return ResponseMsg(None, ResponseMsg.OE_ERROR_MQTT_NOT_CONNECTED)

            if waitForResponse is False:
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
                    if requestId in self.RequestPendingContexts:
                        del self.RequestPendingContexts[requestId]


    def _ClientWorker(self) -> None:
        isConnectAttemptFromEventBump = False
        while True:
            ipOrHostname:str = "None"
            try:
                self.MqttConnected = False
                self.MqttRegistered = False
                self.ConnectionFinalized = False
                self.RegisterRequestId = None
                self.RegisterSubscribeMid = None
                self.StatusSubscribeMid = None
                self.ResponseSubscribeMid = None
                self.StatusSubscribed = False
                self.ResponseSubscribed = False

                connectionContext = self._GetConnectionContextToTry(isConnectAttemptFromEventBump)
                ipOrHostname = connectionContext.IpOrHostname
                self.WebsocketConnectionIp = ipOrHostname
                self.CurrentConnectionContext = connectionContext

                LocalIpHelper.SetConnectionTargetIpOverride(ipOrHostname)
                OctoHttpRequest.SetLocalHostAddress(ipOrHostname)

                self.CurrentClientId = self._GenerateClientId()
                self.Client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.CurrentClientId) #pyright: ignore[reportPrivateImportUsage]
                self.Client.reconnect_delay_set(min_delay=1, max_delay=5)
                self.Client.username_pw_set("elegoo", connectionContext.AccessCode)
                self.Client.on_connect = self._OnConnect
                self.Client.on_message = self._OnMessage
                self.Client.on_disconnect = self._OnDisconnect
                self.Client.on_subscribe = self._OnSubscribe
                self.Client.on_log = self._OnLog

                self.Logger.info(f"Trying to connect to Elegoo CC2 printer at {ipOrHostname}:{connectionContext.PortStr}...")
                with RepeatTimer(self.Logger, "ElegooCc2MqttHeartbeat", 10.0, self._RepeatTimerHeartbeatTick) as t:
                    t.start()
                    self.Client.connect(ipOrHostname, int(connectionContext.PortStr), keepalive=30)
                    self.Client.loop_forever()
            except Exception as e:
                if isinstance(e, ConnectionRefusedError):
                    self.Logger.warning(f"Failed to connect to the Elegoo CC2 printer {ipOrHostname}:{self.PortStr}, we will retry in a bit. {e}")
                elif isinstance(e, TimeoutError):
                    self.Logger.warning(f"Failed to connect to the Elegoo CC2 printer {ipOrHostname}:{self.PortStr}, we will retry in a bit. {e}")
                elif isinstance(e, OSError) and ("Network is unreachable" in str(e) or "No route to host" in str(e)):
                    self.Logger.warning(f"Failed to connect to the Elegoo CC2 printer {ipOrHostname}:{self.PortStr}, we will retry in a bit. {e}")
                elif isinstance(e, socket.timeout) and "timed out" in str(e):
                    self.Logger.warning(f"Failed to connect to the Elegoo CC2 printer {ipOrHostname}:{self.PortStr} due to a timeout, we will retry in a bit. {e}")
                else:
                    if Sentry.IsCommonConnectionException(e):
                        self.Logger.warning("Elegoo CC2 printer connection error: %s", str(e))
                    else:
                        Sentry.OnException(f"Failed to connect to the Elegoo CC2 printer {ipOrHostname}:{self.PortStr}. We will retry in a bit.", e)

            LocalWebApi.Get().SetPrinterConnectionState(False)
            sleepDelay = self.ConsecutivelyFailedConnectionAttempts
            sleepDelay = min(sleepDelay, 6)
            sleepDelaySec = 5.0 * sleepDelay
            self.Logger.info(f"Sleeping for {sleepDelaySec} seconds before trying to reconnect to the Elegoo CC2 printer.")
            isConnectAttemptFromEventBump = self.SleepEvent.wait(sleepDelaySec)
            self.SleepEvent.clear()


    def _RepeatTimerHeartbeatTick(self) -> None:
        if self.MqttRegistered is False:
            return
        try:
            client = self.Client
            if client is None:
                return
            if self.LastPongTimeSec > 0 and time.time() - self.LastPongTimeSec > 65:
                self.Logger.warning("Elegoo CC2 heartbeat timed out, disconnecting.")
                client.disconnect()
                return
            client.publish(self._GetRequestTopic(), json.dumps({"type": "PING"}))
        except Exception as e:
            Sentry.OnException("Elegoo CC2 heartbeat failed.", e)
            c = self.Client
            if c is not None:
                c.disconnect()


    def _CleanupStateOnDisconnect(self) -> None:
        self.State = None
        self.Attributes = None
        self.FullStatus = {}
        self.MqttConnected = False
        self.MqttRegistered = False
        self.ConnectionFinalized = False
        self.StatusSubscribed = False
        self.ResponseSubscribed = False
        self.LastStatusId = None
        self.MissedStatusCounter = 0
        self.LastPongTimeSec = 0.0


    def _OnConnect(self, client:mqtt.Client, userdata:Any, flags:Any, reason_code:Any, properties:Any) -> None:
        if reason_code.is_failure:
            self.Logger.warning("Elegoo CC2 MQTT connection failed: %s", reason_code)
            client.disconnect()
            return

        self.Logger.info("Connection to the Elegoo CC2 printer established. Registering client.")
        self.MqttConnected = True
        self.LastConnectionFailedDueToTooManyClients = False
        if self.CurrentClientId is None:
            self.CurrentClientId = self._GenerateClientId()
        self.RegisterRequestId = f"{self.CurrentClientId}_req"

        (result, mid) = client.subscribe(self._GetRegisterResponseTopic())
        if result != mqtt.MQTT_ERR_SUCCESS or mid is None:
            self.Logger.warning("Elegoo CC2 failed to subscribe to the register response topic. Result: %s", result)
            client.disconnect()
            return
        self.RegisterSubscribeMid = mid


    def _OnSubscribe(self, client:Any, userdata:Any, mid:Any, reason_code_list:List[mqtt.ReasonCode], properties:Any) -> None: #pyright: ignore[reportPrivateImportUsage]
        try:
            for r in reason_code_list:
                if r.is_failure:
                    self.Logger.error("Elegoo CC2 MQTT subscribe failed. Mid: %s Reason: %s", mid, r)
                    c = self.Client
                    if c is not None:
                        c.disconnect()
                    return

            if self.RegisterSubscribeMid is not None and mid == self.RegisterSubscribeMid:
                self._SendRegisterRequest()
                return

            if self.StatusSubscribeMid is not None and mid == self.StatusSubscribeMid:
                self.StatusSubscribed = True
            if self.ResponseSubscribeMid is not None and mid == self.ResponseSubscribeMid:
                self.ResponseSubscribed = True

            if self.MqttRegistered:
                return

            if self.StatusSubscribed and self.ResponseSubscribed:
                self.MqttRegistered = True
                self.LastPongTimeSec = time.time()
                self.ConsecutivelyFailedConnectionAttempts = 0
                self.ConsecutivelyFailedConnectionAttemptsSinceSearch = 0
                LocalWebApi.Get().SetPrinterConnectionState(True)
                self.Logger.info("Elegoo CC2 MQTT client is registered and subscribed.")
                self.SendRequest(1001, waitForResponse=False)
                self.SendRequest(1002, waitForResponse=False)
        except Exception as e:
            Sentry.OnException("Elegoo CC2 exception in _OnSubscribe.", e)


    def _OnDisconnect(self, client:Any, userdata:Any, disconnect_flags:Any, reason_code:Any, properties:Any) -> None:
        self.Logger.warning("Elegoo CC2 printer connection lost. We will try to reconnect in a few seconds.")

        with self.RequestLock:
            for _, v in self.RequestPendingContexts.items():
                v.SetSocketClosed()

        wasFullyConnected = self.ConnectionFinalized
        self._CleanupStateOnDisconnect()
        self.StateTranslator.OnConnectionLost(wasFullyConnected)


    def _OnLog(self, client:Any, userdata:Any, level:int, msg:str) -> None:
        if level == mqtt.MQTT_LOG_ERR:
            if "exception" in msg:
                Sentry.OnException("Elegoo CC2 MQTT leaked exception.", Exception(msg))
            else:
                self.Logger.error(f"Elegoo CC2 MQTT log error: {msg}")
        elif level == mqtt.MQTT_LOG_WARNING:
            self.Logger.error(f"Elegoo CC2 MQTT log warn: {msg}")


    def _OnMessage(self, client:Any, userdata:Any, mqttMsg:mqtt.MQTTMessage) -> None:
        try:
            msg = json.loads(mqttMsg.payload)
            if msg is None:
                raise Exception("Parsed json MQTT message returned None")

            if ElegooCc2Client.MqttMessageDebugging and self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug("Incoming Elegoo CC2 Message [%s]:\r\n%s", mqttMsg.topic, json.dumps(msg, indent=3))

            topic = mqttMsg.topic
            if topic == self._GetRegisterResponseTopic():
                self._HandleRegisterResponse(msg)
                return
            if topic == self._GetStatusTopic():
                self._HandleStatusMessage(msg, True)
                return
            if topic == self._GetResponseTopic():
                self._HandleResponseMessage(msg)
                return
        except Exception as e:
            Sentry.OnException(f"Failed to handle incoming Elegoo CC2 MQTT message. `{mqttMsg.payload}`", e)


    def _HandleRegisterResponse(self, msg:Dict[str, Any]) -> None:
        result = msg.get("result", None)
        errorMessage = str(msg.get("error", "fail")).lower()
        if errorMessage != "ok":
            if "too many" in errorMessage:
                self.LastConnectionFailedDueToTooManyClients = True
                self.Logger.warning("Elegoo CC2 registration failed because too many clients are connected.")
            else:
                self.Logger.error("Elegoo CC2 registration failed. Error: %s Result: %s", errorMessage, result)
            self._DisconnectClient()
            return

        client = self.Client
        if client is None:
            return

        (statusResult, statusMid) = client.subscribe(self._GetStatusTopic())
        if statusResult != mqtt.MQTT_ERR_SUCCESS or statusMid is None:
            self.Logger.warning("Elegoo CC2 failed to subscribe to status topic. Result: %s", statusResult)
            self._DisconnectClient()
            return
        self.StatusSubscribeMid = statusMid

        (responseResult, responseMid) = client.subscribe(self._GetResponseTopic())
        if responseResult != mqtt.MQTT_ERR_SUCCESS or responseMid is None:
            self.Logger.warning("Elegoo CC2 failed to subscribe to response topic. Result: %s", responseResult)
            self._DisconnectClient()
            return
        self.ResponseSubscribeMid = responseMid


    def _HandleResponseMessage(self, msg:Dict[str, Any]) -> None:
        if msg.get("type", None) == "PONG":
            self.LastPongTimeSec = time.time()
            return

        method = msg.get("method", None)
        resultObj = msg.get("result", None)
        if isinstance(resultObj, dict) and int(resultObj.get("error_code", 0)) == 0:
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
                context.SetResultAndEvent(resultObj if isinstance(resultObj, dict) else {})


    def _HandleStatusMessage(self, msg:Dict[str, Any], isAsyncStatus:bool) -> None:
        method = msg.get("method", None)
        if method != 6000:
            return

        statusId = msg.get("status_id", msg.get("id", None))
        if statusId is not None:
            statusIdInt = int(statusId)
            if self.LastStatusId is not None and statusIdInt != self.LastStatusId + 1:
                self.MissedStatusCounter += 1
                self.Logger.debug("Elegoo CC2 missed a status update. Last: %s New: %s Missed Count: %s", self.LastStatusId, statusIdInt, self.MissedStatusCounter)
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

        if self.ConnectionFinalized is True:
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
                self.Logger.error("Elegoo CC2 serial number mismatch. Expected: %s Got: %s", self.SerialNumber, self.Attributes.SerialNumber)

        self.FileManager.Sync()
        self.Logger.info("Elegoo CC2 client connection fully connected.")


    def _PublishRequest(self, obj:Dict[str, Any]) -> bool:
        try:
            client = self.Client
            if client is None or not client.is_connected():
                self.Logger.info("Failed to publish Elegoo CC2 command because we aren't connected.")
                self.SleepEvent.set()
                return False

            if ElegooCc2Client.MqttMessageDebugging and self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug("Outgoing Elegoo CC2 Message:\r\n%s", json.dumps(obj, indent=3))

            state = client.publish(self._GetRequestTopic(), json.dumps(obj))
            state.wait_for_publish(10)
            return True
        except Exception as e:
            Sentry.OnException("Failed to publish message to Elegoo CC2 printer.", e)
        return False


    def _SendRegisterRequest(self) -> None:
        try:
            client = self.Client
            connectionContext = self.CurrentConnectionContext
            clientId = self.CurrentClientId
            if client is None or connectionContext is None or clientId is None:
                self._DisconnectClient()
                return

            if self.RegisterRequestId is None:
                self.RegisterRequestId = f"{clientId}_req"
            msg = {
                "client_id": clientId,
                "request_id": self.RegisterRequestId
            }
            client.publish(self._GetRegisterTopic(), json.dumps(msg))
            registerRequestId = self.RegisterRequestId

            def timeoutThread() -> None:
                time.sleep(ElegooCc2Client.RegistrationTimeoutSec)
                if self.MqttConnected and self.MqttRegistered is False and self.RegisterRequestId == registerRequestId:
                    self.Logger.warning("Elegoo CC2 registration timed out.")
                    self._DisconnectClient()
            threading.Thread(target=timeoutThread, name="ElegooCc2RegisterTimeout").start()
        except Exception as e:
            Sentry.OnException("Elegoo CC2 failed to send register request.", e)
            self._DisconnectClient()


    def _GetConnectionContextToTry(self, isConnectAttemptFromEventBump:bool) -> Cc2ConnectionContext:
        self.ConsecutivelyFailedConnectionAttempts += 1

        configIpOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        serialNumber = self.Config.GetStr(Config.SectionElegoo, Config.ElegooCc2PrinterSn, self.SerialNumber)
        accessCode = self.Config.GetStr(Config.SectionElegoo, Config.ElegooCc2AccessCode, self.AccessCode)
        if accessCode is None:
            accessCode = ElegooCc2Client.DefaultAccessCode
        self.AccessCode = accessCode

        if serialNumber is None or len(serialNumber) == 0:
            discovery = None
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

        if configIpOrHostname is None or len(configIpOrHostname) == 0:
            raise Exception("An IP address or hostname must be provided in the config for Elegoo CC2 Connect.")

        self.SerialNumber = serialNumber
        return Cc2ConnectionContext(configIpOrHostname, self.PortStr, serialNumber, accessCode)


    def _WaitForCurrentConnectionContext(self, isClosed:Callable[[], bool]) -> Optional[Cc2ConnectionContext]:
        attempt = 0
        while isClosed() is False:
            attempt += 1
            context = self.CurrentConnectionContext
            if context is not None:
                return context
            if attempt > 10:
                return None
            self.SleepEvent.set()
            time.sleep(1.5 * attempt)
        return None


    def _GetNextRequestId(self) -> int:
        with self.RequestLock:
            self.NextRequestId += 1
            return self.NextRequestId


    def _GenerateClientId(self) -> str:
        return f"1_PC_{random.randint(1000, 9999)}"


    def _GetRegisterTopic(self) -> str:
        context = self.CurrentConnectionContext
        if context is None:
            raise Exception("No current Elegoo CC2 connection context.")
        return f"elegoo/{context.SerialNumber}/api_register"


    def _GetRegisterResponseTopic(self) -> str:
        context = self.CurrentConnectionContext
        requestId = self.RegisterRequestId
        if context is None or requestId is None:
            raise Exception("No current Elegoo CC2 connection context or registration request id.")
        return f"elegoo/{context.SerialNumber}/{requestId}/register_response"


    def _GetRequestTopic(self) -> str:
        context = self.CurrentConnectionContext
        clientId = self.CurrentClientId
        if context is None or clientId is None:
            raise Exception("No current Elegoo CC2 connection context or client id.")
        return f"elegoo/{context.SerialNumber}/{clientId}/api_request"


    def _GetResponseTopic(self) -> str:
        context = self.CurrentConnectionContext
        clientId = self.CurrentClientId
        if context is None or clientId is None:
            raise Exception("No current Elegoo CC2 connection context or client id.")
        return f"elegoo/{context.SerialNumber}/{clientId}/api_response"


    def _GetStatusTopic(self) -> str:
        context = self.CurrentConnectionContext
        if context is None:
            raise Exception("No current Elegoo CC2 connection context.")
        return f"elegoo/{context.SerialNumber}/api_status"


    def _DisconnectClient(self) -> None:
        c = self.Client
        if c is not None:
            try:
                c.disconnect()
            except Exception as e:
                self.Logger.debug("Elegoo CC2 disconnect exception. %s", e)


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
