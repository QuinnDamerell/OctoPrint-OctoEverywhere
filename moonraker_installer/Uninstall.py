import os

from .Context import Context
from .Context import OsTypes
from .Logging import Logger
from .Configure import Configure
from .Paths import Paths
from .Util import Util

class Uninstall:

    def DoUninstall(self, context:Context):
        Logger.Header("Starting OctoEverywhere uninstall")

        # Since all service names must use the same identifier in them, we can find any services using the same search.
        foundOeServices = []
        fileAndDirList = sorted(os.listdir(Paths.GetServiceFileFolderPath(context)))
        for fileOrDirName in fileAndDirList:
            Logger.Debug(f" Searching for OE services to remove, found: {fileOrDirName}")
            if Configure.c_ServiceCommonName in fileOrDirName.lower():
                foundOeServices.append(fileOrDirName)

        if len(foundOeServices) == 0:
            Logger.Warn("No local plugins or companions were found to remove.")
            return

        # TODO - We need to cleanup more, but for now, just make sure any services are shutdown.
        Logger.Info("Stopping services...")
        for serviceFileName in foundOeServices:
            if context.OsType == OsTypes.SonicPad:
                # We need to build the fill name path
                serviceFilePath = os.path.join(Paths.CrealityOsServiceFilePath, serviceFileName)
                Logger.Debug(f"Full service path: {serviceFilePath}")
                Logger.Info(f"Stopping and deleting {serviceFileName}...")
                Util.RunShellCommand(f"{serviceFilePath} stop", False)
                Util.RunShellCommand(f"{serviceFilePath} disable", False)
                os.remove(serviceFilePath)
            elif context.OsType == OsTypes.K1:
                # We need to build the fill name path
                serviceFilePath = os.path.join(Paths.CrealityOsServiceFilePath, serviceFileName)
                Logger.Debug(f"Full service path: {serviceFilePath}")
                Logger.Info(f"Stopping and deleting {serviceFileName}...")
                Util.RunShellCommand(f"{serviceFilePath} stop", False)
                Util.RunShellCommand("ps -ef | grep 'moonraker_octoeverywhere' | grep -v grep | awk '{print $1}' | xargs -r kill -9", False)
                os.remove(serviceFilePath)
            elif context.OsType == OsTypes.Debian:
                Logger.Info(f"Stopping and deleting {serviceFileName}...")
                Util.RunShellCommand("systemctl stop "+serviceFileName, False)
                Util.RunShellCommand("systemctl disable "+serviceFileName, False)
            else:
                raise Exception("This OS type doesn't support uninstalling at this time.")

        Logger.Blank()
        Logger.Blank()
        Logger.Header("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        Logger.Info(  "          OctoEverywhere Uninstall Complete            ")
        Logger.Info(  "     We will miss you, please come back anytime!       ")
        Logger.Header("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        Logger.Blank()
        Logger.Blank()
