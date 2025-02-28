import os
import json
from enum import IntEnum

from octoeverywhere.telemetry import Telemetry

from .Logging import Logger
from .Paths import Paths

# Indicates the OS type this installer is running on.
# These can't changed, only added to, since they are using to write on disk and such.
class OsTypes(IntEnum):
    Debian = 1
    SonicPad = 2
    K1 = 3 # Both the K1 and K1 Max
    K2 = 4


# This class holds the context of the installer, meaning all of the target vars and paths
# that this instance is using.
# There is a generation system, where generation defines what data is required by when.
# Generation 1 - Must always exist, from the start.
# Generation 2 - Must exist after the discovery phase.
# Generation 3 - Must exist after the configure phase.
class Context:

    # For companions or bambu connect plugins, the primary instance is a little special.
    # It will have an instance ID of 1, but when we use the id we want to exclude the suffix.
    # This is so the first instance will have a normal name like "octoeverywhere-bambu" instead of "octoeverywhere-bambu-1"
    CompanionPrimaryInstanceId = "1"

    def __init__(self) -> None:

        #
        # Generation 1
        #

        # This is the repo root of OctoEverywhere. This is common for all instances.
        self.RepoRootFolder:str = None

        # This is the path to the PY virtual env for OctoEverywhere. This is common for all instances.
        self.VirtualEnvPath:str = None

        # This is the user name of the user who launched the install script.
        # Useful because this module is running as a sudo user.
        self.UserName:str = None

        # This is the user home path of the user who launched the install script.
        # Useful because this module is running as a sudo user.
        self.UserHomePath:str = None

        # A string containing all of the args the install script was launched with.
        self.CmdLineArgs:str = None

        # Detected in this installer as we are starting, this indicates what type of OS we are running on.
        self.OsType:OsTypes = OsTypes.Debian

        # Parsed from the command line args, if debug should be enabled.
        self.Debug:bool = False

        # Parsed from the command line args, if we should show help.
        self.ShowHelp:bool = False

        # Parsed from the command line args, if we should skip sudo actions for debugging.
        self.SkipSudoActions:bool = False

        # Parsed from the command line args, if set, we shouldn't auto select the moonraker instance.
        self.DisableAutoMoonrakerInstanceSelection:bool = False

        # Parsed from the command line args, if set, this plugin should be installed as an companion.
        self.IsCompanionSetup:bool = False

        # Parsed from the command line args, if set, this plugin should be installed as an bambu connect (similar to the companion).
        self.IsBambuSetup:bool = False

        # Parsed from the command line args, if set, this plugin should be installed as an elegoo connect (similar to the companion).
        self.IsElegooSetup:bool = False

        # Parsed from the command line args, if set, the plugin install should be in update mode.
        self.IsUpdateMode:bool = False

        # Parsed from the command line args, if set, the plugin install should be in uninstall mode.
        self.IsUninstallMode:bool = False


        #
        # Generation 2
        #

        # This is the full file path to the moonraker config.
        self.MoonrakerConfigFilePath:str = None

        # This is the file name of the moonraker service we are targeting.
        self.MoonrakerServiceFileName:str = None

        ### - OR - ###
        # These values will be filled out if this is a companion OR Bambu connect setup.

        # The root folder where the companion or Bambu plugin data lives.
        self.CompanionDataRoot:str = None

        # The companion or bambu instance id, so we can support multiple instances on one device.
        # Note that a id of "1" is special, and you can use IsPrimaryCompanionBambuOrElegoo to detect it.
        self.CompanionInstanceId:str = None


        #
        # Generation 3
        #

        # For local plugin configs, this is the printer data folder root.
        # For companion or bambu plugins, this is the same as self.CompanionDataRoot
        self.RootFolder:str = None

        # This the folder where our main plugin config is or will be.
        # For local plugin configs, this is the Moonraker config folder.
        # For companion or bambu plugins, this is the same as self.CompanionDataRoot
        self.ConfigFolder:str = None

        # This is the folder where the plugin logs will go.
        self.LogsFolder:str = None

        # The path to where the local storage will be put for this instance.
        self.LocalFileStorageFolder:str = None

        # This is the name of this OctoEverywhere instance's service.
        self.ServiceName:str = None

        # The full file path and file name of this instance's service file.
        self.ServiceFilePath:str = None

        #
        # Generation 4
        #

        # Generation 4 - If the instance config file existed before we created the service, this will hold the printer id.
        self.ExistingPrinterId:str = None


    # Returns true if the OS is Creality OS, aka K1 or Sonic Pad
    def IsCrealityOs(self) -> bool:
        return self.OsType == OsTypes.SonicPad or self.OsType == OsTypes.K1 or self.OsType == OsTypes.K2


    # Returns true if the target is a companion, bambu connect, or elegoo connect setup.
    def IsCompanionBambuOrElegoo(self) -> bool:
        return self.IsCompanionSetup or self.IsBambuSetup or self.IsElegooSetup


    # Returns true if this is a bambu, elegoo, companion plugin and it's the primary, aka it has an instance ID of 1.
    def IsPrimaryCompanionBambuOrElegoo(self) -> bool:
        if self.IsCompanionBambuOrElegoo() is False:
            raise Exception("IsPrimaryCompanionBambuOrElegoo was called for a non companion or bambu context.")
        return self.CompanionInstanceId == Context.CompanionPrimaryInstanceId


    @staticmethod
    def LoadFromArgString(argString:str):
        Logger.Debug("Found config: "+argString)
        try:
            argObj = json.loads(argString)
            context = Context()
            context.RepoRootFolder = argObj["OE_REPO_DIR"]
            context.VirtualEnvPath = argObj["OE_ENV"]
            context.UserName = argObj["USERNAME"]
            context.UserHomePath = argObj["USER_HOME"]
            context.CmdLineArgs = argObj["CMD_LINE_ARGS"]
            return context
        except Exception as e:
            Logger.Error(f"Failed to parse bootstrap json args. args string: `{argString}`")
            raise e


    def Validate(self, generation = 1) -> None:
        self._ValidatePathAndExists(self.RepoRootFolder, "Required Env Var OE_REPO_DIR was not found; make sure to run the install.sh script to begin the installation process")
        self._ValidatePathAndExists(self.VirtualEnvPath, "Required Env Var OE_ENV was not found; make sure to run the install.sh script to begin the installation process")
        self._ValidatePathAndExists(self.UserHomePath, "Required Env Var USER_HOME was not found; make sure to run the install.sh script to begin the installation process")
        self._ValidateString(self.UserName, "Required Env Var USERNAME was not found; make sure to run the install.sh script to begin the installation process")
        # Can be an empty string, but not None.
        if self.CmdLineArgs is None:
            raise Exception("Required Env Var CMD_LINE_ARGS was not found; make sure to run the install.sh script to begin the installation process.")

        # Since these exist, clean them up.
        self.RepoRootFolder = self.RepoRootFolder.strip()
        self.VirtualEnvPath = self.VirtualEnvPath.strip()
        self.UserName = self.UserName.strip()
        self.UserHomePath = self.UserHomePath.strip()
        self.CmdLineArgs = self.CmdLineArgs.strip()

        if generation >= 2:
            if self.IsCompanionBambuOrElegoo():
                self._ValidatePathAndExists(self.CompanionDataRoot, "Required config var Companion Data Path was not found")
                self._ValidateString(self.CompanionInstanceId, "Required config var Companion Instance Id was not found")
                self.CompanionDataRoot = self.CompanionDataRoot.strip()
                self.CompanionInstanceId = self.CompanionInstanceId.strip()
                if self.OsType != OsTypes.Debian:
                    raise Exception("The OctoEverywhere companion can only be installed on Debian based operating systems.")
            else:
                self._ValidatePathAndExists(self.MoonrakerConfigFilePath, "Required config var Moonraker Config File Path was not found")
                self._ValidateString(self.MoonrakerServiceFileName, "Required config var Moonraker Service File Name was not found")
                self.MoonrakerConfigFilePath = self.MoonrakerConfigFilePath.strip()
                self.MoonrakerServiceFileName = self.MoonrakerServiceFileName.strip()

        if generation >= 3:
            self._ValidatePathAndExists(self.RootFolder, "Required config var Root Folder was not found")
            self._ValidatePathAndExists(self.ConfigFolder, "Required config var Config Folder was not found")
            self._ValidatePathAndExists(self.LogsFolder, "Required config var Logs Folder was not found")
            self._ValidatePathAndExists(self.LocalFileStorageFolder, "Required config var local storage folder was not found")
            # This path wont exist on the first install, because it won't be created until the end of the install.
            self._ValidateString(self.ServiceFilePath, "Required config var service file path was not found")
            self._ValidateString(self.ServiceName, "Required config var service name was not found")

        if generation >= 4:
            # The printer ID can be None, this means it didn't exist before we installed the service.
            pass


    def ParseCmdLineArgs(self):
        # We must have a string, indicating the ./install script passed the var.
        # But it can be an empty string, that's fine.
        if self.CmdLineArgs is None:
            raise Exception("Required Env Var CMD_LINE_ARGS was not found; make sure to run the install.sh script to begin the installation process")

        # Handle the original cmdline args.
        # The format is <moonraker config file path> <moonraker service file path> -other -args
        # Where both file paths are optional, but if only one is given, it's assumed to be the config file path.
        args = self.CmdLineArgs.split(' ')
        for a in args:
            # Ensure there's a string and it's not empty.
            # If no args are passed, there will be one empty string after the split.
            if isinstance(a, str) is False or len(a) == 0:
                continue

            # Handle and flags passed.
            if a[0] == '-':
                rawArg = a[1:]
                rawArgLower = rawArg.lower()
                if rawArgLower == "debug":
                    # Enable debug printing.
                    self.Debug = True
                    Logger.EnableDebugLogging()
                elif rawArgLower == "help" or rawArgLower == "usage" or rawArgLower == "h":
                    self.ShowHelp = True
                elif rawArgLower == "skipsudoactions":
                    Logger.Warn("Skipping sudo actions. ! This will not result in a valid install! ")
                    self.SkipSudoActions = True
                elif rawArgLower == "noatuoselect":
                    Logger.Debug("Disabling Moonraker instance auto selection.")
                    self.DisableAutoMoonrakerInstanceSelection = True
                elif rawArgLower == "observer":
                    # This is the legacy flag
                    Logger.Debug("Setup running in companion setup mode.")
                    self.IsCompanionSetup = True
                elif rawArgLower == "companion":
                    Logger.Debug("Setup running in companion setup mode.")
                    self.IsCompanionSetup = True
                elif rawArgLower == "bambu":
                    Logger.Debug("Setup running in Bambu Connect setup mode.")
                    self.IsBambuSetup = True
                elif rawArgLower == "elegoo":
                    Logger.Debug("Setup running in Elegoo Connect setup mode.")
                    self.IsElegooSetup = True
                elif rawArgLower == "update" or rawArgLower == "upgrade":
                    Logger.Debug("Setup running in update mode.")
                    self.IsUpdateMode = True
                elif rawArgLower == "uninstall":
                    Logger.Debug("Setup running in uninstall mode.")
                    self.IsUninstallMode = True
                else:
                    raise Exception("Unknown argument found. Use install.sh -help for options.")

            # If there's a raw string, assume its a config path or service file name.
            else:
                if self.MoonrakerConfigFilePath is None:
                    self.MoonrakerConfigFilePath = a
                    Logger.Debug("Moonraker config file path found as argument:"+self.MoonrakerConfigFilePath)
                    Telemetry.Write("Installer-MoonrakerConfigPassed", 1)
                elif self.MoonrakerServiceFileName is None:
                    self.MoonrakerServiceFileName = a
                    Logger.Debug("Moonraker service file name found as argument:"+self.MoonrakerServiceFileName)
                else:
                    raise Exception("Unknown argument found. Use install.sh -help for options.")


    def _ValidatePathAndExists(self, path:str, error:str):
        if path is None or os.path.exists(path) is False:
            raise Exception(error)


    def _ValidateString(self, s:str, error:str):
        if s is None or isinstance(s, str) is False or len(s) == 0:
            raise Exception(error)


    def DetectOsType(self):
        #
        # Note! This should closely resemble the ostype.py class in the plugin and the logic in the ./install.sh script!
        #

        # For the k1 and k1 max, we look for the "buildroot" OS.
        if os.path.exists("/etc/os-release"):
            with open("/etc/os-release", "r", encoding="utf-8") as osInfo:
                lines = osInfo.readlines()
                for l in lines:
                    if "ID=buildroot" in l:
                        # If we find it, make sure the user data path is where we expect it to be, and we are good.
                        if os.path.exists(Paths.CrealityOsUserDataPath_K1):
                            self.OsType = OsTypes.K1
                            return
                        raise Exception("We detected a K1 or K1 Max OS, but can't determine the data path. Please contact support.")

        # For the Sonic Pad, we look for the openwrt os
        if os.path.exists("/etc/openwrt_release"):
            with open("/etc/openwrt_release", "r", encoding="utf-8") as osInfo:
                lines = osInfo.readlines()
                for l in lines:
                    l = l.lower()
                    if "sonic" in l:
                        # If we find it, make sure the user data path is where we expect it to be, and we are good.
                        if os.path.exists(Paths.CrealityOsUserDataPath_SonicPad):
                            self.OsType = OsTypes.SonicPad
                            return
                        raise Exception("We detected a Sonic Pad, but can't determine the data path. Please contact support.")
                    # The K2 is based on the OS release "tina" and then we look for the user data path.
                    if "tina" in l:
                        if os.path.exists(Paths.CrealityOsUserDataPath_K2):
                            self.OsType = OsTypes.K2
                            return

        # The OS is debian
        self.OsType = OsTypes.Debian
        return
