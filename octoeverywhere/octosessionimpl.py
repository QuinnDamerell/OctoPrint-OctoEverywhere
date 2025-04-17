import sys
import threading
import logging
from typing import Dict, List

#
# This file represents one connection session to the service. If anything fails it is destroyed and a new connection will be made.
#

from .WebStream.octowebstream import OctoWebStream
from .octohttprequest import OctoHttpRequest
from .localip import LocalIpHelper
from .octostreammsgbuilder import OctoStreamMsgBuilder
from .serverauth import ServerAuthHelper
from .sentry import Sentry
from .ostypeidentifier import OsTypeIdentifier
from .threaddebug import ThreadDebug
from .compression import Compression
from .deviceid import DeviceId
from .interfaces import IPopUpInvoker, IOctoStream, IOctoSession
from .buffer import Buffer, ByteLikeOrMemoryView

from .Proto.OctoStreamMessage import OctoStreamMessage
from .Proto import HandshakeAck
from .Proto import MessageContext
from .Proto import WebStreamMsg
from .Proto import OctoNotification
from .Proto import OctoNotificationTypes
from .Proto import OctoSummon
from .Proto.DataCompression import DataCompression

class OctoSession(IOctoSession):

    def __init__(self,
                    octoStream:IOctoStream,
                    logger:logging.Logger,
                    printerId:str,
                    privateKey:str,
                    isPrimarySession:bool,
                    sessionId:int,
                    uiPopupInvoker:IPopUpInvoker,
                    pluginVersion:str,
                    serverHostType:int,
                    isCompanion:bool
                ):
        self.ActiveWebStreams:Dict[int,OctoWebStream] = {}
        self.ActiveWebStreamsLock = threading.Lock()
        self.IsAcceptingStreams = True

        self.Logger = logger
        self.SessionId = sessionId
        self.OctoStream = octoStream
        self.PrinterId = printerId
        self.PrivateKey = privateKey
        self.isPrimarySession = isPrimarySession
        self.UiPopupInvoker = uiPopupInvoker
        self.PluginVersion = pluginVersion
        self.ServerHostType = serverHostType
        self.IsCompanion = isCompanion

        # Create our server auth helper.
        self.ServerAuth = ServerAuthHelper(self.Logger)


    def OnSessionError(self, backoffModifierSec:int) -> None:
        # Just forward
        self.OctoStream.OnSessionError(self.SessionId, backoffModifierSec)


    def Send(self, buffer:Buffer, msgStartOffsetBytes:int, msgSize:int):
        # The message is already encoded, pass it along to the socket.
        self.OctoStream.SendMsg(buffer, msgStartOffsetBytes, msgSize)


    def HandleSummonRequest(self, msg:OctoStreamMessage):
        try:
            context = msg.Context()
            if context is None:
                self.Logger.error("Summon message is missing context.")
                return

            # Parse the summon message
            summonMsg = OctoSummon.OctoSummon()
            summonMsg.Init(context.Bytes, context.Pos)
            serverConnectUrl = OctoStreamMsgBuilder.BytesToString(summonMsg.ServerConnectUrl())
            summonMethod = summonMsg.SummonMethod()
            if serverConnectUrl is None or len(serverConnectUrl) == 0:
                self.Logger.error("Summon notification is missing a server url.")
                return

            # Process it!
            self.OctoStream.OnSummonRequest(self.SessionId, serverConnectUrl, summonMethod)
        except Exception as e:
            Sentry.OnException("Failed to handle summon request ", e)


    def HandleClientNotification(self, msg:OctoStreamMessage):
        try:
            context = msg.Context()
            if context is None:
                self.Logger.error("Client notification message is missing context.")
                return

            # Handles a notification
            notificationMsg = OctoNotification.OctoNotification()
            notificationMsg.Init(context.Bytes, context.Pos)
            title = OctoStreamMsgBuilder.BytesToString(notificationMsg.Title())
            text = OctoStreamMsgBuilder.BytesToString(notificationMsg.Text())
            msgType = notificationMsg.Type()
            showForSec = notificationMsg.ShowForSec()
            actionText = OctoStreamMsgBuilder.BytesToString(notificationMsg.ActionText())
            actionLink = OctoStreamMsgBuilder.BytesToString(notificationMsg.ActionLink())
            onlyShowIfLoadedViaOeBool = notificationMsg.ShowOnlyIfLoadedFromOe()

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
            self.UiPopupInvoker.ShowUiPopup(title, text, typeStr, actionText, actionLink, showForSec, onlyShowIfLoadedViaOeBool)
        except Exception as e:
            Sentry.OnException("Failed to handle octo notification message.", e)


    def HandleHandshakeAck(self, msg:OctoStreamMessage):
        # Get the context.
        context = msg.Context()
        if context is None:
            raise Exception("HandleHandshakeAck message is missing context.")

        # Handles a handshake ack message.
        handshakeAck = HandshakeAck.HandshakeAck()
        handshakeAck.Init(context.Bytes, context.Pos)

        if handshakeAck.Accepted():
            # Accepted!
            # Parse and validate the RAS challenge.
            rasChallengeResponse = OctoStreamMsgBuilder.BytesToString(handshakeAck.RsaChallengeResult())
            if self.ServerAuth.ValidateChallengeResponse(rasChallengeResponse) is False:
                raise Exception("Server RAS challenge failed!")

            # Parse out the response and report.
            connectedAccounts:List[str] = []
            connectedAccountsLen = handshakeAck.ConnectedAccountsLength()
            if handshakeAck.ConnectedAccountsLength() != 0:
                i = 0
                while i < connectedAccountsLen:
                    account = OctoStreamMsgBuilder.BytesToString(handshakeAck.ConnectedAccounts(i))
                    if account is not None:
                        connectedAccounts.append(account)
                    i += 1

            # Parse out the OctoKey
            octoKey = OctoStreamMsgBuilder.BytesToString(handshakeAck.Octokey())
            if octoKey is None:
                raise Exception("Handshake ack is missing octokey.")

            # Handle it.
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

            # Check if a rekey was requested, if so, the plugin needs to rekey and restart.
            if handshakeAck.RequiresRekey():
                self.OctoStream.OnRekeyRequired()

            self.OnSessionError(backoffModifierSec)


    def HandleWebStreamMessage(self, msg:OctoStreamMessage):
        # Get the context.
        context = msg.Context()
        if context is None:
            raise Exception("HandleWebStreamMessage message is missing context.")

        # Handles a web stream.
        webStreamMsg = WebStreamMsg.WebStreamMsg()
        webStreamMsg.Init(context.Bytes, context.Pos)

        # Get the stream id
        streamId:int = webStreamMsg.StreamId()
        if streamId <= 0:
            self.Logger.error("We got a web stream message for an invalid stream id of 0")
            # throwing here will terminate this entire OcotoSocket and reset.
            raise Exception("We got a web stream message for an invalid stream id of 0")

        # Grab the lock before messing with the map.
        localStream = None
        with self.ActiveWebStreamsLock:
            localStream = self.ActiveWebStreams.get(streamId, None)
            if localStream is None:
                # It doesn't exist. Validate this is a open message.
                if webStreamMsg.IsOpenMsg() is False:
                    # TODO - Handle messages that arrive for just closed streams better.
                    isCloseMessage = webStreamMsg.IsCloseMsg()
                    if isCloseMessage:
                        self.Logger.debug("We got a web stream message for a stream id [" + str(streamId) + "] that doesn't exist and isn't an open message. IsClose:"+str(isCloseMessage))
                    else:
                        self.Logger.warning("We got a web stream message for a stream id [" + str(streamId) + "] that doesn't exist and isn't an open message. IsClose:"+str(isCloseMessage))
                    # Don't throw, because this message maybe be coming in from the server as the local side closed.
                    return

                # Check that we are still accepting streams
                if self.IsAcceptingStreams is False:
                    self.Logger.info("OctoSession got a webstream open request after we stopped accepting streams. streamId:"+str(streamId))
                    return

                # Create the new stream object now.
                localStream = OctoWebStream(name="OctoWebStreamPumper", args=(self.Logger, streamId, self, ))
                # Set it in the map
                self.ActiveWebStreams[streamId] = localStream
                # Start it's main worker thread
                localStream.start()

        # If we get here, we know we must have a localStream
        localStream.OnIncomingServerMessage(webStreamMsg)


    def WebStreamClosed(self, sessionId:int) -> None:
        # Called from the webstream when it's closing.
        with self.ActiveWebStreamsLock:
            # Provide none so this doesn't thrown
            foundStream = self.ActiveWebStreams.pop(sessionId, None)
            if foundStream is None:
                self.Logger.error("A web stream asked to close that wasn't in our webstream map.")


    def CloseAllWebStreamsAndDisable(self):
        # The streams will remove them selves from the map when they close, so all we need to do is ask them
        # to close.
        localWebStreamList:List[OctoWebStream] = []
        with self.ActiveWebStreamsLock:
            # Close them all.
            self.Logger.info("Closing all open web stream sockets ("+str(len(self.ActiveWebStreams))+")")

            # Set the flag to indicate we aren't accepting any more
            self.IsAcceptingStreams = False

            # Copy all of the streams locally.
            # pylint: disable=consider-using-dict-items
            for streamId in self.ActiveWebStreams:
                localWebStreamList.append(self.ActiveWebStreams[streamId])

        # Try catch all of this so we don't leak exceptions.
        # Use our local web stream list to tell them all to close.
        try:
            for webStream in localWebStreamList:
                try:
                    webStream.Close()
                except Exception as e:
                    Sentry.OnException("Exception thrown while closing web streamId", e)
        except Exception as ex:
            Sentry.OnException("Exception thrown while closing all web streams.", ex)


    def StartHandshake(self, summonMethod:int):
        # Send the handshakesyn
        try:
            # Get our unique challenge
            rasChallenge = self.ServerAuth.GetEncryptedChallenge()
            if rasChallenge is None:
                raise Exception("Rsa challenge generation failed.")
            rasChallengeKeyVerInt = ServerAuthHelper.c_ServerAuthKeyVersion

            # Define which type of compression we can receive (beyond None)
            # Ideally this is zstandard lib, but all client must support zlib, so we can fallback to it.
            receiveCompressionType = DataCompression.Zlib
            if Compression.Get().CanUseZStandardLib:
                receiveCompressionType = DataCompression.ZStandard

            # If possible, get a device ID for this plugin.
            # This will return None if no device id can be found.
            deviceId = DeviceId.Get().GetId()

            # Build the message
            buffer, msgStartOffsetBytes, msgSizeBytes = OctoStreamMsgBuilder.BuildHandshakeSyn(self.PrinterId, self.PrivateKey, self.isPrimarySession, self.PluginVersion,
                OctoHttpRequest.GetLocalHttpProxyPort(), LocalIpHelper.TryToGetLocalIp(),
                rasChallenge, rasChallengeKeyVerInt, summonMethod, self.ServerHostType, self.IsCompanion, OsTypeIdentifier.DetectOsType(), receiveCompressionType, deviceId)

            # Send!
            self.OctoStream.SendMsg(buffer, msgStartOffsetBytes, msgSizeBytes)
        except Exception as e:
            Sentry.OnException("Failed to send handshake syn.", e)
            self.OnSessionError(0)


    # This is the main receive function for all messages coming from the server.
    # Since all web stream messages use their own threads, we don't spin off a thread
    # for messages here. However, that means we need to be careful to not do any
    # long processing in the function, since it will delay all incoming messages.
    def HandleMessage(self, msgBytes:Buffer) -> None:
        # Decode the message.
        msg = None
        try:
            msg = self.DecodeOctoStreamMessage(msgBytes)
        except Exception as e:
            Sentry.OnException("Failed to decode message local request.", e)
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

            # We don't know what this is, probably a new message we don't understand.
            self.Logger.info("Unknown message type received, ignoring.")
            return

        except Exception as e:
            # We have seen "failed to create thread" here before, so we do this to debug that.
            ThreadDebug.DoThreadDumpLogout(self.Logger)
            Sentry.OnException("Failed to handle octo message.", e)
            self.OnSessionError(0)
            return


    # Helper to unpack uint32
    def Unpack32Int(self, buffer:ByteLikeOrMemoryView, bufferOffset:int):
        if sys.byteorder == "little":
            return (buffer[0 + bufferOffset]) + (buffer[1 + bufferOffset] << 8) + (buffer[2 + bufferOffset] << 16) + (buffer[3 + bufferOffset] << 24)
        else:
            return (buffer[0 + bufferOffset] << 24) + (buffer[1 + bufferOffset] << 16) + (buffer[2 + bufferOffset] << 8) + (buffer[3 + bufferOffset])


    def DecodeOctoStreamMessage(self, buf:Buffer) -> OctoStreamMessage:
        # Our wire protocol is a uint32 followed by the flatbuffer message.
        rawBuffer = buf.Get()

        # First, read the message size.
        # We add 4 to account for the full buffer size, including the uint32.
        messageSize = self.Unpack32Int(rawBuffer, 0) + 4

        # Check that things make sense.
        if messageSize != len(rawBuffer):
            raise Exception("We got an OctoStreamMsg that's not the correct size! MsgSize:"+str(messageSize)+"; BufferLen:"+str(len(rawBuffer)))

        # Decode and return
        return OctoStreamMessage.GetRootAs(rawBuffer, 4) #pyright: ignore[reportUnknownMemberType]
