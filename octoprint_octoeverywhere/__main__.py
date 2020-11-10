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
    #OctoEverywhereWsUri = "ws://192.168.1.142:5000/octoclientws"
    OctoPrintLocalPort = 5000
    oe = OctoEverywhere(OctoEverywhereWsUri, OctoPrintLocalPort, devId, logger)
    oe.RunBlocking()