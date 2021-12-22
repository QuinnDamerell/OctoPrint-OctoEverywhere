import threading
import json
import os

import requests

from .WebStream.octoheaderimpl import HeaderHelper
from .octohttprequest import OctoHttpRequest


# A class that manages the local auth rights of OctoEverywhere.
#
# To keep OctoEverywhere as secure as possible, we never store OctoPrint access data in our services.
# This keeps multiple layers of security as a bad actor would have to gain remote access to the printer via OctoEverywhere
# and then also gain OctoPrint credentail access.
#
# So the plugin will try to establish an app key for itself to use for API calls. The plugin has full access to these systems internally
# but using the API calls makes the flow for the service to use the APIs easier. When a special http request message is sent from the server,
# the plugin will add the app key auth headers to the call. Since the server's validity has been secured by the RSA challenge, we know that only,
# OctoEverywhere servers are able to set the flag to make the requests.
class LocalAuth:

    _OctoEverywhereAppName = "OctoEverywhere"
    _Instance = None

    @staticmethod
    def Init(logger, pluginDataFolderPath):
        LocalAuth._Instance = LocalAuth(logger, pluginDataFolderPath)

    @staticmethod
    def Get():
        return LocalAuth._Instance

    def __init__(self, logger, pluginDataFolderPath):
        self.Logger = logger
        self.KeyFilePath = os.path.join(pluginDataFolderPath, "AppKey.json")

        self.Lock = threading.Lock()
        self.IsRunningAuthCall = False

        # If we have an app key this is set, if not, it's None
        self.AppKey = None

        # Try to load the app key now
        self._LoadAppKeyFromFile()

    # If OctoEverywhere has been able to setup it's own local app key, this will return it.
    # Otherwise this returns None.
    # This must be thread safe.
    def GetOctoEverywhereAppKeyIfExists(self):
        return self.AppKey

    # If the auth header is created and valid, this adds the header and returns True
    # Otherwise, it returns false.
    def AddAuthHeaderIfPossible(self, headers):
        key = self.GetOctoEverywhereAppKeyIfExists()
        if key is None:
            return False
        # This will overwrite any existing keys.
        headers["X-Api-Key"] = key
        return True

    # Called when the OctoEverywhere app key failed to auth a request.
    def ReportOctoEverywhereAppKeyFailed(self):
        self.Logger.info("Invalid App Auth Key reported, destorying")
        self._InvalidateAppKey()

    # Called when a successfull api call has been made and we can use this opportunity to create an auth key.
    def GenerateOctoEverywhereAppKeyIfNeeded(self, initialHttpContext):

        # Check if we have a valid auth, if so, don't request new auth.
        if self.GetOctoEverywhereAppKeyIfExists() is not None:
            return

        # We will use the same headers the original request did.
        sendHeaders = HeaderHelper.GatherRequestHeaders(initialHttpContext, self.Logger)

        # Start the work on a new thread so we don't block the request thread.
        t = threading.Thread(target=self._GenerateAppKey, args=(sendHeaders,))
        t.start()

    def _GenerateAppKey(self, sendHeaders):
        # Note this must be thread safe! Many api calls can call GenerateOctoEverywhereAppKeyIfNeeded at once
        # but we only want to allow one auth attempt at a time.
        # We must set this back to false on exit!
        with self.Lock:
            if self.IsRunningAuthCall:
                return
            self.IsRunningAuthCall = True

        # Do the work. Do the work in a try catch in a function to ensure all exceptions and return calls
        # still make us clear the processing flag.
        try:
            self._GenerateAppKeysUnderLock(sendHeaders)
        except Exception as e:
            self.Logger.error("_GenerateAppKey failed "+str(e))

        # We must set this back to false on exit!
        with self.Lock:
            self.IsRunningAuthCall = False

    def _GenerateAppKeysUnderLock(self, sendHeaders):
        # We use the direct OctoPrint local host address and port, since it's the most likely to be successful.
        baseUrl = "http://" + OctoHttpRequest.LocalHostAddress + ":" + str(OctoHttpRequest.LocalOctoPrintPort)

        # First, we need to get the user name and check if they are an admin
        # Note that when a user isn't logged in, /api/login will be called and thus this will execute!
        # But that user will be a guest and they won't have permissions to generate the key.
        userLoginUrl = baseUrl + "/api/currentuser"
        response = requests.get(userLoginUrl, headers=sendHeaders)
        if response.status_code != 200:
            raise Exception("Failed to get user login "+str(response.status_code))

        # Parse
        jsonData = response.json()
        permissions = jsonData["permissions"]
        userName = jsonData["name"]

        # Ensure user is admin, otherwise we can't generate a key
        hasAdmin = False
        for permission in permissions:
            if permission.lower() == "admin":
                hasAdmin = True
                break

        if hasAdmin is False:
            # If the user isn't an admin, no big deal, but we can't do anything here.
            self.Logger.info("_GenerateAppKey had user login, but user isn't an admin.")
            return

        # Now try to generate the app key
        appKeysUrl = baseUrl + "/api/plugin/appkeys"
        response = requests.post(appKeysUrl, headers=sendHeaders, json={ "command":"generate", "app": LocalAuth._OctoEverywhereAppName, "user": userName })
        if response.status_code != 200:
            raise Exception("Call to API keys failed. "+str(response.status_code))

        # Parse
        jsonData = response.json()
        apiKey = jsonData["api_key"]
        if len(apiKey) == 0:
            raise Exception("App Keys Gen Call Success But there's no API key?")

        # On success, set the app key
        self._SetAppKey(apiKey)
        self.Logger.info("New OctoEverywhere App Auth Key Generated!")

    # Blocks to write the API key to our file and also sets it when successful.
    def _SetAppKey(self, appKey):
        try:
            # Try to save the file first.
            data = {}
            data['AppKey'] = appKey

            # pylint: disable=unspecified-encoding
            # encoding only supported in py3
            with open(self.KeyFilePath, 'w') as f:
                json.dump(data, f)

            # When successful, set the app key
            self.AppKey = appKey

        except Exception as e:
            # On any failure, reset the app key.
            self.AppKey = None
            self.Logger.error("_SetAppKeyToFile failed "+str(e))

    def _InvalidateAppKey(self):
        try:
            # Kill the local key
            self.AppKey = None

            # Delete the file.
            if os.path.exists(self.KeyFilePath) is False:
                return
            os.remove(self.KeyFilePath)

        except Exception as e:
            self.AppKey = None
            self.Logger.error("_InvalidateAppKey failed "+str(e))

    # Does a blocking call to load the app key from a file and store it in the
    # AppKey var.
    def _LoadAppKeyFromFile(self):
        try:
            # First check if there's a file. No file means no key.
            if os.path.exists(self.KeyFilePath) is False:
                self.AppKey = None
                return

            # Try to open it and get the key. Any failure will null out the key.
            # pylint: disable=unspecified-encoding
            # encoding only supported in py3
            with open(self.KeyFilePath) as f:
                data = json.load(f)
            appKey = data["AppKey"]

            # Validate
            if len(appKey) == 0:
                raise Exception("AppKey has no length")

            # Set
            self.AppKey = appKey
            self.Logger.info("App Auth Key loaded from file successfully")

        except Exception as e:
            # On any failure, reset the app key.
            self.AppKey = None
            self.Logger.error("_LoadAppKeyFromFile failed "+str(e))
