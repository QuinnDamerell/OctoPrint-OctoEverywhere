import logging
import signal
import sys
import random
import string

from base64 import b64encode
from os import urandom

from .octoeverywhereimpl import OctoEverywhere

#
# This file is used for development purposes. It can run the system outside of teh OctoPrtint env.
# 

# A mock of the popup UI interface.
class UiPopupInvokerStub():
    Logger = None

    def __init__(self, logger):
        self.Logger = logger

    def ShowUiPopup(self, title, text, type, autoHide):
        self.Logger.info("Client Notification Received. Title:"+title+"; Text:"+text+"; Type:"+type+"; AutoHide:"+str(autoHide))

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

    # Setup a signal handler to kill everything
    signal.signal(signal.SIGINT, SignalHandler)

    devId = GeneratePrinterId()
    OctoEverywhereWsUri = "wss://octoeverywhere.com/octoclientws"    
    #OctoEverywhereWsUri = "ws://192.168.86.74:5000/octoclientws"
    OctoPrintLocalPort = 5000
    MjpgStreamerLocalPort = 8080
    uiPopInvoker = UiPopupInvokerStub(logger)
    oe = OctoEverywhere(OctoEverywhereWsUri, OctoPrintLocalPort, MjpgStreamerLocalPort, devId, logger, uiPopInvoker, "dev")
    oe.RunBlocking()