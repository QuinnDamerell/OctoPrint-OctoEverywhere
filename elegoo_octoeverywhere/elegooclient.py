import time
import json
import random
import string
import logging
import threading

from enum import Enum

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


# For these messages, we send a request and the request is acked, but the actual message comes as an unsolicited message.
class SpecialMessages(Enum):
    StatusUpdate = 0
    AttributesUpdate = 1


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

        # Note that EITHER the IP address or mainboardID are required.
        # The docker container doesn't use the mainboard ID, since we can't network scan anyways.
        ipOrHostname = config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        self.MainboardId = config.GetStr(Config.SectionElegoo, Config.ElegooMainboardId, None)
        if ipOrHostname is None and self.MainboardId is None:
            raise Exception("An IP address or mainbaord IP must be provided in the config for Elegoo Connect.")

        # Get the port string.
        self.PortStr  = config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, None)
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


    # Sends a status update request to the printer.
    def GetStatus(self, waitForResponse:bool=True) -> ResponseMsg:
        # Status is a special message. We send a request and get an ack response, but the status comes as an unsolicited message.
        # So we must set the special message flag to ensure we get the result.
        return self._SendRequest(0, waitForResponse=waitForResponse, specialMessage=SpecialMessages.StatusUpdate)


    # Sends a status update request to the printer.
    def GetAttributes(self, waitForResponse:bool=True) -> ResponseMsg:
        # Status is a special message. We send a request and get an ack response, but the status comes as an unsolicited message.
        # So we must set the special message flag to ensure we get the result.
        return self._SendRequest(1, waitForResponse=waitForResponse, specialMessage=SpecialMessages.AttributesUpdate)


    # Sends a request to the printer and waits for a response.
    # Always returns a ResponseMsg, with various error codes.
    def SendRequest(self, cmdId:int, data:dict=None, waitForResponse:bool=True) -> ResponseMsg:
        return self._SendRequest(cmdId, data, waitForResponse)


    # An internal method to send a request and wait for a response. This hides the special message logic.
    def _SendRequest(self, cmdId:int, data:dict=None, waitForResponse:bool=True, specialMessage:SpecialMessages=None) -> ResponseMsg:

        # Generate a request id, which is a 32 char lowercase letter and number string
        requestId = ''.join(random.choices(string.ascii_lowercase + string.digits, k=32))

        # The requests always have a empty data dict if there's nothing.
        if data is None:
            data = {}

        # Create our waiting context.
        waitContext = None
        with self.RequestLock:
            waitContext = MsgWaitingContext(requestId, specialMessage)
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
        while True:
            try:
                # Clear the connection flags
                self.WebSocketConnected = False
                self.WebSocketConnectFinalized = False

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

            # Sleep for a bit between tries.
            # The main consideration here is to not log too much when the printer is off. But we do still want to connect quickly, when it's back on.
            # Note that the system might also do a printer scan after many failed attempts, which can be CPU intensive.
            # Right now we allow it to ramp up to 30 seconds between retries.
            sleepDelay = self.ConsecutivelyFailedConnectionAttempts
            if sleepDelay > 6:
                sleepDelay = 6
            sleepDelaySec = 5.0 * sleepDelay
            self.Logger.info(f"Sleeping for {sleepDelaySec} seconds before trying to reconnect to the Elegoo printer.")
            time.sleep(5.0 * sleepDelay)


    # Fired when the websocket is connected.
    def _OnWsConnect(self, ws:Client):
        self.Logger.info("Connection to the Elegoo printer established!")

        # Set the connected flag now, so we can send messages.
        self.WebSocketConnected = True

        # Reset the failed connection attempts.
        self.ConsecutivelyFailedConnectionAttempts = 0
        self.ConsecutivelyFailedConnectionAttemptsSinceSearch = 0

        # On connect, we need to request the status and attributes.
        # Important, we can't wait for for the response or will deadlock.
        self.GetStatus(waitForResponse=False)
        self.GetAttributes(waitForResponse=False)


    # Fired when the websocket is closed.
    def _OnWsClose(self, ws:Client):
        # Don't log this if we already know its due to too many clients.
        if self.LastConnectionFailedDueToTooManyClients is False:
            self.Logger.debug("Elegoo printer connection lost. We will try to reconnect in a few seconds.")

        # Clear any pending requests.
        with self.RequestLock:
            for _, v in self.RequestPendingContexts.items():
                v.SetSocketClosed()


    # Fired when the websocket is closed.
    def _OnWsError(self, ws:Client, e:Exception):
        # There's a special case here where the Elegoo printers can have a limited number of connections.
        # When that happens, we want to note it so we don't just keep trying the same IP over and over.
        msg = str(e)
        if msg.lower().find("too many client") >= 0:
            self.LastConnectionFailedDueToTooManyClients = True
            self.Logger.warning("Elegoo printer connection failed due to too many already connected clients.")
        else:
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

            # Check for a waiting request context.
            # If there is a pending context, give the message to it and we are done.
            data = msg.get("Data", None)
            if data is not None:
                requestId = data.get("RequestID", None)
                if requestId is not None:
                    with self.RequestLock:
                        if requestId in self.RequestPendingContexts:
                            # Special case! - There are a few special messages that we send requests for and get an ack, but the actual result comes as an unsolicited message.
                            # So if the special message is set, we need to give the result to the special message context.
                            context = self.RequestPendingContexts[requestId]
                            if context.SpecialMessage is None:
                                context.SetResultAndEvent(msg)
                            # Either way we return - if this is a special message we are throwing away the ack.
                            return

            #
            # If we are here, this either a response that the context is gone or it's a unsolicited message.
            #

            # Handle unsolicited messages.
            attributes = msg.get("Attributes", None)
            if attributes is not None:
                self._HandleAttributesUpdate(attributes)
                return

            # Handle unsolicited messages.
            status = msg.get("Status", None)
            if status is not None:
                self._HandleStatusUpdate(status)
                return

        except Exception as e:
            Sentry.Exception("Failed to handle incoming Elegoo message.", e)


    def _HandleAttributesUpdate(self, attributes:dict):
        # First, check if there's any special requests waiting on the attributes.
        # If there is, we need to give the result to the waiting context.
        with self.RequestLock:
            for _, v in self.RequestPendingContexts.items():
                if v.SpecialMessage == SpecialMessages.AttributesUpdate:
                    v.SetResultAndEvent(attributes)

        # We only need to handle the finalize once.
        if self.WebSocketConnectFinalized is True:
            return

        # Try to get the mainbaord id
        mainboardID = attributes.get("MainboardID", None)
        if mainboardID is None:
            return

        # If we have a mainboard ID, we can now finalize the connection.
        if self.MainboardId != mainboardID:
            self.Logger.error(f"Elegoo Mainboard ID mismatch. Expected: {self.MainboardId} Got: {mainboardID}")
            return

        # Now that we are fully connected, set the successful IP in the config and the relay
        OctoHttpRequest.SetLocalHostAddress(self.WebSocketConnectionIp)
        self.Config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, self.WebSocketConnectionIp)
        self.Logger.info("Elegoo client connection finalized.")
        self.WebSocketConnectFinalized = True


    def _HandleStatusUpdate(self, status:dict):
        # First, check if there's any special requests waiting on the status.
        # If there is, we need to give the result to the waiting context.
        with self.RequestLock:
            for _, v in self.RequestPendingContexts.items():
                if v.SpecialMessage == SpecialMessages.StatusUpdate:
                    v.SetResultAndEvent(status)


    # Returns the IP for the next connection attempt
    def _GetIpForConnectionAttempt(self) -> str:
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
            self.LastConnectionFailedDueToTooManyClients = False
            if hasConfigIp:
                return configIpOrHostname

        # If the mainboard id is None, we can only ever user the config IP.
        # TODO - We could scan in the docker container if we have an old IP, but we don't do that now.
        if hasMainboardId is False:
            return configIpOrHostname

        # If we have a mainboard ID, we can scan for the printer on the local network.
        # But we only want to do this every now an then due to the CPU load.
        doPrinterSearch = False
        self.ConsecutivelyFailedConnectionAttemptsSinceSearch += 1
        if self.ConsecutivelyFailedConnectionAttemptsSinceSearch > 6:
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


# A helper class used for waiting msg requests
class MsgWaitingContext:

    def __init__(self, msgId:str, specialMessage:SpecialMessages=None) -> None:
        self.Id = msgId
        self.WaitEvent = threading.Event()
        self.Result:dict = None
        # If this is not None, this request invoked a special message and needs to be given the result of a special message.
        self.SpecialMessage = specialMessage


    def GetEvent(self) -> threading.Event:
        return self.WaitEvent


    def GetResult(self) -> dict:
        return self.Result


    def SetResultAndEvent(self, result:dict) -> None:
        self.Result = result
        self.WaitEvent.set()


    def SetSocketClosed(self) -> None:
        self.Result = None
        self.WaitEvent.set()
