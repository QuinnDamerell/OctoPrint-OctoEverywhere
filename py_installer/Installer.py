import sys
import traceback

from .Linker import Linker
from .Logging import Logger
from .Service import Service
from .Context import Context, OsTypes
from .Discovery import Discovery
from .DiscoveryCompanionAndBambu import DiscoveryCompanionAndBambu
from .Configure import Configure
from .Updater import Updater
from .Permissions import Permissions
from .TimeSync import TimeSync
from .Frontend import Frontend
from .Uninstall import Uninstall
from .Ffmpeg import Ffmpeg
from .ZStandard import ZStandard

class Installer:

    def Run(self):
        try:
            # Any error during the process will be thrown, which will be printed here, and exit the installer.
            self._RunInternal()
        except Exception as e:
            tb = traceback.format_exc()
            Logger.Blank()
            Logger.Blank()
            Logger.Error("Installer failed - "+str(e))
            Logger.Blank()
            Logger.Blank()
            Logger.Error("Stack Trace:")
            Logger.Error(str(tb))
            Logger.Blank()
            Logger.Blank()
            Logger.Header("Please contact our support team directly at support@octoeverywhere.com so we can help you fix this issue!")
            Logger.Blank()
            Logger.Blank()


    def _RunInternal(self):

        #
        # Setup Phase
        #

        # The installer script passes a json object to us, which contains all of the args.
        # But it might not be the first arg.
        argObjectStr = self.GetArgumentObjectStr()
        if argObjectStr is None:
            raise Exception("Failed to find cmd line json arg")

        # Parse and validate the args.
        context = Context.LoadFromArgString(argObjectStr)

        # As soon as we have the user home make the log file.
        Logger.InitFile(context.UserHomePath, context.UserName)

        # Parse the original CmdLineArgs
        Logger.Debug("Parsing script cmd line args.")
        context.ParseCmdLineArgs()

        # Figure out the OS type we are installing on.
        # This can be a normal debian device, Sonic Pad, K1, or others.
        context.DetectOsType()
        Logger.Info(f"Os Type Detected: {context.OsType}")

        # Print this again now that the debug cmd flag is parsed, since it might be useful.
        if context.Debug:
            Logger.Debug("Found config: "+argObjectStr)

        # Before we do the first validation, make sure the User var is setup correctly and update if needed.
        permissions = Permissions()
        permissions.CheckUserAndCorrectIfRequired_RanBeforeFirstContextValidation(context)

        # Validate we have the required args, but not the moonraker values yet, since they are optional.
        # All generation 1 vars must exist and be valid.
        Logger.Debug("Validating args")
        context.Validate(1)

        #
        # Run Phase
        #

        # If the help flag is set, do that now and exit.
        if context.ShowHelp:
            # If we should show help, do it now and return.
            self.PrintHelp()
            return

        # Ensure that the system clock sync is enabled. For some MKS PI systems the OS time is wrong and sync is disabled.
        # The user would of had to manually correct the time to get this installer running, but we will ensure that the
        # time sync systemd service is enabled to keep the clock in sync after reboots, otherwise it will cause SSL errors.
        TimeSync.EnsureNtpSyncEnabled(context)

        # Ensure the script at least has sudo permissions.
        # It's required to set file permission and to write / restart the service.
        # See comments in the function for details.
        permissions.EnsureRunningAsRootOrSudo(context)

        # If we are in update mode, do the update logic and exit.
        if context.IsUpdateMode:
            # Before the update, make sure all permissions are set correctly.
            permissions.EnsureFinalPermissions(context)

            # Do the update logic.
            update = Updater()
            update.DoUpdate(context)
            return

        # If we are running as an uninstaller, run that logic and exit.
        if context.IsUninstallMode:
            uninstall = Uninstall()
            uninstall.DoUninstall(context)
            return

        # Since this runs an async thread, kick it off now so it can start working.
        ZStandard.TryToInstallZStandardAsync(context)

        # Next step is to discover and fill out the moonraker config file path and service file name.
        # If we are doing an companion or bambu setup, we need the user to help us input the details to the external moonraker IP or bambu printer.
        # This is the hardest part of the setup, because it's highly dependent on the system and different moonraker setups.
        if context.IsCompanionOrBambu():
            discovery = DiscoveryCompanionAndBambu()
            discovery.Discovery(context)
        else:
            discovery = Discovery()
            discovery.FindTargetMoonrakerFiles(context)

        # Validate the response.
        # All generation 2 values must be set and valid.
        if context is None:
            raise Exception("Discovery returned an invalid context.")
        context.Validate(2)

        # Next, based on the vars generated by discovery, complete the configuration of the context.
        configure = Configure()
        configure.Run(context)

        # After configuration, gen 3 should be fully valid.
        context.Validate(3)

        # For all types, do the frontend setup now.
        frontend = Frontend()
        frontend.DoFrontendSetup(context)

        # We need ffmpeg for the Bambu Connect X1 streaming or any user who wants to use a RTSP camera.
        # Installing ffmpeg is best effort and not required for the plugin to work.
        Ffmpeg.TryToInstallFfmpeg(context)

        # Before we start the service, check if the secrets config file already exists and if a printer id already exists.
        # This will indicate if this is a fresh install or not.
        context.ExistingPrinterId = Linker.GetPrinterIdFromServiceSecretsConfigFile(context)

        # Final validation
        context.Validate(4)

        # Just before we start (or restart) the service, ensure all of the permission are set correctly
        permissions.EnsureFinalPermissions(context)

        # If there was an install running, wait for it to finish now, before the service starts.
        # For most installs, the user will take longer to add the info than it takes to install zstandard.
        ZStandard.WaitForInstallToComplete()

        # We are fully configured, create the service file and it's dependent files.
        service = Service()
        service.Install(context)

        # Add our auto update logic.
        updater = Updater()
        # If this is an companion or a Creality OS install, put the update script in the users root, so it's easy to find.
        if context.IsCompanionOrBambu() or context.IsCrealityOs():
            updater.PlaceUpdateScriptInRoot(context)
        # Also setup our cron updater if we can, so that the plugin will auto update.
        updater.EnsureCronUpdateJob(context.RepoRootFolder)

        # The service is ready! Now do the account linking process.
        linker = Linker()
        linker.Run(context)

        # Success!
        Logger.Blank()
        Logger.Blank()
        Logger.Blank()
        Logger.Purple("            ~~~ OctoEverywhere Setup Complete ~~~            ")
        Logger.Warn(  "  You Can Access Your Printer Anytime From OctoEverywhere.com")
        Logger.Header("                   Welcome To Our Community                  ")
        Logger.Error( "                            <3                               ")
        Logger.Blank()
        Logger.Blank()

        # At the end on success, for OSs that don't have very much disk space, clean up the installer log file, since it's probably not needed.
        # If we need the log file for some reason, we should add a flag to the context to keep it.
        if context.OsType == OsTypes.SonicPad or context.OsType == OsTypes.K1:
            Logger.DeleteLogFile()


    def GetArgumentObjectStr(self) -> str:
        # We want to skip arguments until we find the json string and then concat all args after that together.
        # The reason is the PY args logic will split the entire command line string by space, so any spaces in the json get broken
        # up into different args. This only really happens in the case of the CMD_LINE_ARGS, since it can be like "-companion -debug -whatever"
        jsonStr = None
        for arg in sys.argv:
            # Find the json start.
            if len(arg) > 0 and arg[0] == '{':
                jsonStr = arg
            # Once we have started a json string, keep building it.
            elif jsonStr is not None:
                # We need to add the space back to make up for the space removed during the args split.
                jsonStr += " " + arg
        return jsonStr


    def PrintHelp(self):
        Logger.Blank()
        Logger.Blank()
        Logger.Blank()
        Logger.Blank()
        Logger.Header("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        Logger.Header("    OctoEverywhere For Klipper And Bambu Connect    ")
        Logger.Header("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        Logger.Blank()
        Logger.Info("This installer can be used for:")
        Logger.Info("   - OctoEverywhere for Klipper - Where Moonraker is running on this device.")
        Logger.Info("   - OctoEverywhere for Creality - Where this device is a Creality device (Sonic Pad, K1, Ender v3, etc)")
        Logger.Info("   - OctoEverywhere Companion - Where this plugin will connect to Moonraker running on a different device on the same LAN.")
        Logger.Info("   - OctoEverywhere Bambu Connect - Where this plugin will connect to a Bambu Lab printer on the same LAN.")
        Logger.Blank()
        Logger.Warn("This installer is NOT for:")
        Logger.Info("   - OctoPrint or OctoKlipper - If you're using OctoPrint, install OctoEverywhere directly in OctoPrint from the plugin manager.")
        Logger.Blank()
        Logger.Warn("Command line format:")
        Logger.Info("  <moonraker config file path> <moonraker service file path> -other -args")
        Logger.Blank()
        Logger.Warn("Argument details:")
        Logger.Info("  <moonraker config file path>  - optional - If supplied, the install will target this moonraker setup without asking or searching for others")
        Logger.Info("  <moonraker service name> - optional - If supplied, the install will target this moonraker service file without searching.")
        Logger.Info("       Used when multiple moonraker instances are ran on the same device. The service name is used to find the unique moonraker identifier. OctoEverywhere will follow the same naming convention. Typically the file name is something like `moonraker-1.service` or `moonraker-somename.service`")
        Logger.Blank()
        Logger.Warn("Other Optional Args:")
        Logger.Info("  -help            - Shows this message.")
        Logger.Info("  -update          - The installer will update all OctoEverywhere plugins on this device of any type.")
        Logger.Info("  -companion       - Makes the setup target a OctoEverywhere Companion plugin setup.")
        Logger.Info("  -bambu           - Makes the setup target a OctoEverywhere Bambu Connect plugin setup.")
        Logger.Info("  -noatuoselect    - Disables auto selecting a moonraker instance, allowing the user to always choose.")
        Logger.Info("  -debug           - Enable debug logging to the console.")
        Logger.Info("  -skipsudoactions - Skips sudo required actions. This is useful for debugging, but will make the install not fully work.")
        Logger.Blank()
        Logger.Info("If you need help, contact our support team at support@octoeverywhere.com")
        Logger.Blank()
        Logger.Blank()
