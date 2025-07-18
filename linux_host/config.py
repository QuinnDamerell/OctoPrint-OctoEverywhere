import logging
import os
import threading

from typing import List, Optional

import configparser

# This is what we use as our important settings config.
# This single config class is used for all of the plugin types, but not all of the values are used for each type.
# It's a bit heavy handed with the lock and aggressive saving, but these
# settings are important, and not accessed much.
class Config:

    # This can't change or all past plugins will fail.
    ConfigFileName = "octoeverywhere.conf"

    # We allow strings to be set as None, because then it keeps then in the config with the comment about the key.
    # We use an empty value for None, to indicate that the key is not set.
    c_NoneStringValue = ""

    #
    # Common To All Plugins
    #
    LoggingSection = "logging"
    LogLevelKey = "log_level"
    LogFileMaxSizeMbKey = "max_file_size_mb"
    LogFileMaxCountKey = "max_file_count"

    GeneralSection = "general"
    GeneralBedCooldownThresholdTempC = "bed_cooldown_threshold_temp_celsius"
    GeneralBedCooldownThresholdTempCDefault = 40.0


    #
    # Used for the local Moonraker plugin and companions.
    #
    RelaySection = "relay"
    RelayFrontEndPortKey = "frontend_port"            # This field is shared with the installer, the installer can write this value. It the name can't change!
    RelayFrontEndTypeHintKey = "frontend_type_hint"   # This field is shared with the installer, the installer can write this value. It the name can't change!


    #
    # Used for the local Moonraker plugin and companions.
    #
    WebcamSection = "webcam"
    WebcamAutoSettings = "auto_settings_detection"
    WebcamNameToUseAsPrimary = "webcam_name_to_use_as_primary"
    WebcamStreamUrl = "stream_url"
    WebcamSnapshotUrl = "snapshot_url"
    WebcamFlipH = "flip_horizontally"
    WebcamFlipV = "flip_vertically"
    WebcamRotation = "rotate"

    #
    # Used for the Moonraker specific settings.
    #
    MoonrakerSection = "moonraker"
    MoonrakerApiKey = "moonraker_api_key"


    #
    # Used for both the companion and bambu connect plugins
    #
    SectionCompanion = "companion"
    CompanionKeyIpOrHostname = "ip_or_hostname"
    CompanionKeyPort = "port"


    #
    # Used only for Bambu Connect
    #
    SectionBambu = "bambu"
    BambuAccessToken = "access_token"
    BambuPrinterSn = "printer_serial_number"
    # Used if the user is logged into Bambu Cloud
    BambuCloudContext = "cloud_context"
    BambuCloudRegion = "cloud_region"
    # Explicitly defines what connection mode we are using. Can be "cloud" or "local". Defaults to local
    BambuConnectionMode = "connection_mode"
    BambuConnectionModeValueLocal = "local"
    BambuConnectionModeValueCloud = "cloud"
    BambuConnectionModeDefault = BambuConnectionModeValueLocal

    #
    # Used only for Elegoo Connect
    #
    SectionElegoo = "elegoo"
    ElegooMainboardMac = "mainboard_mac"
    AutoActivateChamberLightForWebcam = "auto_activate_chamber_light_for_webcam"


    # This allows us to add comments into our config.
    # The objects must have two parts, first, a string they target. If the string is found, the comment will be inserted above the target string. This can be a section or value.
    # A string, which is the comment to be inserted.
    c_ConfigComments = [
        { "Target": RelayFrontEndPortKey,  "Comment": "The port used for http relay. If your desired frontend runs on a different port, change this value. The OctoEverywhere plugin service needs to be restarted before changes will take effect."},
        { "Target": RelayFrontEndTypeHintKey,  "Comment": "A string only used by the UI to hint at what web interface this port is."},
        { "Target": LogLevelKey,  "Comment": "The active logging level. Valid values include: DEBUG, INFO, WARNING, or ERROR."},
        { "Target": CompanionKeyIpOrHostname,  "Comment": "The IP or hostname this companion plugin will use to connect to Moonraker. The OctoEverywhere plugin service needs to be restarted before changes will take effect."},
        { "Target": CompanionKeyPort,  "Comment": "The port this companion plugin will use to connect to Moonraker. The OctoEverywhere plugin service needs to be restarted before changes will take effect."},
        { "Target": BambuAccessToken,  "Comment": "The access token to the Bambu printer. It can be found using the LCD screen on the printer, in the settings. The OctoEverywhere plugin service needs to be restarted before changes will take effect."},
        { "Target": BambuPrinterSn,    "Comment": "The serial number of your Bambu printer. It can be found using this guide: https://wiki.bambulab.com/en/general/find-sn  The OctoEverywhere plugin service needs to be restarted before changes will take effect."},
        { "Target": BambuConnectionMode,"Comment": "The connection mode used for Bambu Connect. Can be 'cloud' or 'local'. 'cloud' will use the bambu cloud which requires the user's email and password to be set, `local` will connect via the LAN."},
        { "Target": WebcamNameToUseAsPrimary,  "Comment": "This is the webcam name OctoEverywhere will use for Gadget AI, notifications, and such. This much match the camera 'Name' from your Mainsail of Fluidd webcam settings. The default value of 'Default' will pick whatever camera the system can find."},
        { "Target": WebcamAutoSettings,  "Comment": "Enables or disables auto webcam setting detection. If enabled, OctoEverywhere will find the webcam settings configured via the frontend (Fluidd, Mainsail, etc) and use them. Disable to manually set the values and have them not be overwritten."},
        { "Target": WebcamStreamUrl,  "Comment": "Webcam streaming URL. This can be a local relative path (ex: /webcam/?action=stream) or absolute http URL (ex: http://10.0.0.1:8080/webcam/?action=stream or http://webcam.local/webcam/?action=stream)"},
        { "Target": WebcamSnapshotUrl,  "Comment": "Webcam snapshot URL. This can be a local relative path (ex: /webcam/?action=snapshot) or absolute http URL (ex: http://10.0.0.1:8080/webcam/?action=snapshot or http://webcam.local/webcam/?action=snapshot)"},
        { "Target": WebcamFlipH,  "Comment": "Flips the webcam image horizontally. Valid values are True or False"},
        { "Target": WebcamFlipV,  "Comment": "Flips the webcam image vertically. Valid values are True or False"},
        { "Target": WebcamRotation,  "Comment": "Rotates the webcam image. Valid values are 0, 90, 180, or 270"},
        { "Target": GeneralBedCooldownThresholdTempC,  "Comment": "The temperature in Celsius that the bed must be under to be considered cooled down. This is used to fire the Bed Cooldown Complete notification."},
        { "Target": ElegooMainboardMac,  "Comment": "This is the MAC address of the mainboard for the linked printer."},
        { "Target": AutoActivateChamberLightForWebcam,  "Comment": "If enabled, the chamber light will be automatically turned on when the webcam is in use."},
        { "Target": MoonrakerApiKey,  "Comment": "Leave blank unless your Moonraker requires an API key to connect. Moonraker API keys can be generated from the Mainsail or Fluidd."},
    ]


    # The config lib we use doesn't support the % sign, even though it's valid .cfg syntax.
    # Since we save URLs into the config for the webcam, it's valid syntax to use a %20 and such, thus we should support it.
    PercentageStringReplaceString = "~~~PercentageSignPlaceholder~~~"


    def __init__(self, configDirPath:str) -> None:
        self.Logger:logging.Logger = None #pyright: ignore[reportAttributeAccessIssue]
        # Define our config path
        # Note this path and name MUST STAY THE SAME because the installer PY script looks for this file.
        self.OeConfigFilePath = Config.GetConfigFilePath(configDirPath)
        # A lock to keep file access super safe
        self.ConfigLock = threading.Lock()
        self.Config:configparser.ConfigParser = None #pyright: ignore[reportAttributeAccessIssue]
        # Load the config on init, to ensure it exists.
        # This will throw if there's an error reading the config.
        self._LoadConfigIfNeeded_UnderLock()


    # Returns the config file path given the config folder
    @staticmethod
    def GetConfigFilePath(configDirPath:str) -> str:
        return os.path.join(configDirPath, Config.ConfigFileName)


    # Allows the logger to be set when it's created.
    def SetLogger(self, logger:logging.Logger) -> None:
        self.Logger = logger


    # Forces a full config read & parse from the file.
    def ReloadFromFile(self) -> None:
        # Lock and force a read.
        with self.ConfigLock:
            self._LoadConfigIfNeeded_UnderLock(forceRead=True)


    # Gets a value from the config given the header and key.
    # If the value isn't set, the default value is returned and the default value is saved into the config.
    # If the default value is None, the default will not be written into the config.
    def GetStr(self, section:str, key:str, defaultValue:Optional[str], keepInConfigIfNone=False) -> Optional[str]:
        with self.ConfigLock:
            # Ensure we have the config.
            self._LoadConfigIfNeeded_UnderLock()
            # Check if the section and key exists
            if self.Config.has_section(section):
                if key in self.Config[section].keys():
                    value = self.Config[section][key]
                    # If None or empty string written consider it not a valid value so use the default value.
                    # The default value logic will handle the keepInConfigIfNone case.
                    # Use lower, to accept user generated errors.
                    if value.lower() != "none" and len(value) > 0:
                        # Reverse any possible string replaces we had to add.
                        value = value.replace(Config.PercentageStringReplaceString, "%")
                        return value
        # The value wasn't set, create it using the default.
        self.SetStr(section, key, defaultValue, keepInConfigIfNone)
        return defaultValue


    # Just like GetStr, but it will always return a string and never none.
    def GetStrRequired(self, section:str, key:str, defaultValue:str, keepInConfigIfNone=False) -> str:
        r = self.GetStr(section, key, defaultValue, keepInConfigIfNone)
        if r is None:
            return defaultValue
        return r


    # Gets a value from the config given the header and key.
    # If the value isn't set, the default value is returned and the default value is saved into the config.
    # If the default value is None, the default will not be written into the config.
    def GetInt(self, section:str, key:str, defaultValue:Optional[int]) -> Optional[int]:
        # Use a try catch, so if a user sets an invalid value, it doesn't crash us.
        result = None

        # Convert the default value to a string, if it's not None.
        defaultValueAsStr:Optional[str] = None
        if defaultValue is not None:
            defaultValueAsStr = str(defaultValue)

        try:
            result = self.GetStr(section, key, defaultValueAsStr)
            # If None is returned, don't int it, return None.
            if result is None:
                return defaultValue
            return int(result)

        except Exception as e:
            self.Logger.error(f"Config settings error! {key} failed to get as int. Value was `{result}`. Resetting to default. "+str(e))
            self.SetStr(section, key, defaultValueAsStr)
            return defaultValue


    # Just like GetInt, but it will always return a int and never none.
    def GetIntRequired(self, section:str, key:str, defaultValue:int) -> int:
        r = self.GetInt(section, key, defaultValue)
        if r is None:
            return defaultValue
        return r


    def SetInt(self, section:str, key:str, value:Optional[int], keepInConfigIfNone=False) -> None:
        # Ensure the value is a string, unless it's None.
        s:Optional[str] = None
        if value is not None:
            s = str(value)
        self.SetStr(section, key, s, keepInConfigIfNone)


    # Gets a value from the config given the header and key.
    # If the value isn't set, the default value is returned and the default value is saved into the config.
    # If the default value is None, the default will not be written into the config.
    def GetFloat(self, section:str, key:str, defaultValue:Optional[float]) -> Optional[float]:
        # Use a try catch, so if a user sets an invalid value, it doesn't crash us.
        result = None

        # Convert the default value to a string, if it's not None.
        defaultValueAsStr:Optional[str] = None
        if defaultValue is not None:
            defaultValueAsStr = str(defaultValue)

        try:
            result = self.GetStr(section, key, defaultValueAsStr)
            # If None is returned, don't int it, return the default value, which might be None.
            if result is None:
                return defaultValue
            return float(result)

        except Exception as e:
            self.Logger.error(f"Config settings error! {key} failed to get as float. Value was `{result}`. Resetting to default. "+str(e))
            self.SetStr(section, key, defaultValueAsStr)
            return defaultValue


    # Just like GetFloat, but it will always return a float and never none.
    def GetFloatRequired(self, section:str, key:str, defaultValue:float) -> float:
        r = self.GetFloat(section, key, defaultValue)
        if r is None:
            return defaultValue
        return r


    def SetFloat(self, section:str, key:str, value:Optional[float], keepInConfigIfNone=False) -> None:
        # Ensure the value is a string, unless it's None.
        s:Optional[str] = None
        if value is not None:
            s = str(value)
        self.SetStr(section, key, s, keepInConfigIfNone)


    # Gets a value from the config given the header and key.
    # If the value isn't set, the default value is returned and the default value is saved into the config.
    # If the default value is None, the default will not be written into the config.
    def GetBool(self, section:str, key:str, defaultValue:Optional[bool]) -> Optional[bool]:
        # Use a try catch, so if a user sets an invalid value, it doesn't crash us.
        result = None

        # Convert the default value to a string, if it's not None.
        defaultValueAsStr:Optional[str] = None
        if defaultValue is not None:
            defaultValueAsStr = str(defaultValue)

        try:
            strValue = self.GetStr(section, key, defaultValueAsStr)
            # If None is returned, don't bool it, return the default value, which might be None
            if strValue is None:
                return defaultValue
            # Match it to a bool value.
            strValue = strValue.lower()
            if strValue == "false":
                return False
            elif strValue == "true":
                return True
            raise Exception("Invalid bool value, value was: "+strValue)
        except Exception as e:
            self.Logger.error(f"Config settings error! {key} failed to get as bool. Value was `{result}`. Resetting to default. "+str(e))
            self.SetStr(section, key, defaultValueAsStr)
            return defaultValue


    # Just like GetBool, but it will always return a bool and never none.
    def GetBoolRequired(self, section:str, key:str, defaultValue:bool) -> bool:
        r = self.GetBool(section, key, defaultValue)
        if r is None:
            return defaultValue
        return r


    def SetBool(self, section:str, key:str, value:Optional[bool], keepInConfigIfNone=False) -> None:
        # Ensure the value is a string, unless it's None.
        s:Optional[str] = None
        if value is not None:
            s = str(value)
        self.SetStr(section, key, s, keepInConfigIfNone)


    # The same as Get, but this version ensures that the value matches a case insensitive value in the
    # acceptable value list. If it's not, the default value is used.
    def GetStrIfInAcceptableList(self, section:str, key:str, defaultValue:str, acceptableValueList:List[str]) -> str:
        existing = self.GetStr(section, key, defaultValue)

        if existing is not None:
            # Check the acceptable values
            for v in acceptableValueList:
                # If we match, this is a good value, return it.
                if v.lower() == existing.lower():
                    return existing

        # The acceptable was not found. Set they key back to default.
        self.SetStr(section, key, defaultValue)
        return defaultValue


    # The same as Get, but it makes sure the value is in a range.
    def GetIntIfInRange(self, section:str, key:str, defaultValue:Optional[int], lowerBoundInclusive:int, upperBoundInclusive:int) -> int:
        # A default value of None is not allowed here.
        if defaultValue is None:
            raise Exception(f"A default value of none is not valid for int ranges. {section}:{key}")

        existingStr = self.GetStr(section, key, str(defaultValue))
        if existingStr is not None:
            # Make sure the value is in range.
            try:
                existing = int(existingStr)
                if existing >= lowerBoundInclusive and existing <= upperBoundInclusive:
                    return existing
            except Exception:
                pass

        # The acceptable was not found. Set they key back to default.
        self.SetStr(section, key, str(defaultValue))
        return defaultValue


    # Sets the value into the config and saves it.
    # Setting a value of None will delete the key from the config.
    def SetStr(self, section:str, key:str, value:Optional[str], keepInConfigIfNone=False) -> None:
        # Ensure the value is a string, unless it's None
        if value is not None:
            value = str(value)
            # The config library we use doesn't allow for % to be used in strings, even though it should be legal.
            value = value.replace("%", Config.PercentageStringReplaceString)
        with self.ConfigLock:
            self._LoadConfigIfNeeded_UnderLock()
            # Ensure the section exists
            if self.Config.has_section(section) is False:
                self.Config.add_section(section)
            # If the value is None but we want to keep it in the config, set it to the None string.
            if value is None and keepInConfigIfNone is True:
                value = Config.c_NoneStringValue
            # If the value is still None, we will make sure the key is deleted.
            if value is None:
                # None is a special case, if we are setting it, delete the key if it exists.
                if key in self.Config[section].keys():
                    del self.Config[section][key]
                else:
                    # If there was no key, return early, since we did nothing.
                    # This is a common case, since we use GetStr(..., ..., None) often to get the value if it exists.
                    return
            else:
                # If not none, set the key
                self.Config[section][key] = value
            self._SaveConfig_UnderLock()


    def _LoadConfigIfNeeded_UnderLock(self, forceRead = False) -> None:
        if self.Config is not None and forceRead is False:
            return

        # Always create a new object.
        # For our config, we use strict and such, so we know the config is valid.
        self.Config = configparser.ConfigParser()

        # If a config exists, read it.
        # This will throw on failure.
        if os.path.exists(self.OeConfigFilePath):
            self.Config.read(self.OeConfigFilePath)
        else:
            # If no config exists, create a new file by writing the empty config now.
            #print("Config file doesn't exist. Creating a new file now!")
            self._SaveConfig_UnderLock()


    def _SaveConfig_UnderLock(self) -> None:
        if self.Config is None:
            return

        # Write the current settings to the file.
        # This lets the config lib format everything how it wants.
        with open(self.OeConfigFilePath, 'w', encoding="utf-8") as f:
            self.Config.write(f)

        # After writing, read the file and insert any comments we have.
        finalOutput = ""
        with open(self.OeConfigFilePath, 'r', encoding="utf-8") as f:
            # Read all lines
            lines = f.readlines()
            for line in lines:
                lineLower = line.lower()
                # If anything in the line matches the target, add the comment just before this line.
                for cObj in Config.c_ConfigComments:
                    if cObj["Target"] in lineLower:
                        # Add the comment.
                        finalOutput += "# " + cObj["Comment"] + os.linesep
                        break
                finalOutput += line

        # Finally, write the file back one more time.
        with open(self.OeConfigFilePath, 'w', encoding="utf-8") as f:
            f.write(finalOutput)
