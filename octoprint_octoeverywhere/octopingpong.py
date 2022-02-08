import os
import json
import threading
import time
import requests

#
# The point of this class is to simply ping the aviable OctoEverywhere server regions occasionally to track which region is has the best
# latency to. This information is used by the plugin to ensure it's connected to the best possible server.
#
class OctoPingPong:

    LastWorkTimeKey = "LastWorkTime"
    ServerStatsKey = "ServerStats"
    _Instance = None


    @staticmethod
    def Init(logger, pluginDataFolderPath):
        OctoPingPong._Instance = OctoPingPong(logger, pluginDataFolderPath)


    @staticmethod
    def Get():
        return OctoPingPong._Instance


    def __init__(self, logger, pluginDataFolderPath):
        self.Logger = logger
        self.StatsFilePath = os.path.join(pluginDataFolderPath, "PingPongDataV2.json")

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
            self.Logger.error("Failed to start OctoPingPong Thread: "+str(e))


    # The main worker thread.
    def _WorkerThread(self):
        oneHourOfSeconds = 60 * 60

        # Always sleep a little extra time to let the system settle since we sometimes will start doing work right after a boot.
        time.sleep(60)

        while True:
            try:
                # Compute how long it's been since the last update.
                # Since this is written to disk, it's stays across boots / restarts.
                lastWorkTime = 0
                if OctoPingPong.LastWorkTimeKey in self.Stats:
                    lastWorkTime = int(self.Stats[OctoPingPong.LastWorkTimeKey])
                secondsSinceLastWork = time.time() - lastWorkTime

                # Compute how long until we should do work, this will be negative if the time has passed.
                # We want to do work about every 23 hours. 23 hours will make the cycle of work vary over time.
                #timeUntilNextWorkSec = (oneHourOfSeconds * 23) - secondsSinceLastWork
                # TODO - Temp make this 10 hours to capture initial data faster.
                timeUntilNextWorkSec = (oneHourOfSeconds * 10) - secondsSinceLastWork

                # If it's not time to work, sleep until it is time.
                if timeUntilNextWorkSec > 0:
                    time.sleep(timeUntilNextWorkSec)

                # It's time to work, first update the time we are working is now.
                # Also write to disk to ensure it's known and we don't get in a tight loop of working.
                self.Stats[OctoPingPong.LastWorkTimeKey] = time.time()
                self._SaveStatsToFile()

                # Update now
                self._UpdateStats()

            except Exception as e:
                self.Logger.error("Exception in OctoPingPong thread: "+str(e))


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
        computedStats = {}
        lowestLatencyValueMs = 99999
        lowestLatencyName = None
        defaultServerComputedAvgMs = None
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

            # Don't report a stat unless there are more than 2 records, to reduce noise.
            if c < 3:
                continue

            avg = s/c
            computedStats[sub] = avg
            if avg < lowestLatencyValueMs:
                lowestLatencyValueMs = avg
                lowestLatencyName = sub
            if defaultServerResult is not None and defaultServerResult[2] == sub:
                defaultServerComputedAvgMs = avg

        # Save the new stats to disk.
        self._SaveStatsToFile()

        # If there is no lowest latency name, we don't have enough stats to use yet.
        if lowestLatencyName is None:
            return
        if defaultServerResult is None:
            return
        if defaultServerComputedAvgMs is None:
            self.Logger.warn("PingPong default server name not found in results "+defaultServerResult[2])
            return

        # Report
        # Use the average for the default server so it's smoothed the same way the "lowest latency" is.
        self._ReportTelemetry(defaultServerResult[2], defaultServerComputedAvgMs, lowestLatencyName, lowestLatencyValueMs)


    def _ReportTelemetry(self, defaultServerName, defaultServerLatencyMs, lowestLatencyName, lowestLatencyMs):
        try:
            isDefaultLowest = defaultServerName == lowestLatencyName
            lowestLatencyDelta = lowestLatencyMs - defaultServerLatencyMs
            self.Logger.info("Server Latency Computed. Default:"+str(defaultServerName) + " latency:"+str(defaultServerLatencyMs)+"; Lowest Latency:"+str(lowestLatencyName)+" latency:"+str(lowestLatencyMs))
            data = {
                "Key":"PluginLatencyV2",
                "Value": float(defaultServerLatencyMs),
                "Properties":{
                    "IsDefaultLowest": str(isDefaultLowest),
                    "DefaultSub" : defaultServerName,
                    "LowestLatSub": lowestLatencyName,
                    "LowestLatDelta": str(lowestLatencyDelta)
                }
            }
            response = requests.post('https://octoeverywhere.com/api/stats/telemetryaccumulator', json=data)
            if response.status_code != 200:
                self.Logger.warn("Failed to report ping latency "+response.status_code)
                return
        except Exception as e:
            self.Logger.warm("Failed to report ping latency " + str(e))


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
            servers = obj["Result"]["Servers"]
            thisServer = obj["Result"]["ThisServer"]
            if servers is None or len(servers) == 0:
                return None
            if thisServer is None:
                return None

            # Close this response so the connection gets put back into the pool
            response.close()

            # Now using the same session, use the direct ping call.
            # The session will prevent all of the overhead and should have a pooled open connection
            # So this is as close to an actual realtime ping as we can get.
            start = time.time()
            response = s.get(pingDirectApiUrl, timeout=10)
            end = time.time()
            elapsedMs = (end - start) * 1000.0

            # Close the session to clean up all connections
            # (not required, this will be auto closed, but we do it anyways)
            s.close()

            # Check for failure
            if response.status_code != 200:
                return None

            # Success.
            return [elapsedMs, servers, thisServer]

        except Exception as e:
            self.Logger.info("Failed to call _DoPing "+str(e))
        return None


    # Resets the stats object to it's default state.
    def _ResetStats(self):
        self.Logger.info("OctoPingPong stats reset")
        self.Stats = {}
        # To ensure we don't constantly fail to read the file, restart, and then work,
        # Set this time to be now.
        self.Stats[OctoPingPong.LastWorkTimeKey] = time.time()
        self.Stats[OctoPingPong.ServerStatsKey] = {}


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
