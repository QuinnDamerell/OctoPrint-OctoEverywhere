import os
import json
import base64

from .Util import Util
from .Logging import Logger
from .Context import Context


# Responsible for creating, running, and ensuring the service is installed and running.
class Service:

    def Install(self, context:Context):
        Logger.Header("Setting Up OctoEverywhere's System Service...")

        # We always re-write the service file, to make sure it's current.
        if os.path.exists(context.ServiceFilePath):
            Logger.Info("Service file already exists, recreating.")

        # Create the service file.

        # First, we create a json object that we use as arguments. Using a json object makes parsing and such more flexible.
        # We base64 encode the json string to prevent any arg passing issues with things like quotes, spaces, or other chars.
        # Note some of these vars might be null, in the Observer Setup case
        argsJson = json.dumps({
            'KlipperConfigFolder': context.PrinterDataConfigFolder,
            'MoonrakerConfigFile': context.MoonrakerConfigFilePath,
            'KlipperLogFolder': context.PrinterDataLogsFolder,
            'LocalFileStoragePath': context.LocalFileStorageFolder,
            'ServiceName': context.ServiceName,
            'VirtualEnvPath': context.VirtualEnvPath,
            'RepoRootFolder': context.RepoRootFolder,
            'IsObserver' : context.IsObserverSetup,
            'ObserverConfigFilePath' : context.ObserverConfigFilePath,
            'ObserverInstanceIdStr' : context.ObserverInstanceId
        })
        # We have to convert to bytes -> encode -> back to string.
        argsJsonBase64 = base64.urlsafe_b64encode(bytes(argsJson, "utf-8")).decode("utf-8")

        d = {
            'RepoRootFolder': context.RepoRootFolder,
            'UserName': context.UserName,
            'VirtualEnvPath': context.VirtualEnvPath,
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
    # Since we will only restart on a fatal Logger.Error, set the restart time to be a bit higher, so we don't spin and spam.
    RestartSec=10
    '''.format(**d)

        if context.SkipSudoActions:
            Logger.Warn("Skipping service file creation, registration, and starting due to skip sudo actions flag.")
            return

        Logger.Debug("Service config file contents to write: "+s)
        Logger.Info("Creating service file "+context.ServiceFilePath+"...")
        with open(context.ServiceFilePath, "w", encoding="utf-8") as serviceFile:
            serviceFile.write(s)

        Logger.Info("Registering service...")
        Util.RunShellCommand("systemctl enable "+context.ServiceName)
        Util.RunShellCommand("systemctl daemon-reload")

        # Stop and start to restart any running services.
        Logger.Info("Starting service...")
        Util.RunShellCommand("systemctl stop "+context.ServiceName)
        Util.RunShellCommand("systemctl start "+context.ServiceName)

        Logger.Info("Service setup and start complete!")
