import octoflatbuffers

from octoprint_octoeverywhere import Proto
from .Proto import OctoStreamMessage
from .Proto import HandshakeSyn
from .Proto import MessageContext

# A helper class that builds our OctoStream messages as flatbuffers.
class OctoStreamMsgBuilder:

    @staticmethod
    def BuildHandshakeSyn(printerId, isPrimarySession, pluginVersion, localHttpProxyPort, localIp, rsaChallenge, rasKeyVersionInt):
        # Get the a buffer
        builder = OctoStreamMsgBuilder.CreateBuffer(500)

        # Setup strings
        printerIdOffset = builder.CreateString(printerId)
        pluginVersionOffset = builder.CreateString(pluginVersion)
        localIpOffset = None
        if localIp != None:
            localIpOffset = builder.CreateString(localIp)

        # Setup the data vectors
        rasChallengeOffset = builder.CreateByteVector(rsaChallenge)

        # Build the handshake syn
        Proto.HandshakeSyn.Start(builder)
        Proto.HandshakeSyn.AddPrinterId(builder, printerIdOffset)
        Proto.HandshakeSyn.AddIsPrimaryConnection(builder, isPrimarySession)
        Proto.HandshakeSyn.AddPluginVersion(builder, pluginVersionOffset)
        if localIpOffset != None:
            Proto.HandshakeSyn.AddLocalDeviceIp(builder, localIpOffset)
        Proto.HandshakeSyn.AddLocalHttpProxyPort(builder, localHttpProxyPort)
        Proto.HandshakeSyn.AddRsaChallenge(builder, rasChallengeOffset)
        Proto.HandshakeSyn.AddRasChallengeVersion(builder, rasKeyVersionInt)
        synOffset = Proto.HandshakeSyn.End(builder)

        return OctoStreamMsgBuilder.CreateOctoStreamMsgAndFinalize(builder, MessageContext.MessageContext.HandshakeSyn, synOffset)

    @staticmethod
    def CreateBuffer(size):
        return octoflatbuffers.Builder(size)

    @staticmethod
    def CreateOctoStreamMsgAndFinalize(builder, contextType, contextOffset):
        # Create the message
        Proto.OctoStreamMessage.Start(builder)
        Proto.OctoStreamMessage.AddContextType(builder, contextType)
        Proto.OctoStreamMessage.AddContext(builder, contextOffset)
        streamMsgOffset = Proto.OctoStreamMessage.End(builder)

        # Finalize the message. We use the size prefixed 
        builder.FinishSizePrefixed(streamMsgOffset)
        return builder.Output()

    @staticmethod
    def BytesToString(buf):
        # The default value for optional strings is None
        # So handle it.
        if buf == None:
            return None
        return buf.decode("utf-8")