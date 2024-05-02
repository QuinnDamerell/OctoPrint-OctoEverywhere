import logging

from ..sentry import Sentry

#
# A platform agnostic definition of a webcam stream.
#
class WebcamSettingItem:

    # We need to cap this so they aren't crazy long.
    # However, this COULD mess with teh default camera name logic, since it matches off names.
    c_MaxWebcamNameLength = 20

    # The snapshotUrl and streamUrl can be relative or absolute.
    #
    #  name must exist.
    #  snapshotUrl OR streamUrl can be None if the values aren't available, but not both.
    #  flipHBool & flipVBool & rotationInt must exist.
    #  rotationInt must be 0, 90, 180, or 270
    def __init__(self, name:str = "", snapshotUrl:str = "", streamUrl:str = "", flipHBool:bool = False, flipVBool:bool = False, rotationInt:int = 0, enabled:bool = True):
        self._name = ""
        self.Name = name
        self.SnapshotUrl = snapshotUrl
        self.StreamUrl = streamUrl
        self.FlipH = flipHBool
        self.FlipV = flipVBool
        self.Rotation = rotationInt
        # This is a special flag mostly used for the local plugin webcams to indicate they are no enabled.
        self.Enabled = enabled


    @property
    def Name(self):
        return self._name


    @Name.setter
    def Name(self, value):
        # When the name is set, make sure we convert it to the string style we use internally.
        # This ensures that the name can be used and is consistent across the platform.
        if value is not None and len(value) > 0:
            value = self._MoonrakerToInternalWebcamNameConvert(value)
        self._name = value


    def Validate(self, logger:logging.Logger) -> bool:
        if self.Name is None or len(self.Name) == 0:
            logger.error(f"Name value in WebcamSettingItem is None or empty. {self.StreamUrl}")
            return False
        if self.Rotation is None or (self.Rotation != 0 and self.Rotation != 90 and self.Rotation != 180 and self.Rotation != 270):
            logger.error(f"Rotation value in WebcamSettingItem is an invalid int. {self.Name} - {self.Rotation}")
            return False
        if (self.SnapshotUrl is None or len(self.SnapshotUrl) == 0) and (self.StreamUrl is None or len(self.StreamUrl) == 0):
            logger.error(f"Snapshot and StreamUrl values in WebcamSettingItem are none or empty {self.Name}")
            return False
        if self.FlipH is None:
            logger.error(f"FlipH value in WebcamSettingItem is None {self.Name}")
            return False
        self.FlipH = bool(self.FlipH)
        if self.FlipV is None:
            logger.error(f"FlipV value in WebcamSettingItem is None {self.Name}")
            return False
        self.FlipV = bool(self.FlipV)
        return True


    # Used to serialize the object to a dict that can be used with json.
    # THESE PROPERTY NAMES CAN'T CHANGE, it's used for the API and it's used to serialize to disk.
    def Serialize(self, includeUrls:bool = True) -> dict:
        d = {
            "Name": self.Name,
            "FlipH": self.FlipH,
            "FlipV": self.FlipV,
            "Rotation": self.Rotation,
            "Enabled": self.Enabled
        }
        if includeUrls:
            d["SnapshotUrl"] = self.SnapshotUrl
            d["StreamUrl"] = self.StreamUrl
        return d


    # Used to convert a dict back into a WebcamSettingItem object.
    # Returns None if there's a failure
    @staticmethod
    def Deserialize(d:dict, logger:logging.Logger):
        try:
            name = d.get("Name")
            snapshotUrl = d.get("SnapshotUrl")
            streamUrl = d.get("StreamUrl")
            flipH = d.get("FlipH")
            flipV = d.get("FlipV")
            rotation = d.get("Rotation")
            enabled = d.get("Enabled")
            if name is None or snapshotUrl is None or streamUrl is None or flipH is None or flipV is None or rotation is None or enabled is None:
                raise Exception("Failed to deserialize WebcamSettingItem, missing values.")
            i = WebcamSettingItem(str(name), str(snapshotUrl), str(streamUrl), bool(flipH), bool(flipV), int(rotation), bool(enabled))
            if i.Validate(logger) is False:
                raise Exception("Failed to validate WebcamSettingItem.")
            return i
        except Exception as e:
            Sentry.Exception("Failed to deserialize WebcamSettingItem", e)
        return None


    def _MoonrakerToInternalWebcamNameConvert(self, name:str):
        if name is not None and len(name) > 0:
            # Enforce max name length.
            if len(name) > WebcamSettingItem.c_MaxWebcamNameLength:
                name = name[WebcamSettingItem.c_MaxWebcamNameLength]
            # Ensure the string is only utf8
            name = name.encode('utf-8', 'ignore').decode('utf-8')
            # Make the first letter uppercase
            name = name[0].upper() + name[1:]
            # If there are any / they will break our UI, so remove them.
            name = name.replace("/", "")
        return name
