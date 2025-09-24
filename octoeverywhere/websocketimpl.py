import queue
import ssl
import threading
import logging
from typing import Any, Dict, List, Callable, Optional

import certifi
import octowebsocket

from octowebsocket import WebSocketApp, WebSocket

from .interfaces import WebSocketOpCode, IWebSocketClient
from .buffer import Buffer, BufferOrNone
from .sentry import Sentry

# This class gives a bit of an abstraction over the normal ws
class Client(IWebSocketClient):


    # Allows us to still enable the websocket debug logs if we want.
    @staticmethod
    def SetWebsocketDebuggingLevel(debug:bool) -> None:
        # The websocket lib logs quite a lot of stuff, even to info. It will also always logs errors,
        # even after our handler had handled them. So we will disable it by default.
        wsLibLogger = logging.getLogger("websocket")
        if debug is False:
            wsLibLogger.disabled = True
            return
        wsLibLogger.disabled = False
        wsLibLogger.setLevel(logging.DEBUG)


    def __init__(
                self,
                url:str,
                onWsOpen:Optional[Callable[[IWebSocketClient], None]]=None,
                onWsData:Optional[Callable[[IWebSocketClient, Buffer, WebSocketOpCode], None]]=None,
                onWsClose:Optional[Callable[[IWebSocketClient], None]]=None,
                onWsError:Optional[Callable[[IWebSocketClient, Exception], None]]=None,
                headers:Optional[Dict[str, str]]=None,
                subProtocolList:Optional[List[str]]=None
                ) -> None:

        # Set the default timeout for the socket. There's no other way to do this than this global var, and it will be shared by all websockets.
        # This is used when the system is writing or receiving, but not when it's waiting to receive, as that's a select()
        # We set it to be something high because most all errors will be handled other ways, but this prevents the websocket from hanging forever.
        # The value is in seconds, we currently set it to 10 minutes.
        octowebsocket.setdefaulttimeout(10 * 60)

        # Since we also fire onWsError if there is a send error, we need to capture
        # the callback and have some vars to ensure it only gets fired once.
        self.clientWsErrorCallback = onWsError
        self.clientWsCloseCallback = onWsClose
        self.wsErrorCallbackLock = threading.Lock()
        self.hasFiredWsErrorCallback = False
        self.hasPendingErrorCallbackFire = False
        self.hasDeferredCloseCallbackDueToPendingErrorCallback = False
        self.disableCertCheck = False

        # We use a send queue thread because it allows us to process downloads about 2x faster.
        # This is because the downstream work of the WS can be made faster if it's done in parallel
        self.SendQueue:queue.Queue[SendQueueContext] = queue.Queue()
        self.SendThread:threading.Thread = None #pyright: ignore[reportAttributeAccessIssue]

        # Used to indicate if the client has started to close this WS. If so, we won't fire
        # any errors.
        self.hasClientRequestedClose = False

        # This is used to keep track of this object has been closed.
        # If this flag is true, this object should not be running and will never run again.
        self.isClosed = False
        self.isClosedLock = threading.Lock()

        def OnOpen(ws:WebSocket):
            if onWsOpen:
                onWsOpen(self)

        # Note that the API says this only takes one arg, but after looking into the code
        # _get_close_args will try to send 3 args sometimes. There have been client errors showing that
        # sometimes it tried to send 3 when we only accepted 1.
        def OnClosed(ws:WebSocket, code:Any, msg:Any):
            # We need to check this special case.
            # If the error callback is pending, we need to defer the close callback until the error callback is fired.
            # Otherwise, we handle the error, kick off the thread to fire the error callback, and then fire close before the error callback.
            with self.wsErrorCallbackLock:
                if self.hasPendingErrorCallbackFire:
                    self.hasDeferredCloseCallbackDueToPendingErrorCallback = True
                    return
            self._FireCloseCallback()

        def OnData(ws:WebSocket, buffer:bytearray, msgType:int, msgFin:bool):
            # Note, we only fire on data when the msgFin is True!
            # OnData is called each time a chunk arrives and then when the full buffer is received.
            # To make things more simple, we only worry about the full buffer.
            if msgFin and onWsData:
                onWsData(self, Buffer(buffer), WebSocketOpCode.FromWsLibInt(msgType))

        def OnError(ws:WebSocket, exception:Exception):
            # For this special case, call our function.
            self.handleWsError(exception)

        # Create the websocket. Once created, this is never destroyed while this class exists.
        self.Ws = WebSocketApp(url,
                                  on_open = OnOpen,
                                  on_close = OnClosed,
                                  on_error = OnError,
                                  on_data = OnData,
                                  header = headers,
                                  subprotocols = subProtocolList
        )


    # This has it's own function so the caller very explicitly has to call it, rather than it being an init overload.
    # If set to true, this websocket connection will not validate the cert it's connecting to. This should only be done locally!
    def SetDisableCertCheck(self, disable:bool):
        self.disableCertCheck = disable


    # Runs the websocket blocking until it closes.
    def RunUntilClosed(self, pingIntervalSec:Optional[int]=None, pingTimeoutSec:Optional[int]=None, pingPayload:str="") -> None:
        #
        # The client is responsible for sending keep alive pings the server will then pong respond to.
        # If that's not done, the connection will timeout. We will send a ping every 10 minutes.
        #
        # skip_utf8_validation=True is important, because otherwise we waste a lot of time doing slow, py based validation code.
        #
        # Important note! This websocket lib won't use certify which a Root CA store that mirrors what firefox uses.
        # Since let's encrypt updated their CA root, we need to use certify's root or the connection will likely fail.
        # The requests lib already does this, so we only need to worry about it for websockets.
        # Another important note!
        #
        # The ping_timeout is used to timeout the select() call when the websocket is waiting for data. There's a bug in the WebSocketApp
        # where it will call select() after the socket is closed, which makes select() hang until the time expires.
        # Thus we need to keep the ping_timeout low, so when this happens, it doesn't hang forever.
        if pingTimeoutSec is None or pingTimeoutSec <= 0 or pingTimeoutSec > 60:
            pingTimeoutSec = 20
        # Ensure that the ping timeout is set, otherwise the websocket will hang forever if the connection is lost.
        # This is also important to ensure that NAT routers and load balancers keep the connection alive.
        if pingIntervalSec is None or pingIntervalSec <= 0:
            pingIntervalSec = 30

        try:
            # Start the send queue thread if it hasn't been started.
            # Do this in the try block, so if we fail to start the thread the error is handled.
            if self.SendThread is None:
                self.SendThread = threading.Thread(target=self._SendQueueThread, daemon=True)
                self.SendThread.start()

            # Do validation on the ping interval and timeout.
            # The API requires the timeout be less than the interval.
            if pingTimeoutSec >= pingIntervalSec:
                pingTimeoutSec = pingIntervalSec - 1
            if pingIntervalSec > 0 and pingTimeoutSec <= 0:
                raise Exception("The ping timeout must be greater than 0.")

            # Only if the client explicated called the function to disable this will we turn off cert verification.
            sslopt={"ca_certs":certifi.where()}
            if self.disableCertCheck:
                sslopt = {"cert_reqs":  ssl.CERT_NONE, "check_hostname": False}

            # Since some clients use RunAsync, check that we didn't close before the async action started.
            with self.isClosedLock:
                if self.isClosed:
                    return

            self.Ws.run_forever(skip_utf8_validation=True, ping_interval=pingIntervalSec, ping_timeout=pingTimeoutSec, sslopt=sslopt, ping_payload=pingPayload)  #pyright: ignore[reportUnknownMemberType]
        except Exception as e:
            # There's a compat issue where  run_forever will try to access "isAlive" when the socket is closing
            # "isAlive" apparently doesn't exist in some PY versions of thread, so this throws. We will ignore that error,
            # But for others we will call OnError.
            #
            # If it is the error message we will just return indication that the socket is closed.
            msg = str(e)
            if "'Thread' object has no attribute 'isAlive'" not in msg:
                self.handleWsError(e)


    # Runs the websocket async.
    def RunAsync(self):
        t = threading.Thread(target=self.RunUntilClosed, args=())
        t.daemon = True
        t.start()


    # Closes the websocket.
    def Close(self):
        self.hasClientRequestedClose = True
        # Always try to call close, even if we have already done it.
        self._Close()


    # Internally used to close and cleanup.
    def _Close(self):

        # Set that we are now closed.
        with self.isClosedLock:
            self.isClosed = True

        # Always try to call close, even if we have already done it.
        # Now ensure the websocket is closing. Since it most likely already is, ignore any exceptions.
        try:
            self.Ws.close() #pyright: ignore[reportUnknownMemberType]
        except Exception as e:
            # This is a known bug in the websocket class, it happens when the WS is closing.
            if isinstance(e, AttributeError) and "object has no attribute 'close'" in str(e):
                # We don't have a logger, sooooooo
                print("Websocket closed due to: 'NoneType' object has no attribute 'close'")
            else:
                Sentry.OnException("Websocket fireWsErrorCallbackThread close exception", e)

        # Always ensure we close the send queue.
        try:
            # Push an empty buffer to the send queue, which will close it.
            self.SendQueue.put(SendQueueContext(None))
        except Exception as e:
            Sentry.OnException("Exception while trying to close the send queue.", e)


    def _FireCloseCallback(self):
        if self.clientWsCloseCallback:
            self.clientWsCloseCallback(self)


    # This can be called from our logic internally in this class or from the websocket class itself
    def handleWsError(self, exception:Exception):

        with self.wsErrorCallbackLock:
            # If the client is trying to close this websocket and has made the close call to do so,
            # we won't fire any more errors out of it. This can happen if a send is trying to send data
            # at the same time as the socket is closing for example.
            if self.hasClientRequestedClose:
                return

            # Since this callback can be fired from many sources, we want to ensure it only
            # gets fired once.
            if self.hasFiredWsErrorCallback:
                return
            self.hasFiredWsErrorCallback = True

            # This is important to set, to prevent a race between close being called before the error callback is fired.
            self.hasPendingErrorCallbackFire = True

        # To prevent locking issues or other issues, spin off a thread to fire the callback.
        # This prevents the case where send() fires the callback, we don't want to overlap the
        # send path callback.
        callbackThread = threading.Thread(target=self.fireWsErrorCallbackThread, args=(exception, ))
        callbackThread.start()


    def fireWsErrorCallbackThread(self, exception:Exception):
        try:
            # Fire the error callback.
            if self.clientWsErrorCallback:
                self.clientWsErrorCallback(self, exception)
        except Exception as e :
            Sentry.OnException("Websocket client exception in fireWsErrorCallbackThread", e)

        # Once the error callback is fired, we can now fire the close callback if needed.
        try:
            with self.wsErrorCallbackLock:
                self.hasPendingErrorCallbackFire = False
                # If the close callback was deferred, we need to fire it now.
                if self.hasDeferredCloseCallbackDueToPendingErrorCallback:
                    self.hasDeferredCloseCallbackDueToPendingErrorCallback = False
                    self._FireCloseCallback()
        except Exception as e:
            Sentry.OnException("Websocket client exception in fireWsErrorCallbackThread", e)

        # Be sure we always close the WS
        self._Close()


    def Send(self, buffer:Buffer, msgStartOffsetBytes:Optional[int]=None, msgSize:Optional[int]=None, isData:bool=True) -> None:
        if isData:
            self.SendWithOptCode(buffer, msgStartOffsetBytes, msgSize, WebSocketOpCode.BINARY)
        else:
            self.SendWithOptCode(buffer, msgStartOffsetBytes, msgSize, WebSocketOpCode.TEXT)


    # Sends a buffer, with an optional message start offset and size.
    # If the message start offset and size are not provided, it's assumed the buffer starts at 0 and the size is the full buffer.
    # Providing a bytearray with room in the front allows the system to avoid copying the buffer.
    def SendWithOptCode(self, buffer:Buffer, msgStartOffsetBytes:Optional[int]=None, msgSize:Optional[int]=None, optCode=WebSocketOpCode.BINARY) -> None:
        try:
            # Make sure we have a buffer, this is invalid and it will also shutdown our send thread.
            if buffer is None:
                raise Exception("We tired to send a message to the websocket with a None buffer.")
            self.SendQueue.put(SendQueueContext(buffer, msgStartOffsetBytes, msgSize, optCode))
        except Exception as e:
            # If any exception happens during sending, we want to report the error
            # and shutdown the entire websocket.
            self.handleWsError(e)


    def _SendQueueThread(self):
        try:
            while self.isClosed is False:
                # Wait on something to send.
                context:SendQueueContext = self.SendQueue.get()
                # If it's None, that means we are shutting down.
                if context is None or context.Buffer is None:
                    return
                # Send it!
                # Important! We don't want to use the frame mask because it adds about 30% CPU usage on low end devices.
                # The frame masking was only need back when websockets were used over the internet without SSL.
                # Our server, OctoPrint, and Moonraker all accept unmasked frames, so its safe to do this for all WS.
                dataToSend:Any = context.Buffer.Get()
                self.Ws.send(dataToSend, context.OptCode.ToWsLibInt(), False, context.MsgStartOffsetBytes, context.MsgSize)
        except Exception as e:
            # If any exception happens during sending, we want to report the error
            # and shutdown the entire websocket.
            self.handleWsError(e)
        finally:
            # When the send queue closes, make sure the websocket is closed.
            # This is a safety, in case for some reason the websocket was open and we were told to close.
            self._Close()



    # Support using with:
    def __enter__(self):
        return self


    # Support using with;
    def __exit__(self, exc_type:Any, exc_value:Any, traceback:Any):
        self.Close()


    # When the object is deleted, make sure the threads are cleaned up.
    def __del__(self):
        try:
            if self.Ws is not None and self.Ws.keep_running:
                print("THIS SHOULD NEVER HAPPEN! Websocket was deleted without being closed.")
            # Ensure we are fully closed.
            self.Close()
        except Exception:
            pass


class SendQueueContext():
    def __init__(self, buffer:BufferOrNone, msgStartOffsetBytes:Optional[int] = None, msgSize:Optional[int] = None, optCode=WebSocketOpCode.BINARY) -> None:
        self.Buffer = buffer
        self.MsgStartOffsetBytes = msgStartOffsetBytes
        self.MsgSize = msgSize
        self.OptCode = optCode
