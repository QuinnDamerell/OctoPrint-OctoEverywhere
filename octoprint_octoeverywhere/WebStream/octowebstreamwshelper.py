# namespace: WebStream

import threading
import time
import zlib

import websocket

from ..octohttprequest import OctoHttpRequest
from ..octostreammsgbuilder import OctoStreamMsgBuilder
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

        # These vars indicate if the actual websocket is opened or closed.
        # This is different from IsClosed, which is tracking if the webstream closed status.
        # These are important for when we try to send a message.
        self.IsWsObjOpened = False
        self.IsWsObjClosed = False

        # Get the initial http context
        httpInitialContext = webStreamOpenMsg.HttpInitialContext()
        if httpInitialContext is None:
            raise Exception("Web stream ws helper got a open message with no http context")

        # Get the path
        path = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Path())
        if path is None:
            raise Exception("Web stream ws helper got a open message with no path")

        # Build the uri
        uri = None
        pathType = httpInitialContext.PathType()
        if pathType is PathTypes.PathTypes.Relative:
            # Make a relative path
            uri = "ws://" + OctoHttpRequest.GetLocalhostAddress() + ":" + str(OctoHttpRequest.GetLocalOctoPrintPort()) + path
        elif pathType is PathTypes.PathTypes.Absolute:
            # Make an absolute path
            uri = path
        else:
            raise Exception("Web stream ws helper got a open message with an unknown path type "+str(pathType))

        # Get the headers
        # TODO - enable this. The headers we generate right now don't work for websockets.
        #headers = HeaderHelper.GatherRequestHeaders(httpInitialContext, self.Logger)

        # Make the websocket object and start it running.
        self.Logger.info(self.getLogMsgPrefix()+"opening websocket to "+str(uri))
        self.Ws = Client(uri, self.onWsOpened, None, self.onWsData, self.onWsClosed, self.onWsError)
        self.Ws.RunAsync()


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
        self.Logger.info(self.getLogMsgPrefix()+"websocket closed")
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

            # Sleep for a bit to wait for the socket open.
            time.sleep(0.1)

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

        # Always return false, to keep the socket alive.
        return False


    def onWsData(self, ws, buffer, msgType):
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
        self.IsWsObjClosed = True
        # Make sure the stream is closed.
        self.WebStream.Close()


    def onWsError(self, ws, error):
        # If we are closed, don't bother reporting.
        skipReport = True
        with self.StateLock:
            skipReport = self.IsClosed

        if skipReport is False:
            self.Logger.error(self.getLogMsgPrefix()+" got an error from the websocket: "+str(error))

        # Always call close on the web stream, because it's safe to call even if it's closed already
        # this just makes sure we don't accidentally close the stream somehow.
        self.WebStream.Close()


    def onWsOpened(self, ws):
        # Update the state to indicate we are ready to take messages.
        self.IsWsObjClosed = False
        self.IsWsObjOpened = True


    def getLogMsgPrefix(self):
        return "Web Stream ws   ["+str(self.Id)+"] "
