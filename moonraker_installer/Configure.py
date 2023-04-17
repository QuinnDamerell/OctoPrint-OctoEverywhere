import os

from .Util import Util
from .Logging import Logger
from .Context import Context

# The goal of this class is the take the context object from the Discovery Gen2 phase to the Phase 3.
class Configure:

    def Run(self, context:Context):

        Logger.Header("Starting configuration...")

        # For now we assume the folder structure is the standard Klipper folder config,
        # thus the full moonraker config path will be .../something_data/config/moonraker.conf
        # Based on that, we will define the config folder and the printer data root folder.
        context.PrinterDataConfigFolder = Util.GetParentDirectory(context.MoonrakerConfigFilePath)
        context.PrinterDataFolder = Util.GetParentDirectory(context.PrinterDataConfigFolder)
        Logger.Debug("Printer data folder: "+context.PrinterDataFolder)

        # Now we need to figure out the instance suffix we need to use.
        # To keep with Kiauh style installs, each moonraker instances will be named moonraker-<number or name>.service.
        # If there is only one moonraker instance, the name is moonraker.service.
        # Default to empty string, which means there's no suffix and only one instance.
        serviceSuffixStr = ""
        serviceFileNameNoExtension = context.MoonrakerServiceFileName.split('.')[0]
        if '-' in serviceFileNameNoExtension:
            moonrakerServiceSuffix = serviceFileNameNoExtension.split('-')
            serviceSuffixStr = "-" + moonrakerServiceSuffix[1]

        # This is the name of our service we create. If the port is the default port, use the default name.
        # Otherwise, add the port to keep services unique.
        context.ServiceName = "octoeverywhere"+serviceSuffixStr
        context.ServiceFilePath = os.path.join(Util.SystemdServiceFilePath, context.ServiceName+".service")

        # Since the moonraker config folder is unique to the moonraker instance, we will put our storage in it.
        # This also prevents the user from messing with it accidentally.
        context.LocalFileStorageFolder = os.path.join(context.PrinterDataFolder, "octoeverywhere-store")

        # Ensure the storage folder exists and is owned by the correct user.
        Util.EnsureDirExists(context.LocalFileStorageFolder, context, True)

        # There's not a great way to find the log path from the config file, since the only place it's located is in the systemd file.
        context.PrinterDataLogsFolder = None

        # First, we will see if we can find a named folder relative to this folder.
        context.PrinterDataLogsFolder = os.path.join(context.PrinterDataFolder, "logs")
        if os.path.exists(context.PrinterDataLogsFolder) is False:
            # Try an older path
            context.PrinterDataLogsFolder = os.path.join(context.PrinterDataFolder, "klipper_logs")
            if os.path.exists(context.PrinterDataLogsFolder) is False:
                # Failed, make a folder in the printer data root.
                context.PrinterDataLogsFolder = os.path.join(context.PrinterDataFolder, "octoeverywhere-logs")
                # Create the folder and force the permissions so our service can write to it.
                Util.EnsureDirExists(context.PrinterDataLogsFolder, context, True)

        # Report
        Logger.Info(f'Configured. Service: {context.ServiceName}, Path: {context.ServiceFilePath}, LocalStorage: {context.LocalFileStorageFolder}, Config Dir: {context.PrinterDataConfigFolder}, Logs: {context.PrinterDataLogsFolder}')
