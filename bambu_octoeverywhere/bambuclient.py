import json
import logging
import threading
from typing import Any, Callable, Dict, Optional

from octoeverywhere.localip import LocalIpHelper
from octoeverywhere.mqttmux.localclient import LocalPluginClient
from octoeverywhere.mqttmux.mux import (
    MqttConnectionContext,
    MqttUpstreamMux,
)
from octoeverywhere.mqttmux.muxregistry import MqttMuxRegistry
from octoeverywhere.mqttmux.types import ConnAckReturnCode, MqttMessage, SubToken
from octoeverywhere.sentry import Sentry

from linux_host.config import Config
from linux_host.networksearch import NetworkSearch
from linux_host.localwebapi import LocalWebApi

from .bambucloud import BambuCloud, LoginStatus
from .bambumodels import BambuState, BambuVersion
from .interfaces import IBambuStateTranslator


# Keeps track of the current connection context.
class ConnectionContext:
    def __init__(self, isCloud:bool, ipOrHostname:str, port:str, userName:str, accessToken:str):
        self.IsCloud = isCloud
        self.IpOrHostname = ipOrHostname
        self.UserName = userName
        self.AccessToken = accessToken
        self.Port = port


# Responsible for keeping the plugin's view of the Bambu printer in sync and
# for exposing the command surface the rest of the OctoEverywhere code uses.
#
# As of the mqttmux refactor this class no longer owns a paho connection
# directly - it constructs an MqttUpstreamMux for its printer SN, attaches a
# LocalPluginClient to that mux, and drives Bambu-specific protocol logic
# (the JSON request/response framing, full state syncs, the bad-SN detection)
# on top.
#
# The mux is registered in MqttMuxRegistry so the WS relay and the local TCP
# broker (added in later steps) share this single upstream connection.
class BambuClient:

    _Instance:"BambuClient" = None #pyright: ignore[reportAssignmentType]

    _PrintMQTTMessages = False  # debug toggle


    @staticmethod
    def Init(logger:logging.Logger, config:Config, stateTranslator:IBambuStateTranslator) -> None:
        BambuClient._Instance = BambuClient(logger, config, stateTranslator)


    @staticmethod
    def Get() -> "BambuClient":
        return BambuClient._Instance


    def __init__(self, logger:logging.Logger, config:Config, stateTranslator:IBambuStateTranslator) -> None:
        self.Logger = logger
        self.StateTranslator = stateTranslator

        # Synchronized state - mirrors the printer. None == disconnected.
        self.State:Optional[BambuState] = None
        self.Version:Optional[BambuVersion] = None
        self.HasDoneFirstFullStateSync = False
        self.LastConnectionFailedDueToAuth = False

        # Pull required configs.
        self.Config = config
        self.LanAccessCode = config.GetStr(Config.SectionBambu, Config.BambuAccessToken, None)
        portStr = config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, None)
        printerSn = config.GetStr(Config.SectionBambu, Config.BambuPrinterSn, None)
        if portStr is None or printerSn is None:
            raise Exception("Missing required args from the config")
        self.PortStr = portStr
        self.PrinterSn = printerSn

        # Connection context tracking - the old shape, preserved for relay compatibility until step 5.
        self.CurrentContextLock = threading.Lock()
        self.CurrentContext:Optional[ConnectionContext] = None

        # Discovery / backoff state used by the connection-context callback.
        self.ConnectionAttemptStateLock = threading.Lock()
        self.ConsecutivelyFailedConnectionAttempts = 0
        self.HasDoneNetScanSincePluginStart = False
        self._context_sleep_event = threading.Event()

        # The subscribe-and-bad-creds detection state. Bambu printers silently
        # close the connection when the SN in the SUBSCRIBE topic is wrong, so
        # we look for "disconnect while a sub is pending" and turn it into a
        # helpful log message.
        self.SubConnectionStateLock = threading.Lock()
        self.IsPendingSubscribe = False
        self.ReportSubToken:Optional[SubToken] = None
        self.ConnectionGeneration = 0

        # Build the mux and the in-process client.
        # The mux is the core of the connection management and is shared with other surfaces.
        self._mux = MqttUpstreamMux(
            logger=logger,
            printer_key=self.PrinterSn,
            connection_context_provider=self._BuildConnectionContext,
            subscribe_timeout_sec=15.0,
            publish_timeout_sec=20.0,
            # The default backoff in the mux is bounded the same way as the
            # legacy loop (1s..60s).
            backoff_min_sec=1.0,
            backoff_max_sec=60.0,
        )
        MqttMuxRegistry.Register(self.PrinterSn, self._mux)

        self.Client = LocalPluginClient(logger, self._mux)
        self.Client.Start()
        self.Client.OnConnected(self._OnUpstreamConnected)
        self.Client.OnDisconnected(self._OnUpstreamDisconnected)

        # Start the supervisor - actually opens the upstream connection.
        self._mux.Start()


    def GetState(self) -> Optional[BambuState]:
        if self.State is None:
            # Wake the supervisor if it's sleeping between attempts so the
            # user's status check causes a quick reconnect try.
            self._context_sleep_event.set()
            self._mux.WakeReconnect()
            return None
        return self.State


    def GetVersion(self) -> Optional[BambuVersion]:
        return self.Version


    def IsDisconnectDueToAuth(self) -> bool:
        if self.LastConnectionFailedDueToAuth:
            return True
        reason_code = self._mux.GetLastConnectRefusedReasonCode()
        if reason_code in (
            ConnAckReturnCode.BAD_USERNAME_OR_PASSWORD,
            ConnAckReturnCode.NOT_AUTHORIZED,
            0x86, # MQTT 5 Bad User Name or Password
            0x87, # MQTT 5 Not authorized
        ):
            return True
        reason_str = self._mux.GetLastConnectRefusedReasonString()
        if reason_str is None:
            return False
        reason_str = reason_str.lower()
        return "not authorized" in reason_str or "bad user" in reason_str or "bad username" in reason_str


    # Returned for the existing mqttwebsocketproxy.py to consume. Step 5 will
    # replace those callers with direct mux attachments, removing the need
    # for the legacy shape.
    def GetCurrentConnectionContext(self) -> Optional[ConnectionContext]:
        with self.CurrentContextLock:
            return self.CurrentContext


    # Exposes the shared MqttUpstreamMux so hosts can wire downstream surfaces
    # (local TCP broker, etc.) against it directly.
    def GetMux(self) -> MqttUpstreamMux:
        return self._mux


    # Returns an auth-check function the local TCP broker can use to verify
    # incoming MQTT CONNECT credentials against whatever the upstream printer
    # currently uses (LAN: "bblp" + access code; Cloud: parsed-user + token).
    #
    # The check reads the live connection context per CONNECT so a runtime
    # access-code change is picked up without restarting the broker.
    def GetBrokerAuthCheck(self) -> Callable[[Optional[str], Optional[bytes]], int]:
        def _check(username: Optional[str], password: Optional[bytes]) -> int:
            ctx = self.GetCurrentConnectionContext()
            if ctx is None:
                # Upstream has never connected; we can't verify against
                # anything. Reject rather than risk allowing the wrong user.
                return ConnAckReturnCode.NOT_AUTHORIZED
            if username != ctx.UserName:
                return ConnAckReturnCode.BAD_USERNAME_OR_PASSWORD
            expected = (ctx.AccessToken or "").encode("utf-8")
            if password != expected:
                return ConnAckReturnCode.BAD_USERNAME_OR_PASSWORD
            return ConnAckReturnCode.ACCEPTED
        return _check


    def SendPause(self) -> bool:
        return self._Publish({"print": {"sequence_id": "0", "command": "pause"}})


    def SendResume(self) -> bool:
        return self._Publish({"print": {"sequence_id": "0", "command": "resume"}})


    def SendCancel(self) -> bool:
        return self._Publish({"print": {"sequence_id": "0", "command": "stop"}})


    def SendSetChamberLight(self, on:bool) -> bool:
        mode = "on" if on else "off"
        return self._Publish({"system": {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light",
               "led_mode": mode, "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}})


    def _Publish(self, msg:Dict[str, Any]) -> bool:
        try:
            if self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug("Outgoing Bambu Message:\r\n%s", json.dumps(msg, indent=3))
            if not self.Client.IsConnected():
                self.Logger.info("Failed to publish command because we aren't connected.")
                self._context_sleep_event.set()
                self._mux.WakeReconnect()
                return False
            return self.Client.Publish(f"device/{self.PrinterSn}/request", json.dumps(msg), qos=0)
        except Exception as e:
            Sentry.OnException("Failed to publish message to bambu printer.", e)
            return False


    # Fired when the main mux connection is established.
    def _OnUpstreamConnected(self) -> None:
        with self.SubConnectionStateLock:
            self.ConnectionGeneration += 1
            generation = self.ConnectionGeneration
        # Reset state translator at the start of every fresh connection.
        try:
            self.StateTranslator.ResetForNewConnection()
        except Exception as e:
            Sentry.OnException("BambuClient ResetForNewConnection raised", e)
        # Subscribe and full-state-sync must happen off the paho thread because
        # both block waiting on paho-side acks.
        threading.Thread(target=self._PostConnectWorker, args=(generation,),
                         name="BambuPostConnect", daemon=True).start()


    # Fired when the main mux connection is lost.
    def _OnUpstreamDisconnected(self) -> None:
        with self.SubConnectionStateLock:
            self.ConnectionGeneration += 1
            was_pending = self.IsPendingSubscribe
            self.IsPendingSubscribe = False
            report_token = self.ReportSubToken
            self.ReportSubToken = None
        if report_token is not None:
            try:
                self.Client.Unsubscribe(report_token)
            except Exception as e:
                self.Logger.debug("Bambu report unsubscribe on disconnect raised: %s", e)
        if was_pending:
            self.LastConnectionFailedDueToAuth = True
            self._LogBadCredentialsHelp()
        else:
            self.LastConnectionFailedDueToAuth = False
            self.Logger.warning("Bambu printer connection lost. We will try to reconnect in a few seconds.")
        # Clear cached printer state - it's now unknown.
        self.State = None
        self.Version = None
        self.HasDoneFirstFullStateSync = False
        try:
            LocalWebApi.Get().SetPrinterConnectionState(False)
        except Exception as e:
            self.Logger.debug("LocalWebApi notify (disconnect) failed: %s", e)


    def _PostConnectWorker(self, generation:int) -> None:
        # 1) Subscribe to the report stream. If the SN/access code is wrong,
        #    the printer silently disconnects and OnUpstreamDisconnected will
        #    log the helpful message.
        with self.SubConnectionStateLock:
            if generation != self.ConnectionGeneration:
                return
            self.IsPendingSubscribe = True
        token = self.Client.Subscribe(
            f"device/{self.PrinterSn}/report", 0,
            lambda msg, gen=generation: self._OnReportMessageForGeneration(msg, gen),
        )
        with self.SubConnectionStateLock:
            self.IsPendingSubscribe = False
            if token is None:
                # Either the subscribe was refused or we lost the connection
                # mid-handshake. OnUpstreamDisconnected handles user messaging.
                return
            if generation != self.ConnectionGeneration:
                try:
                    self.Client.Unsubscribe(token)
                except Exception as e:
                    self.Logger.debug("Bambu stale report unsubscribe raised: %s", e)
                return
            self.ReportSubToken = token
        # 2) Subscribe succeeded -> SN and access code are valid. Notify and
        #    request a full state sync.
        self.LastConnectionFailedDueToAuth = False
        try:
            LocalWebApi.Get().SetPrinterConnectionState(True)
        except Exception as e:
            self.Logger.debug("LocalWebApi notify (connected) failed: %s", e)
        with self.ConnectionAttemptStateLock:
            self.ConsecutivelyFailedConnectionAttempts = 0
        self.Logger.info("Connection to the Bambu printer established and subscription succeeded.")
        self._DoFullStateSync()


    def _OnReportMessageForGeneration(self, mqtt_msg: MqttMessage, generation:int) -> None:
        with self.SubConnectionStateLock:
            if generation != self.ConnectionGeneration:
                return
        self._OnReportMessage(mqtt_msg)


    def _DoFullStateSync(self) -> None:
        try:
            self.Logger.info("Starting full state sync.")
            # Get version first so the version object is populated before the
            # first big pushall arrives.
            if not self._Publish({"info": {"sequence_id": "0", "command": "get_version"}}):
                raise Exception("Failed to publish get_version")
            if not self._Publish({"pushing": {"sequence_id": "0", "command": "pushall"}}):
                raise Exception("Failed to publish full sync")
        except Exception as e:
            Sentry.OnException("BambuClient _DoFullStateSync exception.", e)
            # Drop the connection so the supervisor reconnects fresh. Do NOT
            # call mux.Shutdown(); that's terminal and would tear the whole
            # printer down.
            self._mux.ForceReconnect()


    def _OnReportMessage(self, mqtt_msg: MqttMessage) -> None:
        try:
            msg = json.loads(mqtt_msg.payload)
            if msg is None:
                raise Exception("Parsed json MQTT message returned None")
            if BambuClient._PrintMQTTMessages and self.Logger.isEnabledFor(logging.DEBUG):
                self.Logger.debug("Incoming Bambu Message:\r\n%s", json.dumps(msg, indent=3))

            isFirstFullSyncResponse = False
            if "print" in msg:
                printMsg = msg["print"]
                try:
                    if self.State is None:
                        s = BambuState()
                        s.OnUpdate(printMsg)
                        self.State = s
                    else:
                        self.State.OnUpdate(printMsg)
                except Exception as e:
                    Sentry.OnException("Exception calling BambuState.OnUpdate", e)

                if self.HasDoneFirstFullStateSync is False:
                    cmd = printMsg.get("command", None)
                    if cmd is not None and cmd == "push_status":
                        # Heuristic: a true pushall response carries many
                        # top-level fields (~59 on a P1P; same threshold used
                        # in NetworkSearch.ValidateConnection_Bambu).
                        if len(printMsg) > 40:
                            isFirstFullSyncResponse = True
                            self.HasDoneFirstFullStateSync = True

            if "info" in msg:
                try:
                    if self.Version is None:
                        v = BambuVersion(self.Logger)
                        v.OnUpdate(msg["info"])
                        self.Version = v
                    else:
                        self.Version.OnUpdate(msg["info"])
                except Exception as e:
                    Sentry.OnException("Exception calling BambuVersion.OnUpdate", e)

            try:
                if self.State is not None:
                    self.StateTranslator.OnMqttMessage(msg, self.State, isFirstFullSyncResponse)
            except Exception as e:
                Sentry.OnException("Exception calling StateTranslator.OnMqttMessage", e)

        except Exception as e:
            Sentry.OnException(f"Failed to handle incoming mqtt message. `{mqtt_msg.payload!r}`", e)


    # Called fresh on every connect attempt. Mirrors the legacy
    # _GetConnectionContextToTry logic, plus emits the OE-relay-compatible
    # ConnectionContext via _SetCurrentPublicCtx.
    def _BuildConnectionContext(self) -> MqttConnectionContext:
        with self.ConnectionAttemptStateLock:
            self.ConsecutivelyFailedConnectionAttempts += 1
            doPrinterSearch = False
            if (self.HasDoneNetScanSincePluginStart is False and self.ConsecutivelyFailedConnectionAttempts > 1) or self.ConsecutivelyFailedConnectionAttempts > 6:
                self.ConsecutivelyFailedConnectionAttempts = 0
                doPrinterSearch = True

        connectionMode = self.Config.GetStr(Config.SectionBambu, Config.BambuConnectionMode, Config.BambuConnectionModeDefault)
        if connectionMode == Config.BambuConnectionModeValueCloud:
            cloudCtx = self._TryBuildCloudConnectionContext()
            if cloudCtx is not None:
                return cloudCtx
            self.Logger.warning("We tried to connect via Bambu Cloud, but failed. We will try a local connection.")

        configIpOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        if not doPrinterSearch:
            if configIpOrHostname is not None and len(configIpOrHostname) > 0:
                return self._BuildLocalConnectionContext(configIpOrHostname)

        if configIpOrHostname is None or len(configIpOrHostname) == 0:
            raise Exception("There's no valid companion ip_or_hostname set in the config.")

        # LAN scan (CPU-intensive; capped frequency above).
        self.Logger.info("Searching for your Bambu Lab printer %s", self.PrinterSn)
        if self.LanAccessCode is None:
            return self._BuildLocalConnectionContext(configIpOrHostname)
        with self.ConnectionAttemptStateLock:
            self.HasDoneNetScanSincePluginStart = True
        ips = NetworkSearch.ScanForInstances_Bambu(self.Logger, self.LanAccessCode, self.PrinterSn, ipHint=configIpOrHostname, threadCount=25, delaySec=0.2)
        if len(ips) == 1:
            ip = ips[0]
            if configIpOrHostname.strip().lower() == ip.strip().lower():
                self.Logger.info("Network scan confirmed the existing printer IP %s. Using it to connect.", ip)
            else:
                self.Logger.info("We found a new IP for this printer. [%s -> %s] Updating the config and using it to connect.",
                                 configIpOrHostname, ip)
                self.Config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, ip)
            return self._BuildLocalConnectionContext(ip)
        return self._BuildLocalConnectionContext(configIpOrHostname)


    def _BuildLocalConnectionContext(self, ipOrHostname:str) -> MqttConnectionContext:
        accessCode = "000000"
        if self.LanAccessCode is not None:
            accessCode = self.LanAccessCode
        else:
            self.Logger.error("Missing access code in _BuildLocalConnectionContext, can't connect to the printer.")
            self.LastConnectionFailedDueToAuth = True
        # Cache the legacy public shape so callers can still read it.
        self._SetCurrentPublicCtx(ConnectionContext(False, ipOrHostname, self.PortStr, "bblp", accessCode))
        # Local printers use self-signed certs.
        LocalIpHelper.SetConnectionTargetIpOverride(ipOrHostname)
        self.Logger.info("Trying to connect to printer via local connection at %s...", ipOrHostname)
        return MqttConnectionContext(
            host=ipOrHostname,
            port=int(self.PortStr),
            username="bblp",
            password=accessCode,
            use_tls=True,
            allow_invalid_cert=True,
            transport="tcp",
            keep_alive_sec=5,
        )


    def _TryBuildCloudConnectionContext(self) -> Optional[MqttConnectionContext]:
        bCloud = BambuCloud.Get()
        if bCloud.HasContext() is False:
            return None
        # Force a fresh login so the access token is current.
        accessTokenResult = bCloud.GetAccessToken(forceLogin=True)
        if accessTokenResult.Status != LoginStatus.Success or accessTokenResult.AccessToken is None:
            self._LogBambuCloudLoginFailure(accessTokenResult.Status)
            # The legacy loop slept for 10 minutes per failed attempt to avoid
            # hammering Bambu Cloud; mux backoff will only get us to a 60s
            # cap, so reproduce the longer sleep here.
            with self.ConnectionAttemptStateLock:
                failed = self.ConsecutivelyFailedConnectionAttempts
            self._context_sleep_event.wait(timeout=min(600.0 * failed, 3600.0))
            self._context_sleep_event.clear()
            return None
        accessToken = accessTokenResult.AccessToken
        parsedUser = bCloud.GetUserNameFromAccessToken(accessToken)
        if parsedUser is None:
            self.Logger.error("Failed to parse the access token, can't connect to the printer.")
            return None
        self.LastConnectionFailedDueToAuth = False
        host = bCloud.GetMqttHostname()
        self._SetCurrentPublicCtx(ConnectionContext(True, host, self.PortStr, parsedUser, accessToken))
        self.Logger.info("Trying to connect to printer via Bambu Cloud at %s...", host)
        return MqttConnectionContext(
            host=host,
            port=int(self.PortStr),
            username=parsedUser,
            password=accessToken,
            use_tls=True,
            allow_invalid_cert=False,
            transport="tcp",
            keep_alive_sec=5,
        )


    def _SetCurrentPublicCtx(self, ctx:ConnectionContext) -> None:
        with self.CurrentContextLock:
            self.CurrentContext = ctx


    def _LogBambuCloudLoginFailure(self, status:LoginStatus) -> None:
        self.LastConnectionFailedDueToAuth = status in (
            LoginStatus.BadUserNameOrPassword,
            LoginStatus.TwoFactorAuthEnabled,
            LoginStatus.EmailCodeRequired,
        )
        self.Logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        self.Logger.error("                                                     Failed To Log Into Bambu Cloud")
        if status == LoginStatus.BadUserNameOrPassword:
            self.Logger.error("The email address or password is wrong. Re-run the Bambu Connect installer or use the docker files to update your email address and password.")
        elif status == LoginStatus.TwoFactorAuthEnabled:
            self.Logger.error("Two factor auth is enabled on this account. Bambu Lab doesn't allow us to support two factor auth, so it must be disabled on your account or the local connection mode.")
        elif status == LoginStatus.EmailCodeRequired:
            self.Logger.error("This account requires an email code to login. Bambu Lab doesn't allow us to support this, so you must use the local connection mode.")
        else:
            self.Logger.error("Unknown error, we will try again later.")
        self.Logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")


    def _LogBadCredentialsHelp(self) -> None:
        self.Logger.error("")
        self.Logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        self.Logger.error("Bambu printer mqtt connection lost when trying to sub for events.")
        self.Logger.error("This might indicate the printer ACCESS CODE - OR - SERIAL NUMBER IS WRONG.")
        self.Logger.error(f"     Current Serial Number: '{self.PrinterSn}'")
        self.Logger.error(f"     Current Access Code:   '{self.LanAccessCode}'")
        self.Logger.error("")
        self.Logger.error("Check these values match your printer.")
        self.Logger.error("If they changed, run the OctoEverywhere installer again to update them or update your Docker configuration.")
        self.Logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        self.Logger.error("")


# A class returned as the result of all ran commands. Kept here so the
# bambucommandhandler can import it unchanged.
class BambuCommandResult:

    def __init__(self, result:Optional[Dict[str, Any]]=None, connected:bool=True, timeout:bool=False, otherError:Optional[str]=None, exception:Optional[Exception]=None) -> None:
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
