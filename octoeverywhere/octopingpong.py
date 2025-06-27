import os
import json
import time
import threading
import logging
from typing import Any, Callable, Dict, List, Optional

import requests

from .sentry import Sentry
from .telemetry import Telemetry


# A helper class for the ping result.
class PingResult:
    def __init__(self, timeMs:float, servers:List[str], thisServer:str, autoLowestLatencyThresholdRttMs:int) -> None:
        self.TimeMs = timeMs
        self.ServersSubdomains = servers
        self.ThisServerSubdomain = thisServer
        self.AutoLowestLatencyThresholdRttMs = autoLowestLatencyThresholdRttMs


#
# The point of this class is to simply ping the available OctoEverywhere server regions occasionally to track which region is has the best
# latency to. This information is used by the plugin to ensure it's connected to the best possible server.
#
class OctoPingPong:

    LastWorkTimeKey = "LastWorkTime"
    ServerStatsKey = "ServerStats"
    LowestLatencyServerSubKey = "LowestLatencyServerSub"
    _Instance:"OctoPingPong" = None #pyright: ignore[reportAssignmentType]


    @staticmethod
    def Init(logger:logging.Logger, pluginDataFolderPath:str, printerId:str) -> None:
        OctoPingPong._Instance = OctoPingPong(logger, pluginDataFolderPath, printerId)


    @staticmethod
    def Get() -> "OctoPingPong":
        return OctoPingPong._Instance


    def __init__(self, logger:logging.Logger, pluginDataFolderPath:str, printerId:str) -> None:
        self.Logger = logger
        self.PrinterId = printerId
        self.StatsFilePath = os.path.join(pluginDataFolderPath, "PingPongDataV2.json")
        self.PluginFirstRunLatencyCompleteCallback = None
        self.IsDisablePrimaryOverride = False

        # Try to load past stats from the file.
        self.Stats:Dict[str, Any] = None #pyright: ignore[reportAttributeAccessIssue]
        self._LoadStatsFromFile()

        # If failed, just make a new stats obj.
        if self.Stats is None:
            self._ResetStats()

        # Start a new thread to do the occasional work.
        try:
            th = threading.Thread(target=self._WorkerThread)
            th.start()
        except Exception as e:
            Sentry.OnException("Failed to start OctoPingPong Thread.", e)


    # Used for local debugging.
    def DisablePrimaryOverride(self) -> None:
        self.Logger.info("OctoPingPong disabled")
        self.IsDisablePrimaryOverride = True


    # Returns a string to the lowest latency server is known, otherwise None.
    def GetLowestLatencyServerSub(self) -> Optional[str]:
        # Do this in a thread safe way, if we fail, just return None.
        try:
            stats = self.Stats
            if stats is None:
                return None
            lowestLatencyServerSub = stats.get(OctoPingPong.LowestLatencyServerSubKey, None)
            if lowestLatencyServerSub is None:
                return None
            if self.IsDisablePrimaryOverride:
                self.Logger.info("OctoPingPong IsDisablePrimaryOverride - not returning lowest latency server sub: "+lowestLatencyServerSub)
                return None
            if isinstance(lowestLatencyServerSub, str) is False:
                self.Logger.info("OctoPingPong lowest latency server sub is not a string: "+str(lowestLatencyServerSub))
                return None
            return str(lowestLatencyServerSub)
        except Exception as e:
            Sentry.OnException("Exception in OctoPingPong GetLowestLatencyServerSub.", e)
        return None


    # A special function used when the plugin is first installed and ran.
    # Since there will be no known latency data, the connection will default to the default endpoint.
    # When the latency data is ready, this class will fire the callback and allow the main connection to reconnect using it.
    def RegisterPluginFirstRunLatencyCompleteCallback(self, callback:Callable[[], None]) -> None:
        self.PluginFirstRunLatencyCompleteCallback = callback


    # The main worker thread.
    def _WorkerThread(self) -> None:
        oneHourOfSeconds = 60 * 60

        while True:
            try:
                # Compute how long it's been since the last update.
                # Since this is written to disk, it's stays across boots / restarts.
                lastWorkTime = self.Stats.get(OctoPingPong.LastWorkTimeKey, 0)
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
                    self.PluginFirstRunLatencyCompleteCallback = None
                    if callback is not None:
                        callback()

            except Exception as e:
                Sentry.OnException("Exception in OctoPingPong thread.", e)


    def _UpdateStats(self) -> None:
        self.Logger.info("Updating server latencies...")

        # First, get the default starport server's results.
        defaultServerResult = self._DoPing(None)

        # Check for a failure. If so, just return.
        if defaultServerResult is None:
            return

        # Now ping each server we got back
        serverResults:Dict[str, Optional[PingResult]] = {}
        for sub in defaultServerResult.ServersSubdomains:
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
            result = serverResults[sub]
            if result is not None:
                timeMsOrNone = result.TimeMs
            if sub not in self.Stats[OctoPingPong.ServerStatsKey]:
                self.Stats[OctoPingPong.ServerStatsKey][sub] = []
            self.Stats[OctoPingPong.ServerStatsKey][sub].append(timeMsOrNone)

        # Compute the stats now.
        self._ComputeStats(defaultServerResult)


    # Given the default response and the currently updated stats, this computes values.
    def _ComputeStats(self, defaultServerResult:PingResult) -> None:

        # Before we compute stats, remove any servers from our on disk stats that are no longer in the default response.
        # This is important to ensure the lowest latency server doesn't get stuck to a hostname that doesn't exist.
        toRemove:List[str] = []
        for sub in self.Stats[OctoPingPong.ServerStatsKey]:
            if sub not in defaultServerResult.ServersSubdomains:
                toRemove.append(sub)
        for sub in toRemove:
            del self.Stats[OctoPingPong.ServerStatsKey][sub]

        c_largeInt = 999999
        computedStats:Dict[str, int] = {}
        lowestLatencyValueMs = c_largeInt
        lowestLatencySubName:Optional[str] = None
        defaultServerComputedAvgMs:Optional[int] = None
        selectedLatencyMs:Optional[int] = None
        smallestBucketStatCount = c_largeInt
        for sub in self.Stats[OctoPingPong.ServerStatsKey]:

            # Trim the list to never be longer than a count of data points.
            # Since we append to the end, we will pop the oldest values in the front.
            while len(self.Stats[OctoPingPong.ServerStatsKey][sub]) > 10:
                self.Stats[OctoPingPong.ServerStatsKey][sub].pop(0)

            # Compute the average
            s:float = 0
            c:int = 0
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

            avg:int = int(float(s)/float(c))
            computedStats[sub] = avg
            if avg < lowestLatencyValueMs:
                lowestLatencyValueMs = avg
                lowestLatencySubName = sub
            if defaultServerResult.ThisServerSubdomain == sub:
                defaultServerComputedAvgMs = avg

        # This needs to be done before we return if smallestBucketStatCount is too low, because we still want to set the lowest latency server even with few data points.
        # If we don't have enough data points, we don't want to pick a default lowest server.
        if defaultServerComputedAvgMs is not None and lowestLatencyValueMs is not None:
            # Compute the latency delta from the lowest latency possible to the default, where higher means the lowest latency server is lower latency.
            deltaRttMs = defaultServerComputedAvgMs - lowestLatencyValueMs

            # Only if the latency delta is more than what the server is asking for do we override the default server for the lowest latency server.
            # See the server side code for AutoLowestLatencyThresholdRttMs to understand where the value comes from.
            if deltaRttMs > defaultServerResult.AutoLowestLatencyThresholdRttMs:
                self.Stats[OctoPingPong.LowestLatencyServerSubKey] = lowestLatencySubName
                selectedLatencyMs = lowestLatencyValueMs
            else:
                # Else, no override, we will use the default server.
                self.Stats[OctoPingPong.LowestLatencyServerSubKey] = None
                selectedLatencyMs = defaultServerComputedAvgMs

        # Save the new stats to disk.
        self._SaveStatsToFile()

        # Report info
        self.Logger.info("Ping Pong Stats: Default:["+str(defaultServerResult.ThisServerSubdomain)+","+str(defaultServerComputedAvgMs)+"], Lowest:["+str(lowestLatencySubName)+","+str(lowestLatencyValueMs)+"] Latency Threshold: "+str(defaultServerResult.AutoLowestLatencyThresholdRttMs))

        # If any of the stats buckers are too low of readings, don't report stats yet.
        # Note this does mean that when a new server is added, we won't report status until 2 readings have been taken.
        if smallestBucketStatCount < 3:
            return

        # Ensure we got a lowest latency server, we always should if minStatCount is > 0
        if lowestLatencySubName is None:
            return

        # Sanity check we found the default server.
        if defaultServerComputedAvgMs is None:
            self.Logger.warning("PingPong default server name not found in results "+defaultServerResult.ThisServerSubdomain)
            return

        # Report
        # Use the average for the default server so it's smoothed the same way the "lowest latency" is.
        self._ReportTelemetry(defaultServerResult.ThisServerSubdomain, defaultServerComputedAvgMs, lowestLatencySubName, lowestLatencyValueMs, selectedLatencyMs)


    def _ReportTelemetry(self, defaultServerName:str, defaultServerLatencyMs:float, lowestLatencyName:str, lowestLatencyMs:float, selectedLatencyMs:Optional[float]) -> None:
        isDefaultLowest = defaultServerName == lowestLatencyName
        lowestLatencyDelta = lowestLatencyMs - defaultServerLatencyMs
        Telemetry.Write("PluginLatencyV5", int(defaultServerLatencyMs),
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
    def _DoPing(self, subdomain:Optional[str]) -> Optional[PingResult]:
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
            with requests.Session() as s:
                with s.get(pingInfoApiUrl, timeout=10) as response:
                    # Check for failure
                    if response.status_code != 200:
                        return None

                    # Parse and check.
                    obj = response.json()
                    result = obj.get("Result", None)
                    if result is None:
                        self.Logger.warning("OctoPingPong server response had no result obj.")
                        return None
                    servers:Optional[List[str]] = result.get("Servers", None)
                    if servers is None:
                        self.Logger.warning("OctoPingPong server response had no servers obj.")
                        return None
                    thisServer:Optional[str] = result.get("ThisServer", None)
                    if thisServer is None:
                        self.Logger.warning("OctoPingPong server response had no ThisServer obj.")
                        return None
                    # Note we no longer look at this, we only use AutoLowestLatencyThresholdRttMs
                    #enablePluginAutoLowestLatency:Optional[bool] = result.get("EnablePluginAutoLowestLatency", None)
                    autoLowestLatencyThresholdRttMs:Optional[int] = result.get("AutoLowestLatencyThresholdRttMs", None)
                    if autoLowestLatencyThresholdRttMs is None:
                        self.Logger.warning("OctoPingPong server response had no AutoLowestLatencyThresholdRttMs obj.")
                        return None
                    if len(servers) == 0:
                        return None

                    # Close this response so the connection gets put back into the pool
                    response.close()

                    # Now using the same session, use the direct ping call.
                    # The session will prevent all of the overhead and should have a pooled open connection
                    # So this is as close to an actual realtime ping as we can get.
                    #
                    results:List[float] = []
                    for _ in range(0, 3):
                        # Do the test.
                        start = time.time()
                        with s.get(pingDirectApiUrl, timeout=10) as response:
                            end = time.time()
                            # Only consider 200s valid, otherwise the request might have never made it to the server.
                            if response.status_code == 200:
                                elapsedTimeMs = (end - start) * 1000.0
                                results.append(elapsedTimeMs)
                        # Give the new test a few ms before starting again.
                        time.sleep(0.05)

                    # Ensure we got at least one result
                    if len(results) == 0:
                        return None

                    # Since the lowest time is the fastest the server responded, that's all we care about.
                    minElapsedTimeMs = 999999.0
                    for result in results:
                        if minElapsedTimeMs is None or result < minElapsedTimeMs:
                            minElapsedTimeMs = result

                    # Success.
                    return PingResult(minElapsedTimeMs, servers, thisServer, autoLowestLatencyThresholdRttMs)

        except Exception as e:
            self.Logger.info("Failed to call _DoPing "+str(e))
        return None


    # Resets the stats object to it's default state.
    def _ResetStats(self) -> None:
        self.Logger.info("OctoPingPong stats reset")
        self.Stats = {}
        self.Stats[OctoPingPong.LastWorkTimeKey] = 0
        self.Stats[OctoPingPong.ServerStatsKey] = {}
        self.Stats[OctoPingPong.LowestLatencyServerSubKey] = None


    # Blocks to write the current stats to a file.
    def _SaveStatsToFile(self) -> None:
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
    def _LoadStatsFromFile(self) -> None:
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
