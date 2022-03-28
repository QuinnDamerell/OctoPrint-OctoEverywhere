import octoflatbuffers

from .Proto import MessageContext
from .Proto import HandshakeSyn
from .Proto import OctoStreamMessage

# A helper class that builds our OctoStream messages as flatbuffers.
class OctoStreamMsgBuilder:

    @staticmethod
    def BuildHandshakeSyn(printerId, privateKey, isPrimarySession, pluginVersion, localHttpProxyPort, localIp, rsaChallenge, rasKeyVersionInt,
        webcamFlipH, webcamFlipV, webcamRotate90):
        # Get the a buffer
        builder = OctoStreamMsgBuilder.CreateBuffer(500)

        # Setup strings
        printerIdOffset = builder.CreateString(printerId)
        privateKeyOffset = builder.CreateString(privateKey)
        pluginVersionOffset = builder.CreateString(pluginVersion)
        localIpOffset = None
        if localIp is not None:
            localIpOffset = builder.CreateString(localIp)

        # Setup the data vectors
        rasChallengeOffset = builder.CreateByteVector(rsaChallenge)

        # Build the handshake syn
        HandshakeSyn.Start(builder)
        HandshakeSyn.AddPrinterId(builder, printerIdOffset)
        HandshakeSyn.AddPrivateKey(builder, privateKeyOffset)
        HandshakeSyn.AddIsPrimaryConnection(builder, isPrimarySession)
        HandshakeSyn.AddPluginVersion(builder, pluginVersionOffset)
        if localIpOffset is not None:
            HandshakeSyn.AddLocalDeviceIp(builder, localIpOffset)
        HandshakeSyn.AddLocalHttpProxyPort(builder, localHttpProxyPort)
        HandshakeSyn.AddRsaChallenge(builder, rasChallengeOffset)
        HandshakeSyn.AddRasChallengeVersion(builder, rasKeyVersionInt)
        HandshakeSyn.AddWebcamFlipH(builder, webcamFlipH)
        HandshakeSyn.AddWebcamFlipV(builder, webcamFlipV)
        HandshakeSyn.AddWebcamFlipRotate90(builder, webcamRotate90)
        synOffset = HandshakeSyn.End(builder)

        return OctoStreamMsgBuilder.CreateOctoStreamMsgAndFinalize(builder, MessageContext.MessageContext.HandshakeSyn, synOffset)

    @staticmethod
    def CreateBuffer(size):
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
        return builder.Output()

    @staticmethod
    def BytesToString(buf):
        # The default value for optional strings is None
        # So handle it.
        if buf is None:
            return None
        return buf.decode("utf-8")
