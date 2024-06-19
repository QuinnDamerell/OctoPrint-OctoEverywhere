import logging
import traceback

from octoeverywhere.mdns import MDns
from octoeverywhere.sentry import Sentry
from octoeverywhere.telemetry import Telemetry
from octoeverywhere.hostcommon import HostCommon
from octoeverywhere.compression import Compression
from octoeverywhere.printinfo import PrintInfoManager
from octoeverywhere.Webcam.webcamhelper import WebcamHelper
from octoeverywhere.octopingpong import OctoPingPong
from octoeverywhere.commandhandler import CommandHandler
from octoeverywhere.octoeverywhereimpl import OctoEverywhere
from octoeverywhere.notificationshandler import NotificationsHandler
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.Proto.ServerHost import ServerHost
from octoeverywhere.compat import Compat

from linux_host.config import Config
from linux_host.secrets import Secrets
from linux_host.version import Version
from linux_host.logger import LoggerInit

from .bambuclient import BambuClient
from .bambuwebcamhelper import BambuWebcamHelper
from .bambucommandhandler import BambuCommandHandler
from .bambustatetranslater import BambuStateTranslator

# This file is the main host for the bambu service.
class BambuHost:

    def __init__(self, configDir:str, logDir:str, devConfig_CanBeNone) -> None:
        # When we create our class, make sure all of our core requirements are created.
        self.Secrets = None
        self.NotificationHandler:NotificationsHandler = None

        # Let the compat system know this is an Bambu host.
        Compat.SetIsBambu(True)

        try:
            # First, we need to load our config.
            # Note that the config MUST BE WRITTEN into this folder, that's where the setup installer is going to look for it.
            # If this fails, it will throw.
            self.Config = Config(configDir)

            # Setup the logger.
            logLevelOverride_CanBeNone = self.GetDevConfigStr(devConfig_CanBeNone, "LogLevel")
            self.Logger = LoggerInit.GetLogger(self.Config, logDir, logLevelOverride_CanBeNone)
            self.Config.SetLogger(self.Logger)

            # Give the logger to Sentry ASAP.
            Sentry.SetLogger(self.Logger)

        except Exception as e:
            tb = traceback.format_exc()
            print("Failed to init Bambu Host! "+str(e) + "; "+str(tb))
            # Raise the exception so we don't continue.
            raise


    def RunBlocking(self, configPath, localStorageDir, repoRoot, devConfig_CanBeNone):
        # Do all of this in a try catch, so we can log any issues before exiting
        try:
            self.Logger.info("################################################")
            self.Logger.info("#### OctoEverywhere Bambu Connect Starting #####")
            self.Logger.info("################################################")

            # Find the version of the plugin, this is required and it will throw if it fails.
            pluginVersionStr = Version.GetPluginVersion(repoRoot)
            self.Logger.info("Plugin Version: %s", pluginVersionStr)

            # As soon as we have the plugin version, setup Sentry
            # Enabling profiling and no filtering, since we are the only PY in this process.
            Sentry.Setup(pluginVersionStr, "bambu", devConfig_CanBeNone is not None, enableProfiling=True, filterExceptionsByPackage=False, restartOnCantCreateThreadBug=True)

            # Before the first time setup, we must also init the Secrets class and do the migration for the printer id and private key, if needed.
            self.Secrets = Secrets(self.Logger, localStorageDir)

            # Now, detect if this is a new instance and we need to init our global vars. If so, the setup script will be waiting on this.
            self.DoFirstTimeSetupIfNeeded()

            # Get our required vars
            printerId = self.GetPrinterId()
            privateKey = self.GetPrivateKey()

            # Set the printer ID into sentry.
            Sentry.SetPrinterId(printerId)

            # Unpack any dev vars that might exist
            DevLocalServerAddress_CanBeNone = self.GetDevConfigStr(devConfig_CanBeNone, "LocalServerAddress")
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

            # Setup the print info manager.
            PrintInfoManager.Init(self.Logger, localStorageDir)

            # For bambu, there's no frontend to connect to, so we disable the http relay system.
            OctoHttpRequest.SetDisableHttpRelay(True)
            # But we still want to set the "local OctoPrint port" to 80, because that's the default port it will try for relative URLs.
            # Relative URLs for Bambu only come from the alternative webcam streaming system, which the user might be trying to access a webcam stream from this device.
            # If they don't specify a IP (or localhost) and port, then we will default all relative URLs to the "local OctoPrint port" value.
            OctoHttpRequest.SetLocalHttpProxyPort(80)
            OctoHttpRequest.SetLocalOctoPrintPort(80)
            OctoHttpRequest.SetLocalHttpProxyIsHttps(False)

            # Init the ping pong helper.
            OctoPingPong.Init(self.Logger, localStorageDir, printerId)
            if DevLocalServerAddress_CanBeNone is not None:
                OctoPingPong.Get().DisablePrimaryOverride()

            # Setup the webcam helper
            webcamHelper = BambuWebcamHelper(self.Logger, self.Config)
            WebcamHelper.Init(self.Logger, webcamHelper, localStorageDir)

            # Setup the state translator and notification handler
            stateTranslator = BambuStateTranslator(self.Logger)
            self.NotificationHandler = NotificationsHandler(self.Logger, stateTranslator)
            self.NotificationHandler.SetPrinterId(printerId)
            stateTranslator.SetNotificationHandler(self.NotificationHandler)

            # Setup the command handler
            CommandHandler.Init(self.Logger, self.NotificationHandler, BambuCommandHandler(self.Logger))

            # Setup and start the Bambu Client
            BambuClient.Init(self.Logger, self.Config, stateTranslator)

            # Now start the main runner!
            OctoEverywhereWsUri = HostCommon.c_OctoEverywhereOctoClientWsUri
            if DevLocalServerAddress_CanBeNone is not None:
                OctoEverywhereWsUri = "ws://"+DevLocalServerAddress_CanBeNone+"/octoclientws"
            oe = OctoEverywhere(OctoEverywhereWsUri, printerId, privateKey, self.Logger, self, self, pluginVersionStr, ServerHost.Bambu, False)
            oe.RunBlocking()
        except Exception as e:
            Sentry.Exception("!! Exception thrown out of main host run function.", e)

        # Allow the loggers to flush before we exit
        try:
            self.Logger.info("##################################")
            self.Logger.info("#### OctoEverywhere Exiting ######")
            self.Logger.info("##################################")
            logging.shutdown()
        except Exception as e:
            print("Exception in logging.shutdown "+str(e))


    # Ensures all required values are setup and valid before starting.
    def DoFirstTimeSetupIfNeeded(self):
        # Try to get the printer id from the config.
        printerId = self.GetPrinterId()
        if HostCommon.IsPrinterIdValid(printerId) is False:
            if printerId is None:
                self.Logger.info("No printer id was found, generating one now!")
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


    # Returns None if no printer id has been set.
    def GetPrinterId(self):
        return self.Secrets.GetPrinterId()


    # Returns None if no private id has been set.
    def GetPrivateKey(self):
        return self.Secrets.GetPrivateKey()


    # Tries to load a dev config option as a string.
    # If not found or it fails, this return None
    def GetDevConfigStr(self, devConfig, value):
        if devConfig is None:
            return None
        if value in devConfig:
            v = devConfig[value]
            if v is not None and len(v) > 0 and v != "None":
                return v
        return None


    # UiPopupInvoker Interface function - Sends a UI popup message for various uses.
    # Must stay in sync with the OctoPrint handler!
    # title - string, the title text.
    # text  - string, the message.
    # type  - string, [notice, info, success, error] the type of message shown.
    # actionText - string, if not None or empty, this is the text to show on the action button or text link.
    # actionLink - string, if not None or empty, this is the URL to show on the action button or text link.
    # onlyShowIfLoadedViaOeBool - bool, if set, the message should only be shown on browsers loading the portal from OE.
    def ShowUiPopup(self, title:str, text:str, msgType:str, actionText:str, actionLink:str, showForSec:int, onlyShowIfLoadedViaOeBool:bool):
        # This isn't supported on Bambu
        pass


    #
    # StatusChangeHandler Interface - Called by the OctoEverywhere logic when the server connection has been established.
    #
    def OnPrimaryConnectionEstablished(self, octoKey, connectedAccounts):
        self.Logger.info("Primary Connection To OctoEverywhere Established - We Are Ready To Go!")

        # Give the octoKey to who needs it.
        self.NotificationHandler.SetOctoKey(octoKey)

        # Check if this printer is unlinked, if so add a message to the log to help the user setup the printer if desired.
        # This would be if the skipped the printer link or missed it in the setup script.
        if connectedAccounts is None or len(connectedAccounts) == 0:
            self.Logger.warning("")
            self.Logger.warning("")
            self.Logger.warning("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            self.Logger.warning("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            self.Logger.warning("          This Plugin Isn't Connected To OctoEverywhere!          ")
            self.Logger.warning(" Use the following link to finish the setup and get remote access:")
            self.Logger.warning(" %s", HostCommon.GetAddPrinterUrl(self.GetPrinterId(), False))
            self.Logger.warning("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            self.Logger.warning("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            self.Logger.warning("")
            self.Logger.warning("")


    #
    # StatusChangeHandler Interface - Called by the OctoEverywhere logic when a plugin update is required for this client.
    #
    def OnPluginUpdateRequired(self):
        self.Logger.error("!!! A Plugin Update Is Required -- If This Plugin Isn't Updated It Might Stop Working !!!")
        self.Logger.error("!!! Please use the update manager in Mainsail of Fluidd to update this plugin         !!!")
