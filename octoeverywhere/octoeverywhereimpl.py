import time
import threading

from .sentry import Sentry
from .threaddebug import ThreadDebug
from .octoservercon import OctoServerCon
from .Proto import SummonMethods

#
# This is the main running class that will connect and keep a connection to the service.
#
class OctoEverywhere:

    # How long the primary connection will stay connected before recycling.
    # We want to reconnect occasionally to make sure we are connected to the most ideal sever in terms of latency.
    # Note the RunFor system does account for user activity, and won't disconnect while the connection is active.
    PrimaryConnectionRunForTimeSec = 60 * 60 * 47 # 47 hours.

    # How long a secondary connection will stay connected for.
    # Currently set to 15 minutes.
    # The RunFor system will keep the connection alive if there's user activity on it. If the connection does
    # die but then a user tries to use it quickly, we will just summon the connection again.
    SecondaryConnectionRunForTimeSec = 60 * 15 # 15 minutes.

    def __init__(self, endpoint, printerId, privateKey, logger, uiPopupInvoker, statusChangeHandler, pluginVersion, serverHostType, isCompanion):
        self.Endpoint = endpoint
        self.PrinterId = printerId
        self.PrivateKey = privateKey
        self.Logger = logger
        self.UiPopupInvoker = uiPopupInvoker
        self.StatusChangeHandler = statusChangeHandler
        self.PluginVersion = pluginVersion
        self.ServerHostType = serverHostType
        self.IsCompanion = isCompanion
        self.SecondaryServerCons = {}
        self.SecondaryServerConsLock = threading.Lock()

    def RunBlocking(self):
        # This is the main thread for the entire plugin, and it hosts the primary connection.
        # This connection should always be active, so we run it in a while loop that will never end and
        # will catch any exceptions that occur.
        while 1:
            try:
                # Create the primary connection.
                # Allow this connection to use the lowest latency server if possible.
                serverCon = self.createOctoServerCon(self.Endpoint, True, True, self.StatusChangeHandler, self.PrimaryConnectionRunForTimeSec, SummonMethods.SummonMethods.Unknown)
                serverCon.RunBlocking()
            except RuntimeError as e:
                # From telemetry, we have seen that this error can fire very often for some users when there are too many threads running.
                # To try to fix this, we will dump the thread info to the log and then fire the exception, so the log message will show up.
                #msg = str(e)
                #if "can't start new thread" in msg:
                ThreadDebug.DoThreadDumpLogout(self.Logger)
                Sentry.Exception("RuntimeError in OctoEverywhere's main RunBlocking function.", e)
                # Sleep for a long time, since this can't be recovered from easily.
                time.sleep(60 * 60 * 2)
            except Exception as e:
                Sentry.Exception("Exception in OctoEverywhere's main RunBlocking function.", e)
                # Sleep for just a bit and try again.
                time.sleep(5)

    def OnSummonRequest(self, summonConnectUrl, summonMethod):
        # Grab the map lock.
        with self.SecondaryServerConsLock:

            # Check if we already have a secondary connection to this server.
            if summonConnectUrl in self.SecondaryServerCons :
                self.Logger.warn("We got a summon request for a server that we already have a secondary connection to. "+str(summonConnectUrl)+", method "+str(summonMethod))
                return

            # We don't have a connection, so make a new connection now.
            thread = threading.Thread(target=self.HandleSecondaryServerCon, args=(summonConnectUrl, summonMethod,))
            thread.daemon = True
            thread.start()
            self.SecondaryServerCons[summonConnectUrl] = thread

    def HandleSecondaryServerCon(self, summonConnectUrl, summonMethod):
        # Run the secondary connection for until the RunFor time limit. Note RunFor will account for user activity.
        self.Logger.info("Starting a secondary connection to "+str(summonConnectUrl)+ " method "+str(summonMethod))
        try:
            # Never allow the lowest latency server to be used for secondary connection, since it won't connect to where it needs to be.
            serverCon = self.createOctoServerCon(summonConnectUrl, False, False, None, self.SecondaryConnectionRunForTimeSec, summonMethod)
            serverCon.RunBlocking()
        except Exception as e:
            Sentry.Exception("Exception in HandleSecondaryServerCon function.", e)

        # Since this is a secondary connection, when RunBlocking() returns we want to be done.
        with self.SecondaryServerConsLock:
            try:
                # Check if we already have a secondary connection to this server.
                if summonConnectUrl in self.SecondaryServerCons :
                    del self.SecondaryServerCons[summonConnectUrl]
                else:
                    self.Logger.error("Secondary ended but there's not an ref of it in the map?")
            except Exception as _:
                Sentry.Exception("Exception when removing secondary connection from map.", e)

        self.Logger.info("Secondary connection to "+str(summonConnectUrl)+" has ended")

    def createOctoServerCon(self, endpoint, isPrimary, shouldUseLowestLatencyServer, statusChangeHandler, runTime, summonMethod):
        return OctoServerCon(self, endpoint, isPrimary, shouldUseLowestLatencyServer, self.PrinterId, self.PrivateKey, self.Logger, self.UiPopupInvoker, statusChangeHandler, self.PluginVersion, runTime, summonMethod, self.ServerHostType, self.IsCompanion)
