import threading
import time
import logging
import json

import requests

from octoeverywhere.sentry import Sentry
from octoeverywhere.Webcam.webcamhelper import WebcamSettingItem, WebcamHelper

from linux_host.config import Config

from .moonrakerclient import MoonrakerClient
from .moonrakerclient import JsonRpcResponse


# This class implements the webcam platform helper interface for moonraker.
class MoonrakerWebcamHelper():

    # The amount of time we will wait between settings checks.
    # These are also invoked when there's webcam activity, so we don't need to check too frequently.
    # Note that we get push notifications from moonraker if the settings change.
    c_DelayBetweenAutoSettingsCheckSec = 60 * 60 * 12

    # The min time between checks when there's webcam activity.
    # Note that we get push notifications from moonraker if the settings change.
    c_MinTimeBetweenWebcamActivityInvokesSec = 60 * 60 * 1

    # When the plugin starts, this is the delay we use before checking.
    # We want this to be shorter, so if something changed or this is the first install, we pickup the webcam settings quickly
    # We will always run once klippy is connected, but even if it's not, we should try to do a run.
    # Give the system just enough time to start up, then run.
    c_DelayForFirstRunAutoSettingsCheckSec = 5


    # Default settings.
    c_DefaultAutoSettings = True
    c_DefaultWebcamNameToUseAsPrimary = "Default"
    # Use relative paths for the defaults, because if they aren't correct our http system can try other options since they are relative.
    c_DefaultStreamUrl = "/webcam/?action=stream"
    c_DefaultSnapshotUrl = "/webcam/?action=snapshot"
    c_DefaultFlipH = False
    c_DefaultFlipV = False
    c_DefaultRotation = 0


    def __init__(self, logger:logging.Logger, config : Config) -> None:
        self.Logger = logger
        self.Config = config

        # Locks the cached results and local settings.
        # If auto settings are enabled, AutoSettingsResults will hold all webcams we could discover in the system.
        self.ResultsLock = threading.Lock()
        self.AutoSettingsResults = []

        # Set this to 0, so when the moonraker client will wake us up when the websocket is connected, and we don't want to miss it.
        self.AutoSettingsLastWake = 0

        # Always start the auto update thread, since this also monitors the auto settings state.
        self.AutoSettingsWorkerEvent = threading.Event()

        # Get this so it sets the default primary webcam name.
        self.Config.GetStr(Config.WebcamSection, Config.WebcamNameToUseAsPrimary, MoonrakerWebcamHelper.c_DefaultWebcamNameToUseAsPrimary)

        # Get this so it sets the default, if it's not set or is an incorrect value.
        self.EnableAutoSettings = self.Config.GetBool(Config.WebcamSection, Config.WebcamAutoSettings, MoonrakerWebcamHelper.c_DefaultAutoSettings)

        # Setup the manual values in the class and read what's currently in the config file.
        # This will also set the defaults into the file if they aren't there.
        # These will always be set. If auto settings is enabled, these will be set to the currently found auto settings.
        # Otherwise, these will be the user set values or the defaults.
        self.StreamUrl:str = MoonrakerWebcamHelper.c_DefaultStreamUrl
        self.SnapshotUrl:str = MoonrakerWebcamHelper.c_DefaultSnapshotUrl
        self.FlipH:bool = MoonrakerWebcamHelper.c_DefaultFlipH
        self.FlipV:bool = MoonrakerWebcamHelper.c_DefaultFlipV
        self.Rotation:int = MoonrakerWebcamHelper.c_DefaultRotation
        self._ReadManuallySetValues()

        t = threading.Thread(target=self._WebcamSettingsUpdateWorker)
        t.daemon = True
        t.start()


    # !! Interface Function !!
    # This must return an array of WebcamSettingItems.
    # Index 0 is used as the default webcam.
    # The order the webcams are returned is the order the user will see in any selection UIs.
    # Returns None on failure.
    def GetWebcamConfig(self):

        # Kick the settings worker since the webcam was accessed.
        self.KickOffWebcamSettingsUpdate()

        # Grab the lock to see what we should be returning.
        with self.ResultsLock:
            # If auto settings are enabled, return any cached auto settings that we found.
            # If we have anything, make a copy of the array and return it.
            if self.EnableAutoSettings:
                if len(self.AutoSettingsResults) != 0:
                    results = []
                    for i in self.AutoSettingsResults:
                        results.append(i)
                    return results
            # If we don't have auto settings enabled or we don't have any results, return what we have in memory.
            # This will either be the default values or a values that the user has set.
            item = WebcamSettingItem("Default", self.SnapshotUrl, self.StreamUrl, self.FlipH, self.FlipV, self.Rotation)
            # Validate the settings, but always return them.
            item.Validate(self.Logger)
            return [
                item
            ]


    # !! Interface Function !!
    # This function is called to determine if a QuickCam stream should keep running or not.
    # The idea is since a QuickCam stream can take longer to start, for example, the Bambu Websocket stream on sends 1FPS,
    # we can keep the stream running while the print is running to lower the latency of getting images.
    # Most most platforms, this should return true if the print is running or paused, otherwise false.
    # Also consider something like Gadget, it takes pictures every 20-40 seconds, so the stream will be started frequently if it's not already running.
    def ShouldQuickCamStreamKeepRunning(self) -> bool:
        # TODO - Ideally this should return true if we are printing.
        return False


    # Wakes up the auto settings worker.
    # Called by moonrakerclient when the websocket is connected, to ensure we pull settings on moonraker connections.
    def KickOffWebcamSettingsUpdate(self, forceUpdate = False):
        # Always kick off the thread, so if auto settings changes, we read it.
        timeSinceLastWakeSec = time.time() - self.AutoSettingsLastWake
        # Determine if we are still waiting to get auto settings.
        # When we first try to get the settings, we might not have a Moonraker connection and thus wont get settings.
        # In that case, we want to keep trying, since we will get kicked when the moonraker connection becomes valid.
        # If auto settings are enabled and we don't have results, we should try again.
        needToFindAutoSettings = False
        if self.EnableAutoSettings:
            with self.ResultsLock:
                needToFindAutoSettings = len(self.AutoSettingsResults) == 0

        if forceUpdate or needToFindAutoSettings or timeSinceLastWakeSec > MoonrakerWebcamHelper.c_MinTimeBetweenWebcamActivityInvokesSec:
            self.AutoSettingsLastWake = time.time()
            self.AutoSettingsWorkerEvent.set()


    # This is the main worker thread that keeps track of webcam settings.
    # This also keeps checking the auto settings option in the config, so we know if the user changes it.
    # Note that there's a moonraker command `notify_webcams_changed` we listen for. When it first we will force this update loop to run.
    def _WebcamSettingsUpdateWorker(self):
        isFirstRun = True
        while True:
            try:
                # Adjust the delay of our first run on plugin start.
                delayTimeSec = MoonrakerWebcamHelper.c_DelayBetweenAutoSettingsCheckSec
                if isFirstRun:
                    delayTimeSec = MoonrakerWebcamHelper.c_DelayForFirstRunAutoSettingsCheckSec
                    isFirstRun = False

                # Start the loop by clearing and waiting on the value. This means our first wake up will usually be either webcam activity
                # or the moonraker client telling us the websocket is connected. Note we also do a shorter time on first run, so if klippy isn't
                # in a ready state, we still try to check.
                self.AutoSettingsWorkerEvent.clear()
                self.AutoSettingsWorkerEvent.wait(delayTimeSec)

                # Force a config reload, so if the user changed this setting, we respect it.
                self.Config.ReloadFromFile()
                newAutoSettings = self.Config.GetBool(Config.WebcamSection, Config.WebcamAutoSettings, MoonrakerWebcamHelper.c_DefaultAutoSettings)

                # Log if the value changed.
                if self.EnableAutoSettings != newAutoSettings:
                    self.Logger.info("Webcam auto settings detection value updated: "+str(newAutoSettings))
                    self.EnableAutoSettings = newAutoSettings

                # Do an update if we should.
                if self.EnableAutoSettings:
                    self._DoAutoSettingsUpdate()
                else:
                    # Otherwise, update our in memory values with what's in the config.
                    self._ReadManuallySetValues()

            except Exception as e:
                Sentry.Exception("Webcam helper - _WebcamSettingsUpdateWorker exception. ", e)


    # Reads the values currently set in the config and sets them into our local settings.
    def _ReadManuallySetValues(self):
        # Read these under lock, so we don't get conflicts with them changing while being queried.
        with self.ResultsLock:
            self.EnableAutoSettings = self.Config.GetBool(Config.WebcamSection, Config.WebcamAutoSettings, MoonrakerWebcamHelper.c_DefaultAutoSettings)
            self.StreamUrl = self.Config.GetStr(Config.WebcamSection, Config.WebcamStreamUrl, MoonrakerWebcamHelper.c_DefaultStreamUrl)
            self.SnapshotUrl = self.Config.GetStr(Config.WebcamSection, Config.WebcamSnapshotUrl, MoonrakerWebcamHelper.c_DefaultSnapshotUrl)
            self.FlipH = self.Config.GetBool(Config.WebcamSection, Config.WebcamFlipH, MoonrakerWebcamHelper.c_DefaultFlipH)
            self.FlipV = self.Config.GetBool(Config.WebcamSection, Config.WebcamFlipV, MoonrakerWebcamHelper.c_DefaultFlipV)
            self.Rotation = self.Config.GetInt(Config.WebcamSection, Config.WebcamRotation, MoonrakerWebcamHelper.c_DefaultRotation)
            if self.Rotation != 0 and self.Rotation != 180 and self.Rotation != 270:
                self.Logger.error("MoonrakerWebcamHelper has an invalid rotation value "+str(self.Rotation)+", resetting the default.")
                self.Config.SetStr(Config.WebcamSection, Config.WebcamRotation, 0)
                self.Rotation = 0


    # Does the settings update.
    def _DoAutoSettingsUpdate(self):
        try:
            self.Logger.debug("Starting auto webcam settings update...")

            # First, try to use the newer webcam API.
            # It seems that even if the frontend still uses the older DB based entry, it will still showup in this new API.
            # So we do this first, since it's the most correct if it exists.
            if self._TryToFindWebcamFromApi():
                # On success, we found the webcam we want, so we are done.
                return

            # Fallback to try the old common Moonraker DB webcams
            # Note we must keep this around, because some printers are stuck on older version of Moonraker / frontends
            # that use these APIs, and they can't be updated.
            if self._TryToFindWebcamFromMoonrakerDb():
                # On success, we found the webcam we want, so we are done.
                return

            # Finally fallback to the old way Fluidd stored webcams, in it's own custom db namespace.
            # Note we must keep this around, because some printers are stuck on older version of Moonraker / frontends
            # that use these APIs, and they can't be updated.
            if self._TryToFindWebcamFromFluiddCustomDb():
                # On success, we found the webcam we want, so we are done.
                return

            # We failed to find a webcam in the list that's valid or there are no webcams in the list.
            # Revert to defaults
            self.Logger.debug("Failed to find any configured webcams, setting defaults.")
            self._ResetValuesToDefaults()

        except Exception as e:
            Sentry.Exception("Webcam helper - _DoAutoSettingsUpdate exception. ", e)


    # Tries to find the webcam config using the new Moonraker webcam APIs.
    def _TryToFindWebcamFromApi(self) -> bool:
        # It seems that even if the frontend still uses the older DB based entry, it will still showup in this new API.
        result = MoonrakerClient.Get().SendJsonRpcRequest("server.webcams.list")

        # If we failed don't do anything.
        if result.HasError():
            # If the error is that there's no websocket connected, we don't want to auto logic to continue, so it doesn't
            # pull results from the old api if the WS becomes connected. So in this case, return True, so the auto logic will stop.
            # We have logic in KickOffWebcamSettingsUpdate that will always kick an update if there are no auto results and auto is enabled.
            # So when the websocket connects, KickOffWebcamSettingsUpdate will be called, and we will run again.
            if result.ErrorCode == JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED:
                self.Logger.info("Moonraker webcam helper failed to query for webcams from API, no websocket is connected.")
                return True
            self.Logger.warn("Moonraker webcam helper failed to query for webcams from API. "+result.GetLoggingErrorStr())
            return False

        res = result.GetResult()
        if "webcams" not in res:
            self.Logger.warn("Moonraker webcam helper failed to find webcams value in API result.")
            return False

        # To help debugging, log the result.
        if self.Logger.isEnabledFor(logging.DEBUG):
            self.Logger.debug("Moonraker WebCam API Result: %s", json.dumps(res, indent=4, separators=(", ", ": ")))

        webcamList = res["webcams"]
        if len(webcamList) == 0:
            return False

        # Parse all webcam we can find.
        webcamSettingsItemResults = []
        for webcamSettingsObj in webcamList:
            webcamSettings = self._TryToParseWebcamApiItem(webcamSettingsObj)
            if webcamSettings is not None:
                webcamSettingsItemResults.append(webcamSettings)

        # If we didn't get anything, return false.
        if len(webcamSettingsItemResults) == 0:
            return False

        # Set whatever we found.
        self.Logger.debug("Using webcam values found in from the new Webcam APIs")
        self._SetNewValues(webcamSettingsItemResults)

        # Return success.
        return True


    # Given a Moonraker webcam API result list item, this will try to parse it.
    # If successful, this will return a valid AbstractWebcamSettings object.
    # If the parse fails or the params are wrong, this will return None
    def _TryToParseWebcamApiItem(self, webcamApiItem) -> WebcamSettingItem:
        try:
            # This new converged logic is amazing, see this doc for the full schema
            # https://moonraker.readthedocs.io/en/latest/web_api/#webcam-apis

            # Skip if it's not set to enabled.
            if "enabled" in webcamApiItem and webcamApiItem["enabled"] is False:
                return None

            # Parse the settings.
            webcamSettings = WebcamSettingItem()
            if "name" in webcamApiItem:
                webcamSettings.Name = webcamApiItem["name"]
            if "stream_url" in webcamApiItem:
                webcamSettings.StreamUrl = webcamApiItem["stream_url"]
            if "snapshot_url" in webcamApiItem:
                webcamSettings.SnapshotUrl = webcamApiItem["snapshot_url"]
            if "flip_horizontal" in webcamApiItem:
                webcamSettings.FlipH = webcamApiItem["flip_horizontal"]
            if "flip_vertical" in webcamApiItem:
                webcamSettings.FlipV = webcamApiItem["flip_vertical"]
            if "rotation" in webcamApiItem:
                webcamSettings.Rotation = webcamApiItem["rotation"]

            # Validate and return if we found good settings.
            if self._ValidateAndFixupWebCamSettings(webcamSettings) is False:
                return None

            # If the settings are validated, return success!
            return webcamSettings
        except Exception as e:
            Sentry.Exception("Webcam helper _TryToParseWebcamApiItem exception. ", e)
        return None


    # Tries to find the webcam config using the older Moonraker common db entry.
    def _TryToFindWebcamFromMoonrakerDb(self) -> bool:
        # Query the common moonraker webcam database namespace.
        result = MoonrakerClient.Get().SendJsonRpcRequest("server.database.get_item",
            {
                "namespace": "webcams",
            }
        )

        # If we failed don't do anything.
        if result.HasError():
            # If the error was due to some issue talking to moonraker, we don't want to rest the webcam config to the defaults, SO WE RETURN True.
            # When the connection is re-established, we will force sync the webcam settings.
            if result.IsErrorCodeOeError():
                self.Logger.warn("Moonraker webcam helper failed to query DB for webcams due to a coms issue. We won't reset the config. "+result.GetLoggingErrorStr())
                return True
            # If this happens, it means there are no webcams configured in this DB entry
            if "Namespace 'webcams' not found".lower() in result.ErrorStr.lower():
                self.Logger.debug("server.database.get_item returned no webcam namespace found.")
                return False
            self.Logger.warn("Moonraker webcam helper failed to query DB for webcams. "+result.GetLoggingErrorStr())
            return False

        res = result.GetResult()
        if "value" not in res:
            self.Logger.warn("Moonraker webcam helper failed to find value in DB result.")
            return False

        # To help debugging, log the result.
        if self.Logger.isEnabledFor(logging.DEBUG):
            self.Logger.debug("Returned webcam database data: %s", json.dumps(res, indent=4, separators=(", ", ": ")))

        value = res["value"]
        if len(value) == 0:
            return False

        # Parse everything we got back.
        webcamSettingItems = []
        for guid in value:
            webcamSettingsObj = value[guid]
            webcamSettings = self._TryToParseMoonrakerWebcamDbEntry(webcamSettingsObj)
            if webcamSettings is not None:
                webcamSettingItems.append(webcamSettings)

        # If we didn't get any webcams, return we didn't find anything.
        if len(webcamSettingItems) == 0:
            return False

        # Set the webcams we found!
        self.Logger.debug("Using webcam values found in from the older moonraker common DB webcam entry")
        self._SetNewValues(webcamSettingItems)
        return True


    # Given a Moonraker webcam db entry, this will try to parse it.
    # If successful, this will return a valid WebcamSettingItem object.
    # If the parse fails or the params are wrong, this will return None
    def _TryToParseMoonrakerWebcamDbEntry(self, webcamSettingsObj) -> WebcamSettingItem:
        try:
            # Skip if it's not set to enabled.
            if "enabled" in webcamSettingsObj and webcamSettingsObj["enabled"] is False:
                return None

            # This logic is tricky, because Mainsail and Fluid set some values that overlap names, some that don't and some only set on or the other setting.
            # The biggest problem is some of these values can go stale, for example Fluidd doesn't update or show the urlSnapshot value in the UI.

            # It seems they both always set streamUrl, which is good, because our snapshot logic can fallback to it.
            webcamSettings = WebcamSettingItem()
            if "name" in webcamSettingsObj:
                webcamSettings.Name = webcamSettingsObj["name"]

            if "urlStream" in webcamSettingsObj:
                webcamSettings.StreamUrl = webcamSettingsObj["urlStream"]

            # Snapshot URL seems to be only set and visible in the mainsail UI
            if "urlSnapshot" in webcamSettingsObj:
                webcamSettings.SnapshotUrl = webcamSettingsObj["urlSnapshot"]

            # Both seem to set the flip values
            if "flipX" in webcamSettingsObj:
                webcamSettings.FlipH = webcamSettingsObj["flipX"]
            if "flipY" in webcamSettingsObj:
                webcamSettings.FlipV = webcamSettingsObj["flipY"]

            # For rotation, fluidd sets a key of 'rotation' as an int
            # Mainsail uses 'rotate' as an int, but only lets the user set it for "adaptive mjpeg-stream"
            # We decided to try rotate first, and if it exists get the value
            if "rotate" in webcamSettingsObj:
                webcamSettings.Rotation = webcamSettingsObj["rotate"]
            # If 'rotate' didn't exist or it's 0, also check rotation
            # 'rotation' is fluidd's way of doing it, and it's more accessible than moonraker's value.
            if webcamSettings.Rotation == 0 and "rotation" in webcamSettingsObj:
                webcamSettings.Rotation = webcamSettingsObj["rotation"]

            # Validate and return if we found good settings.
            if self._ValidateAndFixupWebCamSettings(webcamSettings) is False:
                return None

            # If the settings are validated, return success!
            return webcamSettings
        except Exception as e:
            Sentry.Exception("Webcam helper _TryToParseWebcamDbEntry exception. ", e)
        return None


    # Tries to find the webcam config using the older Fluidd common namespace db entry.
    def _TryToFindWebcamFromFluiddCustomDb(self) -> bool:
        # Older versions of Fluidd had their own DB entries in a custom namespace.
        # The format is something like this:
        # https://github.com/fluidd-core/fluidd/blob/8f091c2c75c6646cd29ab288863a379b7ca6c63e/src/store/webcams/actions.ts#L34
        self.Logger.debug("Webcam helper is trying to get webcam settings from the custom fluidd namespace...")
        result = MoonrakerClient.Get().SendJsonRpcRequest("server.database.get_item",
            {
                "namespace": "fluidd",
                "key": "cameras"
            }
        )

        # If we failed don't do anything.
        if result.HasError():
            # If the error was due to some issue talking to moonraker, we don't want to rest the webcam config to the defaults, SO WE RETURN True.
            # When the connection is re-established, we will force sync the webcam settings.
            if result.IsErrorCodeOeError():
                self.Logger.warn("Moonraker webcam helper failed to query DB for FLUIDD webcams due to a coms issue. We won't reset the config. "+result.GetLoggingErrorStr())
                return True
            # If this happens, it means there are no webcams configured in this DB entry
            if "not found".lower() in result.ErrorStr.lower():
                self.Logger.debug("server.database.get_item for custom FLUIDD namespace returned no webcam namespace found.")
                return False
            self.Logger.warn("Moonraker webcam helper failed to FLUIDD DB query for webcams. "+result.GetLoggingErrorStr())
            return False

        res = result.GetResult()
        if "value" not in res:
            self.Logger.warn("Moonraker webcam helper failed to find value in result FLUIDD DB.")
            return False

        # To help debugging, log the result.
        if self.Logger.isEnabledFor(logging.DEBUG):
            self.Logger.debug("Returned FLUIDD webcam database data: %s", json.dumps(res, indent=4, separators=(", ", ": ")))

        value = res["value"]
        if len(value) == 0:
            return False

        # Parse everything we got back.
        webcamSettingItems = []
        for guid in value:
            webcamSettingsObj = value[guid]
            webcamSettings = self._TryToParseFluiddCustomWebcamDbEntry(webcamSettingsObj)
            if webcamSettings is not None:
                webcamSettingItems.append(webcamSettings)

        # If we found anything, set them!
        if len(webcamSettingItems) == 0:
            self.Logger.debug("No valid webcam config objects found in the FLUIDD custom namespace db entry.")
            return False

        self.Logger.debug("Using webcam values found in from the old FLUIDD custom namespace db entry")
        self._SetNewValues(webcamSettingItems)
        return True


    # Given a Fluidd namespace webcam db entry, this will try to parse it.
    # If successful, this will return a valid WebcamSettingItem object.
    # If the parse fails or the params are wrong, this will return None
    def _TryToParseFluiddCustomWebcamDbEntry(self, webcamSettingsObj) -> WebcamSettingItem:
        try:
            # Skip if it's not set to enabled.
            if "enabled" in webcamSettingsObj and webcamSettingsObj["enabled"] is False:
                return None

            webcamSettings = WebcamSettingItem()
            if "name" in webcamSettingsObj:
                webcamSettings.Name = webcamSettingsObj["name"]

            # There seems to only be one URL, which can be the snapshot URL or the stream URL.
            # Our _ValidateAndFixupWebCamSettings requires a stream url to be set, but it will try to figure out the snapshot URL
            # if it's not set. So we will always set the URL for the stream, and sometimes the snapshot.
            # Note: It seems that most of the time, this URL is a stream URL.
            if "url" in webcamSettingsObj:
                url:str = webcamSettingsObj["url"]

                # Try to detect if this is a snapshot or stream URL
                urlLower = url.lower()
                if urlLower.find("snapshot") != -1:
                    webcamSettings.SnapshotUrl = url
                    # For the stream url, always use the basic URL. If we can find "snapshot" replace it with "stream"
                    # The url might be like .../webcam/snapshot or .../webcam/action=snapshot in which case, stream might work.
                    webcamSettings.StreamUrl = urlLower.replace("snapshot", "stream")
                else:
                    # Assume it's a stream URL, and try to set it. The validation system will handle getting the snapshot url from it.
                    webcamSettings.StreamUrl = url

            # Set the flip values.
            if "flipX" in webcamSettingsObj:
                webcamSettings.FlipH = webcamSettingsObj["flipX"]
            if "flipY" in webcamSettingsObj:
                webcamSettings.FlipV = webcamSettingsObj["flipY"]

            # Set rotation
            if "rotate" in webcamSettingsObj:
                webcamSettings.Rotation = webcamSettingsObj["rotate"]

            # Validate and return if we found good settings.
            if self._ValidateAndFixupWebCamSettings(webcamSettings) is False:
                return None

            # If the settings are validated, return success!
            return webcamSettings
        except Exception as e:
            Sentry.Exception("Webcam helper _TryToParseFluiddCustomWebcamDbEntry exception. ", e)
        return None


    # Validates if webcam abstract settings are valid and also will edit special logic we need to apply.
    # Returns True if the settings are valid, otherwise False.
    def _ValidateAndFixupWebCamSettings(self, webcamSettings:WebcamSettingItem) -> bool:
        try:
            # Stream URL is required.
            if webcamSettings.StreamUrl is None or len(webcamSettings.StreamUrl) == 0:
                return False

            # If the URL is relative, ensure the values start with a '/'. Mainsail works if they don't, but we expect them to.
            if webcamSettings.StreamUrl.find("://") == -1 and webcamSettings.StreamUrl.startswith("/") is False:
                webcamSettings.StreamUrl = "/" + webcamSettings.StreamUrl
            # Do the same for the SnapshotUrl, if there is one.
            if webcamSettings.SnapshotUrl is not None and len(webcamSettings.SnapshotUrl) > 0:
                if webcamSettings.SnapshotUrl.find("://") == -1 and webcamSettings.SnapshotUrl.startswith("/") is False:
                    webcamSettings.SnapshotUrl = "/" + webcamSettings.SnapshotUrl

            # This is a fix for a Fluidd bug or something a user might do. The Fluidd UI defaults to /webcam?action... which results in nginx redirecting to /webcam/?action...
            # That's ok, but it makes us take an entire extra trip for webcam calls. So if we see it, we will correct it.
            # It also can break our local snapshot getting, if we don't follow redirects. (we didn't in the past but we do now.)
            fixedStreamUrl = WebcamHelper.FixMissingSlashInWebcamUrlIfNeeded(self.Logger, webcamSettings.StreamUrl)
            if fixedStreamUrl is not None:
                webcamSettings.StreamUrl = fixedStreamUrl
            if webcamSettings.SnapshotUrl is not None:
                fixedSnapshotUrl = WebcamHelper.FixMissingSlashInWebcamUrlIfNeeded(self.Logger, webcamSettings.SnapshotUrl)
                if fixedSnapshotUrl is not None:
                    webcamSettings.SnapshotUrl = fixedSnapshotUrl

            # As of crowsnest 4.0, a common backend is camera-streamer, which supports WebRTC! This is a great choice because it's more efficient than jmpeg,
            # but it's impossible to stream via our backend.. For the full portal connection we try to allow WebRTC to work over the WAN, but for OE service things
            # like Live Links and Quick View, we need to use a jmpeg stream so we can proxy the video feed and not expose the user's home IP address.
            # Thus, if we see the /webcam<*>/webrtc URL as the stream URL, we will replace it with the stream URL for OE's internal uses.
            # Luckily, camera-stream also supports jmpeg streaming.
            #
            # We dont need to update the snapshot URL, because camera-streamer has compat for /?action=snapshot -> /snapshot.
            cameraStreamerJmpegUrl = WebcamHelper.DetectCameraStreamerWebRTCStreamUrlAndTranslate(webcamSettings.StreamUrl)
            if cameraStreamerJmpegUrl is not None:
                webcamSettings.StreamUrl = cameraStreamerJmpegUrl

            # Snapshot URL isn't required, but it's nice to have.
            if webcamSettings.SnapshotUrl is None or len(webcamSettings.SnapshotUrl) == 0:
                webcamSettings.SnapshotUrl = self._TryToFigureOutSnapshotUrl(webcamSettings.StreamUrl)

            # Ensure these are the correct types.
            webcamSettings.FlipH = bool(webcamSettings.FlipH)
            webcamSettings.FlipV = bool(webcamSettings.FlipV)
            webcamSettings.Rotation = int(webcamSettings.Rotation)
            if webcamSettings.Rotation != 0 and webcamSettings.Rotation != 90 and webcamSettings.Rotation != 180 and webcamSettings.Rotation != 270:
                self.Logger.warn("Webcam helper found an invalid rotation, resetting to 0")
                webcamSettings.Rotation = 0

            # Finally, do the standard webcam settings validation.
            return webcamSettings.Validate(self.Logger)
        except Exception as e:
            Sentry.Exception("Webcam helper _ValidatePossibleWebCamSettings exception. ", e)
        return False


    # Tries to find the snapshot URL.
    # If successful, it returns the snapshot URL
    # If failed, it return None
    def _TryToFigureOutSnapshotUrl(self, streamUrl:str) -> str:
        # If we have no snapshot url, see if we can figure one out.
        # We know most all webcam interfaces use the "mjpegstreamer" web url signatures.
        # So if we find "action=stream" as in "http://127.0.0.1/webcam/?action=stream", try to get a snapshot.
        # Modern webcam servers also use .../webcam/stream and .../webcam/snapshot, so we will try that as well.
        # Update, the easiest way to do this is if we find "stream", just replace it with "snapshot", and see if it still works.
        c_streamAction = "stream"
        c_snapshotAction = "snapshot"

        # See if stream exists.
        streamUrlLower = streamUrl.lower()
        if streamUrlLower.find(c_streamAction) == -1:
            self.Logger.debug("FAILED to find a snapshot url from stream URL, no stream suffix found.")
            return None

        # We found the stream, try replacing it and see if we hit a valid endpoint.
        # Use the lower version to ensure the match, the case of a URL shouldn't matter.
        possibleSnapshotUrl = streamUrlLower.replace(c_streamAction, c_snapshotAction)
        try:
            # Make sure the path is a full URL. If not, assume localhost port 80.
            absoluteSnapshotUrl = possibleSnapshotUrl
            if absoluteSnapshotUrl.lower().startswith("http") is False:
                if absoluteSnapshotUrl.startswith("/") is False:
                    absoluteSnapshotUrl = "/"+absoluteSnapshotUrl
                absoluteSnapshotUrl = "http://127.0.0.1"+absoluteSnapshotUrl
            self.Logger.debug("Trying to find a snapshot url, testing: %s - from stream URL: %s", absoluteSnapshotUrl, streamUrl)

            # We can't use .head because that only pulls the headers from nginx, it doesn't get the full headers.
            # So we use .get with a timeout.
            with requests.get(absoluteSnapshotUrl, timeout=20) as response:
                # Check for success
                if response.status_code != 200:
                    self.Logger.debug(f"Test snapshot attempt returned http status {response.status_code}. Url: {absoluteSnapshotUrl}")
                    return None

                # This is a good sign, check the content type.
                contentTypeHeaderKey = "content-type"
                if contentTypeHeaderKey in response.headers:
                    if "image" in response.headers[contentTypeHeaderKey].lower():
                        # Success!
                        self.Logger.debug("Found a valid snapshot URL! Url: %s, Content-Type: %s", absoluteSnapshotUrl, response.headers[contentTypeHeaderKey])
                        return possibleSnapshotUrl
            self.Logger.debug(f"We made the web request for the stream URL but didn't get a valid result. {streamUrl}")
        except Exception as e:
            self.Logger.debug(f"FAILED to find a snapshot url from stream URL. Url: {streamUrl}, Error: {str(e)}")
        return None


    # If called, this should force the settings to the defaults, if auto settings are on.
    def _ResetValuesToDefaults(self):
        self.Logger.debug("Resetting the webcam settings to the defaults.")
        webcamSettings = WebcamSettingItem("Default")
        webcamSettings.StreamUrl = MoonrakerWebcamHelper.c_DefaultStreamUrl
        webcamSettings.SnapshotUrl = MoonrakerWebcamHelper.c_DefaultSnapshotUrl
        webcamSettings.FlipH = MoonrakerWebcamHelper.c_DefaultFlipH
        webcamSettings.FlipV = MoonrakerWebcamHelper.c_DefaultFlipV
        webcamSettings.Rotation = MoonrakerWebcamHelper.c_DefaultRotation
        webcamSettings.Validate(self.Logger)
        self._SetNewValues([webcamSettings])


    # Updates the values, if they are different from the current.
    def _SetNewValues(self, webcamSettingsItemResults):

        # Check that we are using auto config.
        if self.EnableAutoSettings is False:
            return

        # Ensure we got something, this shouldn't happen.
        if len(webcamSettingsItemResults) == 0:
            self.Logger.error("Webcam helper _SetNewValues got an empty list. Resetting to defaults.")
            self._ResetValuesToDefaults()
            return

        # We get a list of results, we need to respect the user's selection of a default camera if they set one.
        # In that case, the default camera should be first in the list.
        webcamNameToSelect = self.Config.GetStr(Config.WebcamSection, Config.WebcamNameToUseAsPrimary, MoonrakerWebcamHelper.c_DefaultWebcamNameToUseAsPrimary)
        if webcamNameToSelect is not None and len(webcamNameToSelect) > 0 and webcamNameToSelect != MoonrakerWebcamHelper.c_DefaultWebcamNameToUseAsPrimary:
            webcamNameToSelectLower = webcamNameToSelect.lower()
            i = 0
            while i < len(webcamSettingsItemResults):
                item = webcamSettingsItemResults[i]
                if item.Name.lower() == webcamNameToSelectLower:
                    # If we find it, delete it from it's current position and move it to the front.
                    del webcamSettingsItemResults[i]
                    webcamSettingsItemResults.insert(0, item)
                    break
                i += 1
            if i == len(webcamSettingsItemResults):
                self.Logger.warn(f"A non-default primary webcam name was set, but we didn't find a matching webcam config. name:{webcamNameToSelectLower}")

        # Print for debugging.
        for i in webcamSettingsItemResults:
            self.Logger.debug(f'Webcam helper found settings. name: {i.Name}, streamUrl: {i.StreamUrl}, snapshotUrl: {i.SnapshotUrl}, flipH: {i.FlipH}, FlipV: {i.FlipV}, rotation: {i.Rotation}')
            i.Validate(self.Logger)

        # We want to use this list of results, so set them now.
        # After these are set, if auto is enabled, these will be used.
        with self.ResultsLock:
            self.AutoSettingsResults = webcamSettingsItemResults

        # We also need to update the manual and config set values if auto is enabled. We always use the index 0, aka the default.
        # Make sure there's a difference
        item = webcamSettingsItemResults[0]
        if item.StreamUrl == self.StreamUrl and item.SnapshotUrl == self.SnapshotUrl and item.FlipH == self.FlipH and item.FlipV == self.FlipV and item.Rotation == self.Rotation:
            return

        # Set the values.
        self.StreamUrl = item.StreamUrl
        self.SnapshotUrl = item.SnapshotUrl
        self.FlipH = item.FlipH
        self.FlipV = item.FlipV
        self.Rotation = item.Rotation
        self.Config.SetStr(Config.WebcamSection, Config.WebcamStreamUrl, self.StreamUrl)
        self.Config.SetStr(Config.WebcamSection, Config.WebcamSnapshotUrl, self.SnapshotUrl)
        self.Config.SetStr(Config.WebcamSection, Config.WebcamFlipH, self.FlipH)
        self.Config.SetStr(Config.WebcamSection, Config.WebcamFlipV, self.FlipV)
        self.Config.SetStr(Config.WebcamSection, Config.WebcamRotation, self.Rotation)

        self.Logger.info(f'Webcam helper updated webcam settings. streamUrl: {self.StreamUrl}, snapshotUrl: {self.SnapshotUrl}, flipH: {self.FlipH}, FlipV: {self.FlipV}, rotation: {self.Rotation}')
