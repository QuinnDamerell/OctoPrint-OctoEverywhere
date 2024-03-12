import logging
import signal
import sys
import random
import string

from octoeverywhere.webcamhelper import WebcamHelper
from octoeverywhere.octoeverywhereimpl import OctoEverywhere
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.commandhandler import CommandHandler
from octoeverywhere.octopingpong import OctoPingPong
from octoeverywhere.telemetry import Telemetry
from octoeverywhere.sentry import Sentry
from octoeverywhere.mdns import MDns
from octoeverywhere.notificationshandler import NotificationsHandler
from octoeverywhere.Proto.ServerHost import ServerHost
from octoeverywhere.printinfo import PrintInfoManager
from octoeverywhere.compat import Compat
#from .threaddebug import ThreadDebug

from .localauth import LocalAuth
from .slipstream import Slipstream
from .smartpause import SmartPause
from .octoprintwebcamhelper import OctoPrintWebcamHelper



#
# This file is used for development purposes. It can run the system outside of teh OctoPrint env.
#
# Use the following vars to configure the OctoEverywhere server address and the local OctoPrint address
# Use None if you don't want to overwrite the defaults.
#



# For local setups, use these vars to configure things.
LocalServerAddress = None
#LocalServerAddress = "octoeverywhere.dev"

OctoPrintIp = None
OctoPrintIp = "192.168.1.12"

OctoPrintPort = None
OctoPrintPort = 80

# Define a printer id and private key
PrinterId = "0QVGBOO92TENVOVN9XW5T3KT6LV1XV8ODFUEQYWQ"
PrivateKey = "uduuitfqrsstnhhjpsxhmyqwvpxgnajqqbhxferoxunusjaybodfotkupjaecnccdxzwmeajqqmjftnhoonusnjatqcryxfvrzgibouexjflbrmurkhltmsd"

# Defines a place we can write files
PluginFilePathRoot = "C:\\Users\\quinn"

# A mock of the popup UI interface.
class UiPopupInvokerStub():
    def __init__(self, logger):
        self.Logger = logger

    def ShowUiPopup(self, title:str, text:str, msgType:str, actionText:str, actionLink:str, showForSec:int, onlyShowIfLoadedViaOeBool:bool):
        self.Logger.info("Client Notification Received. Title:"+title+"; Text:"+text+"; Type:"+msgType+"; showForSec:"+str(showForSec))



# Implements a common interface shared by OctoPrint and Moonraker.
class MockPrinterStateObject:

    def __init__(self, logger):
        self.Logger = logger

    # ! Interface Function ! The entire interface must change if the function is changed.
    # This function will get the estimated time remaining for the current print.
    # Returns -1 if the estimate is unknown.
    def GetPrintTimeRemainingEstimateInSeconds(self):
        # We failed.
        return -1


    # ! Interface Function ! The entire interface must change if the function is changed.
    # If the printer is warming up, this value would be -1. The First Layer Notification logic depends upon this!
    # Returns the current zoffset if known, otherwise -1.
    def GetCurrentZOffset(self):
        # Failed to find it.
        return -1

    # ! Interface Function ! The entire interface must change if the function is changed.
    # If this platform DOESN'T support getting the layer info from the system, this returns (None, None)
    # If the platform does support it...
    #     If the current value is unknown, (0,0) is returned.
    #     If the values are known, (currentLayer(int), totalLayers(int)) is returned.
    #          Note that total layers will always be > 0, but current layer can be 0!
    def GetCurrentLayerInfo(self):
        return (None, None)


    # ! Interface Function ! The entire interface must change if the function is changed.
    # Returns True if the printing timers (notifications and gadget) should be running, which is only the printing state. (not even paused)
    # False if the printer state is anything else, which means they should stop.
    def ShouldPrintingTimersBeRunning(self):
        return False


    # ! Interface Function ! The entire interface must change if the function is changed.
    # If called while the print state is "Printing", returns True if the print is currently in the warm-up phase. Otherwise False
    def IsPrintWarmingUp(self):
        return False


# A mock of the popup UI interface.
NotificationHandlerInstance = None
class StatusChangeHandlerStub():
    def __init__(self, logger, printerId):
        self.Logger = logger
        self.PrinterId = printerId

    def OnPrimaryConnectionEstablished(self, octoKey, connectedAccounts):
        self.Logger.info("OnPrimaryConnectionEstablished - Connected Accounts:"+str(connectedAccounts) + " - OctoKey:"+str(octoKey))

        # Setup the notification handler
        NotificationHandlerInstance.SetOctoKey(octoKey)
        NotificationHandlerInstance.SetPrinterId(self.PrinterId)

        # Send a test notifications if desired.
        if LocalServerAddress is not None:
            NotificationHandlerInstance.SetServerProtocolAndDomain("http://"+LocalServerAddress)
            NotificationHandlerInstance.SetGadgetServerProtocolAndDomain("http://"+LocalServerAddress)
        #NotificationHandlerInstance.OnStarted("test.gcode")
        #NotificationHandlerInstance.OnFailed("file name thats very long and too long for things.gcode", 20.2, "error")
        # NotificationHandlerInstance.OnPrintProgress(95, 0)
        # time.sleep(10)
        # NotificationHandlerInstance.OnPrintProgress(97, 0)
        # NotificationHandlerInstance.OnDone("filename.gcode", "304458605")
        #NotificationHandlerInstance.OnPaused("filename.gcode")
        #NotificationHandlerInstance.OnResume("filename.gcode")
        # NotificationHandlerInstance.OnError("test error string")
        # NotificationHandlerInstance.OnError("test error string")
        # NotificationHandlerInstance.OnError("test error string")
        #handler.OnFilamentChange()
        #handler.OnPrintProgress(20)

    def OnPluginUpdateRequired(self):
        self.Logger.info("On plugin update required message.")

def SignalHandler(sig, frame):
    print('Ctrl+C Pressed, Exiting!')
    sys.exit(0)

def GeneratePrinterId():
    return ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(40))


if __name__ == '__main__':

    # Setup the logger.
    logger = logging.getLogger("octoeverywhere")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Set our compat mode
    Compat.SetIsOctoPrint(True)

    # Init Sentry, but it won't report since we are in dev mode.
    Sentry.SetLogger(logger)
    Sentry.Setup("0.0.0", "dev", True, False)
    Telemetry.Init(logger)
    if LocalServerAddress is not None:
        Telemetry.SetServerProtocolAndDomain("http://"+LocalServerAddress)

    # Init the mdns client
    MDns.Init(logger, PluginFilePathRoot)
    #MDns.Get().Test()

    # This is a tool to help track stuck or leaked threads.
    #threadDebugger = ThreadDebug()
    #threadDebugger.Start(logger, 30)

    # Setup a signal handler to kill everything
    signal.signal(signal.SIGINT, SignalHandler)

    # Dev props
    OctoEverywhereWsUri = "ws://starport-v1.octoeverywhere.com/octoclientws"

    # Setup the http requester
    OctoHttpRequest.SetLocalHttpProxyPort(80)
    OctoHttpRequest.SetLocalHttpProxyIsHttps(False)
    OctoHttpRequest.SetLocalOctoPrintPort(5000)

    # Overwrite local dev props
    if OctoPrintIp is not None:
        OctoHttpRequest.SetLocalHostAddress(OctoPrintIp)
    if OctoPrintPort is not None:
        OctoHttpRequest.SetLocalOctoPrintPort(OctoPrintPort)
    if LocalServerAddress is not None:
        OctoEverywhereWsUri = "ws://"+LocalServerAddress+"/octoclientws"

    # Init the ping pong helper.
    OctoPingPong.Init(logger, PluginFilePathRoot, PrinterId)
    # If we are using a local dev connection, disable this or it will overwrite.
    if LocalServerAddress is not None:
        OctoPingPong.Get().DisablePrimaryOverride()

    # Setup the print info manager before the notification manager
    PrintInfoManager.Init(logger, PluginFilePathRoot)

    # Setup the notification handler.
    NotificationHandlerInstance = NotificationsHandler(logger, MockPrinterStateObject(logger))

    # Setup the api command handler if needed for testing.
    CommandHandler.Init(logger, NotificationHandlerInstance, None)
    # Note this will throw an exception because we don't have a flask context setup.
    # result = apiCommandHandler.HandleApiCommand("status", None)
    # Setup the command handler

    # Setup the snapshot helper
    WebcamHelper.Init(logger, OctoPrintWebcamHelper(logger, None), PluginFilePathRoot)

    # These 3 classes are OctoPrint specific!
    # The order matters, LocalAuth needs to be init before Slipstream.
    LocalAuth.Init(logger, None)
    LocalAuth.Get().SetApiKeyForTesting("SuperSecureApiKey")
    Slipstream.Init(logger)
    SmartPause.Init(logger, None, None)

    uiPopInvoker = UiPopupInvokerStub(logger)
    statusHandler = StatusChangeHandlerStub(logger, PrinterId)
    oe = OctoEverywhere(OctoEverywhereWsUri, PrinterId, PrivateKey, logger, uiPopInvoker, statusHandler, "1.10.20", ServerHost.OctoPrint, False)
    oe.RunBlocking()
