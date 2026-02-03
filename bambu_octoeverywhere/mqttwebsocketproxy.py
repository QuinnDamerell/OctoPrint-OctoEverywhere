import time
import json
import base64
import logging
import threading
from typing import Dict, Any, Optional, Callable, List

import paho.mqtt.client as mqtt
from paho.mqtt.enums import MQTTErrorCode
from paho.mqtt.reasoncodes import ReasonCode

from octoeverywhere.buffer import Buffer
from octoeverywhere.sentry import Sentry
from octoeverywhere.Proto.HttpInitialContext import HttpInitialContext
from octoeverywhere.interfaces import ICommandWebsocketProviderBuilder, ICommandWebsocketProvider, IWebSocketClient, WebSocketOpCode

from .bambuclient import BambuClient, ConnectionContext

# The actual MQTT proxy websocket class. This is where the magic happens.
class MqttWebsocketProxy(IWebSocketClient):

    def __init__(self, logger:logging.Logger, args:Optional[Dict[str, Any]],
                    streamId:int, path:str, pathType:int, context:HttpInitialContext,
                    onWsOpen:Optional[Callable[[IWebSocketClient], None]]=None,
                    onWsData:Optional[Callable[[IWebSocketClient, Buffer, WebSocketOpCode], None]]=None,
                    onWsClose:Optional[Callable[[IWebSocketClient], None]]=None,
                    onWsError:Optional[Callable[[IWebSocketClient, Exception], None]]=None,
                    headers:Optional[Dict[str, str]]=None,
                    subProtocolList:Optional[List[str]]=None
                 ):
        self.Logger = logger
        self.Args = args
        self.StreamId = streamId
        self._onWsOpen = onWsOpen
        self._onWsData = onWsData
        self._onWsClose = onWsClose
        self._onWsError = onWsError

        # Check if there are any arg overrides
        self.UserNameOverride:Optional[str] = None
        self.AccessCodeOverride:Optional[str] = None
        if args is not None:
            self.UserNameOverride = args.get("username", None)
            self.AccessCodeOverride = args.get("access_code", None)
            if self.UserNameOverride is not None or self.AccessCodeOverride is not None:
                self.Logger.info(f"{self._GetLogMsgStart()} is using an user name or access code override. User: {self.UserNameOverride}, Access Code: {self.AccessCodeOverride}")

        # Allows us to map user message ids to mid responses
        self.MidAckLock = threading.Lock()
        self.MidAckMap:Dict[int, int] = {}
        self.IsMakingMidAckRequest = False

        # Create our internal objects
        self.Client:Optional[mqtt.Client] = None
        self.IsClosed = False
        self.StateLock = threading.Lock()
        self.MainThread:Optional[threading.Thread] = None


    # Interface function.
    def Close(self) -> None:
        self._InternalClose()


    # Interface function.
    def RunAsync(self) -> None:
        if self.IsClosed:
            raise Exception(f"{self._GetLogMsgStart()} Can't run async, already closed.")
        # Start the main async thread.
        self.MainThread = threading.Thread(target=self._RunThread, name="MqttWebsocketProxy")
        self.MainThread.start()


    # Interface function.
    def Send(self, buffer:Buffer, msgStartOffsetBytes:Optional[int]=None, msgSize:Optional[int]=None, isData:bool=True) -> None:
        self.SendWithOptCode(buffer, msgStartOffsetBytes, msgSize, WebSocketOpCode.BINARY if isData else WebSocketOpCode.TEXT)


    # Interface function.
    def SendWithOptCode(self, buffer:Buffer, msgStartOffsetBytes:Optional[int]=None, msgSize:Optional[int]=None, optCode=WebSocketOpCode.BINARY) -> None:
        self._SendMessage(buffer, msgStartOffsetBytes, msgSize)


    # Not needed, but required by the interface.
    def SetDisableCertCheck(self, disable:bool) -> None:
        pass


    #
    # Connection logic.
    #

    # If called with an exception, this will fire on error and then close.
    def _InternalClose(self, exception:Optional[Exception]=None) -> None:
        # Check that we aren't already closed, we only allow close logic once.
        if self.IsClosed:
            return
        with self.StateLock:
            if self.IsClosed:
                return
            self.IsClosed = True

        # Run the close in a thread so we don't block whatever thread this was called on.
        def closeThread():
            # If we have an exception, fire the error callback.
            if exception is not None and self._onWsError is not None:
                try:
                    self.Logger.debug("%s firing error callback. Exception: %s", self._GetLogMsgStart(), exception)
                    self._onWsError(self, exception)
                except Exception as e:
                    self.Logger.error(f"{self._GetLogMsgStart()} Error in _onWsError callback: {e}")

            # If we have a client, ensure we clean it up.
            client:Optional[mqtt.Client] = None
            with self.StateLock:
                client = self.Client
                self.Client = None
            self._EnsureClientIsDisconnected(client)

            # Finally, fire on close.
            if self._onWsClose is not None:
                try:
                    self.Logger.debug("%s firing close callback.", self._GetLogMsgStart())
                    self._onWsClose(self)
                except Exception as e:
                    self.Logger.error(f"{self._GetLogMsgStart()}  Error in _onWsClose callback: {e}")
            self.Logger.debug("%s close complete.", self._GetLogMsgStart())
        threading.Thread(target=closeThread, name="MqttWebsocketProxyClose").start()


    # Ensures if we have a client, it's fully disconnected.
    def _EnsureClientIsDisconnected(self, client:Optional[mqtt.Client]):
        if client is None:
            return
        try:
            self.Logger.debug("%s disconnecting...", self._GetLogMsgStart())
            client.disconnect()
        except Exception as e:
            self.Logger.error(f"{self._GetLogMsgStart()} Error in client disconnect: {e}")
        self.Logger.info(f"{self._GetLogMsgStart()} disconnected.")
        try:
            client.loop_stop()
        except Exception as e:
            self.Logger.error(f"{self._GetLogMsgStart()} Error in client loop stop: {e}")


    # Sets up, runs, and maintains the MQTT connection.
    def _RunThread(self):
        try:
            # Wait for a connection context.
            # After the first successful connection, it will be set and then will always exist.
            connectionContext:Optional[ConnectionContext] = None
            attempt = 0
            while True:
                attempt += 1
                if attempt > 10:
                    raise Exception("{self._GetLogMsgStart()} Timed out waiting for a connection context.")
                connectionContext = BambuClient.Get().GetCurrentConnectionContext()
                if connectionContext is not None:
                    break
                time.sleep(1.5 * attempt)

            # If we have overrides, set them.
            connectionContext.UserName = self.UserNameOverride if self.UserNameOverride is not None else connectionContext.UserName
            connectionContext.AccessToken = self.AccessCodeOverride if self.AccessCodeOverride is not None else connectionContext.AccessToken

            # We always connect locally. We use encryption, but the printer doesn't have a trusted
            # cert root, so we have to disable the cert root checks.
            self.Client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2) #pyright: ignore[reportPrivateImportUsage]

            # Since we are local, we can do more aggressive reconnect logic.
            # The default is min=1 max=120 seconds.
            self.Client.reconnect_delay_set(min_delay=1, max_delay=5)

            # Setup the callback functions.
            self.Client.on_connect = self._OnConnect
            self.Client.on_message = self._OnMessage
            self.Client.on_disconnect = self._OnDisconnect
            self.Client.on_subscribe = self._OnSubscribe
            self.Client.on_unsubscribe = self._OnUnsubscribe
            self.Client.on_log = self._OnLog

            # Try to connect the client, this will throw if it fails.
            # We will fire on connected after mqtt tells us we are fully connected.
            self.Logger.info(f"{self._GetLogMsgStart()} connecting to {connectionContext.IpOrHostname}...")
            BambuClient.SetupAndConnectMqtt(self.Client, connectionContext)

            # This will run forever, including handling reconnects and such.
            self.Client.loop_forever()

        except Exception as e:
            self.Logger.error(f"{self._GetLogMsgStart()} Error in MQTT proxy main thread: {e}")
            # Close with the error.
            self._InternalClose(e)
        finally:
            # Ensure close is always called.
            self._InternalClose()


    # Fired when the MQTT connection is made.
    def _OnConnect(self, client:mqtt.Client, userdata:Any, flags:Any, reason_code:Any, properties:Any) -> None:
        self.Logger.debug("%s mqtt._OnConnect", self._GetLogMsgStart())

        # When we are fully connected, ensure we should still be connected!
        with self.StateLock:
            if self.IsClosed:
                self.Logger.info(f"{self._GetLogMsgStart()} connection closed before we mqtt was fully opened. We are disconnecting the mqtt client.")
                # We can't fire close internal, because it's already been ran. We just need to make sure this client is closed.
                self._EnsureClientIsDisconnected(self.Client)
                self.Client = None
                return

        # Fire the callback.
        if self._onWsOpen is not None:
            try:
                self.Logger.debug("%s firing open callback.", self._GetLogMsgStart())
                self._onWsOpen(self)
            except Exception as e:
                self.Logger.error(f"{self._GetLogMsgStart()} Error in _onWsOpen callback: {e}")


    # Fired when the MQTT connection is lost
    def _OnDisconnect(self, client:Any, userdata:Any, disconnect_flags:Any, reason_code:Any, properties:Any) -> None:
        self.Logger.debug("%s mqtt._OnDisconnect", self._GetLogMsgStart())

        # Close without any error.
        self._InternalClose()


    # Fired when the MQTT connection has something to log.
    def _OnLog(self, client:Any, userdata:Any, level:int, msg:str) -> None:
        if level == mqtt.MQTT_LOG_ERR:
            # If the string is something like "Caught exception in on_connect: ..."
            # It's a leaked exception from us.
            if "exception" in msg:
                Sentry.OnException(f"{self._GetLogMsgStart()} leaked exception.", Exception(msg))
            else:
                self.Logger.error(f"{self._GetLogMsgStart()} log error: {msg}")
        elif level == mqtt.MQTT_LOG_WARNING:
            # Report warnings.
            self.Logger.error(f"{self._GetLogMsgStart()} log warn: {msg}")
        # else:
        #     # Report everything else if debug is enabled.
        #     if self.Logger.isEnabledFor(logging.DEBUG):
        #         self.Logger.debug(f"{self._GetLogMsgStart()} log: {msg}")


    #
    # Message logic.
    #

    # We need to make sure any calls that will fire _OnSubscribe or _OnUnsubscribe are done and have sent the ack before we fire the callback.
    # Otherwise, the client can get a _OnSubscribe with a mid before the ack was sent.
    def _WaitForMidBlockAndGeProxyMessageId(self, mid:int) -> Optional[int]:
        attempt = 0
        while True:
            attempt += 1
            if attempt > 10:
                self.Logger.error(f"{self._GetLogMsgStart()} Timed out waiting for a mid.")
                return None
            # Check if we are clear.
            with self.MidAckLock:
                if self.IsMakingMidAckRequest is False:
                    return self.MidAckMap.get(mid, None)

            # Wait for just a bit to see if we are clear to send.
            time.sleep(20 * attempt)


    # Fired when the MQTT subscribe result has come back.
    def _OnSubscribe(self, client:Any, userdata:Any, mid:Any, reason_code_list:List[mqtt.ReasonCode], properties:Any): #pyright: ignore[reportPrivateImportUsage]
        proxyMessageId = self._WaitForMidBlockAndGeProxyMessageId(mid)
        self.Logger.debug("%s mqtt._OnSubscribe - %s", self._GetLogMsgStart(), mid)
        self._SendOutgoingMessage("on_subscribe", mqttMessageId=mid, reasonCodeList=reason_code_list, proxyMessageId=proxyMessageId)


    # Fired when the MQTT unsubscribe result has come back.
    def _OnUnsubscribe(self, client:Any, userdata:Any, mid:Any, reason_code_list:List[mqtt.ReasonCode], properties:Any): #pyright: ignore[reportPrivateImportUsage]
        proxyMessageId = self._WaitForMidBlockAndGeProxyMessageId(mid)
        self.Logger.debug("%s mqtt._OnUnsubscribe - %s", self._GetLogMsgStart(), mid)
        self._SendOutgoingMessage("on_unsubscribe", mqttMessageId=mid, reasonCodeList=reason_code_list, proxyMessageId=proxyMessageId)


    # Fired when there's an incoming MQTT message.
    def _OnMessage(self, client:Any, userdata:Any, mqttMsg:mqtt.MQTTMessage) -> None:
        self.Logger.debug("%s mqtt._OnMessage", self._GetLogMsgStart())
        self._SendOutgoingMessage("on_message", payload=mqttMsg.payload, mqttMessageId=mqttMsg.mid)


    def _SendOutgoingMessage(self,
                             proxyMessageType:str,                        # Our proxy messages type.
                             payload:Optional[bytes]=None,                # The MQTT payload buffer.
                             reasonCodeList:Optional[List[ReasonCode]]=None, # The MQTT reason code list.
                             mqttMessageId:Optional[int]=None,            # Used for acks, this is the mqtt message id
                             ackResult:Optional[MQTTErrorCode]=None,      # Used for acks, this is the result.
                             proxyMessageId:Optional[int]=None            # Used for acks, this is the client supplied message id.
                             ) -> None:
        try:

            # Build our object to return
            ret:Dict[str, Any] = {
                "Type": proxyMessageType
            }

            # Everything else is optional.
            if payload is not None:
                payloadStr = base64.b64encode(payload).decode("utf-8")
                ret["Payload"] = payloadStr
            if reasonCodeList is not None:
                ids:List[int] = []
                for i in reasonCodeList:
                    ids.append(i.value)
                ret["ReasonCodeList"] = ids
            if mqttMessageId is not None:
                ret["MqttMessageId"] = mqttMessageId
            if ackResult is not None:
                # As a bool
                ret["AckResult"] = ackResult == MQTTErrorCode.MQTT_ERR_SUCCESS
            if proxyMessageId is not None:
                ret["Id"] = proxyMessageId

            # Send it.
            jsonStr = json.dumps(ret)
            jsonBytes = jsonStr.encode("utf-8")
            if self._onWsData:
                self._onWsData(self, Buffer(jsonBytes), WebSocketOpCode.TEXT)

        except Exception as e:
            Sentry.OnException(f"{self._GetLogMsgStart()} Failed to send outgoing message.", e)
            self._InternalClose(e)
            return


    # Handles taking the incoming client websocket messages and sending them as mqtt commands.
    def _SendMessage(self, buffer:Buffer, msgStartOffsetBytes:Optional[int]=None, msgSize:Optional[int]=None) -> None:

        # Ensure we are still open.
        if self.IsClosed or self.Client is None:
            self.Logger.error(f"{self._GetLogMsgStart()} Can't send message, client is closed.")
            return

        # First, we need to get the string.
        msgStr:str = ""
        try:
            buf = buffer.GetBytesLike()
            if msgStartOffsetBytes is not None:
                buf = buf[msgStartOffsetBytes:]
            if msgSize is not None:
                buf = buf[:msgSize]
            msgStr = buf.decode("utf-8")
        except Exception as e:
            Sentry.OnException(f"{self._GetLogMsgStart()} Failed to parse incoming message.", e)
            self._InternalClose(e)
            return

        # Next, parse the json.
        msgJson:Dict[str, Any] = {}
        try:
            msgJson = json.loads(msgStr)
        except Exception as e:
            Sentry.OnException(f"{self._GetLogMsgStart()} Failed to parse incoming message json.", e)
            self._InternalClose(e)
            return

        # We will have our wrapper object around the MQTT json.
        # Note that this is a public API, so the wrapper can't change, it can only be added to.
        proxyMsgType = msgJson.get("Type", None)
        if proxyMsgType is None:
            self.Logger.error(f"{self._GetLogMsgStart()} Failed to parse incoming message. No Type.")
            self._InternalClose(Exception("No Type"))
            return
        proxyMsgTopic = msgJson.get("Topic", None)
        if proxyMsgTopic is None:
            self.Logger.error(f"{self._GetLogMsgStart()} Failed to parse incoming message. No Topic.")
            self._InternalClose(Exception("No Topic"))
            return

        # Clients can optionally send a message id, which we will reflect back in the acks.
        proxyMsgId = msgJson.get("Id", None)
        if proxyMsgId is not None:
            if isinstance(proxyMsgId, int) is False:
                self.Logger.error(f"{self._GetLogMsgStart()} Failed to parse incoming message. Id is not an int.")
                self._InternalClose(Exception("Id is not an int"))
                return
        # Optionally users can disable ack.
        proxyMsgNoAck = msgJson.get("NoAck", False)
        if proxyMsgNoAck is not None:
            if isinstance(proxyMsgNoAck, bool) is False:
                self.Logger.error(f"{self._GetLogMsgStart()} Failed to parse incoming message. NoAck is not an bool.")
                self._InternalClose(Exception("NoAck is not an bool"))
                return

        # A helper function to handle subscription and unsubscription requests.
        def _DoMidSubBlockingRequest(isSub:bool) -> None:
            mid:Optional[int] = None
            try:
                # Set that we are making this request, to prevent the on_* from calling back before we send our WS ack.
                with self.MidAckLock:
                    self.IsMakingMidAckRequest = True

                # Make the call
                if self.Client is None:
                    return
                result:Optional[MQTTErrorCode] = None
                if isSub:
                    (result, mid) = self.Client.subscribe(proxyMsgTopic)
                else:
                    (result, mid) = self.Client.unsubscribe(proxyMsgTopic)

                # Send back the ack
                if proxyMsgNoAck is False:
                    typeStr = "subscribe_ack" if isSub else "unsubscribe_ack"
                    self._SendOutgoingMessage(typeStr, ackResult=result, mqttMessageId=mid, proxyMessageId=proxyMsgId)

            finally:
                # Indicate we are no longer making the request and if we got a mid add it to the map.
                with self.MidAckLock:
                    self.IsMakingMidAckRequest = False
                    if mid is not None and proxyMsgId is not None:
                        self.MidAckMap[mid] = proxyMsgId

        # Get the data, but it's optional depending on the type.
        try:
            typeLower = proxyMsgType.lower()
            if typeLower == "subscribe":
                _DoMidSubBlockingRequest(True)

            elif typeLower == "unsubscribe":
                _DoMidSubBlockingRequest(False)

            elif typeLower == "publish":
                # Get the data if there is any.
                proxyMsgDataStr = msgJson.get("Payload", None)
                proxyMsgData:Optional[bytes] = None
                if proxyMsgDataStr is not None:
                    # Since the data is binary, we have to base 64 encode it.
                    if isinstance(proxyMsgDataStr, str) is False:
                        raise Exception("Payload must be a base64 encoded string.")
                    proxyMsgData = base64.b64decode(proxyMsgDataStr)

                # Send and ack the result.
                (info) = self.Client.publish(proxyMsgTopic, proxyMsgData)
                if proxyMsgNoAck is False:
                    self._SendOutgoingMessage("publish_ack", ackResult=info.rc, mqttMessageId=info.mid, proxyMessageId=proxyMsgId)
            else:
                raise Exception(f"{self._GetLogMsgStart()} Unknown proxy message type. {typeLower}")
        except Exception as e:
            Sentry.OnException(f"{self._GetLogMsgStart()} Failed to send message. {msgStr}", e)
            self._InternalClose(e)
            return


    # A logging helper.
    def _GetLogMsgStart(self) -> str:
        return f"MQTT PROXY [{self.StreamId}]"


# This class allows use to create a websocket builder which can take the args.
class MqttWebsocketProxyProviderBuilder(ICommandWebsocketProviderBuilder):

    def __init__(self, logger:logging.Logger):
        self.Logger = logger

    # This must return a provider or None on failure.
    def GetCommandWebsocketProvider(self, args:Optional[Dict[str, Any]]) -> Optional[ICommandWebsocketProvider]:
        return MqttWebsocketProxyProvider(self.Logger, args)


# This class uses to the args to create a MQTT proxy websocket class.
class MqttWebsocketProxyProvider(ICommandWebsocketProvider):

    def __init__(self, logger:logging.Logger, args:Optional[Dict[str, Any]]):
        self.Logger = logger
        self.Args = args
        self.Lock = threading.Lock()
        self.Id = 0

    # This must return a IWebSocketClient or None on failure.
    def GetWebsocketObject(self, streamId:int, path:str, pathType:int, context:HttpInitialContext,
                    onWsOpen:Optional[Callable[[IWebSocketClient], None]]=None,
                    onWsData:Optional[Callable[[IWebSocketClient, Buffer, WebSocketOpCode], None]]=None,
                    onWsClose:Optional[Callable[[IWebSocketClient], None]]=None,
                    onWsError:Optional[Callable[[IWebSocketClient, Exception], None]]=None,
                    headers:Optional[Dict[str, str]]=None,
                    subProtocolList:Optional[List[str]]=None) -> Optional[IWebSocketClient]:
        return MqttWebsocketProxy(self.Logger, self.Args, streamId, path, pathType, context,
                                  onWsOpen, onWsData, onWsClose, onWsError,
                                  headers, subProtocolList)
