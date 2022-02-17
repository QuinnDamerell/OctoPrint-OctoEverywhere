# namespace: WebStream

import threading
import time
import zlib

import websocket

from ..octohttprequest import OctoHttpRequest
from ..octostreammsgbuilder import OctoStreamMsgBuilder
from ..localip import LocalIpHelper
from ..websocketimpl import Client
from ..Proto import WebStreamMsg
from ..Proto import MessageContext
from ..Proto import WebSocketDataTypes
from ..Proto import PathTypes
from ..Proto import DataCompression

#
# A helper object that handles websocket request for the web stream system.
#
# The helper can close the stream by calling close directly on the WebStream object
# or by returning true from `IncomingServerMessage`
#
class OctoWebStreamWsHelper:

    # Called by the main socket thread so this should be quick!
    # Throwing from here will shutdown the entire connection.
    def __init__(self, streamId, logger, webStream, webStreamOpenMsg, openedTime):
        self.Id = streamId
        self.Logger = logger
        self.WebStream = webStream
        self.WebStreamOpenMsg = webStreamOpenMsg
        self.IsClosed = False
        self.StateLock = threading.Lock()
        self.OpenedTime = openedTime
        self.Ws = None
        self.FirstWsMessageSentToLocal = False

        # These vars indicate if the actual websocket is opened or closed.
        # This is different from IsClosed, which is tracking if the webstream closed status.
        # These are important for when we try to send a message.
        self.IsWsObjOpened = False
        self.IsWsObjClosed = False

        # Capture the initial http context
        self.HttpInitialContext = webStreamOpenMsg.HttpInitialContext()
        if self.HttpInitialContext is None:
            raise Exception("Web stream ws helper got a open message with no http context")

        # Get the headers
        # TODO - enable this. The headers we generate right now don't work for websockets.
        #headers = HeaderHelper.GatherRequestHeaders(httpInitialContext, self.Logger)

        # It might take multiple attempts depending on the network setup of the client.
        # This value keeps track of them.
        self.ConnectionAttempt = 0
        # This boolean tracks if a connection attempt was ever successful or not.
        self.SuccessfullyOpenedSocket = False

        # Attempt to connect to the websocket.
        if self.AttemptConnection() is False:
            raise Exception("Web stream ws AttemptConnection didn't try to connect?")


    # This function will attempt to connect to the desired websocket.
    # Due to different port binding or network setups, we might need to try a few different IP and PORT combinations
    # before we succeeded. For that reason, this function is called when the object is first created and also when there's a
    # websocket error. Each call will attempt a new connection until there are no more possibilities, and then the function will return False.
    #
    # Returns True if a new connection is being attempted.
    # Returns False if a new connection is not being attempted.
    def AttemptConnection(self):
        # If this webstream context has already opened a successful websocket connection to something,
        # never try to connect again.
        if self.SuccessfullyOpenedSocket is True:
            return False

        # If this is not the first attempt, make sure the websocket is closed (which it most likely is)
        if self.Ws is not None:
            try:
                # Since the current websocket has callback handers attached, first grab a local copy and null
                # it out before calling close. This allows the callbacks to check if they are firing for the current
                # websocket or an old one.
                ws = self.Ws
                self.Ws = None
                ws.Close()
            except Exception as _:
                pass

        # Get the path
        path = OctoStreamMsgBuilder.BytesToString(self.HttpInitialContext.Path())
        if path is None:
            raise Exception("Web stream ws helper got a open message with no path")

        # Depending on the connection attempt, build the URI
        uri = None
        pathType = self.HttpInitialContext.PathType()
        if pathType is PathTypes.PathTypes.Relative:
            # If the path is relative, we will make a few attempts to connect.
            # Note these attempts are very closely related to the logic in the OctoHttpRequest class and should stay in sync.
            if self.ConnectionAttempt == 0:
                # Try to connect using the main URL, this is what we expect to work.
                uri = "ws://" + str(OctoHttpRequest.GetLocalhostAddress()) + ":" + str(OctoHttpRequest.GetLocalOctoPrintPort()) + path
            elif self.ConnectionAttempt == 1:
                # Attempt 2 is to where we think the http proxy port is.
                # For this address, we need set the protocol correctly depending if the client detected https or not.
                protocol = "ws://"
                if OctoHttpRequest.GetLocalHttpProxyIsHttps():
                    protocol = "wss://"
                uri = protocol + str(OctoHttpRequest.GetLocalhostAddress()) + ":" +str(OctoHttpRequest.GetLocalHttpProxyPort()) + path
            elif self.ConnectionAttempt == 2:
                # Attempt 3 will be to try to connect with the device IP.
                # This is needed if the server isn't bound to localhost, but only the public IP. Try the http proxy port.
                # Since we are using the public IP, it's more likely that the http proxy port will be bound and not firewalled, since the OctoPrint port is usually internal only.
                protocol = "ws://"
                if OctoHttpRequest.GetLocalHttpProxyIsHttps():
                    protocol = "wss://"
                uri = protocol + LocalIpHelper.TryToGetLocalIp() + ":" + str(OctoHttpRequest.GetLocalHttpProxyPort()) + path
            elif self.ConnectionAttempt == 3:
                # Attempt 4 will be to try to connect with the device IP.
                # This is needed if the server isn't bound to localhost, but only the public IP. Try the OctoPrint local port as a last attempt.
                uri = "ws://" + LocalIpHelper.TryToGetLocalIp() + ":" + str(OctoHttpRequest.GetLocalOctoPrintPort()) + path
            else:
                # Report the issue and return False to indicate we aren't trying to connect.
                self.Logger.info(self.getLogMsgPrefix()+" failed to connect to relative path and has nothing else to try.")
                return False
        elif pathType is PathTypes.PathTypes.Absolute:
            # If the path is absolute, we only have this one path to try.
            if self.ConnectionAttempt != 0:
                # Report the issue and return False to indicate we aren't trying to connect.
                self.Logger.info(self.getLogMsgPrefix()+" failed to connect to absolute path and has nothing else to try.")
                return False

            # Make an absolute path
            uri = path
        else:
            raise Exception("Web stream ws helper got a open message with an unknown path type "+str(pathType))

        # Validate a URI was set
        if uri is None:
            raise Exception(self.getLogMsgPrefix()+" AttemptConnection failed to create a URI")

        # Increment the connection attempt.
        self.ConnectionAttempt += 1

        # Make the websocket object and start it running.
        self.Logger.info(self.getLogMsgPrefix()+"opening websocket to "+str(uri) + " attempt "+ str(self.ConnectionAttempt))
        self.Ws = Client(uri, self.onWsOpened, None, self.onWsData, self.onWsClosed, self.onWsError)
        self.Ws.RunAsync()

        # Return true to indicate we are trying to connect again.
        return True


    # When close is called, all http operations should be shutdown.
    # Called by the main socket thread so this should be quick!
    def Close(self):
        # Don't try to close twice.
        with self.StateLock:
            # If we are already closed, there's nothing to do.
            if self.IsClosed is True:
                return
            # We will close now, so set the flag.
            self.IsClosed = True

        # Since the ws is created in the constructor, we know it must exist and must be running
        # (or at least connecting). So all we have to do here is call close.
        self.Logger.info(self.getLogMsgPrefix()+"websocket closed after" +str(time.time() - self.OpenedTime) + " seconds")
        self.Ws.Close()


    # Called when a new message has arrived for this stream from the server.
    # This function should throw on critical errors, that will reset the connection.
    # Returning true will case the websocket to close on return.
    # This function is called on it's own thread from the web stream, so it's ok to block
    # as long as it gets cleaned up when the socket closes.
    def IncomingServerMessage(self, webStreamMsg):

        # We can get messages from this web stream before the actual websocket has opened and is ready for messages.
        # If this happens, when we try to send the message on the socket and we will get an error saying "the socket is closed" (which is incorrect, it's not open yet).
        # So we need to delay until we know the socket is ready or the webstream is shutdown.
        while self.IsWsObjOpened is False:
            # Check if the webstream has closed or the socket object is now reporting closed.
            if self.IsWsObjClosed is True or self.IsClosed:
                return True

            # Sleep for a bit to wait for the socket open. The socket will open super quickly (5-10ms), so don't delay long.
            # Sleep for 5ms.
            time.sleep(0.005)

        # If the websocket object is closed ingore this message. It will throw if the socket is closed
        # which will take down the entire OctoStream. But since it's closed the web stream is already cleaning up.
        # This can happen if the socket closes locally and we sent the message to clean up to the service, but there
        # were already inbound messages on the way.
        if self.IsWsObjClosed:
            return True

        # Note it's ok for this to be empty. Since DataAsByteArray returns 0 if it doesn't
        # exist, we need to check for it.
        buffer = webStreamMsg.DataAsByteArray()
        if buffer == 0:
            buffer = bytearray(0)

        # If the message is compressed, decompress it.
        if webStreamMsg.DataCompression() == DataCompression.DataCompression.Brotli:
            raise Exception("IncomingServerMessage Failed - Brotli decompress not possible.")
        elif webStreamMsg.DataCompression() == DataCompression.DataCompression.Zlib:
            buffer = zlib.decompress(buffer)

        # Get the send type.
        sendType = 0
        msgType = webStreamMsg.WebsocketDataType()
        if msgType == WebSocketDataTypes.WebSocketDataTypes.Text:
            sendType = websocket.ABNF.OPCODE_TEXT
        elif msgType == WebSocketDataTypes.WebSocketDataTypes.Binary:
            sendType = websocket.ABNF.OPCODE_BINARY
        elif msgType == WebSocketDataTypes.WebSocketDataTypes.Close:
            sendType = websocket.ABNF.OPCODE_CLOSE
        else:
            raise Exception("Web stream ws was sent a data type that's unknown. "+str(msgType))

        # Send!
        self.Ws.SendWithOptCode(buffer, sendType)

        # Log for perf tracking
        if self.FirstWsMessageSentToLocal is False:
            self.Logger.info(self.getLogMsgPrefix()+"first message sent to local server after " +str(time.time() - self.OpenedTime) + " seconds")
            self.FirstWsMessageSentToLocal = True

        # Always return false, to keep the socket alive.
        return False


    def onWsData(self, ws, buffer, msgType):
        # Only handle callbacks for the current websocket.
        if self.Ws is not None and self.Ws != ws:
            return

        try:
            # Figure out the data type
            # TODO - we should support the OPCODE_CONT type at some point. But it's not needed right now.
            sendType = WebSocketDataTypes.WebSocketDataTypes.None_
            if msgType == websocket.ABNF.OPCODE_BINARY:
                sendType = WebSocketDataTypes.WebSocketDataTypes.Binary
            elif msgType == websocket.ABNF.OPCODE_TEXT:
                sendType = WebSocketDataTypes.WebSocketDataTypes.Text
                # If the buffer is text, we need to encode it as bytes.
                buffer = buffer.encode()
            else:
                raise Exception("Web stream ws helper got a message type that's not supported. "+str(msgType))

            # Some messages are large, so compression helps.
            # We also don't consider the message type, since binary messages can very easily be
            # text as well, and the cost of compression in terms of CPU is low.
            usingCompression = len(buffer) > 200
            originalDataSize = 0
            if usingCompression:
                # See notes about the quality and such in the readContentFromBodyAndMakeDataVector.
                originalDataSize = len(buffer)
                buffer = zlib.compress(buffer, 3)

            # Send the message along!
            builder = OctoStreamMsgBuilder.CreateBuffer(len(buffer) + 200)

            # Note its ok to have an empty buffer, we still want to send the ping.
            dataOffset = None
            if len(buffer) > 0:
                dataOffset = builder.CreateByteVector(buffer)

            # Setup the mesage to send.
            WebStreamMsg.Start(builder)
            WebStreamMsg.AddStreamId(builder, self.Id)
            WebStreamMsg.AddIsControlFlagsOnly(builder, False)
            WebStreamMsg.AddWebsocketDataType(builder, sendType)
            if usingCompression:
                WebStreamMsg.AddDataCompression(builder, DataCompression.DataCompression.Zlib)
                WebStreamMsg.AddOriginalDataSize(builder, originalDataSize)
            if dataOffset is not None:
                WebStreamMsg.AddData(builder, dataOffset)
            webStreamMsgOffset = WebStreamMsg.End(builder)
            outputBuf = OctoStreamMsgBuilder.CreateOctoStreamMsgAndFinalize(builder, MessageContext.MessageContext.WebStreamMsg, webStreamMsgOffset)

            # Send it!
            self.WebStream.SendToOctoStream(outputBuf)
        except Exception as e:
            self.Logger.error(self.getLogMsgPrefix()+ " got an error while trying to forward websocket data to the service. "+str(e))
            self.WebStream.Close()


    def onWsClosed(self, ws):
        # Only handle callbacks for the current websocket.
        if self.Ws is not None and self.Ws != ws:
            return

        # Indicate the socket is closed.
        self.IsWsObjClosed = True

        # Make sure the stream is closed.
        self.WebStream.Close()


    def onWsError(self, ws, error):
        # Only handle callbacks for the current websocket.
        if self.Ws is not None and self.Ws != ws:
            return

        # If we are closed, don't bother reporting or reconnecting.
        isClosed = True
        with self.StateLock:
            isClosed = self.IsClosed

        # Check to see if this webstream is closed or not.
        # If the webstream is closed we don't want to bother with any other attempts to re-connect.
        if isClosed is False:
            # If we got an error before the websocket was ever opened, it was an issue connecting the websocket.
            # In that case this function will handle trying to connect again.
            if self.SuccessfullyOpenedSocket is False:
                if self.AttemptConnection() is True:
                    # If AttemptConnection returns true, a new connection is being attempted.
                    # We should not close the webstream, but instead just close to give this connection a chance.
                    return

            # Since the webstream still thinks it's open, report this error since it will be the one shutting the web stream down.
            self.Logger.error(self.getLogMsgPrefix()+" got an error from the websocket: "+str(error))

        # Always call close on the web stream, because it's safe to call even if it's closed already
        # this just makes sure we don't accidentally close the stream somehow.
        self.WebStream.Close()


    def onWsOpened(self, ws):
        # Only handle callbacks for the current websocket.
        if self.Ws is not None and self.Ws != ws:
            return

        # Update the state to indicate we are ready to take messages.
        self.IsWsObjClosed = False
        self.IsWsObjOpened = True
        self.SuccessfullyOpenedSocket = True
        self.Logger.info(self.getLogMsgPrefix()+"opened, attempt "+str(self.ConnectionAttempt) + " after " +str(time.time() - self.OpenedTime) + " seconds")


    def getLogMsgPrefix(self):
        return "Web Stream ws   ["+str(self.Id)+"] "
