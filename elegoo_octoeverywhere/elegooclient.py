import time
import json
import random
import string
import logging
import threading
import octowebsocket

from octoeverywhere.compat import Compat
from octoeverywhere.sentry import Sentry
from octoeverywhere.websocketimpl import Client
from octoeverywhere.octohttprequest import OctoHttpRequest

from linux_host.config import Config
from linux_host.networksearch import NetworkSearch

from .elegoomodels import PrinterState, PrinterAttributes

# The response object for a request message.
# Contains information on the state, and if successful, the result.
class ResponseMsg:

    # Printer errors
    # The only error we get back is if the ack value is 1
    ELEGOO_CMD_ERROR_GENERIC = 88880001
    # Our specific errors
    OE_ERROR_WS_NOT_CONNECTED = 99990001
    OE_ERROR_TIMEOUT = 99990002
    OE_ERROR_EXCEPTION = 99990003
    # Range helpers.
    OE_ERROR_MIN = OE_ERROR_WS_NOT_CONNECTED
    OE_ERROR_MAX = OE_ERROR_EXCEPTION

    def __init__(self, resultObj:dict, errorCode = 0, errorStr : str = None) -> None:
        self.Result = resultObj
        self.ErrorCode = errorCode
        self.ErrorStr = errorStr
        if self.ErrorCode == ResponseMsg.OE_ERROR_TIMEOUT:
            self.ErrorStr = "Timeout waiting for Elegoo Msg Response."
        if self.ErrorCode == ResponseMsg.OE_ERROR_WS_NOT_CONNECTED:
            self.ErrorStr = "No active websocket connected."
        if self.ErrorCode == ResponseMsg.ELEGOO_CMD_ERROR_GENERIC:
            self.ErrorStr = "Printer responded with a failed ack msg."

    def HasError(self) -> bool:
        return self.ErrorCode != 0

    def GetErrorCode(self) -> int:
        return self.ErrorCode

    def IsErrorCodeOeError(self) -> bool:
        return self.ErrorCode >= ResponseMsg.OE_ERROR_MIN and self.ErrorCode <= ResponseMsg.OE_ERROR_MAX

    def GetErrorStr(self) -> str:
        return self.ErrorStr

    def GetLoggingErrorStr(self) -> str:
        return str(self.ErrorCode) + " - " + str(self.ErrorStr)

    def GetResult(self) -> dict:
        return self.Result


# Responsible for connecting to and maintaining a connection to the Elegoo Printer.
class ElegooClient:

    # The max amount of time we will wait for a request before we timeout.
    RequestTimeoutSec = 10.0

    # Logic for a static singleton
    _Instance = None

    # If enabled, this prints all of the websocket messages sent and received.
    WebSocketMessageDebugging = False

    @staticmethod
    def Init(logger:logging.Logger, config:Config, pluginId, pluginVersion, stateTranslator, websocketMux, fileManger):
        ElegooClient._Instance = ElegooClient(logger, config, pluginId, pluginVersion, stateTranslator, websocketMux, fileManger)


    @staticmethod
    def Get():
        return ElegooClient._Instance


    def __init__(self, logger:logging.Logger, config:Config, pluginId:str, pluginVersion:str, stateTranslator, websocketMux, fileManger) -> None:
        self.Logger = logger
        self.Config = config
        self.PluginId = pluginId
        self.PluginVersion = pluginVersion
        self.StateTranslator = stateTranslator # ElegooStateTranslator
        self.WebsocketMux = websocketMux # ElegooWebsocketMux
        self.FileManger = fileManger # ElegooFileManager

        # Setup the request response system.
        self.RequestLock = threading.Lock()
        self.RequestPendingContexts = {}

        #
        # Websocket Vars
        #
        self.WebSocket:Client = None
        # Set when the websocket is connected and ready to send messages.
        self.WebSocketConnected = False
        # Set when the first attribute msg is received and we verified the mainboard id.
        self.WebSocketConnectFinalized = False
        # We use this var to keep track of consecutively failed connections
        self.ConsecutivelyFailedConnectionAttempts = 0
        # We use this var to keep track of how many connection attempts we have made since we did a search.
        self.ConsecutivelyFailedConnectionAttemptsSinceSearch = 0
        # Set if we know the last IP attempt was successful, but we failed bc there were too many clients.
        self.LastConnectionFailedDueToTooManyClients = False
        # Hold the IP we are currently connected to, so when it's successful, we can update the config.
        self.WebSocketConnectionIp:str = None
        # This is the event we will sleep on between connection attempts, which allows us to be poked to connect now.
        self.SleepEvent:threading.Event = threading.Event()

        # We keep track of the states locally so we know the delta between states and
        # So we don't have to ping the printer for every state change.
        # These will be None until we get the first messages!
        self.State:PrinterState = None
        self.Attributes:PrinterAttributes = None
        self._CleanupStateOnDisconnect()

        # Note that EITHER the IP address or mainboardID are required.
        # The docker container doesn't use the mainboard ID, since we can't network scan anyways.
        ipOrHostname = config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        self.MainboardId = config.GetStr(Config.SectionElegoo, Config.ElegooMainboardId, None)
        if ipOrHostname is None and self.MainboardId is None:
            raise Exception("An IP address or mainbaord IP must be provided in the config for Elegoo Connect.")

        # Get the port string.
        self.PortStr = config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, None)
        if self.PortStr is None:
            self.PortStr = NetworkSearch.c_ElegooDefaultPortStr

        # Set the IP we have (if any) and it will be updated later when the connection finalizes.
        if ipOrHostname is not None and len(ipOrHostname) > 0:
            OctoHttpRequest.SetLocalHostAddress(ipOrHostname)
        # Set the main server port as first, then the proxy port.
        OctoHttpRequest.SetLocalOctoPrintPort(int(self.PortStr))
        OctoHttpRequest.SetLocalHttpProxyIsHttps(False)
        OctoHttpRequest.SetLocalHttpProxyPort(80)

        # Start the client worker thread.
        t = threading.Thread(target=self._ClientWorker)
        t.start()


    # Returns the local printer state object with the most up-to-date information.
    # Returns None if the printer is not connected or the state is unknown.
    def GetState(self) -> PrinterState:
        if self.State is None:
            # Set the sleep event, so if the socket is waiting to reconnect, it will wake up and try again.
            self.SleepEvent.set()
            return None
        return self.State


    # Returns the local printer attributes object with the most up-to-date information.
    # Returns None if the printer is not connected or the attributes is unknown.
    def GetAttributes(self, waitForResponse:bool=True) -> PrinterAttributes:
        return self.Attributes


    # Indicates if the websocket is connected and ready to send messages.
    def IsWebsocketConnected(self) -> bool:
        if self.WebSocketConnected is False:
            # Set the sleep event, so if the socket is waiting to reconnect, it will wake up and try again.
            self.SleepEvent.set()
        return self.WebSocketConnected


    # Indicates if the last connection attempt failed due to too many clients.
    def IsDisconnectDueToTooManyClients(self) -> bool:
        return self.LastConnectionFailedDueToTooManyClients


    # Sends a command to the printer to enable the webcam.
    def SendEnableWebcamCommand(self, waitForResponse:bool=True) -> ResponseMsg:
        return ElegooClient.Get().SendRequest(386, {"Enable":1}, waitForResponse=waitForResponse)


    # Sends a command to all connected frontends to show a popup message.
    def SendFrontendPopupMsg(self, title:str, text:str, msgType:str, actionText:str, actionLink:str, showForSec:int, onlyShowIfLoadedViaOeBool:bool) -> None:
        data = {
            "Notification": {
                "title": title,
                "text": text,
                "msg_type": msgType,
                "action_text": actionText,
                "action_link": actionLink,
                "show_for_sec": showForSec,
                "only_show_if_loaded_via_oe": onlyShowIfLoadedViaOeBool
            }
        }
        self._SendMuxFrontendMessage(data)


    # Sends a request to the printer and waits for a response.
    # Always returns a ResponseMsg, with various error codes.
    def SendRequest(self, cmdId:int, data:dict=None, waitForResponse:bool=True, timeoutSec:float=None) -> ResponseMsg:
        # Generate a request id, which is a 32 char lowercase letter and number string
        requestId = ''.join(random.choices(string.ascii_lowercase + string.digits, k=32))

        # The requests always have a empty data dict if there's nothing.
        if data is None:
            data = {}

        # Create our waiting context.
        waitContext = None
        with self.RequestLock:
            waitContext = MsgWaitingContext(requestId)
            self.RequestPendingContexts[requestId] = waitContext

        # From now on, we need to always make sure to clean up the wait context, even in error.
        try:
            # Create the request object
            obj = {
                "Id": "",
                "Data":
                {
                    "Cmd": cmdId,
                    "Data": data,
                    "From": 1, # Not sure what this is, but 1 works.
                    "MainboardId": "",
                    "RequestId": requestId,
                    "TimeStamp": int(time.time())
                }
            }

            # Try to send. default=str makes the json dump use the str function if it fails to serialize something.
            jsonStr = json.dumps(obj, default=str)
            if ElegooClient.WebSocketMessageDebugging and self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug("Elegoo WS Msg Request - %s : %s : %s", str(requestId), str(cmdId), jsonStr)
            if self._WebSocketSend(jsonStr) is False:
                self.Logger.info("Elegoo client failed to send request msg.")
                return ResponseMsg(None, ResponseMsg.OE_ERROR_WS_NOT_CONNECTED)

            # If we don't need to wait for a response, return now.
            if waitForResponse is False:
                return ResponseMsg(None)

            # Wait for a response
            if timeoutSec is None:
                timeoutSec = ElegooClient.RequestTimeoutSec
            waitContext.GetEvent().wait(timeoutSec)

            # Check if we got a result.
            result = waitContext.GetResult()
            if result is None:
                self.Logger.info(f"Elegoo client timeout while waiting for request. {requestId}")
                return ResponseMsg(None, ResponseMsg.OE_ERROR_TIMEOUT)

            # Handle the one common way commands can fail.
            data = result.get("Data", None)
            if data is not None:
                innerData = data.get("Data", None)
                if innerData is not None:
                    ack = innerData.get("Ack", None)
                    if ack is not None and ack == 1:
                        self.Logger.info("Elegoo client received an ack message, but no data.")
                        # Return the result still, but also indicate there was an error.
                        return ResponseMsg(result, ResponseMsg.ELEGOO_CMD_ERROR_GENERIC)

            # Success!
            return ResponseMsg(result)

        except Exception as e:
            Sentry.Exception("Moonraker client json rpc request failed to send.", e)
            return ResponseMsg(None, ResponseMsg.OE_ERROR_EXCEPTION, str(e))

        finally:
            # Before leaving, always clean up any waiting contexts.
            with self.RequestLock:
                if requestId in self.RequestPendingContexts:
                    del self.RequestPendingContexts[requestId]


    # Sends a string to the connected websocket.
    # forceSend is used to send the initial messages before the system is ready.
    def _WebSocketSend(self, jsonStr:str) -> bool:
        # Ensure the websocket is connected and ready.
        if self.WebSocketConnected is False:
            self.Logger.info("Elegoo client - tired to send a websocket message when the socket wasn't open.")
            # Set the sleep event, so if the socket is waiting to reconnect, it will wake up and try again.
            self.SleepEvent.set()
            return False
        localWs = self.WebSocket
        if localWs is None:
            self.Logger.info("Elegoo client - tired to send a websocket message before the websocket was created.")
            return False

        # Print for debugging.
        if ElegooClient.WebSocketMessageDebugging and self.Logger.isEnabledFor(logging.DEBUG):
            self.Logger.debug("Ws ->: %s", jsonStr)

        try:
            # Since we must encode the data, which will create a copy, we might as well just send the buffer as normal,
            # without adding the extra space for the header. We can add the header here or in the WS lib, it's the same amount of work.
            localWs.Send(jsonStr.encode("utf-8"), isData=False)
        except Exception as e:
            Sentry.Exception("Elegoo client exception in websocket send.", e)
            return False
        return True


    # Sets up, runs, and maintains the websocket connection.
    def _ClientWorker(self):
        isConnectAttemptFromEventBump = False
        while True:
            try:
                # Clear the connection flags
                self.WebSocketConnected = False
                self.WebSocketConnectFinalized = False

                # Get the current IP we want to try to connect with.
                self.WebSocketConnectionIp = self._GetIpForConnectionAttempt(isConnectAttemptFromEventBump)

                # Build the connection URL
                url = f"ws://{self.WebSocketConnectionIp}:{self.PortStr}/websocket"

                # Setup the websocket client for this connection.
                self.WebSocket = Client(url, onWsOpen=self._OnWsConnect, onWsClose=self._OnWsClose, onWsError=self._OnWsError, onWsData=self._OnWsData)

                # Connect to the server
                with self.WebSocket:
                    # Use a more aggressive ping timeout because if the printer power cycles, we don't get the TCP close message.
                    # Time ping timeout must be less than the ping interval.
                    self.WebSocket.RunUntilClosed(pingIntervalSec=30)
            except Exception as e:
                Sentry.Exception("Elegoo client exception in main WS loop.", e)

            # Sleep for a bit between tries.
            # The main consideration here is to not log too much when the printer is off. But we do still want to connect quickly, when it's back on.
            # Note that the system might also do a printer scan after many failed attempts, which can be CPU intensive.
            #
            # Since we now have the sleep event, we can sleep longer, because when something attempts to use the socket, the event will wake us up
            # to try a connection again. So, for example, when the user goes to the OE dashboard, the status check will wake us up.
            #
            # So right now, the max sleep time is 5 minutes.
            sleepDelay = self.ConsecutivelyFailedConnectionAttempts
            sleepDelay = min(sleepDelay, 60)
            sleepDelaySec = 5.0 * sleepDelay
            self.Logger.info(f"Sleeping for {sleepDelaySec} seconds before trying to reconnect to the Elegoo printer.")
            # Sleep for the time or until the event is set.
            isConnectAttemptFromEventBump = self.SleepEvent.wait(sleepDelaySec)
            self.SleepEvent.clear()


    # Fired whenever the client is disconnected, we need to clean up the state since it's now unknown.
    def _CleanupStateOnDisconnect(self):
        self.State = None
        self.Attributes = None
        self.WebSocketConnected = False
        self.WebSocketConnectFinalized = None
        self.WebSocketConnectionIp = None


    # Fired when the websocket is connected.
    def _OnWsConnect(self, ws:Client):
        self.Logger.info("Connection to the Elegoo printer established!")

        # Set the connected flag now, so we can send messages.
        self.WebSocketConnected = True

        # Reset the failed connection attempts.
        self.ConsecutivelyFailedConnectionAttempts = 0
        self.ConsecutivelyFailedConnectionAttemptsSinceSearch = 0
        self.LastConnectionFailedDueToTooManyClients = False

        # On connect, we need to request the status and attributes.
        # Important, we can't wait for for the response or will deadlock.
        # Cmd 0 is get state, it can be called at anytime.
        # Cmd 1 is get attributes, it can be called at anytime.
        # These will return ack messages parred with the request id, and then the actual result will come as an unsolicited message.
        self.SendRequest(0, waitForResponse=False)
        self.SendRequest(1, waitForResponse=False)


    # Fired when the websocket is closed.
    def _OnWsClose(self, ws:Client):
        # Don't log this if we already know its due to too many clients.
        if self.LastConnectionFailedDueToTooManyClients is False:
            self.Logger.debug("Elegoo printer connection lost. We will try to reconnect in a few seconds.")

        # Clear any pending requests.
        with self.RequestLock:
            for _, v in self.RequestPendingContexts.items():
                v.SetSocketClosed()

        # Grab if we were fully connected before the state cleanup.
        wasFullyConnected = self.WebSocketConnectFinalized

        # Clean up the state.
        self._CleanupStateOnDisconnect()

        # Report the connection was lost.
        self.StateTranslator.OnConnectionLost(wasFullyConnected)


    # Fired when the websocket is closed.
    def _OnWsError(self, ws:Client, e:Exception):
        # There's a special case here where the Elegoo printers can have a limited number of connections.
        # When that happens, we want to note it so we don't just keep trying the same IP over and over.
        msg = str(e)
        if msg.lower().find("too many client") >= 0:
            self.LastConnectionFailedDueToTooManyClients = True
            self.Logger.warning("Elegoo printer connection failed due to too many already connected clients.")
        else:
            self.LastConnectionFailedDueToTooManyClients = False
            Sentry.Exception("Elegoo printer websocket error.", e)


    # Fired when the websocket is closed.
    def _OnWsData(self, ws:Client, buffer:bytearray, msgType):
        try:
            # Try to deserialize the message.
            msg = json.loads(buffer.decode("utf-8"))
            if msg is None:
                raise Exception("Parsed json message returned None")

            # Print for debugging if desired.
            if ElegooClient.WebSocketMessageDebugging and self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug("Incoming Elegoo Message:\r\n"+json.dumps(msg, indent=3))

            # If set, this message should be sent to all mux sockets.
            # This is the default behavior, because worst case, sending responses that aren't matched is fine.
            sendToAllMuxSockets = True
            # If set, this message should be sent to a specific mux socket.
            sendToMuxSocketId = None
            try:
                #
                # Handle unsolicited messages first, to ensure the states always get updated.
                #

                # Handle state updates.
                status = msg.get("Status", None)
                if status is not None:
                    self._HandleStatusUpdate(status)
                    return

                # Handle attributes updates.
                attributes = msg.get("Attributes", None)
                if attributes is not None:
                    self._HandleAttributesUpdate(attributes)
                    return

                # Check for a waiting request context.
                # If there is a pending context, give the message to it and we are done.
                data = msg.get("Data", None)
                if data is not None:
                    requestId = data.get("RequestID", None)
                    if requestId is not None:
                        with self.RequestLock:
                            context = self.RequestPendingContexts.get(requestId, None)
                            if context is not None:
                                # If the WsId is none, this is a local pending request.
                                if context.WsId is None:
                                    # We shouldn't send this to mux sockets, since we got a local response.
                                    sendToAllMuxSockets = False
                                    context.SetResultAndEvent(msg)
                                else:
                                    # We shouldn't send this to all mux sockets, since we got a mux response.
                                    # But we do need to send it to the one that requested it.
                                    sendToAllMuxSockets = False
                                    sendToMuxSocketId = context.WsId

                                    # Clean up the context for this message.
                                    del self.RequestPendingContexts[requestId]
                                return
            finally:
                # Once the message has been handled locally, we can send it to the mux sockets if needed.
                # Check if there's one mux socket to send to first, if so send it there and be done.
                if sendToMuxSocketId is not None:
                    self.WebsocketMux.OnIncomingMessage(sendToMuxSocketId, buffer, msgType)
                # Otherwise, see if we should send to all mux sockets.
                elif sendToAllMuxSockets:
                    self.WebsocketMux.OnIncomingMessage(None, buffer, msgType)

        except Exception as e:
            Sentry.Exception("Failed to handle incoming Elegoo message.", e)


    def _HandleAttributesUpdate(self, attributes:dict):
        # First update the attributes object.
        try:
            if self.Attributes is None:
                # Build the object before we set it.
                s = PrinterAttributes(self.Logger)
                s.OnUpdate(attributes)
                self.Attributes = s
                self.Logger.info("Elegoo printer attributes object created.")
            else:
                self.Attributes.OnUpdate(attributes)
        except Exception as e:
            Sentry.Exception("Failed to update printer attributes object", e)

        # We only need to handle the finalize once.
        if self.WebSocketConnectFinalized is True:
            return

        # Try to get the mainbaord id
        if self.Attributes.MainboardId is None:
            return

        # If we have a mainboard ID, we can now finalize the connection.
        if self.MainboardId != self.Attributes.MainboardId:
            self.Logger.error(f"Elegoo Mainboard ID mismatch. Expected: {self.MainboardId} Got: {self.Attributes.MainboardId}")
            return

        # Now that we are fully connected, set the successful IP in the config and the relay
        self.WebSocketConnectFinalized = True
        wsConIp = self.WebSocketConnectionIp
        if wsConIp is None:
            self.Logger.error("Elegoo client finalized but we don't have a websocket IP?")
            return
        OctoHttpRequest.SetLocalHostAddress(wsConIp)
        self.Config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, wsConIp)
        self.Logger.info("Elegoo client connection finalized.")

        # Kick off the file manager to sync.
        self.FileManger.Sync()

        # Kick off the slipstream cache
        if Compat.HasSlipstream():
            Compat.GetSlipstream().UpdateCache()


    def _HandleStatusUpdate(self, status:dict):
        # First update the state object.
        isFirstStateUpdate = self.State is None
        try:
            if self.State is None:
                # Build the object before we set it.
                s = PrinterState(self.Logger)
                s.OnUpdate(status)
                self.State = s
                self.Logger.info("Elegoo printer state object created.")
            else:
                self.State.OnUpdate(status)
        except Exception as e:
            Sentry.Exception("Failed to update printer states object", e)

        # After the state is updated, invoke the state translator.
        self.StateTranslator.OnStatusUpdate(self.State, isFirstStateUpdate)


    # Returns the IP for the next connection attempt
    def _GetIpForConnectionAttempt(self, isConnectAttemptFromEventBump:bool) -> str:
        # Always increment the failed attempts - no matter the reason.
        self.ConsecutivelyFailedConnectionAttempts += 1

        # Get our vars.
        configIpOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        hasMainboardId = self.MainboardId is not None and len(self.MainboardId) > 0
        hasConfigIp = configIpOrHostname is not None and len(configIpOrHostname) > 0

        # Sanity check we have what we need.
        if hasMainboardId is False and hasConfigIp is False:
            raise Exception("An IP address or mainbaord IP must be provided in the config for Elegoo Connect.")

        # If the last attempt was successful but it failed due to too many clients, we will try the same IP again.
        # We should always have an ip in the config, because we save it even though the connection failed.
        if self.LastConnectionFailedDueToTooManyClients:
            if hasConfigIp:
                return configIpOrHostname

        # If the mainboard id is None, we can only ever user the config IP.
        # TODO - We could scan in the docker container if we have an old IP, but we don't do that now.
        if hasMainboardId is False:
            return configIpOrHostname

        # Don't bump this for event based reconnect attempts, since they can happen often.
        if isConnectAttemptFromEventBump is False:
            self.ConsecutivelyFailedConnectionAttemptsSinceSearch += 1

        # If we have a mainboard ID, we can scan for the printer on the local network.
        # But we only want to do this every now an then due to the CPU load.
        doPrinterSearch = False
        if self.ConsecutivelyFailedConnectionAttemptsSinceSearch > 15:
            self.ConsecutivelyFailedConnectionAttemptsSinceSearch = 0
            doPrinterSearch = True

        # On the first few attempts, use the expected IP.
        # Every time we reset the count, we will try a network scan to see if we can find the printer guessing it's IP might have changed.
        # The IP can be empty, like if the docker container is used, in which case we should always search for the printer.
        if doPrinterSearch is False and hasConfigIp is True:
            return configIpOrHostname

        # If we fail too many times, try to scan for the printer on the local subnet, the IP could have changed.
        # Since we 100% identify the printer by the mainboard ID, we can scan for it..
        # Note we don't want to do this too often since it's CPU intensive and the printer might just be off.
        # We use a lower thread count and delay before each action to reduce the required load.
        # Using this config, it takes about 30 seconds to scan for the printer.
        self.Logger.info(f"Searching for your Elegoo printer {self.MainboardId}")
        results = NetworkSearch.ScanForInstances_Elegoo(self.Logger, mainboardId=self.MainboardId, threadCount=25, delaySec=0.2)

        # Handle the results.
        if results is None or len(results) == 0:
            if hasConfigIp:
                self.Logger.info("Failed to find the Elegoo printer on the local network, using the existing IP.")
                return configIpOrHostname
            self.Logger.error("Failed to find the Elegoo printer on the local network and we have no known IP.")
            return None

        # If we get an IP back, it is the printer.
        # The scan above will only return an IP if the printer was successfully connected to, logged into, and fully authorized with the Access Token and Printer SN.
        if len(results) == 1:
            # Since we know this is the IP, we will update it in the config. This mean in the future we will use this IP directly
            # And everything else trying to connect to the printer (webcam and ftp) will use the correct IP.
            ip = results[0].Ip
            self.Logger.info(f"We found a new IP for this printer. [{configIpOrHostname} -> {ip}] Updating the config and using it to connect.")
            self.Config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, ip)
            return ip

        # If we don't find anything, just use the config IP.
        return configIpOrHostname

    #
    # APIs for ElegooWebsocketMux
    #


    # This sends our special OE message out through all of the mux websockets to the frontend.
    def _SendMuxFrontendMessage(self, data:dict=None) -> None:
        # No data is fine, it just means we are sending a message with the plugin id and version.
        if data is None:
            data = {}

        # Always include the plugin id and version.
        data["PluginId"] = self.PluginId
        data["PluginVersion"] = self.PluginVersion

        # The outside object needs to be a valid response object, so the Elegoo frontend can parse it and ignore it.
        # Create the request object
        obj = {
            # This ID is just a random 32 char string.
            "Id": ''.join(random.choices(string.ascii_lowercase + string.digits, k=32)),
            # This topic field must exist, but it's what's used to match the message type.
            # So far it looks like defining our own type here is ideal, because then it's ignored.
            # This can't change, because the frontend is looking for this exact string.
            "Topic": "sdcp/octoeverywhere-frontend-msg",
            "Data": data
        }

        # Serialize and send to all of the active mux sockets.
        # Try to send. default=str makes the json dump use the str function if it fails to serialize something.
        jsonStr = json.dumps(obj, default=str).encode("utf-8")
        self.WebsocketMux.OnIncomingMessage(None, jsonStr, octowebsocket.ABNF.OPCODE_TEXT)


    # Called by ElegooWebsocketMux when a mux client sends a message.
    # This returns true if the message was sent, false on failure.
    def MuxSendMessage(self, wsId:int, buffer:bytearray, msgStartOffsetBytes:int, msgSize:int, optCode) -> bool:
        try:
            # We only handle text messages right now
            if optCode != octowebsocket.ABNF.OPCODE_TEXT:
                raise Exception(f"Elegoo client only supports text messages. We got: {octowebsocket.ABNF.OPCODE_TEXT}")

            # Trim the buffer if needed.
            needsToTrim = False
            startTrim = 0
            endTrim = len(buffer)
            if msgStartOffsetBytes is not None and msgStartOffsetBytes != 0:
                startTrim = msgStartOffsetBytes
                needsToTrim = True
            if msgSize is not None and msgSize != len(buffer):
                endTrim = msgSize
                needsToTrim = True
            if needsToTrim:
                buffer = buffer[startTrim:endTrim]

            # For us to be able to map messages back, we need to be able to read the request id if there is one.
            # So if this fails, we can't handle the message.
            msgStr = buffer.decode("utf-8")
            msg = json.loads(msgStr)

            # Try to get the data object and the request id.
            # If it doesn't, we will just send it.
            data = msg.get("Data", None)
            if data is not None:
                requestId = data.get("RequestID", None)
                if requestId is not None:
                    # We have a request id, validate it.
                    if len(requestId) < 20:
                        raise Exception(f"Invalid request id length: {len(requestId)}")
                    # Add it to the pending list.
                    with self.RequestLock:
                        self.RequestPendingContexts[requestId] = MsgWaitingContext(requestId, wsId)

            # Send the message.
            return self._WebSocketSend(msgStr)

        except Exception as e:
            Sentry.Exception("Elegoo client exception in MuxSendMessage.", e)
            return False


    # Called by ElegooWebsocketMux when a mux client is fully opened and can send messages.
    def MuxWebsocketOpened(self, wsId:int) -> None:
        # When a new mux connects, we want to send a frontend message so share the plugin id and version string.
        self._SendMuxFrontendMessage()


    # Called by ElegooWebsocketMux when a mux client closes.
    def MuxWebsocketClosed(self, wsId:int) -> None:
        # Cleanup any pending requests for this websocket.
        with self.RequestLock:
            toDelete = []
            for k, v in self.RequestPendingContexts.items():
                if v.WsId == wsId:
                    toDelete.append(k)
            for k in toDelete:
                del self.RequestPendingContexts[k]


# A helper class used for waiting msg requests
class MsgWaitingContext:

    # If WsId is set, this message is for a specific mux websocket.
    # When the response is received, the result is set and the event is triggered.
    def __init__(self, msgId:str, wsId:int=None) -> None:
        self.Id = msgId
        self.WsId = wsId
        self.WaitEvent = threading.Event()
        self.Result:dict = None


    def GetEvent(self) -> threading.Event:
        return self.WaitEvent


    def GetResult(self) -> dict:
        return self.Result


    def SetResultAndEvent(self, result:dict) -> None:
        if self.WsId is not None:
            raise Exception("This context is not for a local request.")
        self.Result = result
        self.WaitEvent.set()


    def SetSocketClosed(self) -> None:
        self.Result = None
        self.WaitEvent.set()
