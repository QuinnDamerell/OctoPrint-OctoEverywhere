import logging
import threading
from typing import Any, Dict, List, Optional

import requests
from requests.auth import HTTPDigestAuth

from linux_host.config import Config
from linux_host.localwebapi import LocalWebApi

from octoeverywhere.httpsessions import HttpSessions
from octoeverywhere.localip import LocalIpHelper
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.sentry import Sentry

from .interfaces import IStateTranslator
from .prusalinkmodels import PrinterState


class PrusaLinkAuthException(Exception):
    pass


class PrusaLinkConnectionContext:
    def __init__(self, ipOrHostname:str, portStr:str, authMode:str, username:Optional[str], password:Optional[str], apiKey:Optional[str]) -> None:
        self.IpOrHostname = ipOrHostname
        self.PortStr = portStr
        self.AuthMode = authMode
        self.Username = username
        self.Password = password
        self.ApiKey = apiKey


    def GetBaseUrl(self) -> str:
        return f"http://{self.IpOrHostname}:{self.PortStr}"


class PrusaLinkClient:

    PollIntervalSec = 2.0
    RequestTimeoutSec = 10.0

    _Instance:"PrusaLinkClient" = None #pyright: ignore[reportAssignmentType]

    @staticmethod
    def Init(logger:logging.Logger, config:Config, stateTranslator:IStateTranslator) -> None:
        PrusaLinkClient._Instance = PrusaLinkClient(logger, config, stateTranslator)


    @staticmethod
    def Get() -> "PrusaLinkClient":
        return PrusaLinkClient._Instance


    def __init__(self, logger:logging.Logger, config:Config, stateTranslator:IStateTranslator) -> None:
        self.Logger = logger
        self.Config = config
        self.StateTranslator = stateTranslator
        self.Session = requests.Session()
        self.Session.trust_env = False
        self.SessionLock = threading.Lock()

        self.SleepEvent = threading.Event()
        self.Connected = False
        self.ConnectionFinalized = False
        self.LastConnectionFailedDueToAuth = False
        self.CurrentConnectionContext:Optional[PrusaLinkConnectionContext] = None
        self.State:Optional[PrinterState] = None
        self.Version:Optional[Dict[str, Any]] = None
        self.Info:Optional[Dict[str, Any]] = None
        self.ConsecutivelyFailedConnectionAttempts = 0

        t = threading.Thread(target=self._ClientWorker, name="PrusaLinkClient")
        t.daemon = True
        t.start()


    def GetState(self) -> Optional[PrinterState]:
        if self.State is None:
            self.SleepEvent.set()
            return None
        return self.State


    def GetVersion(self) -> Optional[Dict[str, Any]]:
        return self.Version


    def GetInfo(self) -> Optional[Dict[str, Any]]:
        return self.Info


    def GetCurrentConnectionContext(self) -> Optional[PrusaLinkConnectionContext]:
        return self.CurrentConnectionContext


    def IsConnected(self) -> bool:
        if self.Connected is False:
            self.SleepEvent.set()
        return self.Connected


    def IsDisconnectDueToAuth(self) -> bool:
        if self.Connected is False:
            self.SleepEvent.set()
        return self.LastConnectionFailedDueToAuth


    def SendPause(self) -> bool:
        return self._SendJobAction("pause")


    def SendResume(self) -> bool:
        return self._SendJobAction("resume")


    def SendCancel(self) -> bool:
        return self._SendJobAction("cancel")


    def _SendJobAction(self, action:str) -> bool:
        state = self.GetState()
        if state is None or state.JobId is None:
            self.Logger.info("Prusa Link job action requested, but no active job id is known. Action: %s", action)
            return False

        if action == "pause":
            method = "PUT"
            path = f"/api/v1/job/{state.JobId}/pause"
        elif action == "resume":
            method = "PUT"
            path = f"/api/v1/job/{state.JobId}/resume"
        elif action == "cancel":
            method = "DELETE"
            path = f"/api/v1/job/{state.JobId}"
        else:
            raise Exception(f"Unknown Prusa Link job action: {action}")

        try:
            response = self._Request(method, path)
            if response.status_code == 200 or response.status_code == 204:
                self.SleepEvent.set()
                return True
            self.Logger.error("Prusa Link job action failed. Action: %s Status: %s Body: %s", action, response.status_code, response.text[:300])
        except Exception as e:
            if Sentry.IsCommonConnectionException(e):
                self.Logger.warning("Prusa Link job action connection error. Action: %s Error: %s", action, e)
            else:
                Sentry.OnException("Prusa Link job action failed.", e)
        return False


    def _ClientWorker(self) -> None:
        while True:
            ipOrHostname = "None"
            try:
                self._CleanupStateOnDisconnect(clearCachedInfo=False)
                connectionContext = self._GetConnectionContextToTry()
                ipOrHostname = connectionContext.IpOrHostname
                self.CurrentConnectionContext = connectionContext
                self._ApplyConnectionContext(connectionContext)

                self.Logger.info("Trying to connect to Prusa Link printer at %s:%s...", ipOrHostname, connectionContext.PortStr)

                self.Version = self._GetJson("/api/version")
                self.Info = self._GetJson("/api/v1/info")
                self.Logger.info("Prusa Link version info: %s", self._GetVersionString())

                self._PollStatus(isFirstFullSyncResponse=True)
                self.LastConnectionFailedDueToAuth = False
                self.Connected = True
                self.ConnectionFinalized = True
                self.ConsecutivelyFailedConnectionAttempts = 0
                LocalWebApi.Get().SetPrinterConnectionState(True)
                self.Logger.info("Prusa Link client connection fully connected.")

                while True:
                    self._PollStatus(isFirstFullSyncResponse=False)
                    if self.SleepEvent.wait(PrusaLinkClient.PollIntervalSec):
                        self.SleepEvent.clear()
            except Exception as e:
                if isinstance(e, PrusaLinkAuthException):
                    self.LastConnectionFailedDueToAuth = True
                    self.Logger.error("Prusa Link authentication failed. Check the Prusa Link username/password or API key in the config.")
                elif Sentry.IsCommonConnectionException(e):
                    self.LastConnectionFailedDueToAuth = False
                    self.Logger.warning("Prusa Link printer connection error: %s", str(e))
                else:
                    self.LastConnectionFailedDueToAuth = False
                    Sentry.OnException(f"Failed to connect to the Prusa Link printer {ipOrHostname}. We will retry in a bit.", e)

            wasFullyConnected = self.ConnectionFinalized
            self._CleanupStateOnDisconnect(clearCachedInfo=False)
            LocalWebApi.Get().SetPrinterConnectionState(False)
            self.StateTranslator.OnConnectionLost(wasFullyConnected)

            self.ConsecutivelyFailedConnectionAttempts += 1
            sleepDelay = min(self.ConsecutivelyFailedConnectionAttempts, 6)
            sleepDelaySec = 5.0 * sleepDelay
            self.Logger.info("Sleeping for %s seconds before trying to reconnect to the Prusa Link printer.", sleepDelaySec)
            self.SleepEvent.wait(sleepDelaySec)
            self.SleepEvent.clear()


    def _PollStatus(self, isFirstFullSyncResponse:bool) -> None:
        status: Optional[Dict[str, Any]] = self._GetJson("/api/v1/status")
        if status is None:
            raise Exception("Failed to get status from Prusa Link printer.")

        job:Optional[Dict[str, Any]] = None
        statusJob:Optional[Dict[str, Any]] = status.get("job", None)
        if isinstance(statusJob, dict) and statusJob.get("id", None) is not None:
            job = self._GetJson("/api/v1/job", allowNoContent=True)
        else:
            printer:Optional[Dict[str, Any]] = status.get("printer", None)
            if printer is not None and isinstance(printer, dict):
                printerState = str(printer.get("state", "")).upper()
                if printerState in ["PRINTING", "PAUSED", "FINISHED", "STOPPED", "ERROR"]:
                    job = self._GetJson("/api/v1/job", allowNoContent=True)

        isFirstStateUpdate = self.State is None
        if self.State is None:
            self.State = PrinterState(self.Logger)
            self.Logger.debug("Prusa Link printer state object created.")

        self.State.OnUpdate(status, job, self.Info)
        self.StateTranslator.OnStatusUpdate(self.State, isFirstFullSyncResponse or isFirstStateUpdate)


    # Only returns None if 204 is allowed, otherwise this will return json or throw if there's an error.
    def _GetJson(self, path:str, allowNoContent:bool=False) -> Optional[Dict[str, Any]]:
        response = self._Request("GET", path)
        if response.status_code == 204 and allowNoContent:
            return None
        if response.status_code == 401 or response.status_code == 403:
            raise PrusaLinkAuthException()
        if response.status_code < 200 or response.status_code >= 300:
            raise Exception(f"Prusa Link request failed. Path: {path} Status: {response.status_code} Body: {response.text[:300]}")
        if response.text is None or len(response.text) == 0:
            return {}
        result = response.json()
        if isinstance(result, dict):
            return result
        raise Exception(f"Prusa Link request returned non-object JSON. Path: {path}")


    def _Request(self, method:str, path:str) -> requests.Response:
        context = self.CurrentConnectionContext
        if context is None:
            raise Exception("Prusa Link request was called with no connection context.")
        url = context.GetBaseUrl() + path
        with self.SessionLock:
            return self.Session.request(
                method,
                url,
                headers={"Accept": "application/json"},
                timeout=PrusaLinkClient.RequestTimeoutSec,
                allow_redirects=False,
                verify=False,
            )


    def _GetConnectionContextToTry(self) -> PrusaLinkConnectionContext:
        configIpOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        if configIpOrHostname is None or len(configIpOrHostname) == 0:
            raise Exception("An IP address or hostname must be provided in the config for Prusa Link Connect.")

        portStr = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, Config.PrusaLinkDefaultPortStr)
        if portStr is None or len(portStr) == 0:
            portStr = Config.PrusaLinkDefaultPortStr
        try:
            portInt = int(portStr)
            if portInt <= 0 or portInt > 65535:
                raise ValueError("port out of range")
        except Exception as e:
            raise Exception("The configured Prusa Link port must be a number between 1 and 65535.") from e

        authMode = self.Config.GetStr(Config.SectionPrusaLink, Config.PrusaLinkAuthMode, Config.PrusaLinkAuthModePassword)
        if authMode is None:
            authMode = Config.PrusaLinkAuthModePassword
        authMode = authMode.lower().strip()

        username = self.Config.GetStr(Config.SectionPrusaLink, Config.PrusaLinkUsername, None)
        password = self.Config.GetStr(Config.SectionPrusaLink, Config.PrusaLinkPassword, None)
        apiKey = self.Config.GetStr(Config.SectionPrusaLink, Config.PrusaLinkApiKey, None)

        if authMode == Config.PrusaLinkAuthModeApiKey:
            if apiKey is None or len(apiKey) == 0:
                raise Exception("A Prusa Link API key must be provided in the config for API key auth mode.")
        else:
            authMode = Config.PrusaLinkAuthModePassword
            if username is None or len(username) == 0:
                raise Exception("A Prusa Link username must be provided in the config for password auth mode.")
            if password is None or len(password) == 0:
                raise Exception("A Prusa Link password must be provided in the config for password auth mode.")

        return PrusaLinkConnectionContext(configIpOrHostname, portStr, authMode, username, password, apiKey)


    def _ApplyConnectionContext(self, context:PrusaLinkConnectionContext) -> None:
        LocalIpHelper.SetConnectionTargetIpOverride(context.IpOrHostname)
        OctoHttpRequest.SetLocalHostAddress(context.IpOrHostname)
        OctoHttpRequest.SetLocalOctoPrintPort(int(context.PortStr))
        OctoHttpRequest.SetLocalHttpProxyPort(int(context.PortStr))
        OctoHttpRequest.SetLocalHttpProxyIsHttps(False)
        OctoHttpRequest.SetLocalHostUseHttps(False)

        self._ApplyAuthToSession(self.Session, context)

        try:
            relaySession = HttpSessions.GetSession(context.GetBaseUrl())
            self._ApplyAuthToSession(relaySession, context)
        except Exception as e:
            self.Logger.warning("Failed to apply Prusa Link auth to shared HTTP session. %s", e)


    def _ApplyAuthToSession(self, session:requests.Session, context:PrusaLinkConnectionContext) -> None:
        if context.AuthMode == Config.PrusaLinkAuthModeApiKey:
            session.auth = None
            if context.ApiKey is not None:
                session.headers.update({"X-Api-Key": context.ApiKey})
            return

        session.headers.pop("X-Api-Key", None)
        session.auth = HTTPDigestAuth(context.Username or "", context.Password or "")


    def _CleanupStateOnDisconnect(self, clearCachedInfo:bool) -> None:
        self.State = None
        self.Connected = False
        self.ConnectionFinalized = False
        if clearCachedInfo:
            self.Version = None
            self.Info = None


    def _GetVersionString(self) -> str:
        version = self.Version
        if version is None:
            return "Unknown"
        text = version.get("text", None)
        if text is not None:
            return str(text)
        parts:List[str] = []
        for key in ["version", "printer", "firmware"]:
            value = version.get(key, None)
            if value is not None:
                parts.append(str(value))
        if len(parts) == 0:
            return "Unknown"
        return "-".join(parts)
