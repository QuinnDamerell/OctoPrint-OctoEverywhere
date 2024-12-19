import json
import codecs
import base64
import logging
import threading
from enum import Enum

import requests

from linux_host.config import Config

from octoeverywhere.sentry import Sentry


# The result of a login request.
class LoginStatus(Enum):
    Success               = 0  # This is the only successful value
    TwoFactorAuthEnabled  = 1
    BadUserNameOrPassword = 2
    EmailCodeRequired     = 3
    UnknownError          = 4


# The result of a get access token request.
# If the token is None, the Status will indicate why.
class AccessTokenResult():
    def __init__(self, status:LoginStatus, token:str = None) -> None:
        self.Status = status
        self.AccessToken = token


# A class that interacts with the Bambu Cloud.
# This github has a community made API docs:
# https://github.com/Doridian/OpenBambuAPI/blob/main/cloud-http.md
class BambuCloud:

    _Instance = None


    @staticmethod
    def Init(logger:logging.Logger, config:Config):
        BambuCloud._Instance = BambuCloud(logger, config)


    @staticmethod
    def Get():
        return BambuCloud._Instance


    def __init__(self, logger:logging.Logger, config:Config) -> None:
        self.Logger = logger
        self.Config = config
        self.AccessToken = None


    # Logs in given the user name and password. This doesn't support two factor auth at this time.
    # Returns true if the login was successful, otherwise false.
    def Login(self) -> LoginStatus:
        try:
            # Some notes on login. We were going to originally going to cache the access token and refresh token, so we didn't have to store the user name and password.
            # However, the refresh token has an expiration on it, so eventually the user would have to re-enter their password, which isn't ideal.
            # We also don't gain anything by storing the access token, since we need to hit an API anyways to make sure it's still valid and working.
            self.Logger.info("Logging into Bambu Cloud...")

            # Get the correct URL.
            url = self._GetBambuCloudApi("/v1/user-service/user/login")

            # Get the context.
            email, password = self.GetContext()
            if email is None or password is None:
                self.Logger.error("Login Bambu Cloud failed to get context from the config.")
                return LoginStatus.BadUserNameOrPassword

            # Make the request.
            response = requests.post(url, json={'account': email, 'password': password}, timeout=30)

            # Check the response.
            if response.status_code != 200:
                body = ""
                try:
                    body = json.dumps(response.json())
                except Exception:
                    pass
                if response.status_code == 400:
                    self.Logger.error(f"Login Bambu Cloud failed with status code: 400 bad request. The user name or password are probably wrong or has changed. Response: {body}")
                    return LoginStatus.BadUserNameOrPassword
                self.Logger.error(f"Login Bambu Cloud failed with status code: {response.status_code}, Response: {body}")
                return LoginStatus.UnknownError

            # If the user has two factor auth enabled, this will still return 200, but there will be a tfaKey field with a string.
            j = response.json()
            tfaKey = j.get('tfaKey', None)
            if tfaKey is not None and len(tfaKey) > 0:
                self.Logger.error("Login Bambu Cloud failed because two factor auth is enabled. Bambu Lab's APIs don't allow us to support two factor at this time.")
                return LoginStatus.TwoFactorAuthEnabled

            # Try to get the access token
            accessToken = j.get('accessToken', None)
            if accessToken is None or len(accessToken) == 0:
                self.Logger.error("Login Bambu Cloud failed because access token was not found in the response.")
                return LoginStatus.UnknownError
            self.AccessToken = accessToken

            # The token expiration is usually 1 year, we just check it for now.
            expiresIn = int(j.get('expiresIn', 0))
            if expiresIn / 60 / 60 / 24 < 300:
                self.Logger.warn(f"Login Bambu Cloud access token expires in {expiresIn} seconds")

            # Every time we login in, we also want to ensure the printer's cloud info is synced locally.
            # Right now this can only sync the access code, but this is important, because things like the webcam streaming need to know the access code.
            self.SyncBambuCloudInfoAsync()

            # Success
            return LoginStatus.Success

        except Exception as e:
            Sentry.Exception("Bambu Cloud login exception", e)
        return  LoginStatus.UnknownError


    # Returns the access token.
    # If there's no valid access token, this will try a blocking login.
    def GetAccessToken(self, forceLogin = False) -> AccessTokenResult:
        # If we already have the access token, we are good.
        if forceLogin is False and self.AccessToken is not None and len(self.AccessToken) > 0:
            return AccessTokenResult(LoginStatus.Success, self.AccessToken)

        # Else, try a login.
        status = self.Login()
        return AccessTokenResult(status, self.AccessToken)


    # Used to clear the access token if there's a failure using it.
    def _ResetAccessToken(self):
        self.AccessToken = None


    # A helper to decode the access token and get the Bambu Cloud username.
    # Returns None on failure.
    def GetUserNameFromAccessToken(self, accessToken: str) -> str:
        try:
            # The Access Token is a JWT, we need the second part to decode.
            accountInfoBase64 = accessToken.split(".")[1]
            # The string len must be a multiple of 4, padded with "="
            while (len(accountInfoBase64)) % 4 != 0:
                accountInfoBase64 += "="
            # Decode and parse as json.
            jsonAuthToken = json.loads(base64.b64decode(accountInfoBase64))
            return jsonAuthToken["username"]
        except Exception as e:
            Sentry.Exception("Bambu Cloud GetUserNameFromAccessToken exception", e)
        return None


    # Returns a list of the user's devices.
    # Returns None on failure.
    # Special Note: This function is used as a access token validation check. So if this fails due to the access token being invalid, the access token should be cleared so we try to login again.
    def GetDeviceList(self) -> dict:
        tokenResult = self.GetAccessToken()
        if tokenResult.Status != LoginStatus.Success:
            return None

        # Get the API
        url = self._GetBambuCloudApi("/v1/iot-service/api/user/bind")

        # Make the request.
        headers = {'Authorization': 'Bearer ' + tokenResult.AccessToken}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            self.Logger.error(f"Bambu Cloud GetDeviceList failed with status code: {response.status_code}")
            # On failure reset the access token.
            self._ResetAccessToken()
            return None
        self.Logger.debug(f"Bambu Cloud Device List: {response.json()}")
        devices = response.json().get('devices', None)
        if devices is None:
            self.Logger.error("Bambu Cloud GetDeviceList failed, the devices object was missing.")
            return None
        return response.json()['devices']


    # Returns this device info from the Bambu Cloud API by matching the SN
    def GetThisDeviceInfo(self) -> dict:
        devices = self.GetDeviceList()
        localSn = self.Config.GetStr(Config.SectionBambu, Config.BambuPrinterSn, None)
        if localSn is None:
            self.Logger.error("Bambu Cloud GetThisDeviceInfo has no local printer SN to match.")
            return None
        for d in devices:
            sn =  d.get('dev_id', None)
            self.Logger.debug(f"Bambu Cloud Printer Info. SN:{sn} Name:{(d.get('name', None))}")
            if sn == localSn:
                return d
        self.Logger.error("Bambu Cloud failed to find a matching printer SN on the user account.")
        return None


    # Get's the known device info from the Bambu API and ensures it's synced with our config settings.
    def SyncBambuCloudInfoAsync(self) -> bool:
        threading.Thread(target=self.SyncBambuCloudInfo, daemon=True).start()


    def SyncBambuCloudInfo(self) -> bool:
        try:
            info = self.GetThisDeviceInfo()
            if info is None:
                self.Logger.error("Bambu Cloud SyncBambuCloudInfo didn't find printer info.")
                return False
            accessCode = info.get('dev_access_code', None)
            if accessCode is None:
                self.Logger.error("Bambu Cloud SyncBambuCloudInfo didn't find an access code.")
                return False
            # It turns out that sometimes the Access Code from the service wrong, so we only update
            # it if there's no access token set, so the user can override it in the config.
            if self.Config.GetStr(Config.SectionBambu, Config.BambuAccessToken, None) is None:
                self.Config.SetStr(Config.SectionBambu, Config.BambuAccessToken, accessCode)
                self.Logger.info("Bambu Cloud SyncBambuCloudInfo updated the access code.")
            return True
        except Exception as e:
            Sentry.Exception("SyncBambuCloudInfo exception", e)
        return False


    def _IsRegionChina(self) -> bool:
        region = self.Config.GetStr(Config.SectionBambu, Config.BambuCloudRegion, None)
        if region is None:
            self.Logger.warn("Bambu Cloud region not set, assuming world wide.")
            region = "world"
        return region == "china"


    # Returns the correct MQTT hostname depending on the region.
    def GetMqttHostname(self):
        if self._IsRegionChina():
            return "cn.mqtt.bambulab.com"
        return "us.mqtt.bambulab.com"


    # Returns the correct full API URL based on the region.
    def _GetBambuCloudApi(self, urlPathAndParams:str):
        if self._IsRegionChina():
            return "https://api.bambulab.cn" + urlPathAndParams
        return "https://api.bambulab.com" + urlPathAndParams


    # Sets the user's context into the config file.
    def SetContext(self, email:str, p:str) -> bool:
        try:
            # This isn't ideal, but there's nothing we can do better locally on the device.
            # So at least it's not just plain text.
            data = {"email":email, "p":p}
            j = json.dumps(data)
            # In the past we used the crypo lib to actually do crypto with a static key here in the code.
            # But the crypo lib had a lot of native lib requirements and it caused install issues.
            # Since we were using a static key anyways, we will just do this custom obfuscation function.
            token = self._ObfuscateString(j)
            self.Config.SetStr(Config.SectionBambu, Config.BambuCloudContext, token)
            return True
        except Exception as e:
            Sentry.Exception("Bambu Cloud set email exception", e)
        return False


    # Returns if there's a user context in the config file.
    # This doesn't check if the user context is valid, just that it's there.
    def HasContext(self) -> bool:
        (e, p) = self.GetContext()
        return e is not None and p is not None


    # Sets the user's context from the config file.
    def GetContext(self, expectContextToExist = True):
        try:
            token = self.Config.GetStr(Config.SectionBambu, Config.BambuCloudContext, None)
            if token is None:
                if expectContextToExist:
                    self.Logger.error("No Bambu Cloud context found in the config file.")
                return (None, None)
            jsonStr = self._UnobfuscateString(token)
            data = json.loads(jsonStr)
            e = data.get("email", None)
            p = data.get("p", None)
            if e is None or p is None:
                self.Logger.error("No Bambu Cloud context was missing required data.")
                return (None, None)
            return (e, p)
        except Exception as e:
            Sentry.Exception("Bambu Cloud login exception", e)
        return (None, None)


    # The goal here is just to obfuscate the string with a unique algo, so the email and password aren't just plain text in the config file.
    def _ObfuscateString(self, s:str) -> str:
        # First, base64 encode the string.
        base64Str = base64.b64encode(s.encode(encoding="utf-8"))
        # First, next, rotate it.
        return codecs.encode(base64Str.decode(encoding="utf-8"), 'rot13')


    def _UnobfuscateString(self, s:str) -> str:
        # Un-rotate
        base64String = codecs.decode(s, 'rot13')
        # Un-base64 encode
        return base64.b64decode(base64String).decode(encoding="utf-8")
