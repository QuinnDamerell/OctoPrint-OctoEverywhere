import os
import stat

from linux_host.version import Version

from .Context import Context
from .Context import OsTypes
from .Logging import Logger
from .Configure import Configure
from .Paths import Paths
from .Service import Service
from .Util import Util
from .Ffmpeg import Ffmpeg
from .ZStandard import ZStandard

#
# This class is responsible for doing updates for all local, companions, and bambu connect plugins on this local system.
# This update logic is mostly for companion plugins, since normal plugins will be updated via the moonraker update system.
# But it does work for both.
#
# However, this is quite easy, for a few reasons.
#    1) All plugins and companions will use the same ~/octoeverywhere/ git repo.
#          All Sonic Pad based installs will use /usr/shared/octoeverywhere
#          All K1 based installs will use /usr/data/octoeverywhere
#    2) We always run the ./install.sh script before launching the PY installer, which handles updating system packages and PIP packages.
#
# So all we really need to do is find and restart all of the services.
#
class Updater:

    def DoUpdate(self, context:Context):
        Logger.Header("Starting Update Logic")

        # Since this takes a while, kick it off now. The pip install can take upwards of 30 seconds.
        ZStandard.TryToInstallZStandardAsync(context)

        # Enumerate all service file to find any local plugins, Sonic Pad plugins, companion service files, and bambu service files, since all service files contain this name.
        # Note GetServiceFileFolderPath will return dynamically based on the OsType detected.
        # Use sorted, so the results are in a nice user presentable order.
        foundOeServices = []
        fileAndDirList = sorted(os.listdir(Paths.GetServiceFileFolderPath(context)))
        for fileOrDirName in fileAndDirList:
            Logger.Debug(f"Searching for OE services to update, found: {fileOrDirName}")
            fileOrDirNameLower = fileOrDirName.lower()
            if Configure.c_ServiceCommonName in fileOrDirNameLower:
                foundOeServices.append(fileOrDirName)

        if len(foundOeServices) == 0:
            Logger.Warn("No local, companion, or Bambu Connect plugins were found on this device.")
            raise Exception("No local, companion, or Bambu Connect plugins were found on this device.")

        # On any system, try to install or update ffmpeg.
        Ffmpeg.TryToInstallFfmpeg(context)

        # Before we restart the plugins, wait for the zstandard install to be done.
        # Give the updater extra time to work, since it's much shorter
        ZStandard.WaitForInstallToComplete(timeoutSec==20.0)

        Logger.Info("We found the following plugins to update:")
        for s in foundOeServices:
            Logger.Info(f"  {s}")

        Logger.Info("Restarting services...")
        for s in foundOeServices:
            if context.OsType == OsTypes.SonicPad:
                # We need to build the fill name path
                serviceFilePath = os.path.join(Paths.CrealityOsServiceFilePath, s)
                Logger.Debug(f"Full service path: {serviceFilePath}")
                Service.RestartSonicPadService(serviceFilePath, False)
            elif context.OsType == OsTypes.K1:
                # We need to build the fill name path
                serviceFilePath = os.path.join(Paths.CrealityOsServiceFilePath, s)
                Logger.Debug(f"Full service path: {serviceFilePath}")
                Service.RestartK1Service(serviceFilePath, False)
            elif context.OsType == OsTypes.Debian:
                Service.RestartDebianService(s, False)
            else:
                raise Exception("This OS type doesn't support updating at this time.")

        pluginVersionStr = "Unknown"
        try:
            pluginVersionStr = Version.GetPluginVersion(context.RepoRootFolder)
        except Exception as e:
            Logger.Warn("Failed to parse setup.py for plugin version. "+str(e))

        # Try to update the crontab job if needed
        self.EnsureCronUpdateJob(context.RepoRootFolder)

        Logger.Blank()
        Logger.Header("-------------------------------------------")
        Logger.Info(  "    OctoEverywhere Update Successful")
        Logger.Info( f"          New Version: {pluginVersionStr}")
        Logger.Purple("            Happy Printing!")
        Logger.Header("-------------------------------------------")
        Logger.Blank()


    # This function ensures there's an update script placed in the user's root directory, so it's easy for the user to find
    # the script for updating.
    def PlaceUpdateScriptInRoot(self, context:Context) -> bool:
        try:
            # Create the script file with any optional args we might need.

            # For the k1, we need to use the prefix sh for the script run
            updateCmdPrefix = ""
            if context.OsType == OsTypes.K1:
                updateCmdPrefix = "sh "

            s = f'''\
#!/bin/bash

#
# Run this script to update all OctoEverywhere plugins on this device!
#
# This works for all plugin types, such as local Klipper, Creality OS, Companion, and Bambu Connect.
#
# If you need help, feel free to contact us at support@octoeverywhere.com
#

# The update and install scripts need to be ran from the repo root.
# So just cd and execute our update script! Easy peasy!
startingDir=$(pwd)
cd {context.RepoRootFolder}
{updateCmdPrefix}./update.sh
cd $startingDir
            '''
            # Target the user home unless this is a Creality install.
            # For Sonic Pad and K1 the user home will be set differently, but we want to put this script where the user logs in, aka root.
            targetPath = context.UserHomePath
            if context.IsCrealityOs():
                targetPath="/root"

            # Create the file.
            updateFilePath = os.path.join(targetPath, "update-octoeverywhere.sh")
            with open(updateFilePath, 'w', encoding="utf-8") as f:
                f.write(s)

            # Make sure to make it executable
            st = os.stat(updateFilePath)
            os.chmod(updateFilePath, st.st_mode | stat.S_IEXEC)

            # Ensure the user who launched the installer script has permissions to run it.
            Util.SetFileOwnerRecursive(updateFilePath, context.UserName)

            return True
        except Exception as e:
            Logger.Error("Failed to write updater script to user home. "+str(e))
            return False


    # We need to be running as sudo to make a sudo cron job.
    # The cron job has to be sudo, so it can update system packages and restart the service.
    def EnsureCronUpdateJob(self, oeRepoRoot:str):
        pass
        # This is disabled for now, due to problems running the update script as the root user.
        # try:
        #     Logger.Debug("Ensuring cron job is setup.")

        #     # First, get any current crontab jobs.
        #     # Note it's important to use sudo, because we need to be in the sudo crontab to restart our service!
        #     (returnCode, currentCronJobs, errorOut) = Util.RunShellCommand("sudo crontab -l", False)
        #     # Check for failures.
        #     if returnCode != 0:
        #         # If there are no cron jobs, this will be the output.
        #         if "no crontab for" not in errorOut.lower():
        #             raise Exception("Failed to get current cron jobs. "+errorOut)

        #     # Go through the current cron jobs and try to find our cron job.
        #     # If we find ours, filter it out, since we will re-add an updated one.
        #     currentCronJobLines = currentCronJobs.split("\n")
        #     newCronJobLines = []
        #     for job in currentCronJobLines:
        #         # Skip blank lines
        #         if len(job) == 0:
        #             continue
        #         jobLower = job.lower()
        #         if oeRepoRoot.lower() in jobLower:
        #             Logger.Debug(f"Found our current crontab job: {job}")
        #         else:
        #             Logger.Debug(f"Found other crontab line: {job}")
        #             newCronJobLines.append(job)

        #     # We either didn't have a job or removed it, so add our new job.
        #     # This is our current update time "At 23:59 on Sunday."
        #     # https://crontab.guru/#59_23_*_*_7
        #     # We need to cd into the repo root, since that's where the update script is expected to be ran.
        #     # We send logs out to a file, so we can capture them is needed.
        #     # updateScriptPath = os.path.join(oeRepoRoot, "update.sh")
        #     # This is disabled right now due to issues running as root, but needing to be in the user's context for the install.sh script.
        #     # The problem is we need basically "pi user with the sudo command" but the cron tab runs as the sudo user. In this case, things like the USER and HOME env
        #     # vars aren't defined.
        #     #newCronJobLines.append(f"59 23 * * 7 cd {oeRepoRoot} && {updateScriptPath} 1> /var/log/oe-cron.log 2> /var/log/oe-cron-error.log")

        #     # New output.
        #     newInput = ""
        #     for job in newCronJobLines:
        #         newInput += job + "\n"
        #     Logger.Debug(f"New crontab input: {newInput}")

        #     # Set the new cron jobs.
        #     # Note it's important to use sudo, because we need to be in the sudo crontab to restart our service!
        #     result = subprocess.run("sudo crontab -", check=False, shell=True, capture_output=True, text=True, input=newInput)
        #     if result.returncode != 0:
        #         raise Exception("Failed to set new cron jobs. "+result.stderr)

        #     Logger.Debug("Cron job setup successfully.")
        # except Exception as e:
        #     Logger.Warn("Failed to setup cronjob for updates, skipping. "+str(e))
