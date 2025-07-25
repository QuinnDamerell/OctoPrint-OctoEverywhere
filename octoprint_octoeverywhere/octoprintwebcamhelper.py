import logging
import json
import time
from typing import Dict, List, Optional

from octoprint.plugin import PluginSettings
from octoprint.webcams import ProvidedWebcam
from octoprint.schema.webcam import Webcam, WebcamCompatibility

from octoeverywhere.Webcam.webcamhelper import WebcamHelper
from octoeverywhere.Webcam.webcamsettingitem import WebcamSettingItem
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.sentry import Sentry
from octoeverywhere.interfaces import IWebcamPlatformHelper

# This class implements the webcam platform helper interface for OctoPrint.
class OctoPrintWebcamHelper(IWebcamPlatformHelper):

    # The amount of time we will cache webcam settings to avoid somewhat costly calls to get the current settings
    # the trade off here is freshness of data vs the compute cost of getting the settings.
    c_WebcamSettingsCacheTimeSeconds = 10.0


    def __init__(self, logger:logging.Logger, octoPrintSettingsObject:PluginSettings) -> None:
        self.Logger = logger
        self.OctoPrintSettingsObject = octoPrintSettingsObject
        self.CachedWebcamSettingsResults:List[WebcamSettingItem] = []
        self.LastCacheUpdateTimeSec:float = 0.0
        self._ResetCache()


    def _ResetCache(self):
        self.CachedWebcamSettingsResults = []
        self.LastCacheUpdateTimeSec = 0.0


    # !! Interface Function !!
    # This must return an array of WebcamSettingItems.
    # Index 0 is used as the default webcam.
    # The order the webcams are returned is the order the user will see in any selection UIs.
    # Returns None on failure.
    def GetWebcamConfig(self) -> Optional[List[WebcamSettingItem]]:
        # In dev mode, we won't have this.
        if self.OctoPrintSettingsObject is None:
            self.Logger.info("OctoPrintWebcamHelper has no OctoPrintSettingsObject. Returning default address.")
            baseUrl = f"http://{OctoHttpRequest.GetLocalhostAddress()}"
            return [
                WebcamSettingItem("Dev", f"{baseUrl}/webcam/?action=snapshot", f"{baseUrl}/webcam/?action=stream")
            ]

        # Since OctoPrint 1.9.0+ needs to call plugins to return webcam settings, we want to reduce how often we make the call.
        # The trade off there is that when a user changes an option, there will be a longer delay before the setting shows up in OctoEverywhere
        # But that's a very rare case, and in most cases, we want to avoid calling the plugins frequently. GetWebcamConfig() is called every time
        # we need a service snapshot, so for image based live links, it can be 1 time per second. It's also called for all notification and Gadget calls.
        if len(self.CachedWebcamSettingsResults) != 0:
            if time.time() - self.LastCacheUpdateTimeSec < OctoPrintWebcamHelper.c_WebcamSettingsCacheTimeSeconds:
                return self.CachedWebcamSettingsResults

        # Reset the cache if we are pulling again.
        self._ResetCache()

        # A list of webcams we find.
        results:List[WebcamSettingItem] = []

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
                webcams:Dict[str, ProvidedWebcam] = octoprint.webcams.get_webcams() #pyright: ignore[reportUnknownMemberType]
                for webcamName, providerContainer in webcams.items():
                    webcam:Webcam = providerContainer.config
                    # Log for debugging.
                    if self.Logger.isEnabledFor(logging.DEBUG):
                        self.Logger.debug(f"OctoPrint Webcam Config Found: Name: {webcamName}, Can Snapshot: {webcam.canSnapshot}, Webcam Snapshot: \"{webcam.snapshotDisplay}\", Extras: {json.dumps(webcam.extras)}") #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType] these exist, idk why pyright doesn't know that.

                    # Some times this bool seems to be reported incorrectly so for now we don't skip the camera if it's set.
                    # Since the snapshot is critical for Gadget and others, only allow webcams that have snapshot (for now)
                    # Also note the webcam system has a fallback for stream url only webcams, we could rely on that?
                    if webcam.canSnapshot is False: #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownMemberType] these exist, idk why pyright doesn't know that.
                        self.Logger.info(f"We found a webcam {webcamName} but it doesn't support snapshots, we will try to detect the snapshot URL for ourselves.")

                    # We found that some of the webcam plugins do fun things with the names, so we clean them up for the UI.
                    # The multicam plugin prefixes them with multicam/Camera Name
                    if webcamName is not None:
                        slashPos = webcamName.find("/")
                        if slashPos != -1:
                            webcamName = webcamName[slashPos+1:]

                    # Make an empty webcam settings item to fill.
                    webSettingsItem = WebcamSettingItem(webcamName)

                    # The new webcam struct is unstructured, so that makes it hard for us to use.
                    # The compat object is optional, but it has all of the fields explicitly layout, so if it exits, use it.
                    compat:Optional[WebcamCompatibility] = webcam.compat #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownMemberType] these exist, idk why pyright doesn't know that.
                    if compat is not None:
                        webSettingsItem.StreamUrl = compat.stream #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownMemberType] these exist, idk why pyright doesn't know that.
                        webSettingsItem.SnapshotUrl = compat.snapshot #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownMemberType] these exist, idk why pyright doesn't know that.
                    # If we have no compat object, try to get the strings where we expect them to be.
                    # We followed the info provided by the classicwebcam plugin (OctoPrint's default). It seems like other plugins (multicam, etc) follow the same
                    # https://github.com/OctoPrint/OctoPrint/blob/ed4a2646fb4e2904892c895580192987242834c8/src/octoprint/plugins/classicwebcam/__init__.py
                    if webSettingsItem.StreamUrl is None and "stream" in webcam.extras: #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownMemberType] these exist, idk why pyright doesn't know that.
                        webSettingsItem.StreamUrl = webcam.extras["stream"] #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownMemberType] these exist, idk why pyright doesn't know that.
                    if webSettingsItem.SnapshotUrl is None: #pyright: ignore[reportUnknownMemberType] after setting the octoprint values, these go bad.
                        # This is really shotty, this fields should be a "human readable string or URL"
                        # But all of the plugins we have seen thus far set the snapshot URL here.
                        webSettingsItem.SnapshotUrl = webcam.snapshotDisplay #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownMemberType] these exist, idk why pyright doesn't know that.

                    # Ensure we got what we need. We only check snapshot, because that's critical for notifications, Gadget, etc.
                    # It's better to find one webcam with a valid snapshot, rather than finding no webcams with a snapshot and stream url.
                    if webSettingsItem.SnapshotUrl is None: #pyright: ignore[reportUnknownMemberType] after setting the octoprint values, these go bad.
                        self.Logger.debug(f"OctoPrint Webcam Config Found {webcamName} but no snapshot URL, moving on to the next.")
                        continue

                    # Warn if we are missing a stream url, this shouldn't happen often. Most plugins would always have a stream url over a snapshot url.
                    if webSettingsItem.StreamUrl is None: #pyright: ignore[reportUnknownMemberType] after setting the octoprint values, these go bad.
                        self.Logger.warning(F"Warning! We didn't get a stream url for webcam {webcamName} - {webSettingsItem.SnapshotUrl}") #pyright: ignore[reportUnknownMemberType] after setting the octoprint values, these go bad.

                    # We are going to use this webcam, grab the rest of the common vars
                    webSettingsItem.FlipH = webcam.flipH #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownMemberType] these exist, idk why pyright doesn't know that.
                    webSettingsItem.FlipV = webcam.flipV #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownMemberType] these exist, idk why pyright doesn't know that.

                    # Translate the rotate90 -> rotation.
                    # OctoPrint uses rotate90 as a bool, where other platforms use full 0, 90, 180, 270 rotation.
                    # OctoPrint also does a 90 degree rotation counter clock-wise, which is a 270 rotation clockwise.
                    webSettingsItem.Rotation = 0
                    if webcam.rotate90: #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownMemberType] these exist, idk why pyright doesn't know that.
                        webSettingsItem.Rotation = 270

                    # Ensure we have everything required.
                    if webSettingsItem.Validate(self.Logger):
                        results.append(webSettingsItem)
                        self.Logger.debug(f"Webcam found. Name: {webSettingsItem.Name}, {webSettingsItem.StreamUrl}, {webSettingsItem.SnapshotUrl}, {webSettingsItem.FlipH}, {webSettingsItem.FlipV}, {webSettingsItem.Rotation}") #pyright: ignore[reportUnknownMemberType] after setting the octoprint values, these go bad.
                    else:
                        self.Logger.debug(f"Webcam settings item validation failed for {webcamName}")

        except Exception as e:
            Sentry.OnException("GetWebcamConfig failed to handle new 1.9.0 logic. Falling back to the old logic.", e)

        # If we didn't get anything, try a fallback.
        if len(results) == 0:
            # This is the logic for < 1.9.0 OctoPrint instances.
            snapshotUrl:str = self.OctoPrintSettingsObject.global_get(["webcam", "snapshot"]) #pyright: ignore[reportUnknownMemberType] it doesn't know what this resolves to
            streamUrl:str = self.OctoPrintSettingsObject.global_get(["webcam", "stream"]) #pyright: ignore[reportUnknownMemberType] it doesn't know what this resolves to
            flipH:Optional[bool] = self.OctoPrintSettingsObject.global_get(["webcam", "flipH"]) #pyright: ignore[reportUnknownMemberType] it doesn't know what this resolves to
            flipV:Optional[bool] = self.OctoPrintSettingsObject.global_get(["webcam", "flipV"]) #pyright: ignore[reportUnknownMemberType] it doesn't know what this resolves to
            rotate90:Optional[bool] = self.OctoPrintSettingsObject.global_get(["webcam", "rotate90"]) #pyright: ignore[reportUnknownMemberType] it doesn't know what this resolves to
            # TODO - In OctoPrint 1.9 the webcam was moved to a plugin model, such that plugins can implement any kind of webcams they want.
            # There's a backwards compat layer that should keep things like the old calls above working, but it only seems to work for `snapshot` and `stream`.
            # So we will try to get the values from the `classicwebcam` plugin, which is the default OctoPrint plugin for webcams now.
            #
            # In the future, we should switch to the new `octoprint.webcams.get_webcams` static API in OctoPrint (just import it and call)
            # To get a list of webcams and handle their properties. But that will only exist in 1.9+, so for now we don't bother.
            if flipH is None:
                flipH = self.OctoPrintSettingsObject.global_get(["plugins", "classicwebcam", "flipH"]) #pyright: ignore[reportUnknownMemberType] it doesn't know what this resolves to
            if flipV is None:
                flipV = self.OctoPrintSettingsObject.global_get(["plugins", "classicwebcam", "flipV"]) #pyright: ignore[reportUnknownMemberType] it doesn't know what this resolves to
            if rotate90 is None:
                rotate90 = self.OctoPrintSettingsObject.global_get(["plugins", "classicwebcam", "rotate90"]) #pyright: ignore[reportUnknownMemberType] it doesn't know what this resolves to

            # These values must exist, so if they don't default them.
            # TODO - We could better guess at either of these URLs if one doesn't exist and the other does.
            # We default to the relative paths, since our http request class can better manipulate these if needed.
            if snapshotUrl is None or len(snapshotUrl) == 0:
                snapshotUrl = "/webcam/?action=snapshot"
            if streamUrl is None or len(streamUrl) == 0:
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
            rotationInt = 0
            if rotate90:
                rotationInt = 270

            # Try to add the default camera.
            webSettingsItem = WebcamSettingItem("Default", snapshotUrl, streamUrl, flipH, flipV, rotationInt)
            if webSettingsItem.Validate(self.Logger):
                results.append(webSettingsItem)
                self.Logger.debug(f"Webcam fallback found. Name: {webSettingsItem.Name}, {webSettingsItem.StreamUrl}, {webSettingsItem.SnapshotUrl}, {webSettingsItem.FlipH}, {webSettingsItem.FlipV}, {webSettingsItem.Rotation}")
            else:
                self.Logger.debug(f"Webcam settings item validation failed for FALLBACK {webSettingsItem.Name}")


        # As of the new OctoPi image, a common backend is camera-streamer, which supports WebRTC! This is a great choice because it's more efficient than jmpeg,
        # but it's impossible to stream via our backend.. For the full portal connection we try to allow WebRTC to work over the WAN, but for OE service things
        # like Live Links and Quick View, we need to use a jmpeg stream so we can proxy the video feed and not expose the user's home IP address.
        # Thus, if we see the /webcam<*>/webrtc URL as the stream URL, we will replace it with the stream URL for OE's internal uses.
        # Luckily, camera-stream also supports jmpeg streaming.
        #
        # We dont need to update the snapshot URL, because camera-streamer has compat for /?action=snapshot -> /snapshot.
        for item in results:
            cameraStreamerJmpegUrl = WebcamHelper.DetectCameraStreamerWebRTCStreamUrlAndTranslate(item.StreamUrl)
            if cameraStreamerJmpegUrl is not None:
                self.Logger.info(f"Camera-streamer webrtc {item.Name} stream url {item.StreamUrl} converted to jmpeg {cameraStreamerJmpegUrl}")
                item.StreamUrl = cameraStreamerJmpegUrl

        # Save the results in our cache.
        self.CachedWebcamSettingsResults = results
        self.LastCacheUpdateTimeSec = time.time()

        # Return the results.
        return results


    # !! Interface Function !!
    # This function is called to determine if a QuickCam stream should keep running or not.
    # The idea is since a QuickCam stream can take longer to start, for example, the Bambu Websocket stream on sends 1FPS,
    # we can keep the stream running while the print is running to lower the latency of getting images.
    # Most most platforms, this should return true if the print is running or paused, otherwise false.
    # Also consider something like Gadget, it takes pictures every 20-40 seconds, so the stream will be started frequently if it's not already running.
    def ShouldQuickCamStreamKeepRunning(self) -> bool:
        # TODO - this should return true if we are still printing.
        return False


    # !! Interface Function !!
    # Called when quick cam is about to attempt to start a stream.
    def OnQuickCamStreamStart(self, url:str) -> None:
        # Nothing to do.
        pass


    # !! Interface Function !!
    # Called when quick cam detects that the stream might have stalled.
    def OnQuickCamStreamStall(self, url:str) -> None:
        # Nothing to do.
        pass
