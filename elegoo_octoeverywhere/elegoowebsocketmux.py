import queue
import logging
import threading
import octowebsocket

from octoeverywhere.Proto import HttpInitialContext
from octoeverywhere.Proto.PathTypes import PathTypes

from .elegooclient import ElegooClient

class ElegooWebsocketMux:


    def __init__(self, logger:logging.Logger):
        self.Logger = logger
        self.Lock = threading.Lock()
        self.NextId = 0
        self.ConnectedWebsockets = {}


    # !! Interface Function !!
    # Called when each websocket connection is opened.
    # This function can return an object and it will be used as the websocket object for the connection. It MUST match the interface of the websocket class EXACTLY.
    # If None is returned, the websocket will be opened as normally, using the address.
    def GetWebsocketObject(self, path:str, pathType:PathTypes, context:HttpInitialContext, onWsOpen = None, onWsMsg = None, onWsData = None, onWsClose = None, onWsError = None, headers:dict = None, subProtocolList:list = None):
        # All of the frontend WS connections are relative, so we only need to handle those.
        if pathType != PathTypes.Relative:
            return None
        # This is the API path of the websocket, so we need to handle it.
        pathLower = path.lower()
        if not pathLower.startswith("/websocket"):
            # We don't expect this, since there should only be one websocket path.
            self.Logger.warming(f"We got a relative websocket that didn't match our mux address? We won't mux it. {path}")
            return None

        # Create a new websocket proxy.
        wsId = None
        with self.Lock:
            self.NextId += 1
            wsId = self.NextId
        return ElegooWebsocketClientProxy(self, wsId, self.Logger, onWsOpen, onWsMsg, onWsData, onWsClose, onWsError, headers, subProtocolList)


    # Called before a ElegooWebsocketClientProxy fires the open event.
    # Return true to allow the websocket to open, false to prevent it, fire an error and close.
    def ProxyOpen(self, ws:"ElegooWebsocketClientProxy") -> bool:
        # If we have a connection to the printer, return it's ok to open.
        wsId = ws.GetId()
        result = ElegooClient.Get().IsWebsocketConnected()
        if result is True:
            # If successful, add the websocket to the connected list.
            with self.Lock:
                self.ConnectedWebsockets[wsId] = ws
        return result


    # Called after the ElegooWebsocketClientProxy is fully open and is ready to send messages.
    def ProxyOpened(self, ws:"ElegooWebsocketClientProxy"):
        # When the websocket is fully opened, we can tell the ElegooClient.
        ElegooClient.Get().MuxWebsocketOpened(ws.GetId())


    # Called when a ElegooWebsocketClientProxy sends a message.
    # Return true if the message was sent, false if the message was not sent.
    def ProxySend(self, ws:"ElegooWebsocketClientProxy", buffer:bytearray, msgStartOffsetBytes:int, msgSize:int, optCode) -> bool:
        # Send all messages through the ElegooClient.
        return ElegooClient.Get().MuxSendMessage(ws.GetId(), buffer, msgStartOffsetBytes, msgSize, optCode)


    # Called when a ElegooWebsocketClientProxy closes.
    def ProxyClose(self, ws:"ElegooWebsocketClientProxy"):
        # Remove the websocket from the connected list, so it stops getting messages.
        wsId = ws.GetId()
        with self.Lock:
            # Note this will not be in the list if it never opened.
            if wsId in self.ConnectedWebsockets:
                del self.ConnectedWebsockets[ws.GetId()]

        # Tell the ElegooClient the ws is closed, so it can clear any pending contexts.
        ElegooClient.Get().MuxWebsocketClosed(ws.GetId())


    # Called by the ElegooClient when a message is received.
    # If wsId is set, this message is for a specific websocket.
    # If wsId is None, this message is for all websockets.
    def OnIncomingMessage(self, wsId:int, buffer:bytearray, optCode):
        # OnIncomingMessage pushes the message to a receive queue for each websocket.
        # So its ok to call this synchronously.
        with self.Lock:
            # If wsId is None, send to all websockets.
            if wsId is None:
                for ws in self.ConnectedWebsockets.values():
                    ws.OnIncomingMessage(buffer, optCode)
            else:
                # Send it to the one websocket if we have it.
                ws = self.ConnectedWebsockets.get(wsId)
                if ws is not None:
                    ws.OnIncomingMessage(buffer, optCode)


# The proxy websocket states, to prevent double opening or closing.
# This will only be progressed through once.
class ProxyState:
    UnOpened = 0
    Open = 1
    Closed = 2


# This class is a standin for the websocket client, so it must have matching public functions.
class ElegooWebsocketClientProxy():

    def __init__(self, mux:ElegooWebsocketMux, wsId:int, logger:logging.Logger, onWsOpen = None, onWsMsg = None, onWsData = None, onWsClose = None, onWsError = None, headers:dict = None, subProtocolList:list = None):
        self.Mux = mux
        self.Id = wsId
        self.Logger = logger
        self.StateLock = threading.Lock()
        self.State:ProxyState = ProxyState.UnOpened
        self.ReceiveQueue = queue.Queue()

        self.OnWsOpen = onWsOpen
        self.OnWsMsg = onWsMsg
        self.OnWsData = onWsData
        self.OnWsClose = onWsClose
        self.OnWsError = onWsError


    #
    #  These functions must match the websocket Client class exactly and do the same actions!
    #


    # Runs the websocket blocking until it closes.
    def RunUntilClosed(self, pingIntervalSec:int=None, pingTimeoutSec:int=None):
        # Call run async to invoke the open callback.
        self.RunAsync()


    # Runs the websocket async.
    def RunAsync(self):
        def openThread():
            try:
                # Check if we can open or if we need to send a close.
                if not self.Mux.ProxyOpen(self):
                    self._DebugLog("Open blocked, the printer isn't connected.")
                    self._fireErrorAndCloseAsync("Printer not connected.")
                    return

                # Check and update the state.
                with self.StateLock:
                    if self.State != ProxyState.UnOpened:
                        raise Exception("Websocket already opened.")
                    self.State = ProxyState.Open

                # Start the receive thread
                self._startReceiveThread()

                # Fire the open callback.
                self._DebugLog("Opening websocket.")
                if self.OnWsOpen is not None:
                    self.OnWsOpen(self)

                # Tell the mux we are fully opened now.
                self.Mux.ProxyOpened(self)
            except Exception as e:
                self._fireErrorAndCloseAsync("Exception in OnWsOpen callback.", e)
        threading.Thread(target=openThread, name="ElegooWebsocketClientProxy-OpenThread").start()


    # Closes the websocket.
    def Close(self):
        def closeThread():
            # Check and update the state.
            with self.StateLock:
                # Check for closed, so all other states can close.
                if self.State == ProxyState.Closed:
                    self._DebugLog("Close blocked, the websocket is already closed.")
                    return
                self.State = ProxyState.Closed

            # Close the receive queue
            self.ReceiveQueue.put(ReceiveQueueContext(None, None, True))

            self._DebugLog("Closing websocket.")
            # First close the mux, so we don't get any more messages.
            try:
                self.Mux.ProxyClose(self)
            except Exception as e:
                self.Logger.error(f"ElegooWebsocketClientProxy failed to call ProxyClose. {e}")

            # Then call the close callback.
            try:
                if self.OnWsClose is not None:
                    self.OnWsClose(self)
            except Exception as e:
                self.Logger.error(f"ElegooWebsocketClientProxy failed to call OnWsClose. {e}")
        threading.Thread(target=closeThread, name="ElegooWebsocketClientProxy-CloseThread").start()


    def Send(self, buffer:bytearray, msgStartOffsetBytes:int = None, msgSize:int = None, isData:bool = True):
        # Use the other send function, with the correct opt code.
        code = octowebsocket.ABNF.OPCODE_TEXT if not isData else octowebsocket.ABNF.OPCODE_BINARY
        self.SendWithOptCode(buffer, msgStartOffsetBytes, msgSize, code)


    # Sends a buffer, with an optional message start offset and size.
    # If the message start offset and size are not provided, it's assumed the buffer starts at 0 and the size is the full buffer.
    # Providing a bytearray with room in the front allows the system to avoid copying the buffer.
    def SendWithOptCode(self, buffer:bytearray, msgStartOffsetBytes:int = None, msgSize:int = None, optCode = octowebsocket.ABNF.OPCODE_BINARY):
        # Do a unlocked state check, to ensure we are open.
        if self.State != ProxyState.Open:
            self._DebugLog("Message send, the websocket is not open.")
            return

        # Send via the proxy.
        self._DebugLog("Sending message.")
        result = self.Mux.ProxySend(self, buffer, msgStartOffsetBytes, msgSize, optCode)
        if result is False:
            # If it fails, close the websocket.
            self._fireErrorAndCloseAsync("Failed to send websocket message.")


    # Support using with:
    def __enter__(self):
        return self


    # Support using with;
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.Close()
        except Exception:
            pass


    # When the object is deleted, make sure the threads are cleaned up.
    def __del__(self):
        try:
            self.Close()
        except Exception:
            pass


    #
    # End interface functions
    #

    # Returns the websocket id.
    def GetId(self) -> int:
        return self.Id


    # Called by the ElegooWebsocketMux when a message is received.
    def OnIncomingMessage(self, buffer:bytearray, optCode = octowebsocket.ABNF.OPCODE_BINARY):
        self.ReceiveQueue.put(ReceiveQueueContext(buffer, optCode))


    # To emulate the websocket client, we send messages on a thread in the order they are received.
    def _startReceiveThread(self):
        def receiveThread():
            try:
                while self.State == ProxyState.Open:
                    # Get the next context, blocking until we have one.
                    context:ReceiveQueueContext = self.ReceiveQueue.get()
                    if context is None:
                        raise Exception("ReceiveQueueContext is None.")
                    if context.IsClose:
                        return

                     # Do a unlocked state check, to ensure we are open.
                    if self.State != ProxyState.Open:
                        self._DebugLog("Message receive blocked, the websocket is not open.")
                        return

                    self._DebugLog("Received message.")
                    # Just like in the WS logic, fire date first then msg
                    if self.OnWsData is not None:
                        self.OnWsData(self, context.Buffer, context.OptCode)

                    # First message first, with just the buffer.
                    if self.OnWsMsg is not None:
                        self.OnWsMsg(self, context.Buffer)

            except Exception as e:
                self._fireErrorAndCloseAsync("Exception in ReceiveThread.", e)
        threading.Thread(target=receiveThread, name="ElegooWebsocketClientProxy-ReceiveThread").start()


    # A helper to handle all errors and make sure we are closed.
    def _fireErrorAndCloseAsync(self, msg:str, exception:Exception = None):
        def errorThread():
            # First the error callback.
            try:
                self._DebugLog(f"Error: {msg}")
                if self.OnWsError is not None:
                    self.OnWsError(self, Exception(msg, exception))
            except Exception as e:
                self.Logger.error(f"ElegooWebsocketClientProxy failed to call OnWsError. {e}")

            # First next close.
            self.Close()
        threading.Thread(target=errorThread, name="ElegooWebsocketClientProxy-ErrorThread").start()


    # Logging helper.
    def _DebugLog(self, msg:str):
        self.Logger.debug("MuxSock [%d] - %s", self.Id, msg)


class ReceiveQueueContext:
    def __init__(self, buffer:bytearray, optCode, isClose = False):
        self.Buffer = buffer
        self.OptCode = optCode
        self.IsClose = isClose
