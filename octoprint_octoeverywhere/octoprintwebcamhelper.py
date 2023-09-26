import logging
import json
import time

from octoeverywhere.webcamhelper import WebcamSettingItem, WebcamHelper
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.sentry import Sentry

# This class implements the webcam platform helper interface for OctoPrint.
class OctoPrintWebcamHelper():

    # The amount of time we will cache webcam settings to avoid somewhat costly calls to get the current settings
    # the trade off here is freshness of data vs the compute cost of getting the settings.
    c_WebcamSettingsCacheTimeSeconds = 10.0

    def __init__(self, logger:logging.Logger, octoPrintSettingsObject):
        self.Logger = logger
        self.OctoPrintSettingsObject = octoPrintSettingsObject

        self.CachedWebcamSettingsItem:WebcamSettingItem = None
        self.LastCacheUpdateTimeSec:float = 0
        self._ResetCache()


    def _ResetCache(self):
        self.CachedWebcamSettingsItem = None
        self.LastCacheUpdateTimeSec = 0


    # !! Interface Function !!
    # This must return an array of WebcamSettingItems.
    # Index 0 is used as the default webcam.
    # Returns None on failure.
    def GetWebcamConfig(self):
        # In dev mode, we won't have this.
        if self.OctoPrintSettingsObject is None:
            self.Logger.info("OctoPrintWebcamHelper has no OctoPrintSettingsObject. Returning default address.")
            baseUrl = f"http://{OctoHttpRequest.GetLocalhostAddress()}"
            return [
                WebcamSettingItem(f"{baseUrl}/webcam/?action=snapshot", f"{baseUrl}/webcam/?action=stream", False, False, 0)
            ]

        # Since OctoPrint 1.9.0+ needs to call plugins to return webcam settings, we want to reduce how often we make the call.
        # The trade off there is that when a user changes an option, there will be a longer delay before the setting shows up in OctoEverywhere
        # But that's a very rare case, and in most cases, we want to avoid calling the plugins frequently. GetWebcamConfig() is called every time
        # we need a service snapshot, so for image based live links, it can be 1 time per second. It's also called for all notification and Gadget calls.
        if self.CachedWebcamSettingsItem is not None:
            if time.time() - self.LastCacheUpdateTimeSec < OctoPrintWebcamHelper.c_WebcamSettingsCacheTimeSeconds:
                return [ self.CachedWebcamSettingsItem ]

        # Reset the cache if we are pulling again.
        self._ResetCache()

        snapshotUrl = None
        streamUrl = None
        flipH = False
        flipV = False
        rotate90 = False

        # As of OctoPrint 1.9.0, the webcam logic moved to a plugin based system, where plugins can control and present the webcam config.
        # Due to that change, we can't just pull from the global settings, like we did in the past.
        # We will use the modern webcam settings API to get the webcam list, but fallback to the old APIs if this fails (the old APIs have compatibility layers that keep them working.)
        try:
            #pylint: disable=C0415
            from octoprint.util.version import is_octoprint_compatible
            if is_octoprint_compatible(">=1.9.0"):
                import octoprint.webcams
                import octoprint.schema.webcam
                # Get all webcams and try to find one we can use.
                webcams = octoprint.webcams.get_webcams()
                for webcamName, providerContainer in webcams.items():
                    webcam:octoprint.schema.webcam.Webcam = providerContainer.config
                    # Log for debugging.
                    if self.Logger.isEnabledFor(logging.DEBUG):
                        self.Logger.debug(f"OctoPrint Webcam Config Found: Name: {webcamName}, Can Snapshot: {webcam.canSnapshot}, Webcam Snapshot: \"{webcam.snapshotDisplay}\", Extras: {json.dumps(webcam.extras)}")
                    # Since the snapshot is critical for Gadget and others, only allow webcams that have snapshot (for now)
                    # Also note the webcam system has a fallback for stream url only webcams, we could rely on that?
                    if webcam.canSnapshot is False:
                        self.Logger.info(f"We found a webcam {webcamName} but it doesn't support snapshots, so we are ignoring it.")
                        continue
                    # The new webcam struct is unstructured, so that makes it hard for us to use.
                    # The compat object is optional, but it has all of the fields explicitly layout, so if it exits, use it.
                    if webcam.compat is not None:
                        streamUrl = webcam.compat.stream
                        snapshotUrl = webcam.compat.snapshot
                    # If we have no compat object, try to get the strings where we expect them to be.
                    # We followed the info provided by the classicwebcam plugin (OctoPrint's default). It seems like other plugins (multicam, etc) follow the same
                    # https://github.com/OctoPrint/OctoPrint/blob/ed4a2646fb4e2904892c895580192987242834c8/src/octoprint/plugins/classicwebcam/__init__.py
                    if streamUrl is None and "stream" in webcam.extras:
                        streamUrl = webcam.extras["stream"]
                    if snapshotUrl is None:
                        # This is really shotty, this fields should be a "human readable string or URL"
                        # But all of the plugins we have seen thus far set the snapshot URL here.
                        snapshotUrl = webcam.snapshotDisplay

                    # Ensure we got what we need. We only check snapshot, because that's critical for notifications, Gadget, etc.
                    # It's better to find one webcam with a valid snapshot, rather than finding no webcams with a snapshot and stream url.
                    if snapshotUrl is None:
                        self.Logger.debug(f"OctoPrint Webcam Config Found {webcamName} but no snapshot URL, moving on to the next.")
                        continue

                    # Warn if we are missing a stream url, this shouldn't happen often. Most plugins would always have a stream url over a snapshot url.
                    if streamUrl is None:
                        self.Logger.warn(F"Warning! We didn't get a stream url for webcam {webcamName} - {snapshotUrl}")

                    # We are going to use this webcam, grab the rest of the common vars
                    flipH = webcam.flipH
                    flipV = webcam.flipV
                    rotate90 = webcam.rotate90

                    # We have our webcam!
                    self.Logger.debug(f"Webcam selected. Name: {webcamName}, {streamUrl}, {snapshotUrl}, {flipH}, {flipV}, {rotate90}")
                    break

        except Exception as e:
            Sentry.Exception("GetWebcamConfig failed to handle new 1.9.0 logic. Falling back to the old logic.", e)

        # Just as above, only test the one var we know is required to be set from the logic above.
        if snapshotUrl is None:
            # This is the logic for < 1.9.0 OctoPrint instances.
            snapshotUrl = self.OctoPrintSettingsObject.global_get(["webcam", "snapshot"])
            streamUrl = self.OctoPrintSettingsObject.global_get(["webcam", "stream"])
            flipH = self.OctoPrintSettingsObject.global_get(["webcam", "flipH"])
            flipV = self.OctoPrintSettingsObject.global_get(["webcam", "flipV"])
            rotate90 = self.OctoPrintSettingsObject.global_get(["webcam", "rotate90"])
            # TODO - In OctoPrint 1.9 the webcam was moved to a plugin model, such that plugins can implement any kind of webcams they want.
            # There's a backwards compat layer that should keep things like the old calls above working, but it only seems to work for `snapshot` and `stream`.
            # So we will try to get the values from the `classicwebcam` plugin, which is the default OctoPrint plugin for webcams now.
            #
            # In the future, we should switch to the new `octoprint.webcams.get_webcams` static API in OctoPrint (just import it and call)
            # To get a list of webcams and handle their properties. But that will only exist in 1.9+, so for now we don't bother.
            if flipH is None:
                flipH = self.OctoPrintSettingsObject.global_get(["plugins", "classicwebcam", "flipH"])
            if flipV is None:
                flipV = self.OctoPrintSettingsObject.global_get(["plugins", "classicwebcam", "flipV"])
            if rotate90 is None:
                rotate90 = self.OctoPrintSettingsObject.global_get(["plugins", "classicwebcam", "rotate90"])

        # As of the new OctoPi image, a common backend is camera-streamer, which supports WebRTC! This is a great choice because it's more efficient than jmpeg,
        # but it's impossible to stream via our backend.. For the full portal connection we try to allow WebRTC to work over the WAN, but for OE service things
        # like Live Links and Quick View, we need to use a jmpeg stream so we can proxy the video feed and not expose the user's home IP address.
        # Thus, if we see the /webcam<*>/webrtc URL as the stream URL, we will replace it with the stream URL for OE's internal uses.
        # Luckily, camera-stream also supports jmpeg streaming.
        #
        # We dont need to update the snapshot URL, because camera-streamer has compat for /?action=snapshot -> /snapshot.
        cameraStreamerJmpegUrl = WebcamHelper.DetectCameraStreamerWebRTCStreamUrlAndTranslate(streamUrl)
        if cameraStreamerJmpegUrl is not None:
            self.Logger.info(f"Camera-streamer webrtc stream url {streamUrl} converted to jmpeg {cameraStreamerJmpegUrl}")
            streamUrl = cameraStreamerJmpegUrl

        # These values must exist, so if they don't default them.
        # TODO - We could better guess at either of these URLs if one doesn't exist and the other does.
        # We default to the relative paths, since our http request class can better manipulate these if needed.
        if snapshotUrl is None or len(snapshotUrl) == 0:
            snapshotUrl = "/webcam/?action=snapshot"
        if streamUrl is None or len(snapshotUrl) == 0:
            streamUrl = "/webcam/?action=stream"
        if flipH is None:
            flipH = False
        if flipV is None:
            flipV = False
        if rotate90 is None:
            rotate90 = False

        # Translate the rotate90 -> rotation.
        # OctoPrint uses rotate90 as a bool, where other platforms use full 0, 90, 180, 270 rotation.
        # OctoPrint also does a 90 degree rotation counter clock-wise, which is a 270 rotation clockwise.
        rotation = 0
        if rotate90:
            rotation = 270

        # Create the final object, cache it, and return it.
        webcamSettingsItem = WebcamSettingItem(snapshotUrl, streamUrl, flipH, flipV, rotation)
        self.CachedWebcamSettingsItem = webcamSettingsItem
        self.LastCacheUpdateTimeSec = time.time()

        return [
            webcamSettingsItem
        ]
