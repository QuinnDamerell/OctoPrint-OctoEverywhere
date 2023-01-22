import os
import threading

import configparser

# This is what we use as our important settings config.
# It's a bit heavy handed with the lock and aggressive saving, but these
# settings are important, and not accessed much.
class Config:

    # These must stay the same because our installer script requires on the format being as is!
    ServerSection = "server"
    PrinterIdKey = "printer_id"
    PrivateKeyKey = "private_key"

    # Other config items.
    LoggingSection = "logging"
    LogLevelKey = "log_level"
    LogFileMaxSizeMbKey = "max_file_size_mb"
    LogFileMaxCountKey = "max_file_count"

    def __init__(self, klipperConfigPath) -> None:
        # Define our config path
        # Note this path and name MUST STAY THE SAME because the installer PY script looks for this file.
        self.OeConfigFilePath = os.path.join(klipperConfigPath, "octoeverywhere.conf")
        # A lock to keep file access super safe
        self.ConfigLock = threading.Lock()
        self.Config = None
        # Load the config on init, to ensure it exists.
        # This will throw if there's an error reading the config.
        self._LoadConfigIfNeeded_UnderLock()


    # Gets a value from the config given the header and key.
    # If the value isn't set, the default value is returned and the default value is saved into the config.
    def GetStr(self, section, key, defaultValue):
        with self.ConfigLock:
            # Ensure we have the config.
            self._LoadConfigIfNeeded_UnderLock()
            # Check if the section and key exists
            if self.Config.has_section(section):
                if key in self.Config[section].keys():
                    return self.Config[section][key]
        # The value wasn't set, create it using the default.
        self.SetStr(section, key, defaultValue)
        return defaultValue

    # Gets a value from the config given the header and key.
    # If the value isn't set, the default value is returned and the default value is saved into the config.
    def GetInt(self, section, key, defaultValue):
        return int(self.GetStr(section, key, str(defaultValue)))


    # The same as Get, but this version ensures that the value matches a case insensitive value in the
    # acceptable value list. If it's not, the default value is used.
    def GetStrIfInAcceptableList(self, section, key, defaultValue, acceptableValueList):
        existing = self.GetStr(section, key, defaultValue)
        # Check the acceptable values
        for v in acceptableValueList:
            # If we match, this is a good value, return it.
            if v.lower() == existing.lower():
                return existing

        # The acceptable was not found. Set they key back to default.
        self.SetStr(section, key, defaultValue)
        return defaultValue


    # The same as Get, but it makes sure the value is in a range.
    def GetIntIfInRange(self, section, key, defaultValue, lowerBoundInclusive, upperBoundInclusive):
        existingStr = self.GetStr(section, key, str(defaultValue))

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
    def SetStr(self, section, key, value):
        with self.ConfigLock:
            self._LoadConfigIfNeeded_UnderLock()
            # Ensure the section exists
            if self.Config.has_section(section) is False:
                self.Config.add_section(section)
            if value is None:
                # If we are setting to None, delete the key if it exists.
                if key in self.Config[section].keys():
                    del self.Config[section][key]
            else:
                # If not none, set the key
                self.Config[section][key] = value
            self._SaveConfig_UnderLock()


    def _LoadConfigIfNeeded_UnderLock(self) -> None:
        if self.Config is not None:
            return
        # Always create a new object.
        self.Config = configparser.ConfigParser()

        # If a config exists, read it.
        # This will throw on failure.
        if os.path.exists(self.OeConfigFilePath):
            self.Config.read(self.OeConfigFilePath)
        else:
            # If no config exists, create a new file by writing the empty config now.
            print("Config file doesn't exist. Creating a new file now!")
            self._SaveConfig_UnderLock()


    def _SaveConfig_UnderLock(self) -> None:
        if self.Config is None:
            return
        with open(self.OeConfigFilePath, 'w', encoding="utf-8") as f:
            self.Config.write(f)
