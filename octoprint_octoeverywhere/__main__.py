import logging
import signal
import sys
import random
import string

from base64 import b64encode
from os import urandom

from .octoeverywhereimpl import OctoEverywhere
from .octohttprequest import OctoHttpRequest
from .notificationshandler import NotificationsHandler
from .threaddebug import ThreadDebug

#
# This file is used for development purposes. It can run the system outside of teh OctoPrint env.
# 

# A mock of the popup UI interface.
class UiPopupInvokerStub():
    def __init__(self, logger):
        self.Logger = logger

    def ShowUiPopup(self, title, text, type, autoHide):
        self.Logger.info("Client Notification Received. Title:"+title+"; Text:"+text+"; Type:"+type+"; AutoHide:"+str(autoHide))

# A mock of the popup UI interface.
class StatusChangeHandlerStub():
    def __init__(self, logger, printerId):
        self.Logger = logger
        self.PrinterId = printerId

    def OnPrimaryConnectionEstablished(self, octoKey, connectedAccounts):
        self.Logger.info("OnPrimaryConnectionEstablished - Connected Accounts:"+str(connectedAccounts) + " - OctoKey:"+str(octoKey))

        # Send a test notifications if desired.
        #handler = NotificationsHandler(self.Logger)
        #handler.SetOctoKey(octoKey)
        #handler.SetPrinterId(self.PrinterId)
        #handler.SetServerProtocolAndDomain("http://127.0.0.1")
        #handler.OnStarted("test.gcode")
        #handler.OnFailed("file name thats very long and too long for things.gcode", 20.2, "error")   
        #handler.OnDone("filename.gcode", 304458605)   
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
    #OctoEverywhereWsUri = "ws://192.168.86.74:5000/octoclientws"
    printerId = "0QVGBOO92TENVOVN9XW5T3KT6LV1XV8ODFUEQYWQ"

    # Setup the http requester
    OctoHttpRequest.SetLocalHttpProxyPort(80)
    OctoHttpRequest.SetLocalHttpProxyIsHttps(False)
    OctoHttpRequest.SetLocalOctoPrintPort(5000)

    uiPopInvoker = UiPopupInvokerStub(logger)
    statusHandler = StatusChangeHandlerStub(logger, printerId)
    oe = OctoEverywhere(OctoEverywhereWsUri, printerId, logger, uiPopInvoker, statusHandler, "1.0.4")
    oe.RunBlocking()
