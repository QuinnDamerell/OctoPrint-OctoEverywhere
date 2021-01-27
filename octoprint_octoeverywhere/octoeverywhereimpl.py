import time
import random

from .websocketimpl import Client
from .octosessionimpl import OctoSession

# 
# This is the main running class that will connect and keep a connection to the service.
#
class OctoEverywhere:
    ProtocolVersion = 1
    OctoPrintLocalPort = 80
    MjpgStreamerLocalPort = 8080
    Logger = None
    UiPopupInvoker = None
    Endpoint = ""
    PrinterId = ""
    OctoSession = None
    PluginVersion = ""

    Ws = None
    WsConnectBackOffSec_Default = 5
    WsConnectBackOffSec = WsConnectBackOffSec_Default

    def __init__(self, endpoint, octoPrintLocalPort, mjpgStreamerLocalPort, printerId, logger, uiPopupInvoker, pluginVersion):
        self.Logger = logger
        self.PrinterId = printerId
        self.Endpoint = endpoint
        self.OctoPrintLocalPort = octoPrintLocalPort
        self.MjpgStreamerLocalPort = mjpgStreamerLocalPort
        self.UiPopupInvoker = uiPopupInvoker
        self.PluginVersion = pluginVersion

    def OnOpened(self, ws):
        self.Logger.info("Connected To Octo Everywhere. Starting handshake...")

        # Create a new session for this websocket connection.
        self.OctoSession = OctoSession(self, self.Logger, self.PrinterId, self.OctoPrintLocalPort, self.MjpgStreamerLocalPort, self.UiPopupInvoker, self.PluginVersion)
        self.OctoSession.StartHandshake()

    def OnHandshakeComplete(self):
        self.Logger.info("Handshake complete, successfully connected to OctoEverywhere!")

        # Only set the back off when we are done with the handshake and it was successful.
        self.WsConnectBackOffSec = self.WsConnectBackOffSec_Default

    def OnClosed(self, ws):
        self.Logger.info("Service websocket closed.")

    def OnError(self, ws, err):
        self.Logger.error("OctoEverywhere Ws error: " +str(err))

    def OnMsg(self, ws, msg):
        if self.OctoSession :
            try:
                self.OctoSession.HandleMessage(msg)
            except Exception as e:
                self.Logger.error("Exception in OctoSession.HandleMessage " + str(e))
                self.OnSessionError(0)
    
    # Called by the session if we should kill this socket.
    def OnSessionError(self, backoffModifierSec):

        # If a back off modifer is supplied, we should add it to the current backoff.
        # This is driven by the service when it asks us to back off in our connection time.
        if backoffModifierSec > 0:
            self.WsConnectBackOffSec += backoffModifierSec

        self.Logger.error("Session reported an error, closing the websocket. Backoff time sec: " + str(self.WsConnectBackOffSec))

        # Try to close all of the sockets before we disconnect, so we send the messages.
        if self.OctoSession:
            self.OctoSession.CloseAllProxySockets()

        if self.Ws:
            self.Ws.Close()
    
    def RunBlocking(self):
        while 1:
            # Since we want to run forever, we want to make sure any exceptions get caught but then we try again.
            try:
                # Connect to the service.
                self.Logger.info("Attempting to talk to Octo Everywhere. " + str(self.Endpoint))
                self.Ws = Client(self.Endpoint, self.OnOpened, self.OnMsg, None, self.OnClosed, self.OnError)
                self.Ws.RunUntilClosed()

                # Handle disconnects            
                self.Logger.info("Disconnected from Octo Everywhere")

                # Ensure all proxy sockets are closed.
                if self.OctoSession:
                    self.OctoSession.CloseAllProxySockets()

            except Exception as e:
                self.Logger.error("Exception in OctoEverywhere's main RunBlocking function. " + str(e))
                time.sleep(20)

            # We have a back off time, but always add some random noise as well so not all client try to use the exact
            # same time.
            self.WsConnectBackOffSec += random.randint(5, 20)            
            
            # Sleep before incrmenting, so on the first failure we instantly try again.
            self.Logger.info("Sleeping for " + str(self.WsConnectBackOffSec) + " seconds before trying again.")
            time.sleep(self.WsConnectBackOffSec)

            # Increment
            self.WsConnectBackOffSec *= 2
            if self.WsConnectBackOffSec > 180 :
                self.WsConnectBackOffSec = 180                

    def SendMsg(self, msgBytes):
        self.Ws.Send(msgBytes, True)
