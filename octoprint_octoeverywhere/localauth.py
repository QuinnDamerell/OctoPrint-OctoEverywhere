import random
import string

from octoprint.access.permissions import Permissions
from octoeverywhere.compat import Compat

# A class that manages the local auth rights of OctoEverywhere.
#
# To keep OctoEverywhere as secure as possible, we never store OctoPrint access data in our services.
# This keeps multiple layers of security as a bad actor would have to gain remote access to the printer via OctoEverywhere
# and then also gain OctoPrint credential access.
#
# So the plugin will try to establish an app key for itself to use for API calls. The plugin has full access to these systems internally
# but using the API calls makes the flow for the service to use the APIs easier. When a special http request message is sent from the server,
# the plugin will add the app key auth headers to the call. Since the server's validity has been secured by the RSA challenge, we know that only,
# OctoEverywhere servers are able to set the flag to make the requests.
#
# We use an plugin hook that only exists since OctoPrint 1.3.6. If this is running on older versions our authed calls will fail because we won't get the
# ValidateApiKey callback. Less than 2% of all global OctoPrint instances are running 1.3.12
class LocalAuth:

    _Instance = None
    # We use the same key length as OctoPrint, because why not.
    _ApiGeneratedKeyLength = 32


    @staticmethod
    def Init(logger, userManager):
        LocalAuth._Instance = LocalAuth(logger, userManager)
        # Since this platform supports this object, set it in our compat layer.
        Compat.SetLocalAuth(LocalAuth._Instance)


    @staticmethod
    def Get():
        return LocalAuth._Instance


    def __init__(self, logger, userManager):
        self.Logger = logger
        # Note our test running main.py passes None for the userManger.
        # But it will never call ValidateApiKey anyways.
        self.OctoPrintUserManager = userManager
        # Create a new random API key each time OctoPrint is started so we don't have to write it to disk and it changes over time.
        self.ApiKey = ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(LocalAuth._ApiGeneratedKeyLength))


    # Used only for testing without actual OctoPrint, this can set the API key
    # that's actually created in a real OctoPrint instance.
    def SetApiKeyForTesting(self, apiKey):
        self.Logger.warn("LocalAuth is using a dev API key: "+str(apiKey))
        self.ApiKey = apiKey


    # Adds the auth header with the auth key.
    def AddAuthHeader(self, headers):
        # This will overwrite any existing keys.
        headers["X-Api-Key"] = self.ApiKey


    # Called by OctoPrint when a request is made with an API key.
    # If the key is invalid or we don't know, return None, otherwise we must return a user.
    # See for an example: https://github.com/OctoPrint/OctoPrint/blob/master/src/octoprint/plugins/appkeys/__init__.py
    def ValidateApiKey(self, api_key):
        # If the key doesn't match our auth key, we have nothing to do.
        if(api_key is None or api_key != self.ApiKey):
            return None

        # This is us trying to make a request.
        # We need to return a valid user with admin permissions.
        allUsers = self.OctoPrintUserManager.get_all_users()
        for user in allUsers:
            if user.has_permission(Permissions.ADMIN):
                return user

        self.Logger.warn("Failed to find local user with admin permissions to return for authed call.")
        return None
