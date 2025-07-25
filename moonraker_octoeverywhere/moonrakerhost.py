import logging
import traceback
from typing import Any, Dict, List, Optional

from octoeverywhere.mdns import MDns
from octoeverywhere.sentry import Sentry
from octoeverywhere.deviceid import DeviceId
from octoeverywhere.telemetry import Telemetry
from octoeverywhere.hostcommon import HostCommon
from octoeverywhere.linkhelper import LinkHelper
from octoeverywhere.compression import Compression
from octoeverywhere.octopingpong import OctoPingPong
from octoeverywhere.httpsessions import HttpSessions
from octoeverywhere.Webcam.webcamhelper import WebcamHelper
from octoeverywhere.printinfo import PrintInfoManager
from octoeverywhere.commandhandler import CommandHandler
from octoeverywhere.octoeverywhereimpl import OctoEverywhere
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.Proto.ServerHost import ServerHost
from octoeverywhere.localip import LocalIpHelper
from octoeverywhere.compat import Compat
from octoeverywhere.interfaces import IHostCommandHandler, IStateChangeHandler

from linux_host.config import Config
from linux_host.secrets import Secrets
from linux_host.version import Version
from linux_host.logger import LoggerInit

from .smartpause import SmartPause
from .uipopupinvoker import UiPopupInvoker
from .systemconfigmanager import SystemConfigManager
from .moonrakerclient import MoonrakerClient
from .moonrakercommandhandler import MoonrakerCommandHandler
from .moonrakerwebcamhelper import MoonrakerWebcamHelper
from .moonrakerdatabase import MoonrakerDatabase
from .webrequestresponsehandler import MoonrakerWebRequestResponseHandler
from .moonrakerapirouter import MoonrakerApiRouter
from .moonrakercredentialmanager import MoonrakerCredentialManager
from .filemetadatacache import FileMetadataCache
from .uiinjector import UiInjector
from .interfaces import IMoonrakerConnectionStatusHandler


# This file is the main host for the moonraker service.
class MoonrakerHost(IMoonrakerConnectionStatusHandler, IHostCommandHandler, IStateChangeHandler):

    def __init__(self, klipperConfigDir:str, klipperLogDir:str, devConfig:Optional[Dict[str, Any]]) -> None:
        # When we create our class, make sure all of our core requirements are created.
        self.MoonrakerWebcamHelper:MoonrakerWebcamHelper = None #pyright: ignore[reportAttributeAccessIssue]
        self.MoonrakerDatabase:MoonrakerDatabase = None #pyright: ignore[reportAttributeAccessIssue]
        self.Secrets:Secrets = None #pyright: ignore[reportAttributeAccessIssue]

        # Let the compat system know this is an Moonraker host.
        Compat.SetIsMoonraker(True)

        try:
            # First, we need to load our config.
            # Note that the config MUST BE WRITTEN into this folder, that's where the setup installer is going to look for it.
            # If this fails, it will throw.
            self.Config = Config(klipperConfigDir)

            # Next, setup the logger.
            logLevelOverride_CanBeNone = self.GetDevConfigStr(devConfig, "LogLevel")
            self.Logger = LoggerInit.GetLogger(self.Config, klipperLogDir, logLevelOverride_CanBeNone)
            self.Config.SetLogger(self.Logger)

            # Set the logger ASAP.
            Sentry.SetLogger(self.Logger)

        except Exception as e:
            tb = traceback.format_exc()
            print("Failed to init Moonraker Host! "+str(e) + "; "+str(tb))
            # Raise the exception so we don't continue.
            raise


    def RunBlocking(self, klipperConfigDir:str, isCompanionMode:bool, localStorageDir:str, serviceName:str, pyVirtEnvRoot:str, repoRoot:str,
                    moonrakerConfigFilePath:Optional[str], # Will be None in Companion mode
                    devConfig:Optional[Dict[str, Any]]) -> None:
        # Do all of this in a try catch, so we can log any issues before exiting
        try:
            self.Logger.info("################################################")
            if isCompanionMode:
                self.Logger.info("## OctoEverywhere Klipper Companion Starting  ##")
            else:
                self.Logger.info("##### OctoEverywhere For Klipper Starting ######")
            self.Logger.info("################################################")

            # Set companion mode flag as soon as we know it.
            Compat.SetIsCompanionMode(isCompanionMode)

            # Find the version of the plugin, this is required and it will throw if it fails.
            pluginVersionStr = Version.GetPluginVersion(repoRoot)
            self.Logger.info("Plugin Version: %s", pluginVersionStr)

            # Setup the HttpSession cache early, so it can be used whenever
            HttpSessions.Init(self.Logger)

            # As soon as we have the plugin version, setup Sentry
            # Enabling profiling and no filtering, since we are the only PY in this process.
            Sentry.Setup(pluginVersionStr, "klipper", devConfig is not None, enableProfiling=True, filterExceptionsByPackage=False, restartOnCantCreateThreadBug=True)

            # This logic only works if running locally.
            if not isCompanionMode:
                # Before we do this first time setup, make sure our config files are in place. This is important
                # because if this fails it will throw. We don't want to let the user complete the install setup if things
                # with the update aren't working.
                SystemConfigManager.EnsureUpdateManagerFilesSetup(self.Logger, klipperConfigDir, serviceName, pyVirtEnvRoot, repoRoot)

            # Before the first time setup, we must also init the Secrets class and do the migration for the printer id and private key, if needed.
            # As of 8/15/2023, we don't store any sensitive things in teh config file, since all config files are sometimes backed up publicly.
            self.Secrets = Secrets(self.Logger, localStorageDir, self.Config)

            # Now, detect if this is a new instance and we need to init our global vars. If so, the setup script will be waiting on this.
            self.DoFirstTimeSetupIfNeeded(klipperConfigDir, serviceName)

            # Get our required vars
            printerId = self.GetPrinterId()
            privateKey = self.GetPrivateKey()
            if printerId is None or privateKey is None:
                raise Exception("Printer ID or Private Key is None! This should never happen, please report this issue to the OctoEverywhere team.")

            # Set the printer id to Sentry.
            Sentry.SetPrinterId(printerId)

            # Unpack any dev vars that might exist
            DevLocalServerAddress_CanBeNone = self.GetDevConfigStr(devConfig, "LocalServerAddress")
            if DevLocalServerAddress_CanBeNone is not None:
                self.Logger.warning("~~~ Using Local Dev Server Address: %s ~~~", DevLocalServerAddress_CanBeNone)

            # Init Sentry, but it won't report since we are in dev mode.
            Telemetry.Init(self.Logger)
            if DevLocalServerAddress_CanBeNone is not None:
                Telemetry.SetServerProtocolAndDomain("http://"+DevLocalServerAddress_CanBeNone)

            # Init compression
            Compression.Init(self.Logger, localStorageDir)

            # Init the mdns client
            MDns.Init(self.Logger, localStorageDir)

            # Init device id
            DeviceId.Init(self.Logger)

            # Allow the UI injector to run and do it's thing.
            UiInjector.Init(self.Logger, repoRoot)

            # Setup the print info manager
            PrintInfoManager.Init(self.Logger, localStorageDir)

            # Setup the database helper
            self.MoonrakerDatabase = MoonrakerDatabase(self.Logger, printerId, pluginVersionStr)

            # Setup the credential manager.
            MoonrakerCredentialManager.Init(self.Logger, moonrakerConfigFilePath, isCompanionMode)

            # Setup the http requester. We default to port 80 and assume the frontend can be found there.
            # TODO - parse nginx to see what front ends exist and make them switchable
            # TODO - detect HTTPS port if 80 is not bound.
            frontendPort = self.Config.GetInt(Config.RelaySection, Config.RelayFrontEndPortKey, 80)
            if frontendPort is None:
                frontendPort = 80
            self.Logger.info("Setting up relay with frontend port %s", str(frontendPort))
            OctoHttpRequest.SetLocalHttpProxyPort(frontendPort)
            OctoHttpRequest.SetLocalHttpProxyIsHttps(False)
            OctoHttpRequest.SetLocalOctoPrintPort(frontendPort)

            # If we are in companion mode, we need to update the local address to be the other local remote.
            if isCompanionMode:
                ipOrHostnameStr = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
                portStr = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, None)
                if ipOrHostnameStr is None or portStr is None:
                    self.Logger.error("We are in companion mode but we can't get the ip and port from the companion config file.")
                    raise Exception("Failed to read companion config file.")
                OctoHttpRequest.SetLocalHostAddress(ipOrHostnameStr)
                # TODO - this could be an host name, not an IP. That might be a problem?
                LocalIpHelper.SetLocalIpOverride(ipOrHostnameStr)

            # Init the ping pong helper.
            OctoPingPong.Init(self.Logger, localStorageDir, printerId)
            if DevLocalServerAddress_CanBeNone is not None:
                OctoPingPong.Get().DisablePrimaryOverride()

            # Setup the snapshot helper
            self.MoonrakerWebcamHelper = MoonrakerWebcamHelper(self.Logger, self.Config)
            WebcamHelper.Init(self.Logger, self.MoonrakerWebcamHelper, localStorageDir)

            # Setup our smart pause helper
            SmartPause.Init(self.Logger)

            # When everything is setup, start the moonraker client object.
            # This also creates the Notifications Handler and Gadget objects.
            # This doesn't start the moon raker connection, we don't do that until OE connects.
            MoonrakerClient.Init(self.Logger, self.Config, moonrakerConfigFilePath, printerId, self, pluginVersionStr)

            # Init our file meta data cache helper
            FileMetadataCache.Init(self.Logger, MoonrakerClient.Get())

            # Setup the command handler
            CommandHandler.Init(self.Logger, MoonrakerClient.Get().GetNotificationHandler(), MoonrakerCommandHandler(self.Logger), self)

            # If we have a local dev server, set it in the notification handler.
            if DevLocalServerAddress_CanBeNone is not None:
                MoonrakerClient.Get().GetNotificationHandler().SetServerProtocolAndDomain("http://"+DevLocalServerAddress_CanBeNone)
                MoonrakerClient.Get().GetNotificationHandler().SetGadgetServerProtocolAndDomain("http://"+DevLocalServerAddress_CanBeNone)

            # Setup the moonraker config handler
            MoonrakerWebRequestResponseHandler.Init(self.Logger)

            # Setup the moonraker API router
            MoonrakerApiRouter.Init(self.Logger)

            # Now start the main runner!
            OctoEverywhereWsUri = HostCommon.c_OctoEverywhereOctoClientWsUri
            if DevLocalServerAddress_CanBeNone is not None:
                OctoEverywhereWsUri = "ws://"+DevLocalServerAddress_CanBeNone+"/octoclientws"
            oe = OctoEverywhere(OctoEverywhereWsUri, printerId, privateKey, self.Logger, UiPopupInvoker(self.Logger), self, pluginVersionStr, ServerHost.Moonraker, isCompanionMode)
            oe.RunBlocking()
        except Exception as e:
            Sentry.OnException("!! Exception thrown out of main host run function.", e)

        # Allow the loggers to flush before we exit
        try:
            self.Logger.info("##################################")
            self.Logger.info("#### OctoEverywhere Exiting ######")
            self.Logger.info("##################################")
            logging.shutdown()
        except Exception as e:
            print("Exception in logging.shutdown "+str(e))


    # Ensures all required values are setup and valid before starting.
    def DoFirstTimeSetupIfNeeded(self, klipperConfigDir:str, serviceName:str) -> None:
        # Try to get the printer id from the config.
        printerId = self.GetPrinterId()
        if HostCommon.IsPrinterIdValid(printerId) is False:
            if printerId is None:
                self.Logger.info("No printer id was found, generating one now!")
                # If there is no printer id, we consider this the first run.
            else:
                self.Logger.info("An invalid printer id was found [%s], regenerating!", str(printerId))

            # Make a new, valid, key
            printerId = HostCommon.GeneratePrinterId()

            # Save it
            self.Secrets.SetPrinterId(printerId)
            self.Logger.info("New printer id created: %s", printerId)

        privateKey = self.GetPrivateKey()
        if HostCommon.IsPrivateKeyValid(privateKey) is False:
            if privateKey is None:
                self.Logger.info("No private key was found, generating one now!")
            else:
                self.Logger.info("An invalid private key was found [%s], regenerating!", str(privateKey))

            # Make a new, valid, key
            privateKey = HostCommon.GeneratePrivateKey()

            # Save it
            self.Secrets.SetPrivateKey(privateKey)
            self.Logger.info("New private key created.")

        # We always run this now, to ensure if the service name is removed it gets added back again.
        SystemConfigManager.EnsureAllowedServicesFile(self.Logger, klipperConfigDir, serviceName)


    # Returns None if no printer id has been set.
    def GetPrinterId(self) -> Optional[str]:
        return self.Secrets.GetPrinterId()


    # Returns None if no private id has been set.
    def GetPrivateKey(self) -> Optional[str]:
        return self.Secrets.GetPrivateKey()


    # Tries to load a dev config option as a string.
    # If not found or it fails, this return None
    def GetDevConfigStr(self, devConfig:Optional[Dict[str, str]], value:str) -> Optional[str]:
        if devConfig is None:
            return None
        if value in devConfig:
            v = devConfig[value]
            if v is not None and len(v) > 0 and v != "None":
                return v
        return None


    # This is a destructive action! It will remove the printer id and private key from the system and restart the plugin.
    def Rekey(self, reason:str) -> None:
        #pylint: disable=logging-fstring-interpolation
        self.Logger.error(f"HOST REKEY CALLED {reason} - Clearing keys...")
        # It's important we clear the key, or we will reload, fail to connect, try to rekey, and restart again!
        self.Secrets.SetPrinterId(None)
        self.Secrets.SetPrivateKey(None)
        self.Logger.error("Key clear complete, restarting plugin.")
        HostCommon.RestartPlugin()


    #
    # StatusChangeHandler Interface - Called by the OctoEverywhere logic when the server connection has been established.
    #
    def OnPrimaryConnectionEstablished(self, octoKey:str, connectedAccounts:List[str]) -> None:
        self.Logger.info("Primary Connection To OctoEverywhere Established - We Are Ready To Go!")

        # Check if this printer is unlinked, if so add a message to the log to help the user setup the printer if desired.
        # This would be if the skipped the printer link or missed it in the setup script.
        if len(connectedAccounts) == 0:
            printerId = self.GetPrinterId()
            if printerId is None:
                self.Logger.error("This printer is not linked to an OctoEverywhere account. Please link this printer to an account to use the remote features.")
            else:
                LinkHelper.RunLinkPluginConsolePrinterAsync(self.Logger, printerId, "moonraker_host")

        # Now that we are connected, start the moonraker client.
        # We do this after the connection incase it needs to send any notifications or messages when starting.
        MoonrakerClient.Get().StartRunningIfNotAlready(octoKey)


    #
    # StatusChangeHandler Interface - Called by the OctoEverywhere logic when a plugin update is required for this client.
    #
    def OnPluginUpdateRequired(self) -> None:
        self.Logger.error("!!! A Plugin Update Is Required -- If This Plugin Isn't Updated It Might Stop Working !!!")
        self.Logger.error("!!! Please use the update manager in Mainsail of Fluidd to update this plugin         !!!")


    #
    # StatusChangeHandler Interface - Called by the OctoEverywhere handshake when a rekey is required.
    #
    def OnRekeyRequired(self) -> None:
        self.Rekey("Handshake Failed")


    #
    # MoonrakerClient ConnectionStatusHandler Interface - Called by the MoonrakerClient every time the moonraker websocket is open and authed - BUT possibly not connected to klippy.
    # At this point it's ok to query things in moonraker like db items, webcam info, and such. But API calls that have to do with the physical printer will fail, since klippy might not be ready yet.
    #
    def OnMoonrakerWsOpenAndAuthed(self) -> None:

        # Kick off the webcam settings helper, to ensure it pulls fresh settings if desired.
        # Use force, because the websocket might not open for some time and the first auto get might fail.
        # When when moonraker connects, for the settings get, so ensure we are in sync with the system.
        self.MoonrakerWebcamHelper.KickOffWebcamSettingsUpdate(forceUpdate=True)

        # Also allow the database logic to ensure our public keys exist and are updated.
        self.MoonrakerDatabase.EnsureOctoEverywhereDatabaseEntry()

    #
    # MoonrakerClient ConnectionStatusHandler Interface - Called by the MoonrakerClient when it gets a message that the webcam settings have changed.
    #
    def OnWebcamSettingsChanged(self) -> None:
        # Set the force flag to true, since we know the settings just changed.
        self.MoonrakerWebcamHelper.KickOffWebcamSettingsUpdate(forceUpdate=True)

    #
    # MoonrakerClient ConnectionStatusHandler Interface - Called by the MoonrakerClient when the moonraker connection has been established and klippy is fully ready to use.
    #
    def OnMoonrakerClientConnected(self) -> None:
        pass


    #
    # Command Host Interface - Called by the command handler, when called the plugin must clear it's keys and restart to generate new ones.
    #
    def OnRekeyCommand(self) -> bool:
        self.Rekey("Command")
        return True
