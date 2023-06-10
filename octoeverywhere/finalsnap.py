import math
import logging
import threading

from .sentry import Sentry
from .repeattimer import RepeatTimer

# A helper class to try to capture a better "print completed" image by taking images before the complete notification
# so we have images from shortly before the notification fires. This is needed because most printers will move the
# print head away from the print after completing. If the camera is mounted to the print arm, then the print might not
# be in frame.
class FinalSnap:

    # The default interval that we will snap an image at.
    c_defaultSnapIntervalSec = 1

    # When the on complete notification fires, this is how long we will try to go back in time to fetch a snapshot.
    c_onCompleteSnapDelaySec = 4

    # Creates the object and starts the timer.
    def __init__(self, logger:logging.Logger, notificationHandler) -> None:
        self.Logger = logger
        self.NotificationHandler = notificationHandler
        self.SnapLock = threading.Lock()
        self.SnapHistory = []
        self.Timer = RepeatTimer(self.Logger, FinalSnap.c_defaultSnapIntervalSec, self._snapCallback)
        self.Timer.start()


    # Gets a final snapshot image is possible and shuts down the class.
    # If no final image exists, this will return null.
    def GetFinalSnapAndStop(self):
        # Stop the timer
        self.Timer.Stop()

        # Return the oldest snap if we have one.
        with self.SnapLock:
            if len(self.SnapHistory) > 0:
                # If we have an image, return it.
                # Clear the array to free up space of stored images, just incase this class leaks.
                self.Logger.info(f"Stopping final snap and using snapshot from ~ {FinalSnap.c_onCompleteSnapDelaySec} sec ago")
                snap = self.SnapHistory[0]
                self.SnapHistory = []
                return snap

        # If we don't have an image, just return None.
        self.Logger.info("Stopping final snap but there's no snapshot to use.")
        return None


    # Fires when we should take a new snapshot.
    def _snapCallback(self):
        try:
            # Try to get a snapshot.
            snapshot = self.NotificationHandler.GetNotificationSnapshot()
            if snapshot is None:
                self.Logger.info("FinalSnap failed to get a snapshot")
                return

            with self.SnapLock:
                # Make sure we are still running, otherwise there's no reason to store the image.
                if self.Timer.IsRunning() is False:
                    return

                # Add this most recent snapshot to the end.
                self.SnapHistory.append(snapshot)

                # Trim the front of the list according to how far back we want an image.
                # Take the amount of time we want to delay / by the time interval for each snap.
                desiredImageHistoryCount = int(math.ceil(float(FinalSnap.c_onCompleteSnapDelaySec) / float(FinalSnap.c_defaultSnapIntervalSec)))

                # Sanity check.
                if desiredImageHistoryCount < 1:
                    self.Logger.error(f"FinalSnap desiredImageHistoryCount is < 1!! {desiredImageHistoryCount}")
                    desiredImageHistoryCount = 1

                while len(self.SnapHistory) > desiredImageHistoryCount:
                    # Remove the oldest image, until we reach the front of the list.
                    self.SnapHistory.pop(0)

        except Exception as e:
            Sentry.Exception("FinalSnap::_snapCallback failed to get snapshot.", e)
