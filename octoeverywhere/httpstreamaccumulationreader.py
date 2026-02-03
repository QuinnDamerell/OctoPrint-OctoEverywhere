

import collections
import logging
import threading
import time
from typing import  Optional

import urllib3.exceptions

from .buffer import Buffer, BufferOrNone
from .httpresult import HttpResult
from .sentry import Sentry


# This class can be used to read any Request.Response object stream, no matter the content type.
# The point of this class is that when a read call is made, it will always wait for an accumulation time before returning data.
# If there is no data to be returned, it will block until there is data, and then wait for the accumulation time.
#
# The special behavior of this class is that if there's data to be returned, it will always return it after the accumulation time.
# Where as in contrast to a normal read, we might accumulate data but then get stuck in a read call waiting for more data.
#
# For example,
#     Read is called, there's 10 bytes available to read.
#     The 10 bytes is read from the request, but we want to accumulate for 10ms, so we call read on the request again.
#     The server doesn't send more data for 1 minute, like in an event stream.
#   In this example, we have the 10 bytes of data, but it's blocked waiting for the request read before it can be returned.
#
class HttpStreamAccumulationReader:

    # This is only used for logging so it doesn't need to be thread safe.
    c_UniqueIdCounter = 0


    # After being constructed the reading starts immediately.
    # This class must be disposed of properly to stop the read thread.
    def __init__(self, logger:logging.Logger, httpResult:HttpResult, accumulationTimeSec:float, maxReturnBufferSizeBytes:Optional[int]=None):
        self.Logger = logger
        self.HttpResult = httpResult
        self.AccumulationTimeSec = accumulationTimeSec

        # We need this max size to ensure the read loop doesn't read a buffer that's too large for the read call to return.
        if maxReturnBufferSizeBytes is None:
            maxReturnBufferSizeBytes = 10 * 1024 * 1024 # 10 MB
        self.MaxReturnBufferSizeBytes = maxReturnBufferSizeBytes

        # We use a list so we can efficiently append all of the pending buffers at once when they are being sent.
        self.BufferList:collections.deque[bytes] = collections.deque()
        self.BufferListPendingSize:int = 0
        self.BufferLock = threading.Lock()
        self.BufferDataReadyEvent = threading.Event()

        # Set to true when the read is done either from the end of the body or an error.
        # Once true, it will never read again, but we do need to process the BufferList
        self.ReadComplete = False

        # Set when this object has been told to close.
        self.IsClosed = False

        # Not thread safe, but we only use this for logging so it's ok.
        self.LogId = HttpStreamAccumulationReader.c_UniqueIdCounter
        HttpStreamAccumulationReader.c_UniqueIdCounter += 1
        self.ShouldDebugLog = self.Logger.isEnabledFor(logging.DEBUG)

        # Ensure we have something to read
        self.ResponseBody = httpResult.ResponseForBodyRead
        if self.ResponseBody is None:
            raise Exception("HttpStreamAccumulationReader was called with a httpResult that has not Response object to read from.")

        # Start the read thread.
        self.debugLog("Starting read thread.")
        self.ReadThread = threading.Thread(target=self.readThreadWorker)
        # Mark it as a daemon to prevent it from stopping PY from shutting down the process if it's the only thread running.
        self.ReadThread.daemon = True
        self.ReadThread.start()


    # Must be called to close the stream reader and clean up the thread.
    # NOTE - This will also close the stream body to ensure the read thread exists!
    def Close(self):
        try:
            self.debugLog("Close called on HttpStreamAccumulationReader.")

            # Set the event to break the stream read wait, so it will shutdown.
            # Call set under lock, to ensure the other thread doesn't clear it without us seeing it.
            with self.BufferLock:
                if self.IsClosed:
                    self.debugLog("Close called but HttpStreamAccumulationReader is already closed, ignoring this call.")
                    return
                self.IsClosed = True
                self.ReadComplete = True
                self.BufferDataReadyEvent.set()

            # We need to make sure the http body is closed to ensure the read thread exits.
            try:
                if self.ResponseBody is not None:
                    self.debugLog("Closing HTTP response body in HttpStreamAccumulationReader.Close.")
                    self.ResponseBody.raw.close()
            except Exception as e:
                self.Logger.info(f"{self.getLogMsgPrefix()} Exception thrown when closing the HTTP response body in HttpStreamAccumulationReader.Close: {str(e)}")

            # We do not want to join the thread because we don't close the HTTP body, and the thread will not return until the body read is done.
            # Instead, we just set the IsClosed flag and the event, and let the thread exit when the body read is done.

            self.debugLog("Close completed on HttpStreamAccumulationReader.")
        except Exception as e:
            Sentry.OnException(self.getLogMsgPrefix()+ " exception thrown in HttpStreamAccumulationReader.Close", e)



    # Reads up to the max buffer size, and will always wait for accumulation.
    #
    # If data is ready, it will wait the accumulation time, gather all ready data, and return it.
    # If no data is ready, it will block until data is ready, and then wait for accumulation time, gather all data, and return it.
    # If the HTTP body read is complete and there's no more data, it will will return None.
    #
    # If a timeout is set, if the timeout is hit before any data is ready, it will return None.
    def Read(self, timeoutSec:Optional[float]=None) -> BufferOrNone:

        # Just as a sanity check, we will define the max amount of time we will wait for one chunk.
        # This will make sure we don't get stuck in a loop if there are any bugs.
        maxReadTimeSec = 20 * 60 * 60 # 20 hours

        # Validate the timeout.
        if timeoutSec is not None and (timeoutSec < 0 or timeoutSec > self.AccumulationTimeSec):
            raise Exception(f"HttpStreamAccumulationReader.Read was called with an invalid timeoutSec of {timeoutSec}. It must be None or between 0 and the accumulation time of {self.AccumulationTimeSec} seconds.")

        try:
            readCallStartTimeSec = time.time()
            accumulatedBufferList:Optional[collections.deque[bytes]] = None
            accumulatedBufferListSizeBytes = 0
            mustReturnAccumulatedBuffers = False
            firstAccumulatedBufferTime = None
            isFirstLoopRun = True

            # Since we will always sleep for at least the min time, there's no need to do work until the min time is meet.
            # If we did do the loop, we would just end up spinning and sleeping again.
            time.sleep(self.AccumulationTimeSec)

            # Try to read a chunk or wait for the read to be done.
            # Only try to read while the stream is open.
            while self.IsClosed is False:

                # First, sanity check we haven't been running forever.
                loopStartNow = time.time()
                if loopStartNow - readCallStartTimeSec > maxReadTimeSec:
                    raise Exception(f"HttpStreamAccumulationReader has been waiting for a chunk for {maxReadTimeSec} seconds. This is an error.")

                # Check for a read timeout, if we hit it, break.
                # This will return whatever we have accumulated so far, which might be empty, which means the read is done.
                if timeoutSec is not None and loopStartNow - readCallStartTimeSec > timeoutSec:
                    self.debugLog(f"Read timeout of {timeoutSec} seconds hit in HttpStreamAccumulationReader.Read.")
                    break

                # Next, check to see if there are new buffers to add to our accumulation list.
                with self.BufferLock:
                    if len(self.BufferList) > 0:
                        # There are buffers to read!

                        # If the pending size + our accumulated size is under the max, take them all.
                        if accumulatedBufferListSizeBytes + self.BufferListPendingSize < self.MaxReturnBufferSizeBytes:
                            if accumulatedBufferList is None:
                                accumulatedBufferList = self.BufferList
                            else:
                                accumulatedBufferList += self.BufferList
                            accumulatedBufferListSizeBytes += self.BufferListPendingSize

                            # Reset the pending list.
                            self.BufferList = collections.deque()
                            self.BufferListPendingSize = 0

                        else:
                            # If we are here, the pending buffers are larger than our max size.

                            # This is an important flag, it means that we need to return what we currently have because the next buffer will put us over the max.
                            mustReturnAccumulatedBuffers = True

                            # We need to only take as many as we can up to the max size.
                            while accumulatedBufferListSizeBytes < self.MaxReturnBufferSizeBytes and len(self.BufferList) > 0:

                                # See how big the next buffer is.
                                nextBuffer = self.BufferList[0]
                                nextBufferSize = len(nextBuffer)
                                if accumulatedBufferListSizeBytes + nextBufferSize > self.MaxReturnBufferSizeBytes:
                                    # It's too big, we are done.
                                    break

                                # We can take this entire buffer.
                                if accumulatedBufferList is None:
                                    accumulatedBufferList = collections.deque()
                                    accumulatedBufferList.append(nextBuffer)
                                else:
                                    accumulatedBufferList.append(nextBuffer)
                                accumulatedBufferListSizeBytes += nextBufferSize

                                # Remove it from the pending list.
                                self.BufferList.popleft()
                                self.BufferListPendingSize -= nextBufferSize

                        # Before we clear it under lock, always check to see if the isClosed flag is set.
                        # This ensures we don't miss the close flag before we clear the event and wait on it again.
                        if self.IsClosed:
                            # If we are closed, return None to end the read.
                            return None

                        # Clear the event under lock, so we don't miss a new set.
                        self.BufferDataReadyEvent.clear()

                #
                # We are done accumulating new buffers, if any.

                # The first time we get any buffers, we need to start the accumulation timer.
                if accumulatedBufferList is not None and firstAccumulatedBufferTime is None:
                    firstAccumulatedBufferTime = loopStartNow

                # If we have chunks check if we are done.
                # There are three cases:
                #    1. We have to return the accumulated buffers because the next buffer would put us over the max size.
                #    2. If this is the first loop run. Since we always sleep for the accumulation time before starting the loop, if the first run picked up buffers, we are done.
                #    3. If it's not the first loop run, We have been accumulating for longer than the accumulation time.
                if accumulatedBufferList is not None:
                    # This should be impossible due to the above check, but sanity check it.
                    if firstAccumulatedBufferTime is None:
                        raise Exception("Internal error in HttpStreamAccumulationReader.Read: firstAccumulatedBufferTime is None but accumulatedBufferList is not None.")
                    if mustReturnAccumulatedBuffers or isFirstLoopRun or loopStartNow - firstAccumulatedBufferTime >= self.AccumulationTimeSec:
                        break
                isFirstLoopRun = False

                # AFTER we have accumulated the buffers, we need to check to see if the read is complete.
                # This must be done after to ensure that any last buffers are read.
                if self.ReadComplete:
                    break

                # If we are here, we need to wait for more data or the accumulation time.
                # If we already got the first buffer, we know how long we need to wait for accumulation.
                # So there's no need to wait on the event, which might wake us up multiple times in the accumulation time.
                now = time.time()
                if accumulatedBufferList is not None:
                    if firstAccumulatedBufferTime is None:
                        raise Exception("Internal error in HttpStreamAccumulationReader.Read: firstAccumulatedBufferTime is None but accumulatedBufferList is not None.")
                    elapsedAccumulationTimeSec = now - firstAccumulatedBufferTime
                    remainingAccumulationTimeSec = self.AccumulationTimeSec - elapsedAccumulationTimeSec
                    if remainingAccumulationTimeSec > 0:
                        time.sleep(remainingAccumulationTimeSec)
                    # Run the loop again to pick up any new buffers and then return whatever we have.
                    continue

                # If we have no buffers, we need to wait.
                sleepTimeSec = maxReadTimeSec
                if timeoutSec is not None:
                    elapsedSec = now - readCallStartTimeSec
                    remainingTimeoutSec = timeoutSec - elapsedSec
                    sleepTimeSec = min(sleepTimeSec, remainingTimeoutSec)
                if sleepTimeSec > 0:
                    self.BufferDataReadyEvent.wait(sleepTimeSec)

            # If we broke out of the loop, 3 things could have happened:
            #    1. We have accumulated buffers to return.
            #    2. The read is complete.
            #    3. We hit a timeout.
            if accumulatedBufferList is None:
                return None

            # Optimize for the single chunk scenario.
            if len(accumulatedBufferList) == 1:
                return Buffer(accumulatedBufferList[0])

            # Append all of the chunks together and return the buffer!
            # We use this method to efficiently copy memory from multiple buffers into one.
            totalLength = sum(len(b) for b in accumulatedBufferList)

            # Allocate a buffer to hold all of the chunks.
            finalBuffer = bytearray(totalLength)
            offset = 0
            for buffer in accumulatedBufferList:
                view = memoryview(buffer)
                with view:
                    finalBuffer[offset:offset + len(view)] = view
                    offset += len(view)

            # Sanity check
            if len(finalBuffer) != totalLength:
                raise Exception(f"Final appended buffer was {len(finalBuffer)} but it should have been {totalLength}")

            # Return!
            return Buffer(finalBuffer)

        except Exception as e:
            Sentry.OnException(self.getLogMsgPrefix()+ " exception thrown in HttpStreamAccumulationReader. Ending body read.", e)
            return None


    def debugLog(self, msg:str) -> None:
        # This should be as fast as possible when debug logging is disabled.
        if not self.ShouldDebugLog:
            return
        self.Logger.debug("%s %s", self.getLogMsgPrefix(), msg)


    def getLogMsgPrefix(self) -> str:
        return f"[HttpStreamAccumulationReader-{self.LogId}]"


    # This is the read thread, where the actual reading from the HTTP response happens.
    def readThreadWorker(self):
        try:
            self.debugLog("Read thread starting")

            # Get the response to read from.
            response = self.ResponseBody
            if response is None:
                raise Exception("HttpStreamAccumulationReader was called with a httpResult that has not Response object to read from.")

            # Ensure the response raw object has a read1 function.
            useRead1 = hasattr(response.raw, "read1")
            if not useRead1:
                self.Logger.info(f"{self.getLogMsgPrefix()} Warning: response.raw does not have read1 function, falling back to read. This may impact performance.")

            # Loop until the stream is closed.
            while self.IsClosed is False:

                if useRead1:
                    # Read1 is the magic function we need. If there's already a buffer to be read, it will return it up to the size we request.
                    # If not, it will make one OS function call to block until there's anything, and then will return it.
                    # This is exactly what we want here.
                    #
                    # We use the max read size so we basically read as much as we can in one call. Since we are sleeping for the accumulation time before each read,
                    # we need to make sure we read a sufficient amount of data each time to keep up.
                    chunk = response.raw.read1(self.MaxReturnBufferSizeBytes) #pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue] this doesn't exist on PY 3.7
                else:
                    # According to Google some wrappers of the raw object won't have read1, so we fall back to read.
                    # Since read blocks until the requested size is read or the body is done, we read in smaller chunks.
                    chunk = response.raw.read(1024)

                if not chunk:
                    # If nothing is returned, the body read is complete.
                    self.debugLog("Read thread reached clean end of body stream.")
                    break

                # Append the chunk to the buffer list.
                with self.BufferLock:
                    self.BufferList.append(chunk)
                    bufferLen = len(chunk)
                    self.BufferListPendingSize += bufferLen

                    # This should be impossible due to the read1 size, but sanity check it.
                    if bufferLen > self.MaxReturnBufferSizeBytes:
                        raise Exception(f"The unknown body chunk read thread read a chunk larger than the max single chunk size of {self.MaxReturnBufferSizeBytes} bytes! Read size: {bufferLen} bytes.")

                    self.BufferDataReadyEvent.set()

            # When the loop exits, the body read is complete and the stream is closed.

        except urllib3.exceptions.HTTPError as e:
            # These happen for a variety of reasons, including the stream being closed.
            # Don't send it to Sentry.
            self.Logger.info(f"{self.getLogMsgPrefix()} HTTPError exception thrown in HttpStreamAccumulationReader, ending read. {str(e)}")

        except Exception as e:
            # If the web stream is already closed, don't bother logging the exception.
            # These exceptions happen for use cases as above, where stream() doesn't close in time and such.
            # Note the exception can be a timeout, but it can also be a "doesn't have a read" function error bc if the socket gets data the lib will try to call read on a fp that's closed and set to None. :/
            if self.IsClosed is False:
                Sentry.OnException(self.getLogMsgPrefix()+ " exception thrown in HttpStreamAccumulationReader", e)
        finally:

            # Set the event to break the stream read wait, so it will shutdown.
            # Call set under lock, to ensure the other thread doesn't clear it without us seeing it.
            with self.BufferLock:
                # Ensure we always set this flag, so the web stream will know the body read is done.
                self.ReadComplete = True
                self.BufferDataReadyEvent.set()

            try:
                self.debugLog("Read thread exited.")
            except Exception:
                pass
