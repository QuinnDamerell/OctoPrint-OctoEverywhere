import logging
import ssl
import time
import json
import socket
import threading
from typing import List

import paho.mqtt.client as mqtt

from octoeverywhere.sentry import Sentry

from linux_host.config import Config
from linux_host.networksearch import NetworkSearch

from .bambucloud import BambuCloud, LoginStatus
from .bambumodels import BambuState, BambuVersion


class ConnectionContext:
    def __init__(self, isCloud:bool, ipOrHostname:str, userName:str, accessToken:str):
        self.IsCloud = isCloud
        self.IpOrHostname = ipOrHostname
        self.UserName = userName
        self.AccessToken = accessToken


# Responsible for connecting to and maintaining a connection to the Bambu Printer.
# Also responsible for dispatching out MQTT update messages.
class BambuClient:

    _Instance = None

    # Useful for debugging.
    _PrintMQTTMessages = False

    @staticmethod
    def Init(logger:logging.Logger, config:Config, stateTranslator):
        BambuClient._Instance = BambuClient(logger, config, stateTranslator)


    @staticmethod
    def Get():
        return BambuClient._Instance


    def __init__(self, logger:logging.Logger, config:Config, stateTranslator) -> None:
        self.Logger = logger
        self.StateTranslator = stateTranslator # BambuStateTranslator

        # Used to keep track of the printer state
        # None means we are disconnected.
        self.State:BambuState = None
        self.Version:BambuVersion = None
        self.HasDoneFirstFullStateSync = False
        self.ReportSubscribeMid = None
        self.IsPendingSubscribe = False
        self._CleanupStateOnDisconnect()

        # This is used to wake up the connection thread if it's sleeping.
        self.SleepEvent = threading.Event()

        # Get the required args.
        self.Config = config
        self.PortStr  = config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, None)
        self.LanAccessCode  = config.GetStr(Config.SectionBambu, Config.BambuAccessToken, None)
        self.PrinterSn  = config.GetStr(Config.SectionBambu, Config.BambuPrinterSn, None)
        # The port and SN are required, but the Access Code isn't, since sometimes it's not there for cloud connections.
        if self.PortStr is None or self.PrinterSn is None:
            raise Exception("Missing required args from the config")

        # We use this var to keep track of consecutively failed connections
        self.ConsecutivelyFailedConnectionAttempts = 0

        # Start a thread to setup and maintain the connection.
        self.Client:mqtt.Client = None
        t = threading.Thread(target=self._ClientWorker)
        t.start()


    # Returns the current local State object which is kept in sync with the printer.
    # Returns None if the printer is not connected and the state is unknown.
    def GetState(self) -> BambuState:
        if self.State is None:
            # Set the sleep event, so if the socket is waiting to reconnect, it will wake up and try again.
            self.SleepEvent.set()
            return None
        return self.State


    # Returns the current local Version object which is kept in sync with the printer.
    # Returns None if the printer is not connected and the state is unknown.
    def GetVersion(self) -> BambuVersion:
        return self.Version


    # Sends the pause command, returns is the send was successful or not.
    def SendPause(self) -> bool:
        return self._Publish({"print": {"sequence_id": "0", "command": "pause"}})


    # Sends the resume command, returns is the send was successful or not.
    def SendResume(self) -> bool:
        return self._Publish({"print": {"sequence_id": "0", "command": "resume"}})


    # Sends the cancel (stop) command, returns is the send was successful or not.
    def SendCancel(self) -> bool:
        return self._Publish({"print": {"sequence_id": "0", "command": "stop"}})


    # Sets up, runs, and maintains the MQTT connection.
    def _ClientWorker(self):
        localBackoffCounter = 0
        while True:
            ipOrHostname = None
            isConnectAttemptFromEventBump = False
            try:
                # Before we try to connect, ensure we tell the state translator that we are starting a new connection.
                self.StateTranslator.ResetForNewConnection()

                # We always connect locally. We use encryption, but the printer doesn't have a trusted
                # cert root, so we have to disable the cert root checks.
                self.Client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

                # Since we are local, we can do more aggressive reconnect logic.
                # The default is min=1 max=120 seconds.
                self.Client.reconnect_delay_set(min_delay=1, max_delay=5)

                # Setup the callback functions.
                self.Client.on_connect = self._OnConnect
                self.Client.on_message = self._OnMessage
                self.Client.on_disconnect = self._OnDisconnect
                self.Client.on_subscribe = self._OnSubscribe
                self.Client.on_log = self._OnLog

                # Get the IP to try on this connect
                connectionContext = self._GetConnectionContextToTry(isConnectAttemptFromEventBump)
                if connectionContext.IsCloud:
                    self.Logger.info("Trying to connect to printer via Bambu Cloud...")
                    # We are connecting to Bambu Cloud, setup MQTT for it.
                    self.Client.tls_set(tls_version=ssl.PROTOCOL_TLS)
                else:
                    # We are trying to connect to the printer locally, so configure mqtt for a local connection.
                    self.Logger.info("Trying to connect to printer via local connection...")
                    self.Client.tls_set(tls_version=ssl.PROTOCOL_TLS, cert_reqs=ssl.CERT_NONE)
                    self.Client.tls_insecure_set(True)

                # Set the username and access token.
                self.Client.username_pw_set(connectionContext.UserName, connectionContext.AccessToken)

                # Connect to the server
                # This will throw if it fails, but after that, the loop_forever will handle reconnecting.
                localBackoffCounter += 1
                self.Client.connect(connectionContext.IpOrHostname, int(self.PortStr), keepalive=5)

                # Note that self.Client.connect will not throw if there's no MQTT server, but not if auth is wrong.
                # So if it didn't throw, we know there's a server there, but it might not be the right server
                localBackoffCounter = 0

                # This will run forever, including handling reconnects and such.
                self.Client.loop_forever()
            except Exception as e:
                if isinstance(e, ConnectionRefusedError):
                    # This means there was no open socket at the given IP and port.
                    # This happens when the printer is offline, so we only need to log sometimes.
                    self.Logger.warning(f"Failed to connect to the Bambu printer {ipOrHostname}:{self.PortStr}, we will retry in a bit. "+str(e))
                elif isinstance(e, TimeoutError):
                    # This means there was no open socket at the given IP and port.
                    self.Logger.warning(f"Failed to connect to the Bambu printer {ipOrHostname}:{self.PortStr}, we will retry in a bit. "+str(e))
                elif isinstance(e, OSError) and ("Network is unreachable" in str(e) or "No route to host" in str(e)):
                    # This means the IP doesn't route to a device.
                    self.Logger.warning(f"Failed to connect to the Bambu printer {ipOrHostname}:{self.PortStr}, we will retry in a bit. "+str(e))
                elif isinstance(e, socket.timeout) and "timed out" in str(e):
                    # This means the IP doesn't route to a device.
                    self.Logger.warning(f"Failed to connect to the Bambu printer {ipOrHostname}:{self.PortStr} due to a timeout, we will retry in a bit. "+str(e))
                else:
                    # Random other errors.
                    Sentry.Exception(f"Failed to connect to the Bambu printer {ipOrHostname}:{self.PortStr}. We will retry in a bit.", e)

            # Sleep for a bit between tries.
            # The main consideration here is to not log too much when the printer is off. But we do still want to connect quickly, when it's back on.
            # Note that the system might also do a printer scan after many failed attempts, which can be CPU intensive.
            #
            # Since we now have the sleep event, we can sleep longer, because when something attempts to use the socket, the event will wake us up
            # to try a connection again. So, for example, when the user goes to the OE dashboard, the status check will wake us up.
            #
            # So right now, the max sleep time is 5 minutes.
            localBackoffCounter = min(localBackoffCounter, 60)
            sleepDelaySec = 5.0 * localBackoffCounter
            self.Logger.info(f"Sleeping for {sleepDelaySec} seconds before trying to reconnect to the Bambu printer.")
            # Sleep for the time or until the event is set.
            isConnectAttemptFromEventBump = self.SleepEvent.wait(sleepDelaySec)
            self.SleepEvent.clear()


    # Since MQTT sends a full state and then partial updates, we sometimes need to force a full state sync, like on connect.
    # This must be done async for most callers, since it blocks until the publish is acked. If this blocked on the main mqtt thread, it would
    # dead lock.
    # If this fails, it will disconnect the client.
    def _ForceStateSyncAsync(self) -> bool:
        def _FullSyncWorker():
            try:
                self.Logger.info("Starting full state sync.")
                # It's important to request the hardware version first, so we have it parsed before we get the first full sync.
                getInfo = {"info": {"sequence_id": "0", "command": "get_version"}}
                if not self._Publish(getInfo):
                    raise Exception("Failed to publish get_version")
                pushAll = { "pushing": {"sequence_id": "0", "command": "pushall"}}
                if not self._Publish(pushAll):
                    raise Exception("Failed to publish full sync")
            except Exception as e:
                # Report and disconnect since we are in an unknown state.
                Sentry.Exception("BambuClient _ForceStateSyncAsync exception.", e)
                self.Client.disconnect()
        t = threading.Thread(target=_FullSyncWorker)
        t.start()


    # Fired whenever the client is disconnected, we need to clean up the state since it's now unknown.
    def _CleanupStateOnDisconnect(self):
        self.State = None
        self.Version = None
        self.HasDoneFirstFullStateSync = False
        self.ReportSubscribeMid = None
        self.IsPendingSubscribe = False
        # For some reason, the Bambu Cloud MQTT server will fire a disconnect message but doesn't actually disconnect.
        # So we always call disconnect to ensure we force it, to ensure our connection loop closes.
        try:
            c = self.Client
            if c is not None:
                c.disconnect()
        except Exception as e:
            self.Logger.debug(f"_CleanupStateOnDisconnect exception on mqtt disconnect during cleanup. {e}")


    # Fired when the MQTT connection is made.
    def _OnConnect(self, client:mqtt.Client, userdata, flags, reason_code, properties):
        self.Logger.info("Connection to the Bambu printer established! - Subscribing to the report subscription.")
        # After connect, we try to subscribe to the report feed.
        # We must do this before anything else, otherwise we won't get responses for things like
        # the full state sync. The result of the subscribe will be reported to _OnSubscribe
        # Note that at least for my P1P, if the SN is incorrect, the MQTT connection is closed with no _OnSubscribe callback.
        # Thus we set the self.IsPendingSubscribe flag, so we can give the user a better error message.
        self.IsPendingSubscribe = True
        (result, self.ReportSubscribeMid) = self.Client.subscribe(f"device/{self.PrinterSn}/report")
        if result != mqtt.MQTT_ERR_SUCCESS or self.ReportSubscribeMid is None:
            # If we can't sub, disconnect, since we can't do anything.
            self.Logger.warn(f"Failed to subscribe to the MQTT subscription using the serial number '{self.PrinterSn}'. Result: {result}. Disconnecting.")
            self.Client.disconnect()


    # Fired when the MQTT connection is lost
    def _OnDisconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        # If the serial number is wrong in the subscribe call, instead of returning an error the Bambu Lab printers just disconnect.
        # So if we were pending a subscribe call, give the user a better error message so they know the likely cause.
        if self.IsPendingSubscribe:
            self.Logger.error("Bambu printer mqtt connection lost when trying to sub for events.")
            self.Logger.error(f"THIS USUALLY MEANS THE PRINTER SERIAL NUMBER IS WRONG. We tried to use the serial number '{self.PrinterSn}'. Double check the SN is correct.")
        else:
            self.Logger.warn("Bambu printer connection lost. We will try to reconnect in a few seconds.")
        # Clear the state since we lost the connection and won't stay synced.
        self._CleanupStateOnDisconnect()


    # Fired when the MQTT connection has something to log.
    def _OnLog(self, client, userdata, level:int, msg:str):
        if level == mqtt.MQTT_LOG_ERR:
            # If the string is something like "Caught exception in on_connect: ..."
            # It's a leaked exception from us.
            if "exception" in msg:
                Sentry.Exception("MQTT leaked exception.", Exception(msg))
            else:
                self.Logger.error(f"MQTT log error: {msg}")
        elif level == mqtt.MQTT_LOG_WARNING:
            # Report warnings.
            self.Logger.error(f"MQTT log warn: {msg}")
        # else:
        #     # Report everything else if debug is enabled.
        #     if self.Logger.isEnabledFor(logging.DEBUG):
        #         self.Logger.debug(f"MQTT log: {msg}")


    # Fried when the MQTT subscribe result has come back.
    def _OnSubscribe(self, client, userdata, mid, reason_code_list:List[mqtt.ReasonCode], properties):
        # We only want to listen for the result of the report subscribe.
        if self.ReportSubscribeMid is not None and self.ReportSubscribeMid == mid:
            # Ensure the sub was successful.
            for r in reason_code_list:
                if r.is_failure:
                    # On any failure, report it and disconnect.
                    self.Logger.error(f"Sub response for the report subscription reports failure. {r}")
                    self.Client.disconnect()
                    return

            # At this point, we know the connection was successful, the access code is correct, and the SN is correct.
            self.ConsecutivelyFailedConnectionAttempts = 0

            # Sub success! Force a full state sync.
            self._ForceStateSyncAsync()


    # Fired when there's an incoming MQTT message.
    def _OnMessage(self, client, userdata, mqttMsg:mqtt.MQTTMessage):
        try:
            # Try to deserialize the message.
            msg = json.loads(mqttMsg.payload)
            if msg is None:
                raise Exception("Parsed json MQTT message returned None")

            # Print for debugging if desired.
            if BambuClient._PrintMQTTMessages and self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug("Incoming Bambu Message:\r\n"+json.dumps(msg, indent=3))

            # Since we keep a track of the state locally from the partial updates, we need to feed all updates to our state object.
            isFirstFullSyncResponse = False
            if "print" in msg:
                printMsg = msg["print"]
                try:
                    if self.State is None:
                        # Build the object before we set it.
                        s = BambuState()
                        s.OnUpdate(printMsg)
                        self.State = s
                    else:
                        self.State.OnUpdate(printMsg)
                except Exception as e:
                    Sentry.Exception("Exception calling BambuState.OnUpdate", e)

                # Try to detect if this is the response to the first full sync request.
                if self.HasDoneFirstFullStateSync is False:
                    # First make sure the command is the push status.
                    cmd = printMsg.get("command", None)
                    if cmd is not None and cmd == "push_status":
                        # We dont have a 100% great way to know if this is a fully sync message.
                        # For now, we use this stat. The message we get from a P1P has 59 members in the root, so we use 40 as mark.
                        # Note we use this same value in NetworkSearch.ValidateConnection_Bambu
                        if len(printMsg) > 40:
                            isFirstFullSyncResponse = True
                            self.HasDoneFirstFullStateSync = True

            # Update the version info if sent.
            if "info" in msg:
                try:
                    if self.Version is None:
                        # Build the object before we set it.
                        s = BambuVersion(self.Logger)
                        s.OnUpdate(msg["info"])
                        self.Version = s
                    else:
                        self.Version.OnUpdate(msg["info"])
                except Exception as e:
                    Sentry.Exception("Exception calling BambuVersion.OnUpdate", e)

            # Send all messages to the state translator
            # This must happen AFTER we update the State object, so it's current.
            try:
                # Only send the message along if there's a state. This can happen if a push_status isn't the first message we receive.
                if self.State is not None:
                    self.StateTranslator.OnMqttMessage(msg, self.State, isFirstFullSyncResponse)
            except Exception as e:
                Sentry.Exception("Exception calling StateTranslator.OnMqttMessage", e)

        except Exception as e:
            Sentry.Exception(f"Failed to handle incoming mqtt message. {mqttMsg.payload}", e)


    # Publishes a message and blocks until it knows if the message send was successful or not.
    def _Publish(self, msg:dict) -> bool:
        try:
            # Print for debugging if desired.
            if self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug("Incoming Bambu Message:\r\n"+json.dumps(msg, indent=3))

            # Ensure we are connected.
            if self.Client is None or not self.Client.is_connected():
                self.Logger.info("Failed to publish command because we aren't connected.")
                # Set the sleep event, so if the socket is waiting to reconnect, it will wake up and try again.
                self.SleepEvent.set()
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


    # Returns a connection context object we should try to for this connection attempt.
    # The connection context can indicate we are trying to connect to the Bambu Cloud or the local printer,
    # depending on the plugin config and what's available.
    def _GetConnectionContextToTry(self, isConnectAttemptFromEventBump:bool) -> ConnectionContext:
        # Increment and reset if it's too high.
        # This will restart the process of trying cloud connect and falling back.
        # But we don't want to increment if this is from a connection bump, since they can be spammy.
        if isConnectAttemptFromEventBump is False:
            self.ConsecutivelyFailedConnectionAttempts += 1
        doPrinterSearch = False
        if self.ConsecutivelyFailedConnectionAttempts > 6:
            self.ConsecutivelyFailedConnectionAttempts = 0
            doPrinterSearch = True

        # Get the connection mode set by the user. This defaults to local, but the user can explicitly set it to either.
        connectionMode = self.Config.GetStr(Config.SectionBambu, Config.BambuConnectionMode, Config.BambuConnectionModeDefault)
        if connectionMode == Config.BambuConnectionModeValueCloud:
            # If the mode is set to cloud, try to connect via it.
            # If a context can't be created, there's something wrong with the account info
            # or a Bambu service issue. Since we have the local info, we can try it as well.
            cloudContext = self._TryToGetCloudConnectContext()
            if cloudContext is not None:
                return cloudContext
            self.Logger.warning("We tried to connect via Bambu Cloud, but failed. We will try a local connection.")

        # On the first few attempts, use the expected IP or the cloud config.
        # Every time we reset the count, we will try a network scan to see if we can find the printer guessing it's IP might have changed.
        # The IP can be empty, like if the docker container is used, in which case we should always search for the printer.
        configIpOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        if doPrinterSearch is False:
            # If we aren't using a cloud connection or it failed, return the local hostname
            if configIpOrHostname is not None and len(configIpOrHostname) > 0:
                return self._GetLocalConnectionContext(configIpOrHostname)

        # If we fail too many times, try to scan for the printer on the local subnet, the IP could have changed.
        # Since we 100% identify the printer by the access token and printer SN, we can try to scan for it.
        # Note we don't want to do this too often since it's CPU intensive and the printer might just be off.
        # We use a lower thread count and delay before each action to reduce the required load.
        # Using this config, it takes about 30 seconds to scan for the printer.
        self.Logger.info(f"Searching for your Bambu Lab printer {self.PrinterSn}")
        ips = NetworkSearch.ScanForInstances_Bambu(self.Logger, self.LanAccessCode, self.PrinterSn, threadCount=25, delaySec=0.2)

        # If we get an IP back, it is the printer.
        # The scan above will only return an IP if the printer was successfully connected to, logged into, and fully authorized with the Access Token and Printer SN.
        if len(ips) == 1:
            # Since we know this is the IP, we will update it in the config. This mean in the future we will use this IP directly
            # And everything else trying to connect to the printer (webcam and ftp) will use the correct IP.
            ip = ips[0]
            self.Logger.info(f"We found a new IP for this printer. [{configIpOrHostname} -> {ip}] Updating the config and using it to connect.")
            self.Config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, ip)
            return self._GetLocalConnectionContext(ip)

        # If we don't find anything, just use the config IP.
        return self._GetLocalConnectionContext(configIpOrHostname)


    def _GetLocalConnectionContext(self, ipOrHostname) -> ConnectionContext:
        # The username is always the same, we use the local LAN access token.
        return ConnectionContext(False, ipOrHostname, "bblp", self.LanAccessCode)


    # Returns a Bambu Cloud based connection context if it can be made, otherwise None
    def _TryToGetCloudConnectContext(self) -> ConnectionContext:
        bCloud = BambuCloud.Get()
        if bCloud.HasContext() is False:
            return None

        # Try to login and get the access token.
        # Force the login to ensure the access token is current.
        accessTokenResult = BambuCloud.Get().GetAccessToken(forceLogin=True)

        # If we failed, make sure to log the reason, so it's obvious for the user.
        if accessTokenResult.Status != LoginStatus.Success:
            self.Logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            self.Logger.error("                                                     Failed To Log Into Bambu Cloud")
            if accessTokenResult.Status == LoginStatus.BadUserNameOrPassword:
                self.Logger.error("The email address or password is wrong. Re-run the Bambu Connect installer or use the docker files to update your email address and password.")
            elif accessTokenResult.Status == LoginStatus.TwoFactorAuthEnabled:
                self.Logger.error("Two factor auth is enabled on this account. Bambu Lab doesn't allow us to support two factor auth, so it must be disabled on your account or the local connection mode.")
            elif accessTokenResult.Status == LoginStatus.EmailCodeRequired:
                self.Logger.error("This account requires an email code to login. Bambu Lab doesn't allow us to support this, so you must use the local connection mode.")
            else:
                self.Logger.error("Unknown error, we will try again later.")
            self.Logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")

            # We do a delay here, so we don't pound on the service. If we can't login for one of these reasons, we probably can't recover.
            time.sleep(600.0 * self.ConsecutivelyFailedConnectionAttempts)
            return None

        # Return the connection object.
        accessToken = accessTokenResult.AccessToken
        return ConnectionContext(True, bCloud.GetMqttHostname(), bCloud.GetUserNameFromAccessToken(accessToken), accessToken)


# A class returned as the result of all commands.
class BambuCommandResult:

    def __init__(self, result:dict = None, connected:bool = True, timeout:bool = False, otherError:str = None, exception:Exception = None) -> None:
        self.Connected = connected
        self.Timeout = timeout
        self.OtherError = otherError
        self.Ex = exception
        self.Result = result


    def HasError(self) -> bool:
        return self.Ex is not None or self.OtherError is not None or self.Result is None or self.Connected is False or self.Timeout is True


    def GetLoggingErrorStr(self) -> str:
        if self.Ex is not None:
            return str(self.Ex)
        if self.OtherError is not None:
            return self.OtherError
        if self.Connected is False:
            return "MQTT not connected."
        if self.Timeout:
            return "Command timeout."
        if self.Result is None:
            return "No response."
        return "No Error"
