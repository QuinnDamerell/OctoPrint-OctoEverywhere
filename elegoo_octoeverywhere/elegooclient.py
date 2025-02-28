import time
import json
import random
import string
import logging
import threading

from octoeverywhere.sentry import Sentry
from octoeverywhere.websocketimpl import Client
from octoeverywhere.octohttprequest import OctoHttpRequest

from linux_host.config import Config
from linux_host.networksearch import NetworkSearch


# The response object for a request message.
# Contains information on the state, and if successful, the result.
class ResponseMsg:

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
            self.ErrorStr = "Timeout waiting for RPC response."
        if self.ErrorCode == ResponseMsg.OE_ERROR_WS_NOT_CONNECTED:
            self.ErrorStr = "No active websocket connected."

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
    WebSocketMessageDebugging = True

    @staticmethod
    def Init(logger:logging.Logger, config:Config, stateTranslator):
        ElegooClient._Instance = ElegooClient(logger, config, stateTranslator)


    @staticmethod
    def Get():
        return ElegooClient._Instance


    def __init__(self, logger:logging.Logger, config:Config, stateTranslator) -> None:
        self.Logger = logger
        self.Config = config
        self.StateTranslator = stateTranslator # ElegooStateTranslator

        # Setup the request response system.
        self.RequestLock = threading.Lock()
        self.RequestPendingContexts = {}

        # Setup the websocket vars
        self.WebSocket:Client = None
        self.WebSocketConnected = False
        # We use this var to keep track of consecutively failed connections
        self.ConsecutivelyFailedConnectionAttempts = 0
        # Set if we know the last IP attempt was successful, but we failed bc there were too many clients.
        self.LastConnectionFailedDueToTooManyClients = False
        # Hold the IP we are currently connected to, so when it's successful, we can update the config.
        self.WebSocketConnectionIp:str = None

        # Get the port string.
        self.PortStr  = config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, None)
        if self.PortStr is None:
            self.PortStr = NetworkSearch.c_ElegooDefaultPortStr

        # Use the direct web server port as the main relay port, and 80 as a fallback.
        OctoHttpRequest.SetLocalOctoPrintPort(int(self.PortStr))
        OctoHttpRequest.SetLocalHttpProxyIsHttps(False)
        OctoHttpRequest.SetLocalHttpProxyPort(80)

        # Note that EITHER the IP address or mainboardID are required.
        # The docker container doesn't use the mainboard ID, since we can't network scan anyways.
        ipOrHostname = config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        self.MainboardId = config.GetStr(Config.SectionElegoo, Config.ElegooMainboardId, None)
        if ipOrHostname is None and self.MainboardId is None:
            raise Exception("An IP address or mainbaord IP must be provided in the config for Elegoo Connect.")

        # Start the client worker thread.
        t = threading.Thread(target=self._ClientWorker)
        t.start()


    # Sends a request to the printer and waits for a response.
    # Always returns a ResponseMsg, with various error codes.
    def SendRequest(self, cmdId:int, data:dict=None, waitForResponse:bool=True) -> ResponseMsg:

        # Generate a request id, which is a 32 char lowercase letter and number string
        requestId = ''.join(random.choices(string.ascii_lowercase + string.digits, k=32))

        # The messages always have a empty data dict if there's nothing.
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
            self.Logger.debug("Elegoo WS Msg Request - %s : %s : %s", str(requestId), str(cmdId), jsonStr)
            if self._WebSocketSend(jsonStr) is False:
                self.Logger.info("Elegoo client failed to send request msg.")
                return ResponseMsg(None, ResponseMsg.OE_ERROR_WS_NOT_CONNECTED)

            # If we don't need to wait for a response, return now.
            if waitForResponse is False:
                return ResponseMsg(None)

            # Wait for a response
            waitContext.GetEvent().wait(ElegooClient.RequestTimeoutSec)

            # Check if we got a result.
            result = waitContext.GetResult()
            if result is None:
                self.Logger.info(f"Elegoo client timeout while waiting for request. {requestId}")
                return ResponseMsg(None, ResponseMsg.OE_ERROR_TIMEOUT)

            # Check for an error if found, return the error state.
            # TODO - finsih!
            if "error" in result:
                # Get the error parts
                errorCode = ResponseMsg.OE_ERROR_EXCEPTION
                errorStr = "Unknown"
                if "code" in result["error"]:
                    errorCode = result["error"]["code"]
                if "message" in result["error"]:
                    errorStr = result["error"]["message"]
                return ResponseMsg(None, errorCode, errorStr)

            # If there's a result, return the entire response
            if "result" in result:
                return ResponseMsg(result["result"])

            # Finally, both are missing?
            self.Logger.error("Elegoo client json rpc got a response that didn't have an error or result object? "+json.dumps(result))
            return ResponseMsg(None, ResponseMsg.OE_ERROR_EXCEPTION, "No result or error object")

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



#     # Sends the pause command, returns is the send was successful or not.
#     def SendPause(self) -> bool:
#         return self._Publish({"print": {"sequence_id": "0", "command": "pause"}})


#     # Sends the resume command, returns is the send was successful or not.
#     def SendResume(self) -> bool:
#         return self._Publish({"print": {"sequence_id": "0", "command": "resume"}})


#     # Sends the cancel (stop) command, returns is the send was successful or not.
#     def SendCancel(self) -> bool:
#         return self._Publish({"print": {"sequence_id": "0", "command": "stop"}})


    # Sets up, runs, and maintains the websocket connection.
    def _ClientWorker(self):
        while True:
            try:
                # Clear the connection flag
                self.WebSocketConnected = False

                # Get the current IP we want to try to connect with.
                self.WebSocketConnectionIp = self._GetIpForConnectionAttempt()

                # Build the connection URL
                url = f"ws://{self.WebSocketConnectionIp}:{self.PortStr}/websocket"

                # Setup the websocket client for this connection.
                self.WebSocket = Client(url, onWsOpen=self._OnWsConnect, onWsClose=self._OnWsClose, onWsError=self._OnWsError, onWsData=self._OnWsData)

                # Connect to the server
                with self.WebSocket:
                    self.WebSocket.RunUntilClosed()
            except Exception as e:
                Sentry.Exception("Elegoo client exception in main WS loop.", e)

            # Inform that we lost the connection.
            self.Logger.info("Elegoo client websocket connection lost. We will try to restart it soon.")

            # Sleep for a bit between tries.
            # The main consideration here is to not log too much when the printer is off. But we do still want to connect quickly, when it's back on.
            # Note that the system might also do a printer scan after many failed attempts, which can be CPU intensive.
            # Right now we allow it to ramp up to 30 seconds between retries.
            time.sleep(3.0 * self.ConsecutivelyFailedConnectionAttempts)


    # Fired when the websocket is connected.
    def _OnWsConnect(self, ws:Client):
        self.Logger.info("Connection to the Elegoo printer established!")
        self.WebSocketConnected = True
                        # We set the defaults here, but the ElegooClient will update them if needed.
        # TODO move these
        #OctoHttpRequest.SetLocalHostAddress("10.0.0.101")


    # Fired when the websocket is closed.
    def _OnWsClose(self, ws:Client):
        self.Logger.warn("Elegoo printer connection lost. We will try to reconnect in a few seconds.")


    # Fired when the websocket is closed.
    def _OnWsError(self, ws:Client, e:Exception):
        Sentry.Exception("Elegoo printer websocket error.", e)


    # Fired when the websocket is closed.
    def _OnWsData(self, ws:Client, buffer:bytearray, msgType):
        self.Logger.warn("Elegoo printer connection lost. We will try to reconnect in a few seconds.")


#     # Fired when there's an incoming MQTT message.
#     def _OnMessage(self, client, userdata, mqttMsg:mqtt.MQTTMessage):
#         try:
#             # Try to deserialize the message.
#             msg = json.loads(mqttMsg.payload)
#             if msg is None:
#                 raise Exception("Parsed json MQTT message returned None")

#             # Print for debugging if desired.
#             if BambuClient._PrintMQTTMessages and self.Logger.isEnabledFor(logging.DEBUG):
#                 self.Logger.debug("Incoming Bambu Message:\r\n"+json.dumps(msg, indent=3))

#             # Since we keep a track of the state locally from the partial updates, we need to feed all updates to our state object.
#             isFirstFullSyncResponse = False
#             if "print" in msg:
#                 printMsg = msg["print"]
#                 try:
#                     if self.State is None:
#                         # Build the object before we set it.
#                         s = BambuState()
#                         s.OnUpdate(printMsg)
#                         self.State = s
#                     else:
#                         self.State.OnUpdate(printMsg)
#                 except Exception as e:
#                     Sentry.Exception("Exception calling BambuState.OnUpdate", e)

#                 # Try to detect if this is the response to the first full sync request.
#                 if self.HasDoneFirstFullStateSync is False:
#                     # First make sure the command is the push status.
#                     cmd = printMsg.get("command", None)
#                     if cmd is not None and cmd == "push_status":
#                         # We dont have a 100% great way to know if this is a fully sync message.
#                         # For now, we use this stat. The message we get from a P1P has 59 members in the root, so we use 40 as mark.
#                         # Note we use this same value in NetworkSearch.ValidateConnection_Bambu
#                         if len(printMsg) > 40:
#                             isFirstFullSyncResponse = True
#                             self.HasDoneFirstFullStateSync = True

#             # Update the version info if sent.
#             if "info" in msg:
#                 try:
#                     if self.Version is None:
#                         # Build the object before we set it.
#                         s = BambuVersion(self.Logger)
#                         s.OnUpdate(msg["info"])
#                         self.Version = s
#                     else:
#                         self.Version.OnUpdate(msg["info"])
#                 except Exception as e:
#                     Sentry.Exception("Exception calling BambuVersion.OnUpdate", e)

#             # Send all messages to the state translator
#             # This must happen AFTER we update the State object, so it's current.
#             try:
#                 # Only send the message along if there's a state. This can happen if a push_status isn't the first message we receive.
#                 if self.State is not None:
#                     self.StateTranslator.OnMqttMessage(msg, self.State, isFirstFullSyncResponse)
#             except Exception as e:
#                 Sentry.Exception("Exception calling StateTranslator.OnMqttMessage", e)

#         except Exception as e:
#             Sentry.Exception(f"Failed to handle incoming mqtt message. {mqttMsg.payload}", e)


    # Publishes a message and blocks until it knows if the message send was successful or not.
    def _Publish(self, msg:dict) -> bool:
        try:
            # Print for debugging if desired.
            if self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug("Incoming Bambu Message:\r\n"+json.dumps(msg, indent=3))

            # Ensure we are connected.
            if self.Client is None or not self.Client.is_connected():
                self.Logger.info("Failed to publish command because we aren't connected.")
                return False

            # Try to publish.
            state = self.Client.publish(f"device/{self.PrinterSn}/request", json.dumps(msg))

            # Wait for the message publish to be acked.
            # This will throw if the publish fails.
            state.wait_for_publish(20)
            return True
        except Exception as e:
            Sentry.Exception("Failed to publish message to bambu printer.", e)
        return False


    # Returns the IP for the next connection attempt
    def _GetIpForConnectionAttempt(self) -> str:

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
            self.LastConnectionFailedDueToTooManyClients = False
            if hasConfigIp:
                return configIpOrHostname

        # If the mainboard id is None, we can only ever user the config IP.
        # TODO - We could scan in the docker container if we have an old IP, but we don't do that now.
        if hasMainboardId is False:
            return configIpOrHostname

        # Increment and reset if it's too high.
        doPrinterSearch = False
        self.ConsecutivelyFailedConnectionAttempts += 1
        if self.ConsecutivelyFailedConnectionAttempts > 6:
            self.ConsecutivelyFailedConnectionAttempts = 0
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


# A helper class used for waiting msg requests
class MsgWaitingContext:

    def __init__(self, msgId:str) -> None:
        self.Id = msgId
        self.WaitEvent = threading.Event()
        self.Result:dict = None


    def GetEvent(self) -> threading.Event:
        return self.WaitEvent


    def GetResult(self) -> dict:
        return self.Result


    def SetResultAndEvent(self, result) -> None:
        self.Result = result
        self.WaitEvent.set()


    def SetSocketClosed(self) -> None:
        self.Result = None
        self.WaitEvent.set()

# # A class returned as the result of all commands.
# class BambuCommandResult:

#     def __init__(self, result:dict = None, connected:bool = True, timeout:bool = False, otherError:str = None, exception:Exception = None) -> None:
#         self.Connected = connected
#         self.Timeout = timeout
#         self.OtherError = otherError
#         self.Ex = exception
#         self.Result = result


#     def HasError(self) -> bool:
#         return self.Ex is not None or self.OtherError is not None or self.Result is None or self.Connected is False or self.Timeout is True


#     def GetLoggingErrorStr(self) -> str:
#         if self.Ex is not None:
#             return str(self.Ex)
#         if self.OtherError is not None:
#             return self.OtherError
#         if self.Connected is False:
#             return "MQTT not connected."
#         if self.Timeout:
#             return "Command timeout."
#         if self.Result is None:
#             return "No response."
#         return "No Error"
