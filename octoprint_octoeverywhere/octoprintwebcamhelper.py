
from octoeverywhere.webcamhelper import WebcamSettingItem

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
            self.Logger.info("OctoPrintWebcamHelper has no OctoPrintSettingsObject")
            return None

        # This is the normal case when running in OctoPrint.
        snapshotUrl = self.OctoPrintSettingsObject.global_get(["webcam", "snapshot"])
        streamUrl = self.OctoPrintSettingsObject.global_get(["webcam", "stream"])
        flipH = self.OctoPrintSettingsObject.global_get(["webcam", "flipH"])
        flipV = self.OctoPrintSettingsObject.global_get(["webcam", "flipV"])
        rotate90 = self.OctoPrintSettingsObject.global_get(["webcam", "rotate90"])

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
