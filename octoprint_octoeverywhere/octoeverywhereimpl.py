import time
import random
import threading

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
    IsDisconnecting = False
    ActiveSessionId = 0

    # Summon vars
    SummonServerTimeout = None
    SummonServerConnectUrl = ""

    # Must be > 0 or the increment logic will fail (since it's value X 2)
    # We want to keep this low though, so incase the connection closes due to a OctoStream error,
    # we will reconnect quickly again. Remember we always add the random reconnect time as well.
    WsConnectBackOffSec_Default = 1
    # We always add a random second count to the reconnect sleep to add variance. This is the min value.
    WsConnectRandomMinSec = 2
    # We always add a random second count to the reconnect sleep to add variance. This is the max value.
    WsConnectRandomMaxSec = 10
    
    Ws = None
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
        self.Logger.info("Connected To Octo Everywhere, session "+str(self.ActiveSessionId)+". Starting handshake...")

        # Create a new session for this websocket connection.
        self.OctoSession = OctoSession(self, self.Logger, self.PrinterId, self.ActiveSessionId, self.OctoPrintLocalPort, self.MjpgStreamerLocalPort, self.UiPopupInvoker, self.PluginVersion)
        self.OctoSession.StartHandshake()

    def OnClosed(self, ws):
        self.Logger.info("Service websocket closed.")

    def OnError(self, ws, err):
        self.Logger.error("OctoEverywhere Ws error: " +str(err))

    def OnMsg(self, ws, msg):
        if self.OctoSession :
            # Grab the session id now, since it can change by the time this call is done.
            # For example, if this call creates an error that ends up shutting down the ws.
            localSessionId = self.ActiveSessionId
            try:
                self.OctoSession.HandleMessage(msg)
            except Exception as e:
                self.Logger.error("Exception in OctoSession.HandleMessage " + str(e))
                self.OnSessionError(localSessionId, 0)

    def OnHandshakeComplete(self, sessionId):
        if sessionId != self.ActiveSessionId:
            self.Logger.info("Got a handshake complete for an old session, "+str(sessionId)+", ignoring.")
            return

        self.Logger.info("Handshake complete, session "+str(sessionId)+", successfully connected to OctoEverywhere!")

        # Only set the back off when we are done with the handshake and it was successful.
        self.WsConnectBackOffSec = self.WsConnectBackOffSec_Default
    
    # Called by the session if we should kill this socket.
    def OnSessionError(self, sessionId, backoffModifierSec):
        if sessionId != self.ActiveSessionId:
            self.Logger.info("Got a session error callback for an old session, "+str(sessionId)+", ignoring.")
            return

        # If a back off modifer is supplied, we should add it to the current backoff.
        # This is driven by the service when it asks us to back off in our connection time.
        if backoffModifierSec > 0:
            self.WsConnectBackOffSec += backoffModifierSec

        self.Logger.error("Session reported an error, closing the websocket. Backoff time sec: " + str(self.WsConnectBackOffSec))

        # Shut things down
        self.Disconnect()

    # A summon request can be sent by the services if the user is connected to a different
    # server than we are connected to. In such a case, we will go connect to the requested
    # server so that our connection is super speedy!
    def OnSummonRequest(self, sessionId, summonConnectUrl):
        if sessionId != self.ActiveSessionId:
            self.Logger.info("Got a summon command for an old session, "+str(sessionId)+", ignoring.")
            return

        self.Logger.info("We have been summoned by "+summonConnectUrl+"! Let's go say hi!")
        self.SummonServerConnectUrl = summonConnectUrl

        # Call shutdown so we reconnect over to the new server
        self.Disconnect()

    def Disconnect(self):  
        # If we have already gotten the disconnect signal, ingore future requests.
        # This can happen because disconnecting might case proxy socket errors, for example
        # if we closed all of the sockets locally and then the server tries to close one.   
        if self.IsDisconnecting == True:
            self.Logger.info("Ignoring the session disconnect command because we are already working on it.")
            return
        self.IsDisconnecting = True        

        # Try to close all of the sockets before we disconnect, so we send the messages.
        if self.OctoSession:
            self.OctoSession.CloseAllProxySockets()

        # Close the websocket, which will cause the run loop to spin and reconnect.
        if self.Ws:
            self.Ws.Close()

    # This timer is used to reset back to the original server after some time of being summoned.
    def OnSummonServerTimeoutCallback(self):
        try:
            self.Logger.info("Server summon timeout fired, switching back to the default server.")

            # If there is a timer, stop it now.
            localTimer = self.SummonServerTimeout
            self.SummonServerTimeout = None
            if localTimer != None:
                localTimer.cancel()

            # Call disconnect to shutdown the connection, and we will auto reconnect to the default domain.
            self.Disconnect()
        except Exception as e:
            self.Logger.error("Exception in OnSummonServerTimeoutCallback: " + str(e))
    
    def RunBlocking(self):
        while 1:
            # Since we want to run forever, we want to make sure any exceptions get caught but then we try again.
            try:

                # Before we connect, see if we have been summoned to a specific server.
                localEndpoint = self.Endpoint
                if len(self.SummonServerConnectUrl) > 0:
                    # If so, set our endpoint to be the summon server.
                    localEndpoint = self.SummonServerConnectUrl
                    # Clear out the summon subdomain so we only attempt this once.
                    self.SummonServerConnectUrl = ""

                    # Also start a timer so we eventually default back to the normal server.
                    timeoutSec = 12 * 60 * 60 # switch back to default after half a day.
                    self.SummonServerTimeout = threading.Timer(timeoutSec, self.OnSummonServerTimeoutCallback)
                    self.SummonServerTimeout.start()

                # Clear the disconnecting flag.
                # We do this just before connects, because this flag weeds out all of the error noise
                # that might happen while we are performing a disconnect. But at this time, all of that should be
                # 100% done now.
                self.IsDisconnecting = False

                # Since there can be old pending actions from old sessions (session == one websocket connection).
                # We will keep track of the current session, so old errors from sessions don't effect the new one.
                self.ActiveSessionId += 1

                # Connect to the service.
                self.Logger.info("Attempting to talk to OctoEverywhere, session "+str(self.ActiveSessionId)+", url " + str(localEndpoint))
                self.Ws = Client(localEndpoint, self.OnOpened, self.OnMsg, None, self.OnClosed, self.OnError)
                self.Ws.RunUntilClosed()

                # Handle disconnects            
                self.Logger.info("Disconnected from OctoEverywhere, session "+str(self.ActiveSessionId))

                # Ensure all proxy sockets are closed.
                if self.OctoSession:
                    self.OctoSession.CloseAllProxySockets()

            except Exception as e:
                self.Logger.error("Exception in OctoEverywhere's main RunBlocking function. session:"+str(self.ActiveSessionId)+" ex:" + str(e))
                time.sleep(20)

            # If a summon timeout is runing, cancel it since we always default back to the main hostname on a new connection.
            localTimer = self.SummonServerTimeout
            self.SummonServerTimeout = None
            if localTimer != None:
                localTimer.cancel()

            # If we have been summoned, instantly try to connect, don't wait a backoff.
            if len(self.SummonServerConnectUrl) > 0:
                continue

            # We have a back off time, but always add some random noise as well so not all client try to use the exact
            # same time.
            self.WsConnectBackOffSec += random.randint(self.WsConnectRandomMinSec, self.WsConnectRandomMaxSec)            
            
            # Sleep before incrmenting, so on the first failure we instantly try again.
            self.Logger.info("Sleeping for " + str(self.WsConnectBackOffSec) + " seconds before trying again.")
            time.sleep(self.WsConnectBackOffSec)

            # Increment
            self.WsConnectBackOffSec *= 2
            if self.WsConnectBackOffSec > 180 :
                self.WsConnectBackOffSec = 180                

    def SendMsg(self, msgBytes):
        self.Ws.Send(msgBytes, True)
