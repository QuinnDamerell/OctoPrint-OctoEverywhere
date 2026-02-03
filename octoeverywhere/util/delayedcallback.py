import threading
import logging
from typing import Any, Callable

from octoeverywhere.sentry import Sentry


# A simple class that fires a callback after a delay unless it's canceled.
class DelayedCallback(threading.Thread):

    @staticmethod
    def Create(logger:logging.Logger, name:str, delaySec:float, func:Callable[[], None]) -> "DelayedCallback":
        cb = DelayedCallback(logger, name, delaySec, func)
        cb.start()
        return cb


    def __init__(self, logger:logging.Logger, name:str, delaySec:float, func:Callable[[], None]):
        threading.Thread.__init__(self, name=name)
        self.stopEvent = threading.Event()
        self.logger = logger
        self.delaySec = delaySec
        self.callback = func
        self.running = True


    # Overwrite the thread function.
    def run(self):
        try:
            # Wait for the delay to elapse, unless canceled.
            self.logger.debug("DelayedCallback starting: %s", self.name)
            if self.stopEvent.wait(self.delaySec) is False:
                # Ensure we don't fire the callback if we weren't asked to.
                if self.is_alive() is False or self.running is False:
                    return
                try:
                    self.callback()
                except Exception as e:
                    Sentry.OnException("Exception in DelayedCallback thread.", e)
        finally:
            self.logger.debug("DelayedCallback thread exit: %s", self.name)


    # Returns if the timer is currently running or not.
    def IsRunning(self) -> bool:
        return self.running


    # Used to cancel the timer before it fires.
    def Cancel(self):
        self.running = False
        self.stopEvent.set()


    def __enter__(self):
        return self


    def __exit__(self, exc_type:Any, exc_value:Any, traceback:Any):
        self.Cancel()
