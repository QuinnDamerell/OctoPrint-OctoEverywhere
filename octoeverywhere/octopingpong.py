import os
import json
import threading
import time
import requests

from .sentry import Sentry
from .telemetry import Telemetry

#
# The point of this class is to simply ping the available OctoEverywhere server regions occasionally to track which region is has the best
# latency to. This information is used by the plugin to ensure it's connected to the best possible server.
#
class OctoPingPong:

    LastWorkTimeKey = "LastWorkTime"
    ServerStatsKey = "ServerStats"
    LowestLatencyServerSubKey = "LowestLatencyServerSub"
    _Instance = None


    @staticmethod
    def Init(logger, pluginDataFolderPath, printerId):
        OctoPingPong._Instance = OctoPingPong(logger, pluginDataFolderPath, printerId)


    @staticmethod
    def Get():
        return OctoPingPong._Instance


    def __init__(self, logger, pluginDataFolderPath, printerId):
        self.Logger = logger
        self.PrinterId = printerId
        self.StatsFilePath = os.path.join(pluginDataFolderPath, "PingPongDataV2.json")
        self.PluginFirstRunLatencyCompleteCallback = None
        self.IsDisablePrimaryOverride = False

        # Try to load past stats from the file.
        self.Stats = None
        self._LoadStatsFromFile()

        # If failed, just make a new stats obj.
        if self.Stats is None:
            self._ResetStats()

        # Start a new thread to do the occasional work.
        try:
            th = threading.Thread(target=self._WorkerThread)
            # pylint: disable=deprecated-method
            # This is deprecated in PY3.10
            th.setDaemon(True)
            th.start()
        except Exception as e:
            Sentry.Exception("Failed to start OctoPingPong Thread.", e)


    # Used for local debugging.
    def DisablePrimaryOverride(self):
        self.Logger.info("OctoPingPong disabled")
        self.IsDisablePrimaryOverride = True


    # Returns a string to the lowest latency server is known, otherwise None.
    def GetLowestLatencyServerSub(self):
        # Do this in a thread safe way, if we fail, just return None.
        try:
            stats = self.Stats
            if stats is None:
                return None
            if OctoPingPong.LowestLatencyServerSubKey not in stats:
                return None
            lowestLatencyServerSub = stats[OctoPingPong.LowestLatencyServerSubKey]
            if lowestLatencyServerSub is None:
                return None
            if self.IsDisablePrimaryOverride:
                self.Logger.info("OctoPingPong IsDisablePrimaryOverride - not returning lowest latency server sub: "+lowestLatencyServerSub)
                return None
            return lowestLatencyServerSub
        except Exception as e:
            Sentry.Exception("Exception in OctoPingPong GetLowestLatencyServerSub.", e)
        return None


    # A special function used when the plugin is first installed and ran.
    # Since there will be no known latency data, the connection will default to the default endpoint.
    # When the latency data is ready, this class will fire the callback and allow the main connection to reconnect using it.
    def RegisterPluginFirstRunLatencyCompleteCallback(self, callback):
        self.PluginFirstRunLatencyCompleteCallback = callback


    # The main worker thread.
    def _WorkerThread(self):
        oneHourOfSeconds = 60 * 60

        while True:
            try:
                # Compute how long it's been since the last update.
                # Since this is written to disk, it's stays across boots / restarts.
                lastWorkTime = 0
                if OctoPingPong.LastWorkTimeKey in self.Stats:
                    lastWorkTime = int(self.Stats[OctoPingPong.LastWorkTimeKey])
                secondsSinceLastWork = time.time() - lastWorkTime

                # Compute how long until we should do work, this will be negative if the time has passed.
                # Right now the time span is set to 50 hours, which is just over 2 days. We don't need to update too often
                # and it's a decent amount of work due to hitting every server.
                timeUntilNextWorkSec = (oneHourOfSeconds * 50) - secondsSinceLastWork

                # If lastWorkTime is 0, the file was just created, so this is the first time the plugin has ran.
                if lastWorkTime == 0:
                    self.Logger.info("PingPong has detected a first time run. Updating latency stats now.")
                    timeUntilNextWorkSec = 0
                    # Since the first run will be a little after OctoPrint or device boot, we need to wait a bit before for things to settle.
                    # We also don't want to restart right as the user gets setup, so delay a bit.
                    time.sleep(60 * 15)

                # If it's not time to work, sleep until it is time.
                if timeUntilNextWorkSec > 0:
                    time.sleep(timeUntilNextWorkSec)

                # It's time to work, first update the time we are working is now.
                # Also write to disk to ensure it's known and we don't get in a tight loop of working.
                self.Stats[OctoPingPong.LastWorkTimeKey] = time.time()
                self._SaveStatsToFile()

                # Update now
                self._UpdateStats()

                # Only for the very first time this runs after the plugin is installed, fire this callback
                # which might reconnect the main OctoSocket connection to the best possible server.
                if lastWorkTime == 0 and self.GetLowestLatencyServerSub() is not None:
                    callback = self.PluginFirstRunLatencyCompleteCallback
                    if callback is not None:
                        self.PluginFirstRunLatencyCompleteCallback()
                        callback = None

            except Exception as e:
                Sentry.Exception("Exception in OctoPingPong thread.", e)


    def _UpdateStats(self):
        self.Logger.info("Updating server latencies...")

        # First, get the default starport server's results.
        defaultServerResult = self._DoPing(None)

        # Check for a failure. If so, just return.
        if defaultServerResult is None:
            return

        # Now ping each server we got back
        serverResults = {}
        for sub in defaultServerResult[1]:
            # Note this will be None if it failed.
            result = self._DoPing(sub)
            serverResults[sub] = result

        # Make sure the stats root exists.
        if OctoPingPong.ServerStatsKey not in self.Stats:
            self.Stats[OctoPingPong.ServerStatsKey] = {}

        # Update our stats
        # pylint: disable=consider-using-dict-items
        for sub in serverResults:
            timeMsOrNone = None
            if serverResults[sub] is not None:
                timeMsOrNone = serverResults[sub][0]
            if sub not in self.Stats[OctoPingPong.ServerStatsKey]:
                self.Stats[OctoPingPong.ServerStatsKey][sub] = []
            self.Stats[OctoPingPong.ServerStatsKey][sub].append(timeMsOrNone)

        # Compute the stats now.
        self._ComputeStats(defaultServerResult)


    # Given the default response and the currently updated stats, this computes values.
    def _ComputeStats(self, defaultServerResult):

        # Before we compute stats, remove any servers from our on disk stats that are no longer in the default response.
        # This is important to ensure the lowest latency server doesn't get stuck to a hostname that doesn't exist.
        toRemove = []
        for sub in self.Stats[OctoPingPong.ServerStatsKey]:
            if sub not in defaultServerResult[1]:
                toRemove.append(sub)
        for sub in toRemove:
            del self.Stats[OctoPingPong.ServerStatsKey][sub]

        c_largeInt = 99999
        computedStats = {}
        lowestLatencyValueMs = c_largeInt
        lowestLatencySubName = None
        defaultServerComputedAvgMs = None
        selectedLatencyMs = None
        smallestBucketStatCount = c_largeInt
        for sub in self.Stats[OctoPingPong.ServerStatsKey]:

            # Trim the list to never be longer than a count of data points.
            # Since we append to the end, we will pop the oldest values in the front.
            while len(self.Stats[OctoPingPong.ServerStatsKey][sub]) > 10:
                self.Stats[OctoPingPong.ServerStatsKey][sub].pop(0)

            # Compute the average
            s = 0
            c = 0
            for v in self.Stats[OctoPingPong.ServerStatsKey][sub]:
                # If the value is None it means the query failed.
                # We keep track of that so that failed servers values fall off.
                if v is None:
                    continue
                s += v
                c += 1

            # Keep track of which server we have the lowest result counts for.
            smallestBucketStatCount = min(smallestBucketStatCount, c)

            # Prevent divide by zero
            if c == 0:
                continue

            avg = s/c
            computedStats[sub] = avg
            if avg < lowestLatencyValueMs:
                lowestLatencyValueMs = avg
                lowestLatencySubName = sub
            if defaultServerResult[2] == sub:
                defaultServerComputedAvgMs = avg

        # We need to set the lowest latency server into settings if we have the right data.
        # defaultServerResult[3] is the server flag indicating if the plugins should try to connect to the lowest latency servers.
        # This needs to be done before we return if smallestBucketStatCount is too low, because we still want to set the lowest latency server even with few data points.
        #
        # Even if this is the default server we will set it, just so we stay pinned to the lowest latency server
        # The notion of the default server can change over time, as traffic manager changes it's mind.
        #
        # Note, we should be mindful that if the printer is not connected to the default server, there's a higher chance that an app connection
        # will need to make a secondary connection. But since that system is reliable, we won't account for it now.
        #
        # Note that if there isn't enough data to compute stats, lowestLatencySubName can be None.
        if defaultServerResult[3] is True and lowestLatencySubName is not None:
            self.Stats[OctoPingPong.LowestLatencyServerSubKey] = lowestLatencySubName
            selectedLatencyMs = lowestLatencyValueMs
        else:
            self.Stats[OctoPingPong.LowestLatencyServerSubKey] = None
            selectedLatencyMs = defaultServerComputedAvgMs

        # Save the new stats to disk.
        self._SaveStatsToFile()

        # Report info
        self.Logger.info("Ping Pong Stats: Default:["+str(defaultServerResult[2])+","+str(defaultServerComputedAvgMs)+"], Lowest:["+str(lowestLatencySubName)+","+str(lowestLatencyValueMs)+"] Use Low Latency Enabled: "+str(defaultServerResult[3]))

        # If any of the stats buckers are too low of readings, don't report stats yet.
        # Note this does mean that when a new server is added, we won't report status until 2 readings have been taken.
        if smallestBucketStatCount < 3:
            return

        # Ensure we got a lowest latency server, we always should if minStatCount is > 0
        if lowestLatencySubName is None:
            return

        # Sanity check we found the default server.
        if defaultServerComputedAvgMs is None:
            self.Logger.warn("PingPong default server name not found in results "+defaultServerResult[2])
            return

        # Report
        # Use the average for the default server so it's smoothed the same way the "lowest latency" is.
        self._ReportTelemetry(defaultServerResult[2], defaultServerComputedAvgMs, lowestLatencySubName, lowestLatencyValueMs, selectedLatencyMs)


    def _ReportTelemetry(self, defaultServerName, defaultServerLatencyMs, lowestLatencyName, lowestLatencyMs, selectedLatencyMs):
        isDefaultLowest = defaultServerName == lowestLatencyName
        lowestLatencyDelta = lowestLatencyMs - defaultServerLatencyMs
        Telemetry.Write("PluginLatencyV4", int(defaultServerLatencyMs),
        {
            "IsDefaultLowest": isDefaultLowest,
            "DefaultSub" : defaultServerName,
            "LowestLatSub": lowestLatencyName,
            "LowestLatDelta": lowestLatencyDelta,
            "LowestLatMs": lowestLatencyMs,
            "SelectedLatencyMs": selectedLatencyMs
        }, None)

    # Returns the ping latency in ms to the server and the list of servers.
    # If subdomain is given it will be used, otherwise the default subdomain will be used.
    def _DoPing(self, subdomain):
        try:
            # Make the URL
            if subdomain is None:
                subdomain = "starport-v1"

            # Setup the URLs
            host = "https://"+subdomain+".octoeverywhere.com"
            pingInfoApiUrl = host+"/api/plugin/ping"
            pingDirectApiUrl = host+"/api/nginx-direct/ping/"

            # We have to make two calls, because the first call will query DNS, open the TCP connection, start SSL, and get a connection in the pool.
            # The extra stuff above will add an extra 100-150 more MS to the call.
            # For the first call we hit the actual API to get data back.
            s = requests.Session()
            response = s.get(pingInfoApiUrl, timeout=10)

            # Check for failure
            if response.status_code != 200:
                return None

            # Parse and check.
            obj = response.json()
            if "Result" not in obj:
                self.Logger.warn("OctoPingPong server response had no result obj.")
                return None
            if "Servers" not in obj["Result"]:
                self.Logger.warn("OctoPingPong server response had no servers obj.")
                return None
            servers = obj["Result"]["Servers"]
            if "ThisServer" not in obj["Result"]:
                self.Logger.warn("OctoPingPong server response had no ThisServer obj.")
                return None
            thisServer = obj["Result"]["ThisServer"]
            if "EnablePluginAutoLowestLatency" not in obj["Result"]:
                self.Logger.warn("OctoPingPong server response had no EnablePluginAutoLowestLatency obj.")
                return None
            enablePluginAutoLowestLatency = obj["Result"]["EnablePluginAutoLowestLatency"]
            if servers is None or len(servers) == 0:
                return None
            if thisServer is None:
                return None

            # Close this response so the connection gets put back into the pool
            response.close()

            # Now using the same session, use the direct ping call.
            # The session will prevent all of the overhead and should have a pooled open connection
            # So this is as close to an actual realtime ping as we can get.
            #
            results = []
            for _ in range(0, 3):
                # Do the test.
                start = time.time()
                response = s.get(pingDirectApiUrl, timeout=10)
                end = time.time()
                # Close the response so it's back in the pool.
                response.close()
                # Only consider 200s valid, otherwise the request might have never made it to the server.
                if response.status_code == 200:
                    elapsedTimeMs = (end - start) * 1000.0
                    results.append(elapsedTimeMs)
                # Give the new test a few ms before starting again.
                time.sleep(0.05)

            # Close the session to clean up all connections
            # (not required, this will be auto closed, but we do it anyways)
            s.close()

            # Ensure we got at least one result
            if len(results) == 0:
                return None

            # Since the lowest time is the fastest the server responded, that's all we care about.
            minElapsedTimeMs = None
            for result in results:
                if minElapsedTimeMs is None or result < minElapsedTimeMs:
                    minElapsedTimeMs = result

            # Success.
            return [minElapsedTimeMs, servers, thisServer, enablePluginAutoLowestLatency]

        except Exception as e:
            self.Logger.info("Failed to call _DoPing "+str(e))
        return None


    # Resets the stats object to it's default state.
    def _ResetStats(self):
        self.Logger.info("OctoPingPong stats reset")
        self.Stats = {}
        self.Stats[OctoPingPong.LastWorkTimeKey] = 0
        self.Stats[OctoPingPong.ServerStatsKey] = {}
        self.Stats[OctoPingPong.LowestLatencyServerSubKey] = None


    # Blocks to write the current stats to a file.
    def _SaveStatsToFile(self):
        try:
            data = {}
            data['Stats'] = self.Stats
            # pylint: disable=unspecified-encoding
            # encoding only supported in py3
            with open(self.StatsFilePath, 'w') as f:
                json.dump(data, f)

        except Exception as e:
            # On any failure, reset the stats.
            self._ResetStats()
            self.Logger.error("_SaveStatsToFile failed "+str(e))


    # Does a blocking call to load any current stats from the file.
    def _LoadStatsFromFile(self):
        try:
            # First check if there's a file.
            if os.path.exists(self.StatsFilePath) is False:
                # No file means this is most likely the first time run.
                self._ResetStats()
                self._SaveStatsToFile()
                return

            # Try to open it and get the key. Any failure will null out the key.
            # pylint: disable=unspecified-encoding
            # encoding only supported in py3
            with open(self.StatsFilePath) as f:
                data = json.load(f)
            self.Stats = data["Stats"]

            self.Logger.info("OctoPingPong stats loaded from file.")

        except Exception as e:
            self._ResetStats()
            self.Logger.error("_LoadStatsFromFile failed "+str(e))
