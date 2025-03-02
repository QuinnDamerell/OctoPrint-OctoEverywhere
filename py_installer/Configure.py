import os

from .Util import Util
from .Paths import Paths
from .Logging import Logger
from .Context import Context
from .Context import OsTypes
from .NetworkConnectors.BambuConnector import BambuConnector
from .NetworkConnectors.ElegooConnector import ElegooConnector
from .NetworkConnectors.MoonrakerConnector import MoonrakerConnector

# The goal of this class is the take the context object from the Discovery Gen2 phase to the Phase 3.
class Configure:

    # This is the common service prefix (or word used in the file name) we use for all of our service file names.
    # This MUST be used for all instances running on this device, both local plugins and companions.
    # This also MUST NOT CHANGE, as it's used by the Updater logic to find all of the locally running services.
    c_ServiceCommonName = "octoeverywhere"

    def Run(self, context:Context):

        Logger.Header("Starting configuration...")

        # Figure the service suffix.
        # All services start with octoeverywhere (c_ServiceCommonName) but they have different suffixes so they don't collide if there
        # are multiple instances or types install one a signal device.
        serviceSuffixStr = ""
        if context.IsCompanionBambuOrElegoo():
            # For companions, elegoo, or bambu, we use the companion id, with a unique prefix to separate it from any possible local installs
            # Special case, for the primary instance id, we don't add the number suffix, so it easier to use.
            instanceIdSuffix = "" if context.IsPrimaryCompanionBambuOrElegoo() else f"-{context.CompanionInstanceId}"
            pluginTypeStr = "companion"
            if context.IsBambuSetup:
                pluginTypeStr = "bambu"
            elif context.IsElegooSetup:
                pluginTypeStr = "elegoo"
            serviceSuffixStr = f"-{pluginTypeStr}{instanceIdSuffix}"
        elif context.OsType == OsTypes.SonicPad:
            # For Sonic Pad, we know the format of the service file is a bit different.
            # For the SonicIt is moonraker_service or moonraker_service.<number>
            if "." in context.MoonrakerServiceFileName:
                serviceSuffixStr = context.MoonrakerServiceFileName.split(".")[1]
        elif context.OsType == OsTypes.K1 or context.OsType == OsTypes.K2:
            # For the k1 and k2, there's only every one moonraker instance, so this isn't needed.
            pass
        else:
            # Now we need to figure out the instance suffix we need to use.
            # To keep with Kiauh style installs, each moonraker instances will be named moonraker-<number or name>.service.
            # If there is only one moonraker instance, the name is moonraker.service.
            # Default to empty string, which means there's no suffix and only one instance.
            serviceFileNameNoExtension = context.MoonrakerServiceFileName.split('.')[0]
            if '-' in serviceFileNameNoExtension:
                moonrakerServiceSuffix = serviceFileNameNoExtension.split('-')
                serviceSuffixStr = "-" + moonrakerServiceSuffix[1]
        Logger.Debug(f"Moonraker Service File Name: {context.MoonrakerServiceFileName}, Suffix: '{serviceSuffixStr}'")

        if context.IsCompanionBambuOrElegoo():
            # For companion, elegoo, or bambu setups, there is no local moonraker config files, so things are setup differently.
            # The plugin data folder, which is normally the root printer data folder for that instance, becomes our per instance companion folder.
            context.RootFolder = context.CompanionDataRoot
            # The config folder is where our config lives, which we put in the main data root.
            context.ConfigFolder = context.RootFolder
            # Make a logs folder, so it's found bellow
            Util.EnsureDirExists(os.path.join(context.RootFolder, "logs"), context, True)
        elif context.OsType == OsTypes.SonicPad:
            # ONLY FOR THE SONIC PAD, we know the folder setup is different.
            # The user data folder will have /mnt/UDISK/printer_config<number> where the config files are and /mnt/UDISK/printer_logs<number> for logs.
            # Use the normal folder for the config files.
            context.ConfigFolder = Util.GetParentDirectory(context.MoonrakerConfigFilePath)

            # There really is no printer data folder, so make one that's unique per instance.
            # So based on the config folder, go to the root of it, and them make the folder "octoeverywhere_data"
            context.RootFolder = os.path.join(Util.GetParentDirectory(context.ConfigFolder), f"octoeverywhere_data{serviceSuffixStr}")
            Util.EnsureDirExists(context.RootFolder, context, True)
        else:
            # For now we assume the folder structure is the standard Klipper folder config,
            # thus the full moonraker config path will be .../something_data/config/moonraker.conf
            # Based on that, we will define the config folder and the printer data root folder.
            # Note that the K1 and K2 uses this standard folder layout as well.
            context.ConfigFolder = Util.GetParentDirectory(context.MoonrakerConfigFilePath)
            context.RootFolder = Util.GetParentDirectory(context.ConfigFolder)
            Logger.Debug("Printer data folder: "+context.RootFolder)


        # This is the name of our service we create. If the port is the default port, use the default name.
        # Otherwise, add the port to keep services unique.
        if context.OsType == OsTypes.SonicPad or context.OsType == OsTypes.K2:
            # For Sonic Pad and K2, since the service is setup differently, follow the conventions of it.
            # Both the service name and the service file name must match.
            # The format is <name>_service<number>
            # NOTE! For the Update class to work, the name must start with Configure.c_ServiceCommonNamePrefix
            context.ServiceName = Configure.c_ServiceCommonName + "_service"
            if len(serviceSuffixStr) != 0:
                context.ServiceName= context.ServiceName + "." + serviceSuffixStr
            context.ServiceFilePath = os.path.join(Paths.CrealityOsServiceFilePath, context.ServiceName)
        elif context.OsType == OsTypes.K1:
            # For the k1, there's only ever one moonraker and we know the exact service naming convention.
            # Note we use 66 to ensure we start after moonraker.
            # This is page for details on the file name: https://docs.oracle.com/cd/E36784_01/html/E36882/init.d-4.html
            # Note the 'S66' string is looked for in the plugin's EnsureUpdateManagerFilesSetup function. So it must not change!
            context.ServiceName = f"S66{Configure.c_ServiceCommonName}_service"
            context.ServiceFilePath = os.path.join(Paths.CrealityOsServiceFilePath, context.ServiceName)
        else:
            # For normal setups, use the convention that Klipper users
            # NOTE! For the Update class to work, the name must start with Configure.c_ServiceCommonNamePrefix
            context.ServiceName = Configure.c_ServiceCommonName + serviceSuffixStr
            context.ServiceFilePath = os.path.join(Paths.SystemdServiceFilePath, context.ServiceName+".service")

        # Since the moonraker config folder is unique to the moonraker instance, we will put our storage in it.
        # This also prevents the user from messing with it accidentally.
        context.LocalFileStorageFolder = os.path.join(context.RootFolder, "octoeverywhere-store")

        # Ensure the storage folder exists and is owned by the correct user.
        Util.EnsureDirExists(context.LocalFileStorageFolder, context, True)

        # There's not a great way to find the log path from the config file, since the only place it's located is in the systemd file.
        context.LogsFolder = None

        # First, we will see if we can find a named folder relative to this folder.
        # This is the folder created for companion and bambu setups, so it should always exist in those cases.
        context.LogsFolder = os.path.join(context.RootFolder, "logs")
        if os.path.exists(context.LogsFolder) is False:
            # Try an older path
            context.LogsFolder = os.path.join(context.RootFolder, "klipper_logs")
            if os.path.exists(context.LogsFolder) is False:
                # Try the path Creality OS uses, something like /mnt/UDISK/printer_logs<number>
                context.LogsFolder = os.path.join(Util.GetParentDirectory(context.RootFolder), f"printer_logs{serviceSuffixStr}")
                if os.path.exists(context.LogsFolder) is False:
                    # Failed, make a folder in the printer data root.
                    context.LogsFolder = os.path.join(context.RootFolder, "octoeverywhere-logs")
                    # Create the folder and force the permissions so our service can write to it.
                    Util.EnsureDirExists(context.LogsFolder, context, True)

        # If this is an companion setup we need to setup the connection with moonraker and save it into the config.
        if context.IsCompanionSetup:
            mc = MoonrakerConnector()
            mc.EnsureCompanionMoonrakerConnection(context)

        # If this is a Bambu Connect setup, we need to make sure we have a connection to a Bambu printer.
        if context.IsBambuSetup:
            bc = BambuConnector()
            bc.EnsureBambuConnection(context)

        # If this is a Elegoo Connect setup, we need to make sure we have a connection to a Elegoo printer.
        if context.IsElegooSetup:
            ec = ElegooConnector()
            ec.EnsureElegooPrinterConnection(context)

        # Report
        Logger.Debug(f'Configured. Service: {context.ServiceName}, Path: {context.ServiceFilePath}, LocalStorage: {context.LocalFileStorageFolder}, Config Dir: {context.ConfigFolder}, Logs: {context.LogsFolder}')
