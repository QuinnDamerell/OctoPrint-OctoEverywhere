import threading
import logging
from typing import Any, Callable

from .sentry import Sentry

class RepeatTimer(threading.Thread):

    def __init__(self, logger:logging.Logger, name:str, intervalSec:float, func:Callable[[], None]):
        threading.Thread.__init__(self, name=name)
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
                Sentry.OnException("Exception in RepeatTimer thread.", e)
        self.logger.debug("RepeatTimer thread exit")


    # Used to update the repeat interval. This can be called while the timer is running
    # or even while in the callback.
    def SetInterval(self, intervalSec:float):
        self.intervalSec = intervalSec


    # Returns the current interval time in seconds
    def GetInterval(self) -> float:
        return self.intervalSec


    # Returns if the timer is currently running or not.
    def IsRunning(self) -> bool:
        return self.running


    # Used to stop the timer.
    def Stop(self):
        self.running = False
        self.stopEvent.set()


    def __enter__(self):
        return self


    def __exit__(self, exc_type:Any, exc_value:Any, traceback:Any):
        self.Stop()
