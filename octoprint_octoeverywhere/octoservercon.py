import time
import random
from datetime import datetime

from .websocketimpl import Client
from .octosessionimpl import OctoSession
from .repeattimer import RepeatTimer

#
# This class is responsible for connecting and maintaining a connection to a server.
# This includes making manaing all of the websocket connections and making sure they are cleaned up
# Handling disconnects, errors, backoff, and retry logic.
# Handling RunFor logic which limits how long a server connection stays active.
#
class OctoServerCon:

    # The RunFor system allows the host to specify how long this server connection should be active.
    # This time includes all valid connections and disconnects. Simply put, after x amount of time, the class
    # should be cleaned up and RunBlocking will return.
    #
    # This functionally is used to occasionally make the printer refresh it's primary server, because over time the
    # best server connection might change. When the server reconnects it will resolve printer connect hostname again
    # which will route it to the best server.
    # This feature is also used for secondary connections, which allows the printer to connect ot multiple servers at once.
    # Secondary server connections are used when a shared connection url resolves to a different server than we are currently connected.
    #
    # The run for system accounts for user activity, and will allow extra time after the run for time if the user is still using the
    # connection.

    # How frequency we check if RunFor is done.
    RunForTimeCheckerIntervalSec = 60 * 2 # 2 minutes

    # The min amount of time from the last user activity RunFor will wait before disconnecting.
    RunForMinTimeSinceLastUserActivitySec = 60 * 10 # 10 minutes.

    # The max amount of time beyond the RunFor limit we will wait for user activity to stop.
    RunForMaxUserActivityWaitTimeSec = 60 * 60 * 2 # 2 hours.


    # Must be > 0 or the increment logic will fail (since it's value X 2)
    # We want to keep this low though, so incase the connection closes due to a OctoStream error,
    # we will reconnect quickly again. Remember we always add the random reconnect time as well.
    WsConnectBackOffSec_Default = 1
    # We always add a random second count to the reconnect sleep to add variance. This is the min value.
    WsConnectRandomMinSec = 2
    # We always add a random second count to the reconnect sleep to add variance. This is the max value.
    WsConnectRandomMaxSec = 10

    def __init__(self, host, endpoint, isPrimaryConnection, printerId, privateKey, logger, uiPopupInvoker, statusChangeHandler, pluginVersion, runForSeconds):
        self.ProtocolVersion = 1
        self.OctoSession = None
        self.IsDisconnecting = False
        self.ActiveSessionId = 0
        self.Ws = None
        self.WsConnectBackOffSec = self.WsConnectBackOffSec_Default

        self.Host = host
        self.Logger = logger
        self.IsPrimaryConnection = isPrimaryConnection
        self.PrinterId = printerId
        self.PrivateKey = privateKey
        self.Endpoint = endpoint
        self.UiPopupInvoker = uiPopupInvoker
        self.PluginVersion = pluginVersion

        # Note! Will be None for secondary connections!
        self.StatusChangeHandler = statusChangeHandler

        # Setup RunFor
        self.RunForSeconds = runForSeconds
        self.CreationTime = datetime.now()
        self.LastUserActivityTime = self.CreationTime
        # Start the RunFor time checker.
        self.RunForTimeChecker = RepeatTimer(self.Logger, self.RunForTimeCheckerIntervalSec, self.OnRunForTimerCallback)
        self.RunForTimeChecker.start()

    def Cleanup(self):
        # Stop the RunFor time checker if we have one.
        if self.RunForTimeChecker is not None:
            self.RunForTimeChecker.Stop()

    # Returns a printable string that says the endpoint and the active session id.
    def GetConnectionString(self):
        return str(self.Endpoint)+"["+str(self.ActiveSessionId)+"]"

    def OnOpened(self, ws):
        self.Logger.info("Connected To OctoEverywhere, server con "+self.GetConnectionString()+". Starting handshake...")

        # Create a new session for this websocket connection.
        self.OctoSession = OctoSession(self, self.Logger, self.PrinterId, self.PrivateKey, self.IsPrimaryConnection, self.ActiveSessionId, self.UiPopupInvoker, self.PluginVersion)
        self.OctoSession.StartHandshake()

    def OnClosed(self, ws):
        self.Logger.info("Service websocket closed.")

    def OnError(self, ws, err):
        self.Logger.error("OctoEverywhere Ws error: " +str(err))

    def OnMsg(self, ws, msg):
        # When we get any message, consider it user activity.
        self.LastUserActivityTime = datetime.now()

        if self.OctoSession :
            # Grab the session id now, since it can change by the time this call is done.
            # For example, if this call creates an error that ends up shutting down the ws.
            localSessionId = self.ActiveSessionId
            try:
                self.OctoSession.HandleMessage(msg)
            except Exception as e:
                self.Logger.error("Exception in OctoSession.HandleMessage " + self.GetConnectionString() + " :" + str(e))
                self.OnSessionError(localSessionId, 0)

    def OnHandshakeComplete(self, sessionId, octoKey, connectedAccounts):
        if sessionId != self.ActiveSessionId:
            self.Logger.info("Got a handshake complete for an old session, "+str(sessionId)+", ignoring.")
            return

        self.Logger.info("Handshake complete, server con "+self.GetConnectionString()+", successfully connected to OctoEverywhere!")

        # Only primary connections have this handler.
        if self.StatusChangeHandler is not None:
            self.StatusChangeHandler.OnPrimaryConnectionEstablished(octoKey, connectedAccounts)

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

        self.Logger.error("Session reported an error ["+self.GetConnectionString()+"], closing the websocket. Backoff time sec: " + str(self.WsConnectBackOffSec))

        # Shut things down
        self.Disconnect()

    # Called by the server con if the plugin needs to be updated. The backoff time will be set very high
    # and this notification will be handled by the UI to show the user a message.
    def OnPluginUpdateRequired(self):
        # This will be null for secondary connections
        if self.StatusChangeHandler is not None:
            self.StatusChangeHandler.OnPluginUpdateRequired()

    # A summon request can be sent by the services if the user is connected to a different
    # server than we are connected to. In such a case we will multi connect a temp non-primary connection
    # to the request server as well, that will be to service the user.
    def OnSummonRequest(self, sessionId, summonConnectUrl):
        self.Host.OnSummonRequest(summonConnectUrl)

    def Disconnect(self):
        # If we have already gotten the disconnect signal, ingore future requests.
        # This can happen because disconnecting might case proxy socket errors, for example
        # if we closed all of the sockets locally and then the server tries to close one.
        if self.IsDisconnecting is True:
            self.Logger.info("Ignoring the session disconnect command because we are already working on it.")
            return
        self.IsDisconnecting = True

        # Try to close all of the sockets before we disconnect, so we send the messages.
        if self.OctoSession:
            self.OctoSession.CloseAllWebStreamsAndDisable()

        self.Logger.info("OctoServerCon websocket close start")

        # Close the websocket, which will cause the run loop to spin and reconnect.
        if self.Ws:
            self.Ws.Close()

        self.Logger.info("OctoServerCon disconnect complete.")

    # Returns if the RunFor time has expired, including considering user activity.
    def IsRunForTimeComplete(self):
        # Check if we are past our RunFor time.
        hasRanFor = datetime.now() - self.CreationTime
        if hasRanFor.total_seconds() > self.RunForSeconds:
            # Check the last user activity.
            timeSinceUserActivity = datetime.now() - self.LastUserActivityTime
            if timeSinceUserActivity.total_seconds() > self.RunForMinTimeSinceLastUserActivitySec:
                # We have passed the RunFor time and the min amount of time since the last user activity.
                self.Logger.info("Server con "+self.GetConnectionString()+" IS past it's RunFor time "+str(hasRanFor)+" and IS past it's time since last user activity "+str(timeSinceUserActivity))
                return True
            else:
                # Check how long we have been waiting on user activity.
                timeSinceRunForShouldHaveEnded = hasRanFor.total_seconds() - self.RunForSeconds
                if timeSinceRunForShouldHaveEnded > self.RunForMaxUserActivityWaitTimeSec:
                    self.Logger.info("Server con "+self.GetConnectionString()+" IS past it's RunFor time "+str(hasRanFor)+", but IS NOT past it's time since last user activity "+str(timeSinceUserActivity) + " BUT we have exceeded the max user activity time.")
                    return True
                self.Logger.info("Server con "+self.GetConnectionString()+" IS past it's RunFor time "+str(hasRanFor)+", but IS NOT past it's time since last user activity "+str(timeSinceUserActivity))
        return False

    # Fires at a regular interval to see if we should disconnect this server connection.
    def OnRunForTimerCallback(self):
        if self.IsRunForTimeComplete():
            try:
                self.Logger.info("Server con "+self.GetConnectionString()+" RunFor is complete and will be disconnected.")
                self.Disconnect()
            except Exception as e:
                self.Logger.error("Exception in OnRunForTimerCallback durring disconnect. "+self.GetConnectionString()+" ex:" + str(e))

    def RunBlocking(self):
        while 1:
            # Since we want to run forever, we want to make sure any exceptions get caught but then we try again.
            try:
                # Clear the disconnecting flag.
                # We do this just before connects, because this flag weeds out all of the error noise
                # that might happen while we are performing a disconnect. But at this time, all of that should be
                # 100% done now.
                self.IsDisconnecting = False

                # Since there can be old pending actions from old sessions (session == one websocket connection).
                # We will keep track of the current session, so old errors from sessions don't effect the new one.
                self.ActiveSessionId += 1

                # Connect to the service.
                self.Logger.info("Attempting to talk to OctoEverywhere, server con "+self.GetConnectionString())
                self.Ws = Client(self.Endpoint, self.OnOpened, self.OnMsg, None, self.OnClosed, self.OnError)
                self.Ws.RunUntilClosed()

                # Handle disconnects
                self.Logger.info("Disconnected from OctoEverywhere, server con "+self.GetConnectionString())

                # Ensure all proxy sockets are closed.
                if self.OctoSession:
                    self.OctoSession.CloseAllWebStreamsAndDisable()

            except Exception as e:
                self.Logger.error("Exception in OctoEverywhere's main RunBlocking function. server con:"+self.GetConnectionString()+" ex:" + str(e))
                time.sleep(20)

            # On each disconnect, check if the RunFor time is now done.
            if self.IsRunForTimeComplete():
                # If our run for time expired, cleanup and return.
                self.Cleanup()
                self.Logger.info("Server con "+self.GetConnectionString()+" RunFor is complete, disconnected, and exiting the main thread.")
                # Exit the main run blocking loop.
                return

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
                # If we have failed and are waiting over 3 minutes, we will return which will check the server
                # protocol again, since it might have changed.
                return

    def SendMsg(self, msgBytes):
        # When we send any message, consider it user activity.
        self.LastUserActivityTime = datetime.now()
        self.Ws.Send(msgBytes, True)
