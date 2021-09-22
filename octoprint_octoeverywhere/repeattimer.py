import threading

class RepeatTimer(threading.Thread):
    def __init__(self, logger, intervalSec, func):
        threading.Thread.__init__(self)
        self.stopEvent = threading.Event()
        self.logger = logger
        self.intervalSec = intervalSec
        self.callback = func

    # Overwrite of the thread function.
    def run(self):
        # Loop while the event isn't set and the thread is still alive.
        while not self.stopEvent.wait(self.intervalSec) and self.is_alive():
            try:
                self.callback()
            except Exception as e:
                self.logger.error("Exception in RepeatTimer thread. "+str(e))
    
    def stop(self):
        self.stopEvent.set()
