import time

from .websocketimpl import Client
from .octosessionimpl import OctoSession

# 
# This is the main running class that will connect and keep a connection to the service.
#
class OctoEverywhere:
    ProtocolVersion = 1
    Logger = None
    Endpoint = ""
    PrinterId = ""
    OctoSession = None

    Ws = None
    WsConnectBackOffSec = 5

    def __init__(self, endpoint, printerId, logger):
        self.Logger = logger
        self.PrinterId = printerId
        self.Endpoint = endpoint

    def OnOpened(self, ws):
        self.Logger.info("Connected To Octo Everywhere. Starting handshake...")

        # Create a new session for this websocket connection.
        self.OctoSession = OctoSession(self, self.Logger, self.PrinterId)
        self.OctoSession.StartHandshake()

    def OnHandshakeComplete(self):
        self.Logger.info("Handshake complete, successfully connected to OctoEverywhere!")

        # Only set the back off when we are done with the handshake
        self.WsConnectBackOffSec = 5

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
                time.sleep(5)
            
            # Sleep before incrmenting, so on the first failure we instantly try again.
            self.Logger.info("Sleeping for " + str(self.WsConnectBackOffSec) + " seconds before trying again.")
            time.sleep(self.WsConnectBackOffSec)

            # Increment
            self.WsConnectBackOffSec *= 2
            if self.WsConnectBackOffSec > 180 :
                self.WsConnectBackOffSec = 180                

    def SendMsg(self, msgBytes):
        self.Ws.Send(msgBytes, True)
