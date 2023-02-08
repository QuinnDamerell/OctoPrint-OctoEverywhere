import threading
import time
import requests

from octoeverywhere.sentry import Sentry
from octoeverywhere.webcamhelper import WebcamSettingItem

from .config import Config
from .moonrakerclient import MoonrakerClient

# This class implements the webcam platform helper interface for moonraker.
class MoonrakerWebcamHelper():

    # The amount of time we will wait between settings checks.
    # These are also invoked when there's webcam activity, so we don't need to check too frequently.
    c_DelayBetweenAutoSettingsCheckSec = 5 * 60

    # The min time between checks when there's webcam activity.
    c_MinTimeBetweenWebcamActivityInvokesSec = 20 # 60

    # Default settings.
    c_DefaultAutoSettings = True
    # Use relative paths for the defaults, because if they aren't correct our http system can try other options since they are relative.
    c_DefaultStreamUrl = "/webcam/?action=stream"
    c_DefaultSnapshotUrl = "/webcam/?action=snapshot"
    c_DefaultFlipH = False
    c_DefaultFlipV = False
    c_DefaultRotation = 0

    def __init__(self, logger, config : Config) -> None:
        self.Logger = logger
        self.Config = config

        # Get this so it sets the default, if it's not set or is an incorrect value.
        self.Config.GetBool(Config.WebcamSection, Config.WebcamAutoSettings, True)

        # Get the current config values, and also write the defaults if they aren't there.
        self.StreamUrl = self.Config.GetStr(Config.WebcamSection, Config.WebcamStreamUrl, MoonrakerWebcamHelper.c_DefaultStreamUrl)
        self.SnapshotUrl = self.Config.GetStr(Config.WebcamSection, Config.WebcamSnapshotUrl, MoonrakerWebcamHelper.c_DefaultSnapshotUrl)
        self.FlipH = self.Config.GetBool(Config.WebcamSection, Config.WebcamFlipH, MoonrakerWebcamHelper.c_DefaultFlipH)
        self.FlipY = self.Config.GetBool(Config.WebcamSection, Config.WebcamFlipY, MoonrakerWebcamHelper.c_DefaultFlipV)
        self.Rotation = self.Config.GetInt(Config.WebcamSection, Config.WebcamRotation, MoonrakerWebcamHelper.c_DefaultRotation)
        if self.Rotation != 0 and self.Rotation != 180 and self.Rotation != 270:
            self.Logger.error("MoonrakerWebcamHelper has an invalid rotation value "+str(self.Rotation)+", resetting the default.")
            self.Config.SetStr(Config.WebcamSection, Config.WebcamRotation, 0)

        # Set this to 0, so when the moonraker client will wake us up when the websocket is connected, and we don't want to miss it.
        self.AutoSettingsLastWake = 0

        # Always start the auto update thread, since this also monitors the auto settings state.
        self.AutoSettingsWorkerEvent = threading.Event()
        t = threading.Thread(target=self._WebcamSettingsUpdateWorker)
        t.daemon = True
        t.start()


    # !! Interface Function !!
    # This must return an array of WebcamSettingItems.
    # Index 0 is used as the default webcam.
    # Returns None on failure.
    def GetWebcamConfig(self):

        # Kick the settings worker since the webcam was accessed.
        self.KickOffWebcamSettingsUpdate()

        # Return the current values.
        return [
            WebcamSettingItem(self.SnapshotUrl, self.StreamUrl, self.FlipH, self.FlipY, self.Rotation)
        ]


    # Wakes up the auto settings worker.
    # Called by moonrakerclient when the websocket is connected, to ensure we pull settings on moonraker connections.
    def KickOffWebcamSettingsUpdate(self):
        # Always kick off the thread, so if auto settings changes, we read it.
        timeSinceLastWakeSec = time.time() - self.AutoSettingsLastWake
        if timeSinceLastWakeSec > MoonrakerWebcamHelper.c_MinTimeBetweenWebcamActivityInvokesSec:
            self.AutoSettingsLastWake = time.time()
            self.AutoSettingsWorkerEvent.set()


    # This is the main worker thread that keeps track of webcam settings.
    # This also keeps checking the auto settings option in the config, so we know if the user changes it.
    # TODO - right now we use a poll based system, which is kind of shitty. Ideally we would figure out a way to make this push based.
    def _WebcamSettingsUpdateWorker(self):
        lastAutoSettingsValue = True
        while True:
            try:
                # Start the loop by clearing and waiting on the value. This means our first wake up will usually be either webcam activity
                # or the moonraker client telling us the websocket is connected.
                self.AutoSettingsWorkerEvent.clear()
                self.AutoSettingsWorkerEvent.wait(MoonrakerWebcamHelper.c_DelayBetweenAutoSettingsCheckSec)

                # Force a config reload, so if the user changed this setting, we respect it.
                self.Config.ReloadFromFile()
                autoSettings = self.Config.GetBool(Config.WebcamSection, Config.WebcamAutoSettings, True)

                # Log if the value changed.
                if lastAutoSettingsValue != autoSettings:
                    self.Logger.info("Webcam auto settings detection value updated: "+str(autoSettings))
                    lastAutoSettingsValue = autoSettings

                # Do an update if we should.
                if autoSettings:
                    self._DoAutoSettingsUpdate()

            except Exception as e:
                Sentry.Exception("Webcam helper - _WebcamSettingsUpdateWorker exception. ", e)



    # Does the settings update.
    def _DoAutoSettingsUpdate(self):
        try:
            # Try to query the common settings path.
            result = MoonrakerClient.Get().SendJsonRpcRequest("server.database.get_item",
                {
                    "namespace": "webcams",
                },
                True # Use the force flag, so we can try to query even when klipper isn't connected.
                )

            # If we failed don't do anything.
            if result.HasError():
                if "Namespace 'webcams' not found".lower() in result.ErrorStr.lower():
                    # This happens if there are no webcams configured at all.
                    self._ResetValuesToDefaults()
                    return
                self.Logger.warn("Moonraker webcam helper failed to query for webcams. "+result.GetLoggingErrorStr())
                return

            res = result.GetResult()
            if "value" not in res:
                self.Logger.warn("Moonraker webcam helper failed to find value in result.")
                return

            # Look for a camera that's listed and marked enabled.
            # Since we only support one webcam, we take the first for now.
            value = res["value"]
            if len(value) > 0:
                for guid in value:
                    webcamSettingsObj = value[guid]

                    # Skip if it's not set to enabled.
                    if "enabled" in webcamSettingsObj and webcamSettingsObj["enabled"] is False:
                        continue

                    # This logic is tricky, because Mainsail and Fluid set some values that overlap names, some that don't and some only set on or the other setting.
                    # The biggest problem is some of these values can go stale, for example Fluidd doesn't update or show the urlSnapshot value in the UI.

                    # It seems they both always set streamUrl, which is good, because our snapshot logic can fallback to it.
                    streamUrl = None
                    if "urlStream" in webcamSettingsObj:
                        streamUrl = webcamSettingsObj["urlStream"]

                    # Snapshot URL seems to be only set and visible in the mainsail UI
                    snapshotUrl = None
                    if "urlSnapshot" in webcamSettingsObj:
                        snapshotUrl = webcamSettingsObj["urlStream"]

                    # Both seem to set the flip values
                    flipH = False
                    flipV = False
                    if "flipX" in webcamSettingsObj:
                        flipH = webcamSettingsObj["flipX"]
                    if "flipY" in webcamSettingsObj:
                        flipV = webcamSettingsObj["flipY"]

                    # For rotation, fluidd sets a key of 'rotation' as an int
                    # Mainsail uses 'rotate' as an int, but only lets the user set it for "adaptive mjpeg-stream"
                    # We decided to check rotate and use it, if it exists, otherwise use rotation.
                    rotation = 0
                    if "rotate" in webcamSettingsObj:
                        rotation = webcamSettingsObj["rotate"]
                    elif "rotation" in webcamSettingsObj:
                        rotation = webcamSettingsObj["rotation"]

                    # Validate and return if we found good settings.
                    if self._ValidatePossibleWebCamSettings(streamUrl, snapshotUrl, flipH, flipV, rotation):
                        return
                    # If it's not valid, keep looping.

            # We failed to find a webcam in the list that's valid or there are no webcams in the list.
            # Revert to defaults
            self._ResetValuesToDefaults()

        except Exception as e:
            Sentry.Exception("Webcam helper - _DoAutoSettingsUpdate exception. ", e)


    # Validates if webcam settings are valid. If they are, they are set.
    # Returns True if good settings have been committed, otherwise False
    def _ValidatePossibleWebCamSettings(self, streamUrl, snapshotUrl, flipH, flipY, rotation):
        try:
            # Stream URL is required.
            if streamUrl is None or len(streamUrl) == 0:
                return False

            # This is a fix for a Fluidd bug. The UI defaults to /webcam?action... which results in nginx redirecting to /webcam/?action...
            # That's ok, but it makes us take an entire extra trip for webcam calls. So if we see it, we will correct it.
            if streamUrl.startswith("/webcam?action"):
                streamUrl = streamUrl.replace("/webcam?action", "/webcam/?action")

            # Snapshot URL isn't required, but it's nice to have.
            if snapshotUrl is None or len(snapshotUrl) == 0:
                snapshotUrl = self._TryToFigureOutSnapshotUrl(streamUrl)

            # Ensure these are the correct types.
            flipH = bool(flipH)
            flipY = bool(flipY)
            rotation = int(rotation)
            if rotation != 0 and rotation != 90 and rotation != 180 and rotation != 270:
                self.Logger.warn("Webcam helper found an invalid rotation, resetting to 0")
                rotation = 0

            # The data is valid
            self._SetNewValues(streamUrl, snapshotUrl, flipH, flipY, rotation)
            return True
        except Exception as e:
            Sentry.Exception("Webcam helper _ValidatePossibleWebCamSettings exception. ", e)
        return False


    # Tries to find the snapshot URL, if it's successful, it returns the url
    # If it fails, it return None
    def _TryToFigureOutSnapshotUrl(self, streamUrl):
        # If we have no snapshot url, see if we can figure one out.
        # We know most all webcam interfaces use the "jmpegstreamer" web url signatures.
        # So if we find "action=stream" as in "http://127.0.0.1/webcam/?action=stream", try to get a snapshot.
        streamUrlLower = streamUrl.lower()
        c_streamAction = "action=stream"
        c_snapshotAction = "action=snapshot"
        indexOfStreamSuffix = streamUrlLower.index(c_streamAction)

        if indexOfStreamSuffix != -1:
            # We found the action=stream, try replacing it and see if we hit a valid endpoint.
            # keep the original string around, so we can return it if things work out.
            possibleSnapshotUrl = streamUrl[:indexOfStreamSuffix] + c_snapshotAction + streamUrl[indexOfStreamSuffix + len(c_streamAction):]
            try:
                # Make sure the path is a full URL
                # If not, assume localhost port 80.
                testSnapshotUrl = possibleSnapshotUrl
                if testSnapshotUrl.lower().startswith("http") is False:
                    if testSnapshotUrl.startswith("/") is False:
                        testSnapshotUrl = "/"+testSnapshotUrl
                    testSnapshotUrl = "http://127.0.0.1"+testSnapshotUrl

                # We can't use .head because that only pulls the headers from nginx, it doesn't get the full headers.
                # So we use .get with a timeout.
                with requests.get(testSnapshotUrl, timeout=20) as response:
                    # Check for success
                    if response.status_code != 200:
                        return None

                    # This is a good sign, check the content type.
                    if "content-type" in response.headers:
                        if "image" in response.headers["content-type"].lower():
                            # Success!
                            return possibleSnapshotUrl

            except Exception:
                pass
        # On any failure, return None
        return None


    # If called, this should force the settings to the defaults, if auto settings are on.
    def _ResetValuesToDefaults(self):
        self._SetNewValues(MoonrakerWebcamHelper.c_DefaultStreamUrl, MoonrakerWebcamHelper.c_DefaultSnapshotUrl, MoonrakerWebcamHelper.c_DefaultFlipH, MoonrakerWebcamHelper.c_DefaultFlipV, MoonrakerWebcamHelper.c_DefaultRotation)


    # Updates the values, if they are different from the current.
    def _SetNewValues(self, streamUrl, snapshotUrl, flipH, flipY, rotation):

        # Check that we are using auto config.
        if self.Config.GetBool(Config.WebcamSection, Config.WebcamAutoSettings, MoonrakerWebcamHelper.c_DefaultAutoSettings) is False:
            return

        # Make sure there's a difference
        if streamUrl == self.StreamUrl and snapshotUrl == self.SnapshotUrl and flipH == self.FlipH and flipY == self.FlipY and rotation == self.Rotation:
            return

        # Set the values.
        self.StreamUrl = streamUrl
        self.SnapshotUrl = snapshotUrl
        self.FlipH = flipH
        self.FlipY = flipY
        self.Rotation = rotation
        self.Config.SetStr(Config.WebcamSection, Config.WebcamStreamUrl, streamUrl)
        self.Config.SetStr(Config.WebcamSection, Config.WebcamSnapshotUrl, snapshotUrl)
        self.Config.SetStr(Config.WebcamSection, Config.WebcamFlipH, flipH)
        self.Config.SetStr(Config.WebcamSection, Config.WebcamFlipY, flipY)
        self.Config.SetStr(Config.WebcamSection, Config.WebcamRotation, rotation)

        self.Logger.info(f'Webcam helper updated webcam settings. streamUrl: {self.StreamUrl}, snapshotUrl: {self.SnapshotUrl}, flipH: {self.FlipH}, flipY: {self.FlipY}, rotation: {self.Rotation}')
