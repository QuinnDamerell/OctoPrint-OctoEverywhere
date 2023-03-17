import json

from octoeverywhere.compat import Compat
from octoeverywhere.sentry import Sentry

# Implements the platform specific logic for mainsails's config handler.
# This class handles the mainsail config.json file, that controls how the front end interacts with the backend(s).
class MainsailConfigHandler:


    # The static instance.
    _Instance = None


    @staticmethod
    def Init(logger):
        MainsailConfigHandler._Instance = MainsailConfigHandler(logger)
        Compat.SetMainsailConfigHandler(MainsailConfigHandler._Instance)


    @staticmethod
    def Get():
        return MainsailConfigHandler._Instance


    def __init__(self, logger):
        self.Logger = logger
        self.LastPauseNotificationSuppressionTimeSec = 0


    # !! Interface Function !! This implementation must not change!
    def HandleMoonrakerConfigRequest(self, bodyBuffer) -> bytes:
        try:
            #
            # Note that we identify this file just by dont a .endsWith("/config.json") to the URL. Thus other things could match it
            # and we need to be careful to only edit it if we find what we expect.
            #
            # Force the config to always point at "moonraker", which will force mainsail to always connect to the default instance of
            # moonraker running on the system at /websocket. Otherwise, if multiple instances are setup via Kiauh, this will be set to browser
            # and it will give the user a pop-up when they first load the portal.
            #
            # Right now we can't do anything else, because moonraker only allows the user to set custom hostname and ports, not paths, to call
            # the different websockets at. But in the future, we could look into redirecting the websocket and known moonraker http api paths to the
            # known moonraker instance running with this octoeverywhere instance.
            mainsailConfig = json.loads(bodyBuffer.decode("utf8"))
            if "instancesDB" in mainsailConfig:
                # Set mainsail and be sure to clear our any instances.
                mainsailConfig["instancesDB"] = "moonraker"
                mainsailConfig["instances"] = []
            return json.dumps(mainsailConfig, indent=4).encode("utf8")
        except Exception as e:
            Sentry.Exception("MainsailConfigHandler exception while handling mainsail config.", e)
        return bodyBuffer
