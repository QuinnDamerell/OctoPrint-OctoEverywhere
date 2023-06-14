
from octoeverywhere.webcamhelper import WebcamSettingItem, WebcamHelper
from octoeverywhere.octohttprequest import OctoHttpRequest

# This class implements the webcam platform helper interface for OctoPrint.
class OctoPrintWebcamHelper():


    def __init__(self, logger, octoPrintSettingsObject):
        self.Logger = logger
        self.OctoPrintSettingsObject = octoPrintSettingsObject


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

        # This is the normal case when running in OctoPrint.
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

        return [
            WebcamSettingItem(snapshotUrl, streamUrl, flipH, flipV, rotation)
        ]
