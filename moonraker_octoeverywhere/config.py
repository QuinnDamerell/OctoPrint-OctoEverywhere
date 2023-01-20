import os
import threading

import configparser

# This is what we use as our important settings config.
# It's a bit heavy handed with the lock and aggressive saving, but these
# settings are important, and not accessed much.
class Config:

    # These must stay the same because our installer script requires on the format being as is!
    ServerSection = "server"
    PrinterIdKey = "printerid"
    PrivateKeyKey = "privatekey"

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
    # If the value isn't set, the default value is returned.
    def Get(self, section, key, defaultValue):
        with self.ConfigLock:
            # Ensure we have the config.
            self._LoadConfigIfNeeded_UnderLock()
            # Check if the section and key exists
            if self.Config.has_section(section):
                if key in self.Config[section].keys():
                    return self.Config[section][key]
            return defaultValue


    # Sets the value into the config and saves it.
    def Set(self, section, key, value):
        with self.ConfigLock:
            self._LoadConfigIfNeeded_UnderLock()
            if self.Config.has_section(section) is False:
                self.Config.add_section(section)
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
