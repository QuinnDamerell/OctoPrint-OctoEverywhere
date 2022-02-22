import logging
import signal
import sys
import random
import string

from octoprint_octoeverywhere.localauth import LocalAuth
from octoprint_octoeverywhere.snapshothelper import SnapshotHelper

from .octoeverywhereimpl import OctoEverywhere
from .octohttprequest import OctoHttpRequest
from .threaddebug import ThreadDebug
from .octopingpong import OctoPingPong
from .slipstream import Slipstream
#from .notificationshandler import NotificationsHandler

#
# This file is used for development purposes. It can run the system outside of teh OctoPrint env.
#

# A mock of the popup UI interface.
class UiPopupInvokerStub():
    def __init__(self, logger):
        self.Logger = logger

    def ShowUiPopup(self, title, text, msgType, autoHide):
        self.Logger.info("Client Notification Received. Title:"+title+"; Text:"+text+"; Type:"+msgType+"; AutoHide:"+str(autoHide))

# A mock of the popup UI interface.
class StatusChangeHandlerStub():
    def __init__(self, logger, printerId):
        self.Logger = logger
        self.PrinterId = printerId

    def OnPrimaryConnectionEstablished(self, octoKey, connectedAccounts):
        self.Logger.info("OnPrimaryConnectionEstablished - Connected Accounts:"+str(connectedAccounts) + " - OctoKey:"+str(octoKey))

        # Send a test notifications if desired.
        # handler = NotificationsHandler(self.Logger)
        # handler.SetOctoKey(octoKey)
        # handler.SetPrinterId(self.PrinterId)
        #handler.SetServerProtocolAndDomain("http://127.0.0.1")
        #handler.OnStarted("test.gcode")
        #handler.OnFailed("file name thats very long and too long for things.gcode", 20.2, "error")
        #handler.OnDone("filename.gcode", "304458605")
        #handler.OnPaused("filename.gcode")
        #handler.OnResume("filename.gcode")
        #handler.OnError("test error string")
        #handler.OnZChange()
        #handler.OnZChange()
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

    # This is a tool to help track stuck or leaked threads.
    threadDebugger = ThreadDebug()
    threadDebugger.Start(logger, 30)

    # Setup a signal handler to kill everything
    signal.signal(signal.SIGINT, SignalHandler)

    # Dev props
    printerId = GeneratePrinterId()
    OctoEverywhereWsUri = "wss://starport-v1.octoeverywhere.com/octoclientws"

    # Setup the http requester
    OctoHttpRequest.SetLocalHttpProxyPort(80)
    OctoHttpRequest.SetLocalHttpProxyIsHttps(False)
    OctoHttpRequest.SetLocalOctoPrintPort(5000)

    # Special - Dev Env Setup
    printerId = "0QVGBOO92TENVOVN9XW5T3KT6LV1XV8ODFUEQYWQ"
    OctoHttpRequest.SetLocalhostAddress("192.168.86.57")
    OctoHttpRequest.SetLocalOctoPrintPort(80)
    #OctoEverywhereWsUri = "ws://192.168.86.74:80/octoclientws"

    # Setup the local auth healper
    LocalAuth.Init(logger, None)
    LocalAuth.Get().SetApiKeyForTesting("SuperSecureApiKey")

    # Init the ping pong helper.
    OctoPingPong.Init(logger, "C:\\Users\\quinn")

    # Setup the snapshot helper
    SnapshotHelper.Init(logger, None)

    # Init slipstream - This must be inited after localauth
    Slipstream.Init(logger)

    uiPopInvoker = UiPopupInvokerStub(logger)
    statusHandler = StatusChangeHandlerStub(logger, printerId)
    oe = OctoEverywhere(OctoEverywhereWsUri, printerId, logger, uiPopInvoker, statusHandler, "1.5.2")
    oe.RunBlocking()
