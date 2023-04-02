# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#                                                             READ ME
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#
# This script is responsible for the remainder of the OctoEverywhere for klipper setup process, after the bash script bootstrapped things.
# The script should only be launched by the install.sh bash script, since the install.sh script bootstraps the install process by setting up
# required system packages, the virtual env, and the python packages required. This install script runs in the same service virtual env.
#
# The only parameter the system takes is the moonraker config file path. Based on that one file, we can find everything we need, like the answers
# to the setup differences listed below. If the user launches the install scrip by hand, the config file isn't passed usually. We will try to auto-detect
# the config file path or ask the user to provide it. If we are being launched from Kiauh, the config file will be passed, and we don't have to ask the
# user anything.
#
# Info about Kiauh, Klipper, Moonraker, etc. from the Kiauh and Moonraker devs, this is the proper way to setup into the system.
#
# If only one instance of klipper and moonraker is running, thing are very easy.
#
# Service Files:
#    1) Every instance of klipper and moonraker both have their own service files.
#          a) If there is only one instance, the service file name is `moonraker.service`
#          b) If there are multiple instances of klipper and thus moonraker, the service file names will be `moonraker-<number or name>.service` and match `klipper-<number or name>.service`
#                i) These names are set in stone one setup from the install, if the user wanted to change them they would have to re-install.
#    2) Thus OctoEverywhere will follow the same naming convention, in regards to the service file names.
#
# Moonraker Data Folders:
#   1) Every klipper and paired moonraker instance has it's own data folder.
#          a) If there is only one instance, data folder defaults to ~/printer_data
#          b) If there are multiple instances of klipper and thus moonraker, the folders will be ~/<name>_data
#   2) For OctoEverywhere since we setup and target per moonraker instance, all per instances files will be stored in the data folder that matches the targeted instance.
#
#

import os
import sys
import json
import subprocess
import time
import traceback
import base64
# pylint: disable=import-error # This package only exists on Linux, but this script only runs on Linux.
import pwd

import configparser
import requests


#
# Output Helpers
#
class BashColors:
    Green='\033[92m'
    Yellow='\033[93m'
    Magenta='\033[0;35m'
    Red="\033[1;31m"
    Cyan="\033[1;36m"
    Default="\033[0;0m"

def Debug(msg) -> None:
    if MoonrakerInstaller.DebugLogging is True:
        print(BashColors.Yellow+"DEBUG: "+BashColors.Green+msg+BashColors.Default)

def Header(msg)  -> None:
    print(BashColors.Cyan+msg+BashColors.Default)

def Blank() -> None:
    print("")

def Info(msg) -> None:
    print(BashColors.Green+msg+BashColors.Default)

def Warn(msg) -> None:
    print(BashColors.Yellow+msg+BashColors.Default)

def Error(msg) -> None:
    print(BashColors.Red+msg+BashColors.Default)

def Purple(msg) -> None:
    print(BashColors.Magenta+msg+BashColors.Default)


class MoonrakerInstaller:

    DebugLogging = False
    SystemdServiceFilePath = "/etc/systemd/system"

    def __init__(self) -> None:

        # These vars are passed to us by the command line args
        # They are set in ParseArgs()
        self.RepoRootFolder = None
        self.VirtualEnvPath = None
        self.UserName = None
        self.UserHomePath = None
        # This one is optional - for installs with Kiauh it will be passed, otherwise we will auto-detect it and ask the user to confirm.
        self.MOONRAKER_CONFIG = None

        # These var are all setup in InitForMoonrakerConfig(), once we know the config we are targeting.
        self.ServiceName = None
        self.ServiceFilePath = None
        self.LocalFileStoragePath =  None
        self.PrinterDataFolder = None      # This is the data folder root (most single instance setups this is ~/printer_data/)
        self.PrinterConfigFolder = None    # This should be PrinterDataFolder/config
        self.PrinterLogFolder = None       # This should be PrinterDataFolder/log


    def Run(self):
        try:
            # First, ensure we are launched as root.
            # pylint: disable=no-member # Linux only
            if os.geteuid() != 0:
                raise Exception("Script not ran as root.")

            # Parse the required command line args
            self.ParseArgs()
            Debug("Args parsed")

            # Make sure we have a moonraker config. It will either be passed to us as an argument, we auto detect it, or the user can supply it.
            self.EnsureMoonrakerConfig()

            # Setup all of the class vars based on the instance of moonraker we are targeting.
            self.InitForMoonrakerConfig()

            # Ensure all of the folders we need to write to exist and are write able by us.
            self.EnsureDirExists(self.LocalFileStoragePath, True)

            # Before we start the service, check if there is a printer id, so we know if this is a first run or not.
            existingPrinterId = self.GetPrinterIdFromServiceConfigFile()

            # Create the service and run it!
            # Do this every time, to ensure it's always updated if anything changes.
            # If this is the first run, the first thing it should do is create a printer id for us to use.
            self.CreateAndRunService()

            # After the service is running, always check the printer account status.
            # If it's not setup, we will help the user set it up.
            self.CheckIfPrinterIsConnectedToAccountAndSetupIfNot(existingPrinterId)

            # Success!
            Blank()
            Blank()
            Blank()
            Purple("        ~~~ OctoEverywhere For Klipper Setup Complete ~~~    ")
            Warn(  "  You Can Access Your Printer Anytime From OctoEverywhere.com")
            Header("                   Welcome To Our Community                  ")
            Error( "                            <3                               ")
            Blank()
            Blank()

        except Exception as e:
            tb = traceback.format_exc()
            Blank()
            Blank()
            Error("Installer failed. "+str(e)+"; "+str(tb))
            Blank()
            Blank()
            Error("Please contact our support team directly at support@octoeverywhere.com so we can help you fix this issue!")
            Blank()
            Blank()
            sys.exit(1)


    # Parses the expected args and validates they exist.
    def ParseArgs(self):
        try:
            if len(sys.argv) == 0:
                raise Exception("Required env var json was not found; make sure to run the install.sh script to begin the installation process.")

            jsonFound = False
            for argStr in sys.argv:
                # Since this arg can be first or second, depending on the launch.
                if "{" in argStr:
                    jsonFound = True
                    Debug("Found config: "+argStr)
                    argObj = json.loads(argStr)
                    self.RepoRootFolder = argObj["OE_REPO_DIR"]
                    self.VirtualEnvPath = argObj["OE_ENV"]
                    self.UserName = argObj["USERNAME"]
                    self.UserHomePath = argObj["USER_HOME"]
                    # MOONRAKER_CONFIG is optional.
                    if "MOONRAKER_CONFIG" in argObj:
                        self.MOONRAKER_CONFIG = argObj["MOONRAKER_CONFIG"]

                    # Validate the required vars exist.
                    if self.RepoRootFolder is None:
                        raise Exception("Required Env Var OE_REPO_DIR was not found; make sure to run the install.sh script to begin the installation process.")
                    if self.VirtualEnvPath is None:
                        raise Exception("Required Env Var OE_ENV was not found; make sure to run the install.sh script to begin the installation process.")
                    if self.UserName is None:
                        raise Exception("Required Env Var USERNAME was not found; make sure to run the install.sh script to begin the installation process.")
                    if self.UserHomePath is None:
                        raise Exception("Required Env Var USER_HOME was not found; make sure to run the install.sh script to begin the installation process.")
            if jsonFound is False:
                raise Exception("Required Env Var json was not found; make sure to run the install.sh script to begin the installation process.")
        except Exception as e:
            raise Exception("Required Env Var Json could not be parsed. make sure to run the install.sh script to begin the installation process. "+str(e)) from e


    # Helper to ask the user a question.
    def AskYesOrNoQuestion(self, question) -> bool:
        val = None
        while True:
            try:
                val = input(question+" [y/n] ")
                val = val.lower().strip()
                if val == "n" or val == "y":
                    break
            except Exception as e:
                Warn("Invalid input, try again. Error: "+str(e))
        return val == "y"


    # Recursively looks from the root path for the moonraker config file.
    def FindMoonrakerConfigFromPath(self, path, depth = 0):
        if depth > 20:
            return None
        fileAndDirList = os.listdir(path)
        for fileOrDirName in fileAndDirList:
            fullFileOrDirPath = os.path.join(path, fileOrDirName)
            fileNameOrDirLower = fileOrDirName.lower()
            # Look through child folders.
            if os.path.isdir(fullFileOrDirPath):
                # Ignore backup folders
                if fileNameOrDirLower == "backup":
                    continue
                tempResult = self.FindMoonrakerConfigFromPath(fullFileOrDirPath, depth + 1)
                if tempResult is not None:
                    return tempResult
            # If it's a file, test if it.
            elif os.path.isfile(fullFileOrDirPath) and os.path.islink(fullFileOrDirPath) is False:
                # We use an exact match, to prevent things like moonraker.conf.backup from matching, which is common.
                if fileNameOrDirLower == "moonraker.conf":
                    return fullFileOrDirPath
        return None


    # Searches for moonraker service files and tries to find the config files from them.
    # Returns a list of found config files or an empty list if none are found.
    def SearchForMoonrakerConfigsFromServiceFiles(self):
        result = []
        # Find all of the service files.
        moonrakerServiceFilePaths = self.FindAllSystemdServiceFiles("moonraker")
        for filePath in moonrakerServiceFilePaths:
            try:
                Debug("Found moonraker service file: "+filePath)
                with open(filePath, "r", encoding="utf-8") as serviceFile:
                    lines = serviceFile.readlines()
                    configPathFound = False
                    for l in lines:
                        if configPathFound is True:
                            break
                        # Search for the line that has the moonraker environment.
                        # Ex EnvironmentFile=/home/pi/printer_1_data/systemd/moonraker.env
                        if "moonraker.env" in l.lower():
                            Debug("Found moonraker.env line: "+l)

                            # When found, try to file the config path.
                            equalsPos = l.find('=')
                            if equalsPos == -1:
                                continue
                            # Move past the = sign.
                            equalsPos += 1

                            # Find the end of the path.
                            filePathEnd = l.find(' ', equalsPos)
                            if filePathEnd == -1:
                                filePathEnd = len(l)

                            # Get the file path.
                            # Sample path /home/pi/printer_1_data/systemd/moonraker.env
                            envFilePath = l[equalsPos:filePathEnd]

                            # From the env path, remove the file name and test if the config is in the same dir, which is not common.
                            searchConfigPath = self.GetParentDirectory(envFilePath)
                            moonrakerConfigFilePath = self.FindMoonrakerConfigFromPath(searchConfigPath)
                            if moonrakerConfigFilePath is None:
                                # Move to the parent and look explicitly in the config folder, if there is one, this is where we expect to find it.
                                # We do this to prevent finding config files in other printer_data folders, like backup.
                                Debug("Moonraker config not found in env dir")
                                searchConfigPath = self.GetParentDirectory(self.GetParentDirectory(envFilePath))
                                searchConfigPath = os.path.join(searchConfigPath, "config")
                                if os.path.exists(searchConfigPath):
                                    moonrakerConfigFilePath = self.FindMoonrakerConfigFromPath(searchConfigPath)

                                if moonrakerConfigFilePath is None:
                                    # If we still didn't find it, move the printer_data root, and look one last time.
                                    Debug("Moonraker config not config dir")
                                    searchConfigPath = self.GetParentDirectory(self.GetParentDirectory(envFilePath))
                                    moonrakerConfigFilePath = self.FindMoonrakerConfigFromPath(searchConfigPath)

                            # If we don't have it, we can't find it.
                            if moonrakerConfigFilePath is None:
                                Warn("Failed to find moonraker config from service file: "+filePath)
                                continue

                            Debug("Service file "+filePath + " -> "+moonrakerConfigFilePath)
                            result.append(moonrakerConfigFilePath)
                            configPathFound = True
            except Exception as e:
                Warn("Failed to read service config file for config find.: "+filePath+" "+str(e))
        # Return what we found.
        return result


    # Ensures we have a moonraker config to target.
    # If not, it tries to find one or gets it from the user.
    def EnsureMoonrakerConfig(self):
        # If a config was passed to the setup, validate it and use it.
        if self.MOONRAKER_CONFIG is not None and len(self.MOONRAKER_CONFIG) > 0:
            if os.path.exists(self.MOONRAKER_CONFIG):
                Info("Moonraker config passed to setup. "+self.MOONRAKER_CONFIG)
                return
            else:
                Warn("Moonraker config passed to setup, but the file wasn't found. "+self.MOONRAKER_CONFIG)

        # Our plugin current requires the service file to be found, so we can properly bind to the correct service suffix
        # for the given instance. The logic in InitForMoonrakerConfig also relies on the service file being found.
        # So if we need to find a config, we will find all of the service files and then try to find the config files that match.
        #
        # TODO - This could be made better by if we dont't find the service file, asking the use if they want to enter it.
        moonrakerConfigFilePaths = self.SearchForMoonrakerConfigsFromServiceFiles()
        if len(moonrakerConfigFilePaths) > 0:
            # If there is only one config file path found, just use it.
            if len(moonrakerConfigFilePaths) == 1:
                self.MOONRAKER_CONFIG = moonrakerConfigFilePaths[0]
                Info("Moonraker config found from a service file: "+self.MOONRAKER_CONFIG)
                return

            # Otherwise, if multiple files were found, ask the user to select one.
            Blank()
            Blank()
            Warn("Multiple moonraker instances found.")
            Warn("You can setup OctoEverywhere up for all of them, but you must run this install script for each each one individually.")
            Blank()
            # Print the config files found.
            count = 0
            for c in moonrakerConfigFilePaths:
                count += 1
                Info("  "+str(count)+") "+c)
            Blank()
            # Ask the user which number they want.
            responseInt = -1
            isFirstPrint = True
            while True:
                try:
                    if isFirstPrint:
                        isFirstPrint = False
                    else:
                        Warn("If you need help, contact us! https://octoeverywhere.com/support")
                    response = input("Enter the number for the config you would like to setup now (or enter m to manually enter a path): ")
                    response = response.lower().strip()
                    if response == "m":
                        break
                    # Parse the input and -1 it, so it aligns with the array length.
                    tempInt = int(response.lower().strip()) - 1
                    if tempInt >= 0 and tempInt < len(moonrakerConfigFilePaths):
                        responseInt = tempInt
                        break
                    Warn("Invalid number selection, try again.")
                except Exception as e:
                    Warn("Invalid input, try again. Error: "+str(e))

            # If we got a valid response, use it. Otherwise, go into the manual entry mode.
            if responseInt != -1:
                self.MOONRAKER_CONFIG = moonrakerConfigFilePaths[responseInt]
                return

        # No path was found, try to get one from the user.
        self.MOONRAKER_CONFIG = None
        while self.MOONRAKER_CONFIG is None:
            Blank()
            Blank()
            Warn("No moonraker config file found, please the full file path to the moonraker you wish to setup.")
            Warn("Ex: /home/pi/printer_data/config/moonraker.conf")
            self.MOONRAKER_CONFIG = input("Enter Path: ")
            if os.path.exists(self.MOONRAKER_CONFIG):
                return
            self.MOONRAKER_CONFIG = None
            # Failed, ask if they want to try again.
            if self.AskYesOrNoQuestion("No file was found at that path. Try again?") is False:
                Error("No moonraker config path was given.")
                sys.exit(1)


    # Returns the parent directory of the passed directory or file path.
    def GetParentDirectory(self, path):
        return os.path.abspath(os.path.join(path, os.pardir))


    # Recursively looks though the systemd folders for files matching the prefix given and ending in .service.
    # Returns an empty list if none are found, otherwise a list of matched files.
    def FindAllSystemdServiceFiles(self, serviceNamePrefix, path = None):
        if path is None:
            path = MoonrakerInstaller.SystemdServiceFilePath

        # Use sorted, so the results are in a nice user presentable order.
        fileAndDirList = sorted(os.listdir(path))
        result = []
        for fileOrDirName in fileAndDirList:
            fullFileOrDirPath = os.path.join(path, fileOrDirName)

            # Look through child folders.
            if os.path.isdir(fullFileOrDirPath):
                tempResult = self.FindAllSystemdServiceFiles(serviceNamePrefix, fullFileOrDirPath)
                if tempResult is not None and len(tempResult) > 0:
                    for t in tempResult:
                        result.append(t)
            # If it's a file, test if it's one of our matching files.
            # Ignore symlink files, since the .target.wants folders have sym links to the .service files.
            elif os.path.isfile(fullFileOrDirPath) and os.path.islink(fullFileOrDirPath) is False:
                fileNameLower = fileOrDirName.lower()
                if fileNameLower.startswith(serviceNamePrefix.lower()) and fileNameLower.endswith(".service"):
                    result.append(fullFileOrDirPath)
        return result


    # After we know we have a valid config file, setup the rest of our vars based on it.
    def InitForMoonrakerConfig(self):

        # Clean up the config string.
        self.MOONRAKER_CONFIG = self.MOONRAKER_CONFIG.strip()
        Info("Moonraker config set to: "+self.MOONRAKER_CONFIG)

        # As according to the notes at the top of this file, from the config file, we want to target the same service name structure as this moonraker.
        # To do this, using the full config path, we can find data folder for moonraker.
        # The path should be like this
        #   <some name>_data/config/moonraker.conf
        self.PrinterConfigFolder = self.GetParentDirectory(self.MOONRAKER_CONFIG)
        self.PrinterDataFolder = self.GetParentDirectory(self.PrinterConfigFolder)
        # Hack for RatOS! - Rat OS has two moonraker.conf files, one in ~/printer_data/config/moonraker.conf and one at ~/printer_data/config/RatOS/moonraker.conf
        # ~/printer_data/config/RatOS/moonraker.conf is the file with the actual moonraker config info in it. The other file is for users to overwrite some things.
        # It just to happens that our auto config logic will find the correct file (~/printer_data/config/RatOS/moonraker.conf) first.
        # The but the problem is the self.PrinterDataFolder needs to be set two parents up, so it's the actual printer_data folder.
        # Without this, the printer data folder path is ~/printer_data/config which fails to match any service files.
        #
        # TODO - To fix this correctly, when looking for the moonraker config to start with, we should follow any includes to find the file that actually has the [server] block.
        # TODO - Secondly, this logic should be made more robust, so that it doesn't always just do one parent up.
        #
        if self.PrinterDataFolder.lower().find("ratos"):
            Info("RatOs hack applied to the pritner data folder.")
            self.PrinterDataFolder = self.GetParentDirectory(self.PrinterDataFolder)
        Debug("Printer data folder: "+self.PrinterDataFolder)

        # Once we have the data folder, we can find the matching service file for moonraker, to figure out it's name.
        # If there is only one instance running on the device, the name will be moonraker.service.
        # If there are multiple instances, the name will be moonraker-<number or name>.service.
        moonrakerSystemdFilePaths = self.FindAllSystemdServiceFiles("moonraker")
        if len(moonrakerSystemdFilePaths) == 0:
            raise Exception("No moonraker systemd service file(s) found.")

        # Default to None, which means we didn't find it.
        # If there is only one instance, this will be "". If there are many, this will be "-<number or name>"
        serviceSuffixStr = None
        moonrakerServiceName = None

        # Append a / if there isn't one, to make sure we don't match partial paths.
        dataPathToSearchFor = self.PrinterDataFolder.lower()
        if dataPathToSearchFor.endswith('/') is False:
            dataPathToSearchFor += "/"

        # Look through all of the moonraker service files to find a line that matches our printer data path.
        # We read each service file and look for the env line, which looks like this: EnvironmentFile=/home/pi/printer_data/systemd/moonraker.env
        for filePath in moonrakerSystemdFilePaths:
            try:
                with open(filePath, "r", encoding="utf-8") as serviceFile:
                    lines = serviceFile.readlines()
                    for l in lines:
                        if dataPathToSearchFor in l.lower():
                            # We found it, use it's service name as our naming scheme.
                            fileName = filePath[filePath.rindex('/'):]
                            moonrakerServiceName = fileName
                            fileNameNoFileType = fileName.split('.')[0]
                            if '-' in fileNameNoFileType:
                                moonrakerServiceSuffix = fileNameNoFileType.split('-')
                                serviceSuffixStr = "-" + moonrakerServiceSuffix[1]
                            else:
                                serviceSuffixStr = ""
                            break
            except Exception:
                Warn("Failed to read service config file: "+filePath)

        # Ensure we found a matching service file.
        if serviceSuffixStr is None or moonrakerServiceName is None:
            Info("Printer Data Folder: "+self.PrinterDataFolder)
            for f in moonrakerSystemdFilePaths:
                Info("Moonraker Service Config: "+f)
            raise Exception("Failed to find a matching moonraker service file with matching printer data folder.")

        # This is the name of our service we create. If the port is the default port, use the default name.
        # Otherwise, add the port to keep services unique.
        self.ServiceName = "octoeverywhere"+serviceSuffixStr
        self.ServiceFilePath = os.path.join(MoonrakerInstaller.SystemdServiceFilePath, self.ServiceName+".service")

        # Since the moonraker config folder is unique to the moonraker instance, we will put our storage in it.
        # This also prevents the user from messing with it accidentally.
        self.LocalFileStoragePath = os.path.join(self.PrinterDataFolder, "octoeverywhere-store")

        # There's not a great way to find the log path from the config file, since the only place it's located is in the systemd file.
        self.PrinterLogFolder = None

        # First, we will see if we can find a named folder relative to this folder.
        self.PrinterLogFolder = os.path.join(self.PrinterDataFolder, "logs")
        if os.path.exists(self.PrinterLogFolder) is False:
            # Try an older path
            self.PrinterLogFolder = os.path.join(self.PrinterDataFolder, "klipper_logs")
            if os.path.exists(self.PrinterLogFolder) is False:
                # Failed, make a folder in the user's home.
                self.PrinterLogFolder = os.path.join(self.PrinterDataFolder, "octoeverywhere-logs"+serviceSuffixStr)
                # Create the folder and force the permissions so our service can write to it.
                self.EnsureDirExists(self.PrinterLogFolder, True)

        # Report
        Info(f'Configured. Moonraker Service: {moonrakerServiceName}, Service: {self.ServiceName}, Path: {self.ServiceFilePath}, LocalStorage: {self.LocalFileStoragePath}, Config Dir: {self.PrinterConfigFolder}, Logs: {self.PrinterLogFolder}')


    def RunShellCommand(self, cmd):
        status = subprocess.call(cmd, shell=True)
        if status != 0:
            raise Exception("Command "+cmd+" failed to execute. Code: "+str(status))


    def PrintServiceLogsToConsole(self):
        if self.ServiceName is None:
            Info("Can't print service logs, there's no service name.")
            return
        self.RunShellCommand("sudo journalctl -u "+self.ServiceName+" -n 20 --no-pager")


    # Re-creates the service file, stops, and restarts the service.
    def CreateAndRunService(self,):
        Header("Setting Up OctoEverywhere's System Service...")
        # We always re-write the service file, to make sure it's current.
        if os.path.exists(self.ServiceFilePath):
            Info("Service file already exists, recreating.")

        # Create the service file.

        # First, we create a json object that we use as arguments. Using a json object makes parsing and such more flexible.
        # We base64 encode the json string to prevent any arg passing issues with things like quotes, spaces, or other chars.
        argsJson = json.dumps({
            'KlipperConfigFolder': self.PrinterConfigFolder,
            'MoonrakerConfigFile': self.MOONRAKER_CONFIG,
            'KlipperLogFolder': self.PrinterLogFolder,
            'LocalFileStoragePath': self.LocalFileStoragePath,
            'ServiceName': self.ServiceName,
            'VirtualEnvPath': self.VirtualEnvPath,
            'RepoRootFolder': self.RepoRootFolder,
        })
        # We have to convert to bytes -> encode -> back to string.
        argsJsonBase64 = base64.urlsafe_b64encode(bytes(argsJson, "utf-8")).decode("utf-8")

        d = {
            'RepoRootFolder': self.RepoRootFolder,
            'UserName': self.UserName,
            'VirtualEnvPath': self.VirtualEnvPath,
            'ServiceBase64JsonArgs': argsJsonBase64
        }
        s = '''\
    # OctoEverywhere For Moonraker Service
    [Unit]
    Description=OctoEverywhere For Moonraker
    # Start after network and moonraker has started.
    After=network-online.target moonraker.service

    [Install]
    WantedBy=multi-user.target

    # Simple service, targeting the user that was used to install the service, simply running our moonraker py host script.
    [Service]
    Type=simple
    User={UserName}
    WorkingDirectory={RepoRootFolder}
    ExecStart={VirtualEnvPath}/bin/python3 -m moonraker_octoeverywhere "{ServiceBase64JsonArgs}"
    Restart=always
    # Since we will only restart on a fatal error, set the restart time to be a bit higher, so we don't spin and spam.
    RestartSec=10
    '''.format(**d)

        # Write to the file.
        Info("Creating service file "+self.ServiceFilePath+"...")
        with open(self.ServiceFilePath, "w", encoding="utf-8") as serviceFile:
            serviceFile.write(s)

        Info("Registering service...")
        self.RunShellCommand("systemctl enable "+self.ServiceName)
        self.RunShellCommand("systemctl daemon-reload")

        # Stop and start to restart any running services.
        Info("Starting service...")
        self.RunShellCommand("systemctl stop "+self.ServiceName)
        self.RunShellCommand("systemctl start "+self.ServiceName)

        Info("Service setup and start complete!")


    # Ensures a folder exists, and optionally, it has permissions set correctly.
    def EnsureDirExists(self, dirPath, setPermissionsToUser = False):
        # Ensure it exists.
        Header("Enuring path and permissions ["+dirPath+"]...")
        if os.path.exists(dirPath) is False:
            Info("Dir doesn't exist, creating...")
            os.mkdir(dirPath)
        else:
            Info("Dir already exists.")

        if setPermissionsToUser:
            Info("Setting owner permissions to the service user ["+self.UserName+"]...")
            uid = pwd.getpwnam(self.UserName).pw_uid
            gid = pwd.getpwnam(self.UserName).pw_gid
            # pylint: disable=no-member # Linux only
            os.chown(dirPath, uid, gid)

        Info("Directory setup successfully.")


    # Get's the printer id from an existing service config file, if it can be found.
    def GetPrinterIdFromServiceConfigFile(self) -> str or None:
        # This path and name must stay in sync with where the plugin will write the file.
        oeServiceConfigFilePath = os.path.join(self.PrinterConfigFolder, "octoeverywhere.conf")

        # Check if there is a file. If not, it means the service hasn't been run yet and this is a first time setup.
        if os.path.exists(oeServiceConfigFilePath) is False:
            return None

        # If the file exists, try to read it.
        # If this fails, let it throw, so the user knows something is wrong.
        Info("Found existing OctoEverywhere service config.")
        config = configparser.ConfigParser()
        config.read(oeServiceConfigFilePath)

        # Look for these sections, but don't throw if they aren't there. The service first creates the file and then
        # adds these, so it might be the case that the service just hasn't created them yet.
        section = "server"
        key = "printer_id"
        if config.has_section(section) is False:
            Info("Server section not found in OE config.")
            return None
        if key not in config[section].keys():
            Info("Printer id not found in OE config.")
            return None
        return config[section][key]


    # Used for first time setups of the service. Waits for a printer id and then helps the user add the printer to their account.
    def CheckIfPrinterIsConnectedToAccountAndSetupIfNot(self, existingPrinterId):
        # First, wait for the printer ID to show up.
        printerId = None
        startTimeSec = time.time()
        Info("Waiting for the service to produce a printer id...")
        while printerId is None:
            # Give the service time to start.
            time.sleep(0.1)
            # Try to get it again.
            printerId = self.GetPrinterIdFromServiceConfigFile()

            # If we failed, try to handle the case where the service might be having an error.
            if printerId is None:
                timeDelta = time.time() - startTimeSec
                if timeDelta > 10.0:
                    startTimeSec = time.time()
                    Warn("The service is taking a while to start, there might be something wrong.")
                    if self.AskYesOrNoQuestion("Do you want to keep waiting?"):
                        continue
                    # Handle the error and cleanup.
                    Blank()
                    Blank()
                    Error("We didn't get a response from the OctoEverywhere service when waiting for the printer id.")
                    Error("You can find service logs which might indicate the error in: "+self.PrinterLogFolder)
                    Blank()
                    Blank()
                    Error("Attempting to print the service logs:")
                    # Try to print the service logs to the console.
                    self.PrintServiceLogsToConsole()
                    raise Exception("Failed to wait for printer id")

        # Check if the printer is already connected to an account.
        # If so, report and we don't need to do the setup.
        (isConnectedToService, printerNameIfConnectedToAccount) = self.IsPrinterConnectedToAnAccount(printerId)
        if isConnectedToService and printerNameIfConnectedToAccount is not None:
            Header("This printer is securely connected to your OctoEverywhere account as '"+str(printerNameIfConnectedToAccount)+"'")
            return

        # The printer isn't connected. If this is not the first time setup, ask the user if they want to do it now.
        if existingPrinterId is not None:
            Blank()
            Warn("This printer isn't connected to an OctoEverywhere account.")
            if self.AskYesOrNoQuestion("Would you like to link it now?") is False:
                Blank()
                Header("You can connect this printer anytime, using this URL: ")
                Warn(self.GetAddPrinterUrl(printerId))
                return

        # Help the user setup the printer!
        Blank()
        Blank()
        Warn( "You're 10 seconds away from free and unlimited printer access from anywhere!")
        self.PrintShortCodeStyleOrFullUrl(printerId)
        Blank()
        Blank()

        Info("Waiting for the printer to be linked to your account...")
        isLinked = False
        notConnectedTimeSec = time.time()
        startTimeSec = time.time()
        while isLinked is False:
            # Query status.
            (isConnectedToService, printerNameIfConnectedToAccount) = self.IsPrinterConnectedToAnAccount(printerId)

            if printerNameIfConnectedToAccount is not None:
                # Connected!
                isLinked = True
                Blank()
                Header("Success! This printer is securely connected to your account as '"+str(printerNameIfConnectedToAccount)+"'")
                return

            # We expect the plugin to be connected to the service. If it's not, something might be wrong.
            if isConnectedToService is False:
                notConnectedDeltaSec = time.time() - notConnectedTimeSec
                Info("Waiting for the plugin to connect to our service...")
                if notConnectedDeltaSec > 10.0:
                    notConnectedTimeSec = time.time()
                    Warn("It looks like your plugin hasn't connected to the service yet, which it should have.")
                    if self.AskYesOrNoQuestion("Do you want to keep waiting?"):
                        continue
                    # Handle the error and cleanup.
                    Blank()
                    Blank()
                    Error("The plugin hasn't connected to our service yet. Something might be wrong.")
                    Error("You can find service logs which might indicate the error in: "+self.PrinterLogFolder)
                    Blank()
                    Blank()
                    Error("Attempting to print the service logs:")
                    # Try to print the service logs to the console.
                    self.PrintServiceLogsToConsole()
                    raise Exception("Failed to wait for printer to connect to service.")
            else:
                # The plugin is connected but no user account is connected yet.
                timeDeltaSec = time.time() - startTimeSec
                if timeDeltaSec > 60.0:
                    startTimeSec = time.time()
                    Warn("It doesn't look like this printer has been connected to your account yet.")
                    if self.AskYesOrNoQuestion("Do you want to keep waiting?"):
                        Blank()
                        Blank()
                        self.PrintShortCodeStyleOrFullUrl(printerId)
                        Blank()
                        continue

                    Blank()
                    Blank()
                    Blank()
                    Warn("You can use the following URL at anytime to link this printer to your account. Or run this install script again for help.")
                    Header(self.GetAddPrinterUrl(printerId))
                    Blank()
                    Blank()
                    return

            # Sleep before trying the API again.
            time.sleep(1.0)


    # Checks with the service to see if the printer is setup on a account.
    # Returns a tuple of two values
    #   1 - bool - Is the printer connected to the service
    #   2 - string - If the printer is setup on an account, the printer name.
    def IsPrinterConnectedToAnAccount(self, printerId):
        # Query the printer status.
        r = requests.post('https://octoeverywhere.com/api/printer/info', json={"Id": printerId}, timeout=20)

        # Any bad code reports as not connected.
        Debug("OE Printer Info API Result: "+str(r.status_code))
        if r.status_code != 200:
            return (False, None)

        # On success, try to parse the response and see if it's connected.
        jResult = r.json()
        Debug("OE Printer API Info; Name:"+jResult["Result"]["Name"] + " HasOwners:" +str(jResult["Result"]["HasOwners"]))
        printerName = None
        if jResult["Result"]["HasOwners"] is True:
            printerName = jResult["Result"]["Name"]
        return (True, printerName)


    def GetAddPrinterUrl(self, printerId):
        return "https://octoeverywhere.com/getstarted?printerid="+printerId


    def PrintShortCodeStyleOrFullUrl(self, printerId):
        # To make the setup easier, we will present the user with a short code if we can get one.
        # If not, fallback to the full URL.
        try:
            # Try to get a short code. We do a quick timeout so if this fails, we just present the user the longer URL.
            # Any failures, like rate limiting, server errors, whatever, and we just use the long URL.
            r = requests.post('https://octoeverywhere.com/api/shortcode/create', json={"Type": 1, "PrinterId": printerId}, timeout=2.0)
            if r.status_code == 200:
                jsonResponse = r.json()
                if "Result" in jsonResponse and "Code" in jsonResponse["Result"]:
                    codeStr = jsonResponse["Result"]["Code"]
                    if len(codeStr) > 0:
                        Warn("To securely link this printer to your OctoEverywhere account, go to the following website and use the code.")
                        Blank()
                        Header("Website: https://octoeverywhere.com/code")
                        Header("Code:    "+codeStr)
                        return
        except Exception:
            pass

        Warn("Use this URL to securely link this printer to your OctoEverywhere account:")
        Header(self.GetAddPrinterUrl(printerId))

# Run the installer
installer = MoonrakerInstaller()
installer.Run()
