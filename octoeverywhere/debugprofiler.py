import time
import logging
from enum import Enum


# A list of possible features that can be profiled.
class DebugProfilerFeatures(Enum):
    WebStream                = 1
    NotificationHandlerEvent = 2
    FinalSnap                = 3
    Gadget                   = 4
    MoonrakerWsThread        = 5
    MoonrakerWsMsgThread     = 6
    MoonrakerWebcamHelper    = 7
    UiInjector               = 8


# A debug class that helps with profiling.
#
# This class must be created and only used on a single thread, that's how the profiler works.
# It can be used in two ways.
# One Off
#    In one off, the class is created, started, ended, and done.
#    Like:
#        with DebugProfiler(self.Logger, DebugProfilerFeatures.UiInjector):
# Repeat
#     For longer live threads, repeat can be used.
#     You make the DebugProfiler and call start once, then you call ReportIfNeeded every so often.
#     ReportIfNeeded will end the profile, print, and then start a new profile.
#
class DebugProfiler:

    # Note, this should only be True on dev builds!
    # Also, the package pyinstrument needs to be manually installed.
    _EnableProfiling = False


    # A list of individual features that can be enabled for profiling.
    # This also acts as a way to know what can be profiled easily.
    # A value of...
    #    None = Disabled
    #    0    = Run once
    #    int  = Re-run every <int> seconds.
    _EnabledFeatures = {
        # Enables all web streams to be profiled.
        # The best way to do this is to get one URL you want to debug, enable it, and then only hit that URL.
        # This includes Http and WS web streams!
        # Ex: https://octoeverywhere.com/api/printer/snapshot?id=<printerid>
        #     https://octoeverywhere.com/api/live/stream?id=.<printerid>
        #     https://klipper.octoeverywhere.com/assets/index-17a5ec1d.js
        DebugProfilerFeatures.WebStream : 0,

        # Threads used to handle notification system events.
        DebugProfilerFeatures.NotificationHandlerEvent: 0,

        # The thread used for final snap.
        DebugProfilerFeatures.FinalSnap: 10,

        # The thread used for final gadget.
        DebugProfilerFeatures.Gadget: 10,

        #
        # Klipper Only
        #

        # The Moonraker main ws thread that handles the WS connection, fires message callbacks, and such.
        # This usually doesn't do much, since it dispatches messages off to other threads quickly.
        #DebugProfilerFeatures.MoonrakerWsThread: 10,

        # The Moonraker main thread that handles any unmatched command message.
        # This does the work to handle events like print stop, start, etc.
        #DebugProfilerFeatures.MoonrakerWsMsgThread: 10,

        # The webcam helper thread
        # To make this fire, there must be webcam changes or something to spin the thread.
        #DebugProfilerFeatures.MoonrakerWebcamHelper: 10,

        # Used to profile the UiInjector
        #DebugProfilerFeatures.UiInjector : 10,
    }


    def __init__(self, logger:logging.Logger, feature:DebugProfilerFeatures, disableAutoStart = False) -> None:
        self.Logger = logger
        self.Feature = feature
        self.Profiler = None
        self.HasRan = False
        self.StartedSec = 0.0
        if disableAutoStart is False:
            self.StartProfile()


    # Support using for easy integration.
    def __enter__(self):
        self.StartProfile()
        return self

    # Support using for easy integration.

    def __exit__(self, exc_type, exc_value, traceback):
        self.StopProfile()


    # Starts the profile, only needed if you disabled auto start.
    def StartProfile(self, force=False) -> None:
        try:
            # Ensure the profiler is enabled.
            if self._EnsureEnabled() is False:
                return

            # Only let this class run once.
            if self.HasRan and force is False:
                return
            self.HasRan = True

            # Setup the profiler
            # pylint: disable=import-error
            # pylint: disable=import-outside-toplevel
            self.Logger.info(f"Profiler started for {self.Feature}")
            from pyinstrument import Profiler
            self.Profiler = Profiler()
            self.Profiler.start()
            self.StartedSec = time.time()
        except Exception as e:
            self.Logger.error(f"Failed to start profiler: {e}")


    def StopProfile(self) -> None:
        try:
            # Ensure the profiler is enabled.
            if self._EnsureEnabled() is False:
                return

            self.Logger.info(f"Profiler stopping for {self.Feature}")
            self.Profiler.stop()
            self.Profiler.print(unicode=True, color=True, show_all=True)
        except Exception as e:
            self.Logger.error(f"Failed to stop profiler: {e}")


    def ReportIfNeeded(self) -> None:
        try:
            # Ensure the profiler is enabled.
            if self._EnsureEnabled() is False:
                return

            # Get the time to repeat.
            repeatTimeSec = DebugProfiler._EnabledFeatures.get(self.Feature, None)
            if repeatTimeSec is None or repeatTimeSec < 1:
                self.Logger.error(f"Debug profiler ReportIfNeeded called on a feature not setup for repeat profiling. {self.Feature}")
                return

            # Return if it's not time yet.
            if time.time() - self.StartedSec < repeatTimeSec:
                return

            # Stop and restart the profile
            self.Logger.info(f"Profiler reporting for {self.Feature}")
            self.StopProfile()
            self.StartProfile(force=True)
        except Exception as e:
            self.Logger.error(f"Failed to stop profiler: {e}")
        return


    def _EnsureEnabled(self) -> bool:
        if DebugProfiler._EnableProfiling is False:
            return False
        if DebugProfiler._EnabledFeatures.get(self.Feature, None) is None:
            return False
        return True


class MemoryProfiler():

    # Note, this should only be True on dev builds!
    # Also, the package pyinstrument needs to be manually installed.
    _EnableProfiling = False


    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger
        self.Tracker = None
        self._TakeMemoryProfileSnapshot()


    def PrintMemoryDiff(self) -> None:
        try:
            # Ensure the profiler is enabled.
            if self._EnableProfiling is False:
                return

            # Print the diff.
            self.Tracker.print_diff()
        except Exception as e:
            self.Logger.error(f"Failed to start memory profiler: {e}")


    def PrintAllObjectsSummary(self) -> None:
        try:
            # Ensure the profiler is enabled.
            if self._EnableProfiling is False:
                return

            # pylint: disable=import-error
            # pylint: disable=import-outside-toplevel
            from pympler import muppy
            from pympler import summary
            allObjects = muppy.get_objects()
            sumy = summary.summarize(allObjects)
            summary.print_(sumy)
        except Exception as e:
            self.Logger.error(f"Failed to print all objects summary: {e}")


    def PrintRefTreeSummary(self, rootObject) -> None:
        try:
            # Ensure the profiler is enabled.
            if self._EnableProfiling is False:
                return

            # pylint: disable=import-error
            # pylint: disable=import-outside-toplevel
            from pympler import refbrowser
            def output_function(o):
                return str(type(o))
            cb = refbrowser.ConsoleBrowser(rootObject, maxdepth=5, str_func=output_function)
            cb.print_tree()
        except Exception as e:
            self.Logger.error(f"Failed to print ref tree summary: {e}")


    def _TakeMemoryProfileSnapshot(self) -> None:
        try:
            # Ensure the profiler is enabled.
            if self._EnableProfiling is False:
                return

            # Setup the profiler
            # pylint: disable=import-error
            # pylint: disable=import-outside-toplevel
            self.Logger.info("Memory profiler starting snapshot taken.")
            from pympler import tracker
            self.Tracker = tracker.SummaryTracker()
        except Exception as e:
            self.Logger.error(f"Failed to start memory profiler: {e}")
