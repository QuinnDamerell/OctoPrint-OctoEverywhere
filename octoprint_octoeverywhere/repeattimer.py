import threading

class RepeatTimer(threading.Thread):
    def __init__(self, logger, intervalSec, func):
        threading.Thread.__init__(self)
        self.stopEvent = threading.Event()
        self.logger = logger
        self.intervalSec = intervalSec
        self.callback = func
        self.running = True

    # Overwrite of the thread function.
    def run(self):
        # Loop while the event isn't set and the thread is still alive.
        while not self.stopEvent.wait(self.intervalSec) and self.is_alive() and self.running:
            try:
                # Ensure we don't fire the callback if we were asked not to.
                if self.running is not True:
                    return
                self.callback()
            except Exception as e:
                self.logger.error("Exception in RepeatTimer thread. "+str(e))
        self.logger.info("RepeatTimer thread exit")

    # Used to stop the timer.
    def Stop(self):
        self.running = False
        self.stopEvent.set()
