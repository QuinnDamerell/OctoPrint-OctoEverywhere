import sys
import struct
import threading
import traceback

#
# This file respresents one connection session to the service. If anything fails it is destroyed and a new connection will be made.
#

from .WebStream import octowebstream
from .octohttprequest import OctoHttpRequest
from .localip import LocalIpHelper
from .octostreammsgbuilder import OctoStreamMsgBuilder
from .serverauth import ServerAuthHelper

from .Proto import OctoStreamMessage
from .Proto import HandshakeAck
from .Proto import MessageContext
from .Proto import WebStreamMsg
from .Proto import OctoNotification
from .Proto import OctoNotificationTypes
from .Proto import OctoSummon

class OctoSession:

    def __init__(self, octoStream, logger, printerId, isPrimarySession, sessionId, uiPopupInvoker, pluginVersion):
        self.ActiveWebStreams = {}
        self.ActiveWebStreamsLock = threading.Lock()
        self.IsAcceptingStreams = True

        self.Logger = logger
        self.SessionId = sessionId
        self.OctoStream = octoStream
        self.PrinterId = printerId
        self.isPrimarySession = isPrimarySession
        self.UiPopupInvoker = uiPopupInvoker
        self.PluginVersion = pluginVersion

        # Create our server auth helper.
        self.ServerAuth = ServerAuthHelper(self.Logger)


    def OnSessionError(self, backoffModifierSec):
        # Just forward
        self.OctoStream.OnSessionError(self.SessionId, backoffModifierSec)


    def Send(self, msg):
        # The message is already encoded, pass it along to the socket.
        self.OctoStream.SendMsg(msg)


    def HandleSummonRequest(self, msg):
        try:
            summonMsg = OctoSummon.OctoSummon()
            summonMsg.Init(msg.Context().Bytes, msg.Context().Pos)
            serverConnectUrl = OctoStreamMsgBuilder.BytesToString(summonMsg.ServerConnectUrl())
            if serverConnectUrl is None or len(serverConnectUrl) == 0:
                self.Logger.error("Summon notification is missing a server url.")
                return
            # Process it!
            self.OctoStream.OnSummonRequest(self.SessionId, serverConnectUrl)
        except Exception as e:
            self.Logger.error("Failed to handle summon request " + str(e))


    def HandleClientNotification(self, msg):
        try:
            # Handles a notification
            notificationMsg = OctoNotification.OctoNotification()
            notificationMsg.Init(msg.Context().Bytes, msg.Context().Pos)
            title = OctoStreamMsgBuilder.BytesToString(notificationMsg.Title())
            text = OctoStreamMsgBuilder.BytesToString(notificationMsg.Text())
            msgType = notificationMsg.Type()
            autoHide = notificationMsg.AutoHide()

            # Validate
            if title is None or text is None or len(title) == 0 or len(text) == 0:
                self.Logger.error("Octo notification is missing a title or text.")
                return

            # Convert type
            typeStr = "notice"
            if msgType == OctoNotificationTypes.OctoNotificationTypes.Success:
                typeStr = "success"
            elif msgType == OctoNotificationTypes.OctoNotificationTypes.Info:
                typeStr = "info"
            elif msgType == OctoNotificationTypes.OctoNotificationTypes.Error:
                typeStr = "error"

            # Send it to the UI
            self.UiPopupInvoker.ShowUiPopup(title, text, typeStr, autoHide)
        except Exception as e:
            self.Logger.error("Failed to handle octo notification message. " + str(e))


    def HandleHandshakeAck(self, msg):
        # Handles a handshake ack message.
        handshakeAck = HandshakeAck.HandshakeAck()
        handshakeAck.Init(msg.Context().Bytes, msg.Context().Pos)

        if handshakeAck.Accepted():
            # Accepted!
            # Parse and validate the RAS challenge.
            rasChallengeResponse = OctoStreamMsgBuilder.BytesToString(handshakeAck.RsaChallengeResult())
            if self.ServerAuth.ValidateChallengResponse(rasChallengeResponse) is False:
                raise Exception("Server RAS challenge failed!")
            # Parse out the response and report.
            connectedAccounts = None
            connectedAccountsLen = handshakeAck.ConnectedAccountsLength()
            if handshakeAck.ConnectedAccountsLength() != 0:
                i = 0
                connectedAccounts = []
                while i < connectedAccountsLen:
                    connectedAccounts.append(OctoStreamMsgBuilder.BytesToString(handshakeAck.ConnectedAccounts(i)))
                    i += 1

            # Parse out the OctoKey
            octoKey = OctoStreamMsgBuilder.BytesToString(handshakeAck.Octokey())
            self.OctoStream.OnHandshakeComplete(self.SessionId, octoKey, connectedAccounts)
        else:
            # Pull out the error.
            error = handshakeAck.Error()
            if error is not None:
                error = OctoStreamMsgBuilder.BytesToString(error)
            else:
                error = "no error given"
            self.Logger.error("Handshake failed, reason '" + str(error) + "'")

            # The server can send back a backoff time we should respect.
            backoffModifierSec = handshakeAck.BackoffSeconds()

            # Check if an update is required, if so we need to tell the UI and set the back off to be crazy high.
            if handshakeAck.RequiresPluginUpdate():
                backoffModifierSec = 43200 # 1 month
                self.OctoStream.OnPluginUpdateRequired()

            self.OnSessionError(backoffModifierSec)


    def HandleWebStreamMessage(self, msg):
        # Handles a web stream.
        webStreamMsg = WebStreamMsg.WebStreamMsg()
        webStreamMsg.Init(msg.Context().Bytes, msg.Context().Pos)

        # Get the stream id
        streamId = webStreamMsg.StreamId()
        if streamId == 0:
            self.Logger.error("We got a web stream message for an invalid stream id of 0")
            # throwing here will terminate this entire OcotoSocket and reset.
            raise Exception("We got a web stream message for an invalid stream id of 0")

        # Grab the lock before messing with the map.
        localStream = None
        self.ActiveWebStreamsLock.acquire()
        try:
            # First, check if the stream exists.
            if streamId in self.ActiveWebStreams :
                # It exists, so use it.
                localStream = self.ActiveWebStreams[streamId]
            else:
                # It doesn't exist. Validate this is a open message.
                if webStreamMsg.IsOpenMsg() is False:
                    # TODO - Handle messages that arrive for just closed streams better.
                    isCloseMessage = webStreamMsg.IsCloseMsg()
                    self.Logger.warn("We got a web stream message for a stream id [" + str(streamId) + "] that doesn't exist and isn't an open message. IsClose:"+str(isCloseMessage))
                    # Don't throw, because this message maybe be coming in from the server as the local side closed.
                    return

                # Check that we are still accepting streams
                if self.IsAcceptingStreams is False:
                    self.Logger.info("OctoSession got a webstream open request after we stopped accpeting streams. streamId:"+str(streamId))
                    return

                # Create the new stream object now.
                localStream = octowebstream.OctoWebStream(args=(self.Logger, streamId, self,))
                # Set it in the map
                self.ActiveWebStreams[streamId] = localStream
                # Start it's main worker thread
                localStream.start()

        except Exception as e:
            # rethrow any exceptions in the code
            raise e
        finally:
            # Always unlock
            self.ActiveWebStreamsLock.release()

        # If we get here, we know we must have a localStream
        localStream.OnIncomingServerMessage(webStreamMsg)


    def WebStreamClosed(self, streamId):
        # Called from the webstream when it's closing.
        self.ActiveWebStreamsLock.acquire()
        try:
            if streamId in self.ActiveWebStreams :
                self.ActiveWebStreams.pop(streamId)
            else:
                self.Logger.error("A web stream asked to close that wasn't in our webstream map.")
        except Exception as e:
            # rethrow any exceptions in the code
            raise e
        finally:
            # Always unlock
            self.ActiveWebStreamsLock.release()


    def CloseAllWebStreamsAndDisable(self):
        # The streams will remove them selves from the map when they close, so all we need to do is ask them
        # to close.
        localWebStreamList = []
        self.ActiveWebStreamsLock.acquire()
        try:
            # Close them all.
            self.Logger.info("Closing all open web stream sockets ("+str(len(self.ActiveWebStreams))+")")

            # Set the flag to indicate we aren't accepting any more
            self.IsAcceptingStreams = False

            # Copy all of the streams locally.
            # pylint: disable=consider-using-dict-items
            for streamId in self.ActiveWebStreams:
                localWebStreamList.append(self.ActiveWebStreams[streamId])

        except Exception as e:
            # rethrow any exceptions in the code
            raise e
        finally:
            # Always unlock
            self.ActiveWebStreamsLock.release()

        # Try catch all of this so we don't leak exceptions.
        # Use our local web stream list to tell them all to close.
        try:
            for webStream in localWebStreamList:
                try:
                    webStream.Close()
                except Exception as e:
                    self.Logger.error("Exception thrown while closing web streamId - " + str(e))
        except Exception as ex:
            self.Logger.error("Exception thrown while closing all web streams: " + str(ex))


    def StartHandshake(self):
        # Send the handshakesyn
        try:
            # Get our unique challenge
            rasChallenge = self.ServerAuth.GetEncryptedChallenge()
            if rasChallenge is None:
                raise Exception("Rsa challenge generation failed.")
            rasChallengeKeyVerInt = ServerAuthHelper.c_ServerAuthKeyVersion

            # Build the message
            buf = OctoStreamMsgBuilder.BuildHandshakeSyn(self.PrinterId, self.isPrimarySession,
                self.PluginVersion, OctoHttpRequest.GetLocalHttpProxyPort(), LocalIpHelper.TryToGetLocalIp(),
                rasChallenge, rasChallengeKeyVerInt)

            # Send!
            self.OctoStream.SendMsg(buf)
        except Exception as e:
            self.Logger.error("Failed to send handshake syn. " + str(e))
            self.OnSessionError(0)
            return


    # This is the main receive function for all messages coming from the server.
    # Since all web stream messages use their own threads, we don't spin off a thread
    # for messages here. However, that means we need to be careful to not do any
    # long processing in the function, since it will delay all incoming messages.
    def HandleMessage(self, msgBytes):
        # Decode the message.
        msg = None
        try:
            msg = self.DecodeOctoStreamMessage(msgBytes)
        except Exception as e:
            self.Logger.error("Failed to decode message local request. " + str(e))
            self.OnSessionError(0)
            return

        # Handle it.
        try:
            # If this is a handshake ack, handle it.
            if msg.ContextType() == MessageContext.MessageContext.HandshakeAck:
                self.HandleHandshakeAck(msg)
                return

            # Handle web stream messages
            if msg.ContextType() == MessageContext.MessageContext.WebStreamMsg:
                self.HandleWebStreamMessage(msg)
                return

            # Handle notifications
            if msg.ContextType() == MessageContext.MessageContext.OctoNotification:
                self.HandleClientNotification(msg)
                return

            # Handle summon notifications
            if msg.ContextType() == MessageContext.MessageContext.OctoSummon:
                self.HandleSummonRequest(msg)
                return

            # We don't know what this is, probally a new message we don't understand.
            self.Logger.info("Unknown message type received, ignoring.")
            return

        except Exception as e:
            # If anything throws, we consider it a protocol failure.
            traceback.print_exc()
            self.Logger.error("Failed to handle octo message. " + str(e))
            self.OnSessionError(0)
            return

    # Helper to unpack uint32
    def Unpack32Int(self, buffer, bufferOffset) :
        if sys.byteorder == "little":
            if sys.version_info[0] < 3:
                return (struct.unpack('1B', buffer[0 + bufferOffset])[0]) + (struct.unpack('1B', buffer[1 + bufferOffset])[0] << 8) + (struct.unpack('1B', buffer[2 + bufferOffset])[0] << 16) + (struct.unpack('1B', buffer[3 + bufferOffset])[0] << 24)
            else:
                return (buffer[0 + bufferOffset]) + (buffer[1 + bufferOffset] << 8) + (buffer[2 + bufferOffset] << 16) + (buffer[3 + bufferOffset] << 24)
        else:
            if sys.version_info[0] < 3:
                return (struct.unpack('1B', buffer[0 + bufferOffset])[0] << 24) + (struct.unpack('1B', buffer[1 + bufferOffset])[0] << 16) + (struct.unpack('1B', buffer[2 + bufferOffset])[0] << 8) + struct.unpack('1B', buffer[3 + bufferOffset])[0]
            else:
                return (buffer[0 + bufferOffset] << 24) + (buffer[1 + bufferOffset] << 16) + (buffer[2 + bufferOffset] << 8) + (buffer[3 + bufferOffset])

    def DecodeOctoStreamMessage(self, buf):
        # Our wire protocol is a uint32 followed by the flatbuffer message.

        # First, read the message size.
        # We add 4 to account for the full buffer size, including the uint32.
        messageSize = self.Unpack32Int(buf, 0) + 4

        # Check that things make sense.
        if messageSize != len(buf):
            raise Exception("We got an OctoStreamMsg that's not the correct size! MsgSize:"+str(messageSize)+"; BufferLen:"+str(len(buf)))

        # Decode and return
        return OctoStreamMessage.OctoStreamMessage.GetRootAs(buf, 4)
