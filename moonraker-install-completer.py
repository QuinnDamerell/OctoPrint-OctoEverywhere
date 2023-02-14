
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
# This script is responsible for the remainder of the moonraker setup process, after the bash script bootstrapped things.
# This script should only be launched by the bash script, since it requires params and to be ran in the octoeverywhere virt env.
#

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
        # This one is optional.
        self.MOONRAKER_CONFIG = None

        # These var are all setup in InitForMoonrakerConfig(), once we know the config we are targeting.
        self.ServiceName = None
        self.ServiceFilePath = None
        self.LocalFileStoragePath =  None
        self.KlipperConfigFolder = None
        self.KlipperLogFolder = None


    def Run(self):
        try:
            # First, ensure we are launched as root.
            # pylint: disable=no-member # Linux only
            if os.geteuid() != 0:
                raise Exception("Script not ran as root.")

            # Parse the required command line args
            self.ParseArgs()
            Debug("Args parsed")

            # Make sure we have a moonraker config, or the user supplied one.
            self.EnsureMoonrakerConfig()
            self.MOONRAKER_CONFIG = self.MOONRAKER_CONFIG.strip()
            Info("Moonraker config set to: "+self.MOONRAKER_CONFIG)

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
            Blank()
            Error( "               Remember This Is A Beta Release!               ")
            Error( " Please Send Issues Or Feedback To support@octoeverywhere.com ")
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


    # Ensures we have a moonraker config to target.
    # If not, it tries to find one or gets it from the user.
    def EnsureMoonrakerConfig(self):
        # If we already have one, validate it.
        if self.MOONRAKER_CONFIG is not None:
            if os.path.exists(self.MOONRAKER_CONFIG):
                Info("Moonraker config passed to setup. "+self.MOONRAKER_CONFIG)
                return
            else:
                Warn("Moonraker config passed to setup, but the file wasn't found. "+self.MOONRAKER_CONFIG)

        # Look for a config, try the default locations.
        self.MOONRAKER_CONFIG = os.path.join(self.UserHomePath, "printer_data/config/moonraker.conf")
        Debug("Testing path "+self.MOONRAKER_CONFIG)
        if os.path.exists(self.MOONRAKER_CONFIG) is False:
            Debug("Testing path "+self.MOONRAKER_CONFIG)
            self.MOONRAKER_CONFIG = os.path.join(self.UserHomePath, "klipper_config/moonraker.conf")

        # If we got a path, ask if it's the one they want to use.
        if os.path.exists(self.MOONRAKER_CONFIG):
            Blank()
            Blank()
            Warn("Moonraker config detected as ["+self.MOONRAKER_CONFIG+"]")
            if self.AskYesOrNoQuestion("Do you want to use this config?"):
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


    # After we know we have a valid config file, setup the rest of our vars based on it.
    def InitForMoonrakerConfig(self):
        # To support multiple configs, we base some of our instance names off the moonraker port number defined in the config.
        config = configparser.ConfigParser()
        config.read(self.MOONRAKER_CONFIG)
        moonrakerPort = config['server']['port']
        if moonrakerPort is None:
            raise Exception("Moonraker port not found in config file.")

        # If the port is default, use no suffix.
        # Otherwise, suffix things with the port.
        portSuffixStr = ""
        if int(moonrakerPort) != 7125:
            portSuffixStr = "-"+str(moonrakerPort)

        # This is the name of our service we create. If the port is the default port, use the default name.
        # Otherwise, add the port to keep services unique.
        self.ServiceName = "octoeverywhere"+portSuffixStr
        self.ServiceFilePath = os.path.join(MoonrakerInstaller.SystemdServiceFilePath, self.ServiceName+".service")

        # According to the mainsail developers, the moonraker folder layout is like:
        #   <name>_data
        #       - config
        #           - Config files
        #       - logs
        #       - ...
        # But, this layout is only for newish installs. There was an older layout from the past.
        # Also, this folder structure is bound to one instance of moonraker, so everything in here is unique per instance.

        # Find the base config folder.
        self.KlipperConfigFolder = os.path.abspath(os.path.join(self.MOONRAKER_CONFIG, os.pardir))

        # Find the root folder for this moonraker instance.
        klipperBaseFolder = os.path.abspath(os.path.join(self.KlipperConfigFolder, os.pardir))

        # Since the moonraker config folder is unique to the moonraker instance, we will put our storage in it.
        # This also prevents the user from messing with it accidentally.
        self.LocalFileStoragePath = os.path.join(klipperBaseFolder, "octoeverywhere-store")

        # There's not a great way to find the log path from the config file, since the only place it's located is in the systemd file.
        self.KlipperLogFolder = None

        # First, we will see if we can find a named folder relative to this folder.
        self.KlipperLogFolder = os.path.join(klipperBaseFolder, "logs")
        if os.path.exists(self.KlipperLogFolder) is False:
            # Try an older path
            self.KlipperLogFolder = os.path.join(klipperBaseFolder, "klipper_logs")
            if os.path.exists(self.KlipperLogFolder) is False:
                # Failed, make a folder in the user's home.
                self.KlipperLogFolder = os.path.join(klipperBaseFolder, "octoeverywhere-logs"+portSuffixStr)
                # Create the folder and force the permissions so our service can write to it.
                self.EnsureDirExists(self.KlipperLogFolder, True)

        # Report
        Info(f'Configured. Port: {str(moonrakerPort)}, Service: {self.ServiceName}, Path: {self.ServiceFilePath}, LocalStorage: {self.LocalFileStoragePath}, Config Dir: {self.KlipperConfigFolder}, Logs: {self.KlipperLogFolder}')


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
            'KlipperConfigFolder': self.KlipperConfigFolder,
            'MoonrakerConfigFile': self.MOONRAKER_CONFIG,
            'KlipperLogFolder': self.KlipperLogFolder,
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
        oeServiceConfigFilePath = os.path.join(self.KlipperConfigFolder, "octoeverywhere.conf")

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
                    Error("You can find service logs which might indicate the error in: "+self.KlipperLogFolder)
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
                    Error("You can find service logs which might indicate the error in: "+self.KlipperLogFolder)
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
