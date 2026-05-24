import getpass
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.auth import HTTPDigestAuth

from linux_host.config import Config
from linux_host.networksearch import NetworkSearch

from py_installer.ConfigHelper import ConfigHelper
from py_installer.Context import Context
from py_installer.Logging import Logger
from py_installer.Util import Util


class _PrusaLinkAuthDetails:
    def __init__(self, authMode:str, username:Optional[str]=None, password:Optional[str]=None, apiKey:Optional[str]=None) -> None:
        self.AuthMode = authMode
        self.Username = username
        self.Password = password
        self.ApiKey = apiKey


class _PrusaLinkValidationResult:
    def __init__(
        self,
        failedToConnect:bool=False,
        failedAuth:bool=False,
        success:bool=False,
        versionText:Optional[str]=None,
        printerName:Optional[str]=None,
        exception:Optional[Exception]=None,
    ) -> None:
        self.FailedToConnect = failedToConnect
        self.FailedAuth = failedAuth
        self.IsSuccess = success
        self.VersionText = versionText
        self.PrinterName = printerName
        self.Exception = exception


    def Success(self) -> bool:
        return self.Exception is None and self.FailedToConnect is False and self.FailedAuth is False and self.IsSuccess


class PrusaLinkConnector:

    def EnsurePrusaLinkConnection(self, context:Context) -> None:
        Logger.Debug("Running Prusa Link connect ensure config logic.")

        if self._TryExistingPrusaLinkConnection(context):
            return

        ip, port, authDetails = self._SetupNewPrusaLinkConnection(context)
        ConfigHelper.WriteCompanionDetails(context, ip, port)
        self._WriteAuthDetails(context, authDetails)

        Logger.Info(f"Prusa Link was found and authentication was successful! IP: {ip}:{port}")
        Logger.Blank()
        Logger.Header("Prusa Link connection successful!")
        Logger.Blank()


    def _TryExistingPrusaLinkConnection(self, context:Context) -> bool:
        ip, port = ConfigHelper.TryToGetCompanionDetails(context)
        authMode, username, password, apiKey = ConfigHelper.TryToGetPrusaLinkData(context)
        if ip is None or port is None:
            return False

        authDetails = self._NormalizeAuthDetails(authMode, username, password, apiKey)
        if authDetails is None:
            return False

        Logger.Info(f"Existing Prusa Link config found. IP: {ip}")
        Logger.Info("Checking if we can connect to Prusa Link...")
        result = self._ValidateConnection(ip, port, authDetails, timeoutSec=10.0)
        if result.Success():
            Logger.Info("Successfully connected to Prusa Link! Printer Name: " + (result.PrinterName or "Unknown"))
            return True

        Logger.Blank()
        if result.FailedAuth:
            Logger.Warn(f"We connected to Prusa Link at {ip}, but authentication failed.")
        else:
            Logger.Warn(f"We failed to connect to Prusa Link using {ip}.")
        Logger.Blank()
        if Util.AskYesOrNoQuestion("Do you want to setup the Prusa Link connection again?") is False:
            Logger.Info(f"Keeping the existing Prusa Link connection setup. {ip}")
            return True
        return False


    def _SetupNewPrusaLinkConnection(self, context:Context) -> Tuple[str, str, _PrusaLinkAuthDetails]:
        while True:
            Logger.Blank()
            Logger.Blank()
            Logger.Blank()
            Logger.Header("##################################")
            Logger.Header("        Prusa Link Setup")
            Logger.Header("##################################")
            Logger.Blank()
            Logger.Info("OctoEverywhere connects to your Prusa Link 3D printer on your local network.")
            Logger.Info("For access, you can supply either your Prusa Link username and password or an API key.")
            Logger.Info("Your Prusa Link credentials are only stored on this device and will not be uploaded.")

            Logger.Blank()

            authDetails = self._AskForAuthDetails(context)

            Logger.Blank()
            Logger.Warn("Searching for Prusa Link on your network, this can take about 10 seconds...")
            ips = self._ScanForPrusaLinkPrinters(authDetails)
            Logger.Blank()

            if len(ips) == 1:
                return (ips[0], Config.PrusaLinkDefaultPortStr, authDetails)

            if len(ips) > 1:
                Logger.Info("Prusa Link was found on the following IP addresses:")
                for i, ip in enumerate(ips):
                    Logger.Info(f"  {i + 1}) {ip}:{Config.PrusaLinkDefaultPortStr}")
                Logger.Info("  m) Enter the IP address manually")
                while True:
                    response = input("Enter the number next to the Prusa Link you want to use or enter `m`: ").lower().strip()
                    if response == "m":
                        break
                    try:
                        selection = int(response) - 1
                        if selection >= 0 and selection < len(ips):
                            return (ips[selection], Config.PrusaLinkDefaultPortStr, authDetails)
                    except Exception:
                        pass
                    Logger.Warn("Invalid selection, try again.")

            Logger.Info("No Prusa Link 3D printers could be automatically found with those credentials.")
            while True:
                manualResult = self._SetupPrusaLinkConnectionManually(authDetails)
                if manualResult is not None:
                    ip, port = manualResult
                    return (ip, port, authDetails)

                Logger.Blank()
                if Util.AskYesOrNoQuestion("Do you want to re-enter your Prusa Link credentials and try again?"):
                    break
                Logger.Info("Keeping the same credentials and trying manual setup again.")


    def _SetupPrusaLinkConnectionManually(self, authDetails:_PrusaLinkAuthDetails) -> Optional[Tuple[str, str]]:
        while True:
            Logger.Blank()
            Logger.Info("Enter your Prusa Link server's IP address or hostname.")
            Logger.Info("For help finding your printer's IP address, see: https://octoeverywhere.com/s/prusa-link-ip")
            Logger.Blank()
            ipOrHostname = input("Enter the IP Address or Hostname: ").strip()
            ipOrHostname = self._CleanIpOrHostname(ipOrHostname)
            if len(ipOrHostname) == 0:
                Logger.Error("The IP address or hostname can't be empty.")
                continue

            port = input(f"Enter the Prusa Link port, or press enter to use the default ({Config.PrusaLinkDefaultPortStr}): ").strip()
            if len(port) == 0:
                port = Config.PrusaLinkDefaultPortStr
            if self._IsValidPort(port) is False:
                Logger.Error("The Prusa Link port must be a number between 1 and 65535.")
                continue

            Logger.Blank()
            Logger.Info(f"Trying to connect to Prusa Link at {ipOrHostname}:{port}...")
            result = self._ValidateConnection(ipOrHostname, port, authDetails, timeoutSec=10.0)
            Logger.Blank()

            if result.Success():
                return (ipOrHostname, port)

            if result.FailedAuth:
                Logger.Error("Failed to authenticate to Prusa Link. The username/password or API key was incorrect.")
                return None

            Logger.Error("Failed to connect to Prusa Link. Ensure the printer is powered on, connected to the network, and the IP/port are correct.")
            if Util.AskYesOrNoQuestion("Do you want to enter the IP address again?"):
                continue
            return None


    def _AskForAuthDetails(self, context:Context) -> _PrusaLinkAuthDetails:
        oldAuthMode, oldUsername, oldPassword, oldApiKey = ConfigHelper.TryToGetPrusaLinkData(context)
        oldDetails = self._NormalizeAuthDetails(oldAuthMode, oldUsername, oldPassword, oldApiKey)
        if oldDetails is not None:
            Logger.Info("A previous Prusa Link authentication setup was found.")
            if Util.AskYesOrNoQuestion("Do you want to continue using the saved Prusa Link authentication details?"):
                return oldDetails

        Logger.Blank()
        selection = Util.AskMultipleChoiceQuestion("How do you want to authenticate to Prusa Link?", ["Username and Password", "API Key"])
        if selection == 0:
            Logger.Blank()
            username = None
            while username is None or len(username) == 0:
                username = input("Enter your Prusa Link username: ").strip()
                if len(username) == 0:
                    Logger.Error("The username can't be empty.")
            password = ""
            while len(password) == 0:
                password = getpass.getpass("Enter your Prusa Link password: ").strip()
                if len(password) == 0:
                    Logger.Error("The password can't be empty.")
            return _PrusaLinkAuthDetails(Config.PrusaLinkAuthModePassword, username=username, password=password)

        apiKey = ""
        while len(apiKey) == 0:
            Logger.Blank()
            Logger.Info("To find your Prusa Link API key:")
            Logger.Info("  1) Open the Prusa Link webpage and login with your credentials.")
            Logger.Info("  2) Click on the Settings tab in the top right.")
            Logger.Info("  3) Scroll down to the API key section.")
            Logger.Info("  4) If the 'API Key' is empty, click the 'Reset' button to generate a new API key.")
            Logger.Info("  5) Copy the API key and paste it below.")
            Logger.Blank()
            Logger.Info("If you need help, go here: https://octoeverywhere.com/s/prusa-link-api-key")
            Logger.Blank()
            apiKey = getpass.getpass("Enter your Prusa Link API key: ").strip()
            if len(apiKey) == 0:
                Logger.Error("The API key can't be empty.")
        return _PrusaLinkAuthDetails(Config.PrusaLinkAuthModeApiKey, apiKey=apiKey)


    def _NormalizeAuthDetails(self, authMode:Optional[str], username:Optional[str], password:Optional[str], apiKey:Optional[str]) -> Optional[_PrusaLinkAuthDetails]:
        if authMode is not None:
            authMode = authMode.lower().strip()
        if authMode == Config.PrusaLinkAuthModeApiKey:
            if apiKey is None or len(apiKey) == 0:
                return None
            return _PrusaLinkAuthDetails(Config.PrusaLinkAuthModeApiKey, apiKey=apiKey)

        if username is None or len(username) == 0:
            return None
        if password is None or len(password) == 0:
            return None
        return _PrusaLinkAuthDetails(Config.PrusaLinkAuthModePassword, username=username, password=password)


    def _WriteAuthDetails(self, context:Context, authDetails:_PrusaLinkAuthDetails) -> None:
        if authDetails.AuthMode == Config.PrusaLinkAuthModeApiKey:
            if authDetails.ApiKey is None:
                raise Exception("Prusa Link API key auth was selected but no API key was provided.")
            ConfigHelper.WritePrusaLinkApiKeyDetails(context, authDetails.ApiKey)
            return
        if authDetails.Username is None or authDetails.Password is None:
            raise Exception("Prusa Link digest auth was selected but username or password was missing.")
        ConfigHelper.WritePrusaLinkDigestDetails(context, authDetails.Username, authDetails.Password)


    def _ScanForPrusaLinkPrinters(self, authDetails:_PrusaLinkAuthDetails) -> List[str]:
        def callback(ip:str) -> _PrusaLinkValidationResult:
            return self._ValidateConnection(ip, Config.PrusaLinkDefaultPortStr, authDetails, timeoutSec=3.0)

        return NetworkSearch._ScanForInstances(Logger.GetPyLogger(), callback, returnAfterNumberFound=0, threadCount=50, perThreadDelaySec=0.0) # pylint: disable=protected-access


    def _ValidateConnection(self, ipOrHostname:str, portStr:str, authDetails:_PrusaLinkAuthDetails, timeoutSec:float=5.0) -> _PrusaLinkValidationResult:
        session:Optional[requests.Session] = None
        try:
            session = requests.Session()
            session.trust_env = False
            headers:Dict[str, str] = {"Accept": "application/json"}
            if authDetails.AuthMode == Config.PrusaLinkAuthModeApiKey:
                if authDetails.ApiKey is not None:
                    headers["X-Api-Key"] = authDetails.ApiKey
            else:
                session.auth = HTTPDigestAuth(authDetails.Username or "", authDetails.Password or "")

            # Make the request
            baseUrl = f"http://{ipOrHostname}:{portStr}"
            response = session.get(f"{baseUrl}/api/version", headers=headers, timeout=timeoutSec, allow_redirects=False)

            # We try to see if we can find the PrusaLink header, which 100% lets us know if this was a prusa link server.
            isPrusaLinkServer = False
            if response.headers is not None:
                serverHeader = response.headers.get("Server", "")
                if "PrusaLink" in serverHeader:
                    isPrusaLinkServer = True

            # Detect auth failure vs connection failure
            if response.status_code == 401 or response.status_code == 403:
                if isPrusaLinkServer:
                    Logger.Debug(f"Prusa Link authentication failure for {ipOrHostname}:{portStr}. Status code: {response.status_code}")
                    return _PrusaLinkValidationResult(failedAuth=True)
                else:
                    Logger.Info(f"Received a {response.status_code} status code from {ipOrHostname}:{portStr}, but it doesn't appear to be a Prusa Link server. Assuming it's a connection failure, not an auth failure.")
                    return _PrusaLinkValidationResult(failedToConnect=True)
            if response.status_code != 200:
                return _PrusaLinkValidationResult(failedToConnect=True)

            # We need to validate the response is from PrusaLink
            versionObj = response.json()
            if isinstance(versionObj, dict) is False or "api" not in versionObj or "server" not in versionObj or "text" not in versionObj or "capabilities" not in versionObj:
                return _PrusaLinkValidationResult(failedToConnect=True)

            # Try to read the printer name.
            printerName = None
            try:
                infoResponse = session.get(f"{baseUrl}/api/v1/info", headers=headers, timeout=timeoutSec, allow_redirects=False)
                if infoResponse.status_code == 200:
                    infoObj:Dict[str, Any] = infoResponse.json()
                    if isinstance(infoObj, dict):
                        name = infoObj.get("name", None)
                        if name is not None and len(name) > 0:
                            printerName = str(name)
                        else:
                            # If the name is empty, try to use the location field as a fallback since some printers put the printer name there instead.
                            location = infoObj.get("location", None)
                            if location is not None and len(location) > 0:
                                printerName = str(location)
            except Exception:
                pass
            return _PrusaLinkValidationResult(
                success=True,
                versionText=str(versionObj.get("text", versionObj.get("version", "PrusaLink"))),
                printerName=printerName,
            )
        except requests.exceptions.RequestException as e:
            Logger.Debug(f"Prusa Link validation connection failure for {ipOrHostname}:{portStr}: {e}")
            return _PrusaLinkValidationResult(failedToConnect=True, exception=e)
        except Exception as e:
            Logger.Debug(f"Prusa Link validation failure for {ipOrHostname}:{portStr}: {e}")
            return _PrusaLinkValidationResult(failedToConnect=True, exception=e)
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass


    def _CleanIpOrHostname(self, value:str) -> str:
        value = value.strip()
        if "://" in value:
            value = value[value.find("://") + 3:]
        if "/" in value:
            value = value[:value.find("/")]
        if ":" in value and value.count(":") == 1:
            value = value[:value.find(":")]
        return value


    def _IsValidPort(self, value:str) -> bool:
        try:
            port = int(value)
            return port > 0 and port <= 65535
        except Exception:
            return False
