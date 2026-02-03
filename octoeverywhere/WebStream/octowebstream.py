import sys
import time
import queue
import threading
import logging
from typing import Any, Optional

from ..buffer import Buffer
from ..sentry import Sentry
from ..interfaces import IOctoSession, IWebStream
from ..octostreammsgbuilder import OctoStreamMsgBuilder
from .octowebstreamhttphelper import OctoWebStreamHttpHelper
from .octowebstreamwshelper import OctoWebStreamWsHelper
from ..Proto import WebStreamMsg
from ..Proto.MessageContext import MessageContext
from ..Proto.MessagePriority import MessagePriority
from ..debugprofiler import DebugProfiler, DebugProfilerFeatures

#
# Represents a web stream, which is how we send http request and web socket messages.
#
class OctoWebStream(threading.Thread, IWebStream):

    # Created when an open message is sent for a new web stream from the server.
    def __init__(self, group:Any=None, target:Any=None, name:Any=None, args:Any=(), kwargs:Any=None, verbose:Any=None) -> None:
        threading.Thread.__init__(self, group=group, target=target, name=name)
        self.Logger:logging.Logger = args[0]
        self.Id:int = args[1]
        self.OctoSession:IOctoSession = args[2]
        self.OpenWebStreamMsg:Optional[WebStreamMsg.WebStreamMsg] = None
        self.IsClosed = False
        self.HasSentCloseMessage = False
        self.StateLock = threading.Lock()
        self.MsgQueue:queue.Queue[Optional[WebStreamMsg.WebStreamMsg]] = queue.Queue()
        self.HttpHelper:Optional[OctoWebStreamHttpHelper] = None
        self.WsHelper:Optional[OctoWebStreamWsHelper] = None
        self.IsHelperClosed = False
        self.OpenedTime = time.time()
        self.ClosedDueToRequestConnectionError = False

        # Vars for high pri streams
        self.IsHighPriStream = False
        self.HighPriLock = threading.Lock()
        self.ActiveHighPriStreamCount = 0
        self.ActiveHighPriStreamStart = time.time()

        self._debug_id = hex(id(self))
        self.Logger.info(f"🟢 STREAM CREATED [ID: {self._debug_id}] - Total refcount: {sys.getrefcount(self)}")

    def __del__(self):
        # This runs ONLY when the Garbage Collector actually removes the object from memory
        self.Logger.info(f"🔴 STREAM DESTROYED/GC'd [ID: {self._debug_id}]")

    # Called for all messages for this stream id.
    #
    # This function is called on the main OctoSocket receive thread, so it should pass the
    # message off to the thread as quickly as possible.
    def OnIncomingServerMessage(self, webStreamMsg:WebStreamMsg.WebStreamMsg) -> None:
        # Don't accept messages after we are closed.
        # Check under lock to avoid race condition with Close()
        with self.StateLock:
            if self.IsClosed:
                self.Logger.info("Web stream class "+str(self.Id)+" got a incoming message after it has been closed.")
                return

        # If this is a close message, we need to call close now
        # since the main thread might be blocked waiting on a http call or something.
        if webStreamMsg.IsCloseMsg():
            # Note right now we don't support getting close messages with data.
            if webStreamMsg.IsControlFlagsOnly is False:
                self.Logger.warning("Web stream "+str(self.Id)+" got a close message with data. The data will be ignored.")
            # Set this flag under lock, because we don't need to send a close message if the server already did.
            with self.StateLock:
                self.HasSentCloseMessage = True
            # Call close.
            self.Close()
        else:
            # Otherwise, put the message into the queue, so the thread will pick it up.
            self.MsgQueue.put(webStreamMsg)


    # Closes the web stream and all related elements.
    # This is called from the main socket receive thread, so it should
    # execute as quickly as possible.
    def Close(self) -> None:
        # Check the state and set the flag. Only allow this code to run
        # once.
        localHttpHelper:Optional[OctoWebStreamHttpHelper] = None
        localWsHelper:Optional[OctoWebStreamWsHelper] = None

        self.Logger.info(f"🟠 STREAM CLOSED  [ID: {self._debug_id}]")

        with self.StateLock:
            # If we are already closed, there's nothing to do.
            if self.IsClosed is True:
                return
            # We will close now, so set the flag.
            self.IsClosed = True

            # While under lock, exists, and if so, has it been closed.
            # Note it's possible that this helper is being crated on a different
            # thread and will be set just after we exit the lock. In that case
            # the creator logic will notice that the stream is closed and call close on it.
            # So if the http helper doesn't exist yet, we can't set the isClosed flag to false.
            if self.HttpHelper is not None or self.WsHelper is not None:
                if self.IsHelperClosed is False:
                    self.IsHelperClosed = True
                    localHttpHelper = self.HttpHelper
                    localWsHelper = self.WsHelper
                    # Important! Ensure these are set to None so we don't have a circular ref.
                    self.HttpHelper = None
                    self.WsHelper = None

        # Remove ourselves from the session map
        self.OctoSession.WebStreamClosed(self.Id)

        # Put an empty message on the queue to wake it up to exit.
        self.MsgQueue.put(None)

        # Ensure we have sent the close message
        self.ensureCloseMessageSent()

        # If this was high pri, clear the state
        if self.IsHighPriStream:
            self.highPriStreamEnded()

        # If we got a ref to the helper, we need to call close on it.
        try:
            if localHttpHelper is not None:
                localHttpHelper.Close()
            if localWsHelper is not None:
                localWsHelper.Close()
        except Exception as e:
            Sentry.OnException("Web stream "+str(self.Id)+" helper threw an exception during close", e)


    def SetClosedDueToFailedRequestConnection(self) -> None:
        self.ClosedDueToRequestConnectionError = True


    # This is our main thread, where we will process all incoming messages.
    def run(self) -> None:
        # Enable the profiler if needed- it will do nothing if not enabled.
        with DebugProfiler(self.Logger, DebugProfilerFeatures.WebStream):
            try:
                self.mainThread()
            except Exception as e:
                Sentry.OnException("Exception in web stream ["+str(self.Id)+"] connect loop.", e)
                self.OctoSession.OnSessionError(0)


    def mainThread(self) -> None:
        # Loop until we are closed.
        # Check under lock to avoid race condition with Close()
        while True:
            with self.StateLock:
                if self.IsClosed:
                    return

            # Wait on incoming messages
            # Timeout after 60 seconds just to check that we aren't closed.
            # It's important to set this value to None, otherwise on loops it will hold it's old value
            # which can accidentally re-process old messages.
            webStreamMsg:Optional[WebStreamMsg.WebStreamMsg] = None
            try:
                webStreamMsg = self.MsgQueue.get(timeout=60)
            except Exception as _:
                # We get this exception on the timeout.
                pass

            # Check that we aren't closed (under lock for thread safety)
            with self.StateLock:
                if self.IsClosed:
                    return

            # Check that we got a message and this wasn't just a timeout
            if webStreamMsg is None:
                continue

            # Handle the message.
            if webStreamMsg.IsOpenMsg():
                self.initFromOpenMessage(webStreamMsg)

            # Ensure we have an open message.
            if self.OpenWebStreamMsg is None:
                # Throw so we reset the connection.
                raise Exception("Web stream ["+str(self.Id)+"] got a non open message before it's open message.")

            # Don't pass it to the helper if there's nothing more.
            if webStreamMsg.IsControlFlagsOnly():
                continue

            # Allow the helper to process the message
            # We should only ever have one, but just for safety, check both.
            # We need to take a local reference, since they are cleared under lock on close.
            returnValue = True
            httpHelper = self.HttpHelper
            wsHelper = self.WsHelper
            if httpHelper is not None:
                returnValue = httpHelper.IncomingServerMessage(webStreamMsg)
            if wsHelper is not None:
                returnValue = wsHelper.IncomingServerMessage(webStreamMsg)

            # If process server message returns true, we should close the stream.
            if returnValue is True:
                self.Close()
                return

            # When the http helper sends messages, it can indicate that the close flag has been set.
            # In such a case, self.HasSentCloseMessage will be true. We don't want to rely on the client
            # returning the correct returnValue, so if we see that we will call close to make sure things
            # are going down. Since Close() is guarded against multiple entries, this is totally fine.
            # Check under lock for thread safety.
            with self.StateLock:
                shouldClose = self.HasSentCloseMessage is True and self.IsClosed is False
            if shouldClose:
                self.Logger.warning("Web stream "+str(self.Id)+" processed a message and has sent a close message, but didn't call close on the web stream. Closing now.")
                self.Close()
                return


    def initFromOpenMessage(self, webStreamMsg:WebStreamMsg.WebStreamMsg) -> None:
        # Sanity check.
        if self.OpenWebStreamMsg is not None:
            # Throw so we reset the connection.
            raise Exception("Web stream ["+str(self.Id)+"] already have an open message and we got another.")

        # Set the message.
        self.OpenWebStreamMsg = webStreamMsg

        # Check if this is high pri, if so, tell them system a high pri is active
        if self.OpenWebStreamMsg.MsgPriority() < MessagePriority.Normal:
            self.IsHighPriStream = True
            self.highPriStreamStarted()

        # At this point we know what kind of stream we are, http or ws.
        # Create the helper out of lock and then set it.
        # WE MUST ALWAYS SET THE HTTP HELPER OBJECT since down stream logic depends on it existing.
        # But, if the stream has closed since we created this object, we must call close on it.
        httpHelper = None
        wsHelper = None
        if webStreamMsg.IsWebsocketStream():
            wsHelper = OctoWebStreamWsHelper(self.Id, self.Logger, self, self.OpenWebStreamMsg, self.OpenedTime)
        else:
            httpHelper = OctoWebStreamHttpHelper(self.Id, self.Logger, self, self.OpenWebStreamMsg, self.OpenedTime)

        needsToCallCloseOnHelper = False
        with self.StateLock:
            # Set the helper, which ever we made.
            self.HttpHelper = httpHelper
            self.WsHelper = wsHelper

            # If the stream is now closed...
            if self.IsClosed is True:
                # and the http helper didn't get closed called yet...
                if self.IsHelperClosed is False:
                    # We need to call it now.
                    self.IsHelperClosed = True
                    needsToCallCloseOnHelper = True
                    # Important! Ensure these are set to None so we don't have a circular ref.
                    self.HttpHelper = None
                    self.WsHelper = None

        # Outside of lock, if we need to close this helper, do it.
        if needsToCallCloseOnHelper is True:
            if httpHelper is not None:
                httpHelper.Close()
            if wsHelper is not None:
                wsHelper.Close()


    # Called by the helpers to send messages to the server.
    def SendToOctoStream(self, buffer:Buffer, msgStartOffsetBytes:int, msgSize:int, isCloseFlagSet=False, silentlyFail=False) -> None:
        # Make sure we aren't closed. If we are, don't allow the message to be sent.
        with self.StateLock:
            if self.IsClosed is True:
                # The only reason we are allowed to send after a close is if we are sending the
                # close flag message.
                if isCloseFlagSet is False:
                    self.Logger.info("Web Stream "+str(self.Id) + " tried to send a message after close.")
                    return
                else:
                    # We can only send one close flag, so only allow this to send if we haven't sent yet.
                    if self.HasSentCloseMessage:
                        if silentlyFail is False:
                            self.Logger.warning("Web Stream "+str(self.Id)+" tried to send a close message after a close message was already sent")
                        return

            # No matter what, if the close flag is set, set the has sent now.
            if isCloseFlagSet:
                self.HasSentCloseMessage = True

        # Send now
        try:
            self.OctoSession.Send(buffer, msgStartOffsetBytes, msgSize)
        except Exception as e:
            Sentry.OnException("Web stream "+str(self.Id)+ " failed to send a message to the OctoStream.", e)

            # If this was the close message, set the has set flag back to false so we send again.
            # (this mostly won't matter, since the entire connection will go down anyways)
            # Reset under lock for thread safety.
            with self.StateLock:
                self.HasSentCloseMessage = False

            # If we fail, close the entire connection.
            self.OctoSession.OnSessionError(0)

            # Return since things are going down.
            return


    # Ensures the close message is always sent, but only once.
    # The only way the close message doesn't need to be sent is if
    # the other side started the close with a close message.
    def ensureCloseMessageSent(self):
        # Since the send function does the checking to ensure only one close message
        # gets sent, we will always try to create and send a message.
        try:
            builder = OctoStreamMsgBuilder.CreateBuffer(200)
            WebStreamMsg.Start(builder)
            WebStreamMsg.AddStreamId(builder, self.Id)
            WebStreamMsg.AddIsControlFlagsOnly(builder, True)
            WebStreamMsg.AddIsCloseMsg(builder, True)
            WebStreamMsg.AddCloseDueToRequestConnectionFailure(builder, self.ClosedDueToRequestConnectionError)
            webStreamMsgOffset = WebStreamMsg.End(builder)
            buffer, msgStartOffsetBytes, msgSizeBytes = OctoStreamMsgBuilder.CreateOctoStreamMsgAndFinalize(builder, MessageContext.WebStreamMsg, webStreamMsgOffset)
            # Set the flag to silently fail, since the message might have already been sent by the helper.
            self.SendToOctoStream(buffer, msgStartOffsetBytes, msgSizeBytes, True, True)
        except Exception as e:
            # This is bad, log it and kill the stream.
            Sentry.OnException("Exception thrown while trying to send close message for web stream "+str(self.Id), e)
            self.OctoSession.OnSessionError(0)


    # Called by the OctoStreamHttpHelper if the request is normal pri.
    # If a high pri request is active, this should block until it's complete or for a little while.
    def BlockIfHighPriStreamActive(self) ->  None:
        # Check the counter, don't worry about taking the lock, worst case
        # this logic would allow one request through or not block one.

        # If there are no high pri requests, don't block.
        if self.ActiveHighPriStreamCount == 0:
            return
        # As a sanity check, if the high pri request started more than 5 seconds ago
        # don't block.
        timeSinceStartSec = time.time() - self.ActiveHighPriStreamStart
        if timeSinceStartSec > 5.0:
            return

        # We should block this request
        # TODO - this would be better if we blocked on a event or something, but for now this is fine.
        # Note that the http will call this function before the request and on each response read loop, so the delays add up.
        time.sleep(0.1)


    # Called when a high pri stream is started
    def highPriStreamStarted(self) -> None:
        with self.HighPriLock:
            self.ActiveHighPriStreamCount += 1
            self.ActiveHighPriStreamStart = time.time()


    # Called when a high pri stream is ended.
    def highPriStreamEnded(self)  -> None:
        with self.HighPriLock:
            self.ActiveHighPriStreamCount -= 1
