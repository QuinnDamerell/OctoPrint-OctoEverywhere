import octoflatbuffers

from .Proto import MessageContext
from .Proto import HandshakeSyn
from .Proto import OctoStreamMessage
from .Proto import OsType
from .Proto.DataCompression import DataCompression

# A helper class that builds our OctoStream messages as flatbuffers.
class OctoStreamMsgBuilder:

    @staticmethod
    def BuildHandshakeSyn(printerId, privateKey, isPrimarySession, pluginVersion, localHttpProxyPort, localIp, rsaChallenge, rasKeyVersionInt, summonMethod, serverHostType, isCompanion, osType:OsType.OsType, receiveCompressionType:DataCompression, deviceId:str):
        # Get a buffer
        builder = OctoStreamMsgBuilder.CreateBuffer(500)

        # Setup strings
        printerIdOffset = builder.CreateString(printerId)
        privateKeyOffset = builder.CreateString(privateKey)
        pluginVersionOffset = builder.CreateString(pluginVersion)
        localIpOffset = None
        deviceIdOffset = None
        if localIp is not None:
            localIpOffset = builder.CreateString(localIp)
        if deviceId is not None:
            deviceIdOffset = builder.CreateString(deviceId)

        # Setup the data vectors
        rasChallengeOffset = builder.CreateByteVector(rsaChallenge)

        # Build the handshake syn
        HandshakeSyn.Start(builder)
        HandshakeSyn.AddPrinterId(builder, printerIdOffset)
        HandshakeSyn.AddPrivateKey(builder, privateKeyOffset)
        HandshakeSyn.AddIsPrimaryConnection(builder, isPrimarySession)
        HandshakeSyn.AddPluginVersion(builder, pluginVersionOffset)
        HandshakeSyn.AddSummonMethod(builder, summonMethod)
        HandshakeSyn.AddServerHost(builder, serverHostType)
        HandshakeSyn.AddIsCompanion(builder, isCompanion)
        if localIpOffset is not None:
            HandshakeSyn.AddLocalDeviceIp(builder, localIpOffset)
        HandshakeSyn.AddLocalHttpProxyPort(builder, localHttpProxyPort)
        HandshakeSyn.AddRsaChallenge(builder, rasChallengeOffset)
        HandshakeSyn.AddRasChallengeVersion(builder, rasKeyVersionInt)
        HandshakeSyn.AddOsType(builder, osType)
        HandshakeSyn.AddReceiveCompressionType(builder, receiveCompressionType)
        if deviceIdOffset is not None:
            HandshakeSyn.AddDeviceId(builder, deviceIdOffset)
        synOffset = HandshakeSyn.End(builder)

        return OctoStreamMsgBuilder.CreateOctoStreamMsgAndFinalize(builder, MessageContext.MessageContext.HandshakeSyn, synOffset)

    @staticmethod
    def CreateBuffer(size) -> octoflatbuffers.Builder:
        return octoflatbuffers.Builder(size)

    @staticmethod
    def CreateOctoStreamMsgAndFinalize(builder, contextType, contextOffset):
        # Create the message
        OctoStreamMessage.Start(builder)
        OctoStreamMessage.AddContextType(builder, contextType)
        OctoStreamMessage.AddContext(builder, contextOffset)
        streamMsgOffset = OctoStreamMessage.End(builder)

        # Finalize the message. We use the size prefixed
        builder.FinishSizePrefixed(streamMsgOffset)

        # Instead of using Output, which will create a copy of the buffer that's trimmed, we return the fully built buffer
        # with the header offset set and size. Flatbuffers are built backwards, so there's usually space in the front were we can add data
        # without creating a new buffer!
        # Note that the buffer is a bytearray
        buffer = builder.Bytes
        msgStartOffsetBytes = builder.Head()
        return (buffer, msgStartOffsetBytes, len(buffer) - msgStartOffsetBytes)
        #return builder.Output()

    @staticmethod
    def BytesToString(buf) -> str:
        # The default value for optional strings is None
        # So, we handle it.
        if buf is None:
            return None
        return buf.decode("utf-8")
