import threading
import sys

# A simple class to make a timer that repeats.
# Unfortunately, due to differences in PY 2 and 3, we have to this hacky mess.
if sys.version_info[0] < 3:
    class RepeatTimer(threading._Timer):
        def run(self):
            while not self.finished.wait(self.interval):
                self.function(*self.args, **self.kwargs)
else:
    class RepeatTimer(threading.Timer):
        def run(self):
            while not self.finished.wait(self.interval):
                self.function(*self.args, **self.kwargs)