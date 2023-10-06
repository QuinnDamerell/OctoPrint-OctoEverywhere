from octoeverywhere.compat import Compat
from octoeverywhere.sentry import Sentry

from .moonrakerclient import MoonrakerClient

# Implements the platform specific logic for api router logic.
# This is the logic that allows one instance of mainsail or fluidd to hit multiple moonraker instances.
# Since we force mainsail (and fluidd) to always connect to the default /websocket when the plugin instance
# get the call it can route it to the correct moonraker API.
#
# Note that we only do this for moonraker, since the webcams are already setup to be shared, we dont have to mess
# with them.
class MoonrakerApiRouter:

    # The static instance.
    _Instance = None

    @staticmethod
    def Init(logger):
        MoonrakerApiRouter._Instance = MoonrakerApiRouter(logger)
        Compat.SetApiRouterHandler(MoonrakerApiRouter._Instance)


    @staticmethod
    def Get():
        return MoonrakerApiRouter._Instance


    def __init__(self, logger):
        self.Logger = logger
        self.MoonrakerPortStr = None

        # Get the moonraker port from the config.
        (ipOrHostnameStr, portInt) = MoonrakerClient.Get().GetMoonrakerHostAndPortFromConfig()
        if ipOrHostnameStr is None or portInt is None:
            return
        self.MoonrakerHostAndPortStr = f"{ipOrHostnameStr}:{str(portInt)}"
        self.Logger.info("MoonrakerApiRouter using bound to moonraker at "+self.MoonrakerHostAndPortStr)


    # !! Interface Function !! This implementation must not change!
    # Must return an absolute URL if it's being updated, otherwise None.
    #
    # This is only needed for relative paths, since absolute paths can't be mapped like this.
    # Basically the frontend is going to always call the https://<sub>.octoeverywhere.com/<websocket/printer/etc>
    # Since the subdomain will map the request to the correct instance bound to the moonraker instance, the
    # plugin can figure which calls are for moonraker and map them to the known instance port.
    # Note this will be used by both websockets and http calls.
    def MapRelativePathToAbsolutePathIfNeeded(self, relativeUrl, protocol):
        # If we have no port, do nothing.
        if self.MoonrakerHostAndPortStr is None:
            return None
        try:
            # Basically we need to map all of moonraker's APIs,
            # which can be found in the moonraker APIs docs or by looking at the nginx configs
            #    cat /etc/nginx/sites-available/mainsail
            #    cat /etc/nginx/sites-available/fluidd
            #
            # Remember that there can be whatever suffixes or ? arguments on the URLs
            relativeUrlLower = relativeUrl.lower()
            if relativeUrlLower.startswith("/websocket") or relativeUrlLower.startswith("/printer/") or relativeUrlLower.startswith("/api/") or relativeUrlLower.startswith("/access/") or relativeUrlLower.startswith("/machine/") or relativeUrlLower.startswith("/server/") or relativeUrlLower.startswith("/debug/"):
                return protocol + self.MoonrakerHostAndPortStr + relativeUrl
        except Exception as e:
            Sentry.Exception("MoonrakerApiRouter exception while handling MapRelativePathToAbsolutePathIfNeeded.", e)
        return None
