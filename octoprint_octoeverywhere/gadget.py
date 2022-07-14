import threading

import requests

from octoprint_octoeverywhere.sentry import Sentry
from .repeattimer import RepeatTimer

class Gadget:

    # The default amount of time we will use for the first interval callback.
    c_defaultIntervalSec = 20

    # The default amount of time we will use if we can't get a snapshot.
    c_defaultIntervalSec_NoSnapshot = 120

    # The default amount of time we will use if there was a connection error.
    c_defaultIntervalSec_ConnectionError = 120


    def __init__(self, logger, notificationHandler):
        self.Logger = logger
        self.NotificationHandler = notificationHandler
        self.Lock = threading.Lock()
        self.Timer = None
        self.ProtocolAndDomain = "https://gadget-v1-oeapi.octoeverywhere.com"
        self.FailedConnectionAttempts = 0


    def SetServerProtocolAndDomain(self, protocolAndDomain):
        self.Logger.info("Gadget default domain and protocol set to: "+protocolAndDomain)
        self.ProtocolAndDomain = protocolAndDomain


    def StartWatching(self):
        with self.Lock:
            # Stop any running timer.
            self._stopTimerUnderLock()

            self.Logger.info("Gadget is now watching!")

            # Start a new timer.
            self.Timer = RepeatTimer(self.Logger, Gadget.c_defaultIntervalSec, self._timerCallback)
            self.Timer.start()


    def StopWatching(self):
        with self.Lock:
            self._stopTimerUnderLock()


    def _stopTimerUnderLock(self):
        if self.Timer is not None:
            self.Logger.info("Gadget has stopped watching!")
            self.Timer.Stop()
            self.Timer = None


    def _updateTimerInterval(self, newIntervalSec):
        timer = self.Timer
        if timer is not None:
            timer.SetInterval(newIntervalSec)


    def _getTimerInterval(self):
        timer = self.Timer
        if timer is not None:
            return timer.GetInterval()
        else:
            return Gadget.c_defaultIntervalSec


    def _timerCallback(self):
        try:


            # Before we do anything, update the timer interval to the default, incase there's some error
            # and we don't update it properly. In all cases either an error should update this or the response
            # from the inspect call.
            lastIntervalSec = self._getTimerInterval()
            self._updateTimerInterval(Gadget.c_defaultIntervalSec)

            # Check to ensure we should still be running. If the state is anything other than printing, we shouldn't be running
            # We will be restarted on a new print starting or when resume is called.
            if self.NotificationHandler.ShouldPrintingTimersBeRunning() is False:
                self.Logger.warn("Gadget timer is running but the print state is not printing, so the timer is topping.")
                self.StopWatching()
                return

            # Now, get the common event args, which will include the snapshot.
            requestData = self.NotificationHandler.BuildCommonEventArgs("inspect", None, None)

            # Handle the result indicating we don't have the proper var to send yet.
            if requestData is None:
                self.Logger.info("Gadget didn't send because we don't have the proper id and key yet.")
                self._updateTimerInterval(Gadget.c_defaultIntervalSec)
                return

            # Break out the args
            args = requestData[0]
            files = requestData[1]

            # Add the last interval, so the server knows
            args["LastIntervalSec"] = lastIntervalSec

            # Next, check if there's a valid snapshot image.
            if len(files) == 0:
                # If not, update our interval to be the default no snapshot interval and return.
                self._updateTimerInterval(Gadget.c_defaultIntervalSec_NoSnapshot)
                return

            jsonResponse = None
            try:
                # Setup the url.
                gadgetApiUrl = self.ProtocolAndDomain + "/api/gadget/inspect"

                # Since we are sending the snapshot, we must send a multipart form.
                # Thus we must use the data and files fields, the json field will not work.
                r = requests.post(gadgetApiUrl, data=args, files=files)

                # Check for success. Anything but a 200 we will consider a connection failure.
                if r.status_code != 200:
                    raise Exception("Bad response code "+str(r.status_code))

                # Get the response
                jsonResponse = r.json()
                if jsonResponse is None:
                    raise Exception("No json response found.")

            except Exception as e:
                # For any connection based error, either we fail to connect or we get back not a 200,
                # We will handle it with out logging too much. This can happen if we need to load shed, so we
                # dont need to log about it much.
                if self.FailedConnectionAttempts % 20 == 0:
                    self.Logger.info("Failed to send gadget inspection due to a connection error. "+str(e))
                self.FailedConnectionAttempts += 1

                # Update our timer interval for the failure and return.
                self._updateTimerInterval(Gadget.c_defaultIntervalSec_ConnectionError)
                return

            # Handle the json response. We should find an int telling us how long we should wait before sending the next
            # inspection report.
            if "Result" not in jsonResponse:
                self.Logger.warn("Gadget inspection result had no Result object")
                self._updateTimerInterval(Gadget.c_defaultIntervalSec)
                return
            resultObj = jsonResponse["Result"]
            if "NextInspectIntervalSec" not in resultObj:
                self.Logger.warn("Gadget inspection result had no NextInspectIntervalSec field")
                self._updateTimerInterval(Gadget.c_defaultIntervalSec)
                return

            # Update the next interval time according to what gadget is requesting.
            nextIntervalSec = int(resultObj["NextInspectIntervalSec"])
            self._updateTimerInterval(nextIntervalSec)

            # Reset the failed attempts counter
            self.FailedConnectionAttempts = 0

        except Exception as e:
            Sentry.Exception("Exception in gadget timer", e)
