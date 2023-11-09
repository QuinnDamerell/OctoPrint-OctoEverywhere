import os
import json
import base64

from .Util import Util
from .Logging import Logger
from .Context import Context
from .Context import OsTypes


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

        # Base on the OS type, install the service differently
        if context.OsType == OsTypes.Debian:
            self._InstallDebian(context, argsJsonBase64)
        elif context.OsType == OsTypes.SonicPad:
            self._InstallSonicPad(context, argsJsonBase64)
        else:
            raise Exception("Service install is not supported for this OS type yet. Contact support!")


    # Install for debian setups
    def _InstallDebian(self, context:Context, argsJsonBase64):
        s = f'''\
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
    User={context.UserName}
    WorkingDirectory={context.RepoRootFolder}
    ExecStart={context.VirtualEnvPath}/bin/python3 -m moonraker_octoeverywhere "{argsJsonBase64}"
    Restart=always
    # Since we will only restart on a fatal Logger.Error, set the restart time to be a bit higher, so we don't spin and spam.
    RestartSec=10
'''
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


    # Install for sonic pad setups.
    def _InstallSonicPad(self, context:Context, argsJsonBase64):
        # First, write the service file
        # Notes:
        #   Set start to be 66, so we start after Moonraker.
        #   OOM_ADJ=-17 prevents us from being killed in an OOM
        startNumberStr = "66"
        s = f'''\
#!/bin/sh /etc/rc.common
# Copyright (C) 2006-2011 OpenWrt.org

START={startNumberStr}
STOP=1
DEPEND=moonraker_service
USE_PROCD=1
OOM_ADJ=-17

start_service() {{
    procd_open_instance
    procd_set_param env HOME=/root
    procd_set_param env PYTHONPATH={context.RepoRootFolder}
    procd_set_param oom_adj $OOM_ADJ
    procd_set_param command {context.VirtualEnvPath}/bin/python3 -m moonraker_octoeverywhere "{argsJsonBase64}"
    procd_close_instance
}}
'''
        if context.SkipSudoActions:
            Logger.Warn("Skipping service file creation, registration, and starting due to skip sudo actions flag.")
            return

        Logger.Debug("Service config file contents to write: "+s)
        Logger.Info("Creating service file "+context.ServiceFilePath+"...")
        with open(context.ServiceFilePath, "w", encoding="utf-8") as serviceFile:
            serviceFile.write(s)

        # Make the script executable.
        Logger.Info("Making the service executable...")
        Util.RunShellCommand(f"chmod +x {context.ServiceFilePath}")

        Logger.Info("Starting the service...")
        # These some times fail depending on the state of the service, which is fine.
        Util.RunShellCommand(f"{context.ServiceFilePath} stop", False)
        Util.RunShellCommand(f"{context.ServiceFilePath} reload", False)
        Util.RunShellCommand(f"{context.ServiceFilePath} enable" , False)
        Util.RunShellCommand(f"{context.ServiceFilePath} start")

        Logger.Info("Service setup and start complete!")
