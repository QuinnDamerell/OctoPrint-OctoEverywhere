import logging
import traceback
from typing import Any, Dict, List, Optional

from linux_host.config import Config
from linux_host.localwebapi import LocalWebApi
from linux_host.logger import LoggerInit
from linux_host.secrets import Secrets
from linux_host.version import Version

from octoeverywhere.Webcam.webcamhelper import WebcamHelper
from octoeverywhere.commandhandler import CommandHandler
from octoeverywhere.compat import Compat
from octoeverywhere.compression import Compression
from octoeverywhere.deviceid import DeviceId
from octoeverywhere.hostcommon import HostCommon
from octoeverywhere.httpsessions import HttpSessions
from octoeverywhere.interfaces import IHostCommandHandler, IPopUpInvoker, IStateChangeHandler
from octoeverywhere.linkhelper import LinkHelper
from octoeverywhere.localip import LocalIpHelper
from octoeverywhere.mdns import MDns
from octoeverywhere.notificationshandler import NotificationsHandler
from octoeverywhere.octoeverywhereimpl import OctoEverywhere
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.pingpong import PingPong
from octoeverywhere.printinfo import PrintInfoManager
from octoeverywhere.Proto.ServerHost import ServerHost
from octoeverywhere.sentry import Sentry
from octoeverywhere.telemetry import Telemetry

from .elegoocc2client import ElegooCc2Client
from .elegoocc2commandhandler import ElegooCc2CommandHandler
from .elegoocc2filemanager import ElegooCc2FileManager
from .elegoocc2mqttwebsocketproxy import MqttWebsocketProxyProviderBuilder
from .elegoocc2statetranslater import ElegooCc2StateTranslator
from .elegoocc2webcamhelper import ElegooCc2WebcamHelper

class ElegooCc2Host(IHostCommandHandler, IPopUpInvoker, IStateChangeHandler):

    def __init__(self, configDir:str, logDir:str, devConfig:Optional[Dict[str, Any]]) -> None:
        self.Secrets:Secrets = None #pyright: ignore[reportAttributeAccessIssue]
        self.NotificationHandler:NotificationsHandler = None #pyright: ignore[reportAttributeAccessIssue]

        Compat.SetIsElegooCc2(True)

        try:
            self.Config = Config(configDir)

            logLevelOverride = self.GetDevConfigStr(devConfig, "LogLevel")
            self.Logger = LoggerInit.GetLogger(self.Config, logDir, logLevelOverride)
            self.Config.SetLogger(self.Logger)

            Sentry.SetLogger(self.Logger)
        except Exception as e:
            tb = traceback.format_exc()
            print("Failed to init Elegoo CC2 Host! "+str(e) + "; "+str(tb))
            raise


    def RunBlocking(self, configPath:str, localStorageDir:str, repoRoot:str, isDockerContainer:bool, devConfig:Optional[Dict[str, Any]]) -> None:
        try:
            self.Logger.info("######################################################")
            self.Logger.info("#### OctoEverywhere Elegoo CC2 Connect Starting #####")
            self.Logger.info("######################################################")

            pluginVersionStr = Version.GetPluginVersion(repoRoot)
            self.Logger.info("Plugin Version: %s", pluginVersionStr)

            HttpSessions.Init(self.Logger)

            Sentry.Setup(pluginVersionStr, "elegoo_cc2", devConfig is not None, canEnableProfiling=True, filterExceptionsByPackage=False, restartOnCantCreateThreadBug=True)

            self.Secrets = Secrets(self.Logger, localStorageDir)
            self.DoFirstTimeSetupIfNeeded()

            printerId = self.GetPrinterId()
            privateKey = self.GetPrivateKey()
            if printerId is None or privateKey is None:
                raise Exception("Printer ID or Private Key is None! This should never happen, please report this issue to the OctoEverywhere team.")

            Sentry.SetPrinterId(printerId)

            DevLocalServerAddress_CanBeNone = self.GetDevConfigStr(devConfig, "LocalServerAddress")
            if DevLocalServerAddress_CanBeNone is not None:
                self.Logger.warning("~~~ Using Local Dev Server Address: %s ~~~", DevLocalServerAddress_CanBeNone)

            Telemetry.Init(self.Logger)
            if DevLocalServerAddress_CanBeNone is not None:
                Telemetry.SetServerProtocolAndDomain("http://"+DevLocalServerAddress_CanBeNone)

            Compression.Init(self.Logger, localStorageDir)
            MDns.Init(self.Logger, localStorageDir)
            LocalWebApi.Init(self.Logger, printerId, self.Config)
            DeviceId.Init(self.Logger)
            PrintInfoManager.Init(self.Logger, localStorageDir)

            configIpOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
            if configIpOrHostname is not None:
                LocalIpHelper.SetConnectionTargetIpOverride(configIpOrHostname)

            PingPong.Init(self.Logger, localStorageDir, printerId)
            if DevLocalServerAddress_CanBeNone is not None:
                PingPong.Get().DisablePrimaryOverride()

            webcamHelper = ElegooCc2WebcamHelper(self.Logger, self.Config)
            WebcamHelper.Init(self.Logger, webcamHelper, localStorageDir)

            stateTranslator = ElegooCc2StateTranslator(self.Logger)
            self.NotificationHandler = NotificationsHandler(self.Logger, stateTranslator)
            self.NotificationHandler.SetPrinterId(printerId)
            self.NotificationHandler.SetBedCooldownThresholdTemp(self.Config.GetFloatRequired(Config.GeneralSection, Config.GeneralBedCooldownThresholdTempC, Config.GeneralBedCooldownThresholdTempCDefault))
            stateTranslator.SetNotificationHandler(self.NotificationHandler)

            CommandHandler.Init(self.Logger, self.NotificationHandler, ElegooCc2CommandHandler(self.Logger), self)

            ElegooCc2FileManager.Init(self.Logger)
            ElegooCc2Client.Init(self.Logger, self.Config, printerId, pluginVersionStr, stateTranslator, ElegooCc2FileManager.Get())

            Compat.SetMqttWebsocketProxyProviderBuilder(MqttWebsocketProxyProviderBuilder(self.Logger))

            # The Elegoo CC2 runs a HTTP server on 9001 for the websocket and web server on 8080 for the webcam stream.
            # The frontend isn't served, they ship the frontend HTML with the slicer.
            OctoHttpRequest.SetLocalOctoPrintPort(9001)
            OctoHttpRequest.SetLocalHttpProxyPort(8080)
            OctoHttpRequest.SetLocalHttpProxyIsHttps(False)

            # For the CC2, we need to send the serial number and access code because they need to be added into the JS in the frontend.
            # These can't change in the plugin unless the user changes them and restarts, so it safe to send them in the handshake.
            conProperties:Dict[str, Any] = {
                "access_code": self.Config.GetStrRequired(Config.SectionElegoo, Config.ElegooCc2AccessCode, ""),
                "sn": self.Config.GetStrRequired(Config.SectionElegoo, Config.ElegooCc2PrinterSn, ""),
            }

            OctoEverywhereWsUri = HostCommon.c_OctoEverywhereOctoClientWsUri
            if DevLocalServerAddress_CanBeNone is not None:
                OctoEverywhereWsUri = "ws://"+DevLocalServerAddress_CanBeNone+"/"+HostCommon.c_OctoEverywhereOctoClientEndpointBase
            oe = OctoEverywhere(OctoEverywhereWsUri, printerId, privateKey, self.Logger, self, self, pluginVersionStr, ServerHost.Elegoo2, True, isDockerContainer, conProperties)
            oe.RunBlocking()
        except Exception as e:
            Sentry.OnException("!! Exception thrown out of main Elegoo CC2 host run function.", e)

        try:
            self.Logger.info("##################################")
            self.Logger.info("#### OctoEverywhere Exiting ######")
            self.Logger.info("##################################")
            logging.shutdown()
        except Exception as e:
            print("Exception in logging.shutdown "+str(e))


    def DoFirstTimeSetupIfNeeded(self) -> None:
        printerId = self.GetPrinterId()
        if HostCommon.IsPrinterIdValid(printerId) is False:
            if printerId is None:
                self.Logger.info("No printer id was found, generating one now!")
            else:
                self.Logger.info("An invalid printer id was found [%s], regenerating!", str(printerId))
            printerId = HostCommon.GeneratePrinterId()
            self.Secrets.SetPrinterId(printerId)
            self.Logger.info("New printer id created: %s", printerId)

        privateKey = self.GetPrivateKey()
        if HostCommon.IsPrivateKeyValid(privateKey) is False:
            if privateKey is None:
                self.Logger.info("No private key was found, generating one now!")
            else:
                self.Logger.info("An invalid private key was found [%s], regenerating!", str(privateKey))
            privateKey = HostCommon.GeneratePrivateKey()
            self.Secrets.SetPrivateKey(privateKey)
            self.Logger.info("New private key created.")


    def GetPrinterId(self) -> Optional[str]:
        return self.Secrets.GetPrinterId()


    def GetPrivateKey(self) -> Optional[str]:
        return self.Secrets.GetPrivateKey()


    def GetDevConfigStr(self, devConfig:Optional[Dict[str, Any]], value:str) -> Optional[str]:
        if devConfig is None:
            return None
        if value in devConfig:
            v = devConfig[value]
            if v is not None and len(v) > 0 and v != "None":
                return v
        return None


    def Rekey(self, reason:str) -> None:
        self.Logger.error("HOST REKEY CALLED %s - Clearing keys...", reason)
        self.Secrets.SetPrinterId(None)
        self.Secrets.SetPrivateKey(None)
        self.Logger.error("Key clear complete, restarting plugin.")
        HostCommon.RestartPlugin()


    def ShowUiPopup(self, title:str, text:str, msgType:str, actionText:Optional[str], actionLink:Optional[str], showForSec:int, onlyShowIfLoadedViaOeBool:bool) -> None:
        ElegooCc2Client.Get().SendFrontendPopupMsg(title, text, msgType, actionText, actionLink, showForSec, onlyShowIfLoadedViaOeBool)


    def OnPrimaryConnectionEstablished(self, octoKey:str, connectedAccounts:List[str]) -> None:
        self.Logger.info("Primary Connection To OctoEverywhere Established - We Are Ready To Go!")
        LocalWebApi.Get().OnPrimaryConnectionEstablished(len(connectedAccounts) > 0)

        self.NotificationHandler.SetOctoKey(octoKey)

        if len(connectedAccounts) == 0:
            printerId = self.GetPrinterId()
            if printerId is not None:
                LinkHelper.RunLinkPluginConsolePrinterAsync(self.Logger, printerId, "elegoo_cc2_host")


    def OnPluginUpdateRequired(self) -> None:
        self.Logger.error("!!! A Plugin Update Is Required -- If This Plugin Isn't Updated It Might Stop Working !!!")
        self.Logger.error("!!! Please SSH into the device running this plug-in and run the update script or update the docker container!  !!!")


    def OnRekeyRequired(self) -> None:
        self.Rekey("Handshake Failed")


    def OnRekeyCommand(self) -> bool:
        self.Rekey("Command")
        return True
