import time
import logging
import threading

from ..sentry import Sentry
from ..repeattimer import RepeatTimer

# A simple class to watch for the bed to cooldown and then fires a notification.
class BedCooldownWatcher:

    # The amount of time between checks in seconds.
    c_checkIntervalSec = 5

    # The max amount of time we will allow this to keep watching.
    # In some cases like enclosed printers, the bed cooldown might take a very long time.
    # We cancel this watcher when a new print starts, so it's safe to have a long runtime.
    c_maxWatcherRuntimeSec = 60 * 60


    def __init__(self, logger:logging.Logger, notificationHandler, printerStateInterface):

        # Default the  the bed is under ~100F, we will consider it cooled down.
        # This can be changed in the config by the user.
        self.CooldownThresholdTempC:float = 40.0

        self.Logger = logger
        self.NotificationHandler = notificationHandler
        self.PrinterStateInterface = printerStateInterface
        self.Timer = None
        self.TimerStartSec = None
        self.IsFirstTimerRead = True
        self.Lock = threading.Lock()


    # Starts the waiter if it's not running.
    def Start(self) -> None:
        with self.Lock:
            # Stop any running timer.
            self._stopTimerUnderLock()

            self.Logger.info("Bed cooldown watcher starting")
            self.TimerStartSec = time.time()
            self.IsFirstTimerRead = True

            # Start a new timer.
            self.Timer = RepeatTimer(self.Logger, "BedCooldownWatcher", BedCooldownWatcher.c_checkIntervalSec, self._timerCallback)
            self.Timer.start()


    # Stops the timer if it's running.
    def Stop(self):
        with self.Lock:
            self._stopTimerUnderLock()


    # Sets the temp that the bed must be under to be considered cooled down.
    def SetBedCooldownThresholdTemp(self, tempC:float):
        self.Logger.debug(f"Bed cooldown watcher, setting threshold temp to {tempC}")
        self.CooldownThresholdTempC = tempC


    def _stopTimerUnderLock(self):
        if self.Timer is not None:
            self.Logger.info("Bed cooldown watcher stopped.")
            self.Timer.Stop()
            self.Timer = None


    def _timerCallback(self):
        try:
            # Check if we should stop watching.
            if time.time() - self.TimerStartSec > BedCooldownWatcher.c_maxWatcherRuntimeSec:
                self.Logger.info("Bed cooldown watcher, max runtime reached. Stopping.")
                self.Stop()
                return

            # Try to get the current temps
            (_, bedTempCelsiusFloat) = self.PrinterStateInterface.GetTemps()
            if bedTempCelsiusFloat is None:
                self.Logger.info("Bed cooldown watcher, no bed temp available. Stopping.")
                self.Stop()
                return

            isFirstTimerRead = self.IsFirstTimerRead
            self.IsFirstTimerRead = False

            # Check if we are cooled down yet.
            if bedTempCelsiusFloat > self.CooldownThresholdTempC:
                # Keep waiting.
                self.Logger.debug(f"Bed cooldown watcher, bed temp is {bedTempCelsiusFloat}. Waiting...")
                return

            # If this is the first read and the bed is already cool, we won't notify.
            if isFirstTimerRead:
                self.Logger.info("Bed cooldown watcher, bed is cooled down, but it was already cool on the first read, so we won't notify.")
                self.Stop()
                return

            # The bed is cooled down.
            self.Logger.info("Bed cooldown watcher, bed is cooled down.")
            self.Stop()

            # Fire the notification.
            self.NotificationHandler.OnBedCooldownComplete(bedTempCelsiusFloat)

        except Exception as e:
            Sentry.Exception("BedCooldownWatcher exception in timer callback", e)
