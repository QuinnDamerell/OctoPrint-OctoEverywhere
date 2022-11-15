import threading

from octoprint_octoeverywhere.sentry import Sentry

class RepeatTimer(threading.Thread):
    def __init__(self, logger, intervalSec, func):
        threading.Thread.__init__(self)
        self.stopEvent = threading.Event()
        self.logger = logger
        self.intervalSec = intervalSec
        self.callback = func
        self.running = True

    # Overwrite the thread function.
    def run(self):
        # Loop while the event isn't set and the thread is still alive.
        while not self.stopEvent.wait(self.intervalSec) and self.is_alive() and self.running:
            try:
                # Ensure we don't fire the callback if we weren't asked to.
                if self.running is not True:
                    return
                self.callback()
            except Exception as e:
                Sentry.Exception("Exception in RepeatTimer thread.", e)
        self.logger.info("RepeatTimer thread exit")

    # Used to update the repeat interval. This can be called while the timer is running
    # or even while in the callback.
    def SetInterval(self, intervalSec):
        self.intervalSec = intervalSec

    # Returns the current interval time in seconds
    def GetInterval(self):
        return self.intervalSec

    # Used to stop the timer.
    def Stop(self):
        self.running = False
        self.stopEvent.set()
