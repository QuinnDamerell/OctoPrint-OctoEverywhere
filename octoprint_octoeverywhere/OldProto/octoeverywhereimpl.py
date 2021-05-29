import time
import random
import threading

from .websocketimpl import Client
from .octosessionimpl import OctoSession
from .octoservercon import OctoServerCon

# 
# This is the main running class that will connect and keep a connection to the service.
#
class OctoEverywhere:

    # How long the primary connection will stay connected before recycling.
    # This is currently set to 48 hours. We want to reconnect occasionally to make sure we are
    # connected to the most ideal sever in terms of latency.
    # Note the RunFor system does account for user activity, and won't disconnect while the connection is active.
    PrimaryConnectionRunForTimeSec = 60 * 60 * 48 # 48 hours.

    # How long secondary connections will stay connected for.
    # Currently set to 15 mintues.
    # The RunFor system will keep the connection alive if there's user activity on it. If the connection does
    # die but then tries to get used quickly, we will just be summoned again. 
    SecondaryConnectionRunForTimeSec = 60 * 15 # 15 minutes.

    def __init__(self, endpoint, printerId, logger, uiPopupInvoker, statusChangeHandler, pluginVersion):
        self.Endpoint = endpoint
        self.PrinterId = printerId
        self.Logger = logger
        self.UiPopupInvoker = uiPopupInvoker
        self.StatusChangeHandler = statusChangeHandler
        self.PluginVersion = pluginVersion
        self.SecondaryServerCons = {}
        self.SecondaryServerConsLock = threading.Lock()
    
    def RunBlocking(self):
        # This is the main thread for the entire plugin, and it hosts the primary connection.
        # This connection should always be active, so we run it in a while loop that never exits and
        # catch any exceptions that occur.
        while 1:
            try:
                # Create the primary connection.
                serverCon = OctoServerCon(self, self.Endpoint, True, self.PrinterId, self.Logger, self.UiPopupInvoker, self.StatusChangeHandler, self.PluginVersion, self.PrimaryConnectionRunForTimeSec)
                serverCon.RunBlocking()
            except Exception as e:
                self.Logger.error("Exception in OctoEverywhere's main RunBlocking function. ex:" + str(e))
                # Sleep for just a bit and try again.
                time.sleep(5)

    def OnSummonRequest(self, summonConnectUrl):
        # Grab the map lock.
        self.SecondaryServerConsLock.acquire()
        try:
            # Check if we already have a secondary connection to this server.
            if summonConnectUrl in self.SecondaryServerCons :
                self.Logger.warn("We got a summon request for a server that we already have a secondary connection to. "+str(summonConnectUrl))
                return

            # We don't have a connection, so make one now.
            thread = threading.Thread(target=self.HandleSecondaryServerCon, args=(summonConnectUrl,))
            thread.daemon = True
            thread.start()
            self.SecondaryServerCons[summonConnectUrl] = thread

        except Exception as _:
            # rethrow any exceptions in the code
            raise                
        finally:
            # Always unlock                
            self.SecondaryServerConsLock.release()

    def HandleSecondaryServerCon(self, summonConnectUrl):
        # Run the secondary connection for until the RunFor time limint. Note RunFor will account for user activity.
        self.Logger.info("Starting a secondary connection to "+str(summonConnectUrl))
        try:
            serverCon = OctoServerCon(self, summonConnectUrl, False, self.PrinterId, self.Logger, self.UiPopupInvoker, None, self.PluginVersion, self.SecondaryConnectionRunForTimeSec)
            serverCon.RunBlocking()
        except Exception as e:
            self.Logger.error("Exception in HandleSecondaryServerCon function. ex:" + str(e))

        # Since this is a secondary connection, when RunBlocking returns we want to be done.
        self.SecondaryServerConsLock.acquire()
        try:
            # Check if we already have a secondary connection to this server.
            if summonConnectUrl in self.SecondaryServerCons :
                del self.SecondaryServerCons[summonConnectUrl]
            else:
                self.Logger.error("Secondary ended but there's not an ref of it in the map?")
        except Exception as _:
            self.Logger.Error("Exception when removing secondary connection from map. "+str(e))             
        finally:
            # Always unlock                
            self.SecondaryServerConsLock.release()

        self.Logger.info("Secondary connection to "+str(summonConnectUrl)+" has ended")

