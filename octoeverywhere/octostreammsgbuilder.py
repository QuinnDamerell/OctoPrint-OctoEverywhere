from typing import Any, Optional, Tuple
import octoflatbuffers

from .buffer import Buffer
from .Proto import MessageContext
from .Proto import HandshakeSyn
from .Proto import OctoStreamMessage

# A helper class that builds our OctoStream messages as flatbuffers.
class OctoStreamMsgBuilder:

    @staticmethod
    def BuildHandshakeSyn(
                            printerId:str,
                            privateKey:str,
                            isPrimarySession:bool,
                            pluginVersion:str,
                            localHttpProxyPort:int,
                            targetLocalIp:str,
                            rsaChallenge:bytes,
                            rasKeyVersionInt:int,
                            summonMethod:int,
                            serverHostType:int,
                            osType:int,
                            receiveCompressionType:int,
                            deviceId:Optional[str],
                            isCompanion:bool,
                            isDockerContainer:bool
                        ) -> Tuple[Buffer, int, int]:
        # Get a buffer
        builder = OctoStreamMsgBuilder.CreateBuffer(500)

        # Setup strings
        printerIdOffset = builder.CreateString(printerId) #pyright: ignore[reportUnknownMemberType]
        privateKeyOffset = builder.CreateString(privateKey) #pyright: ignore[reportUnknownMemberType]
        pluginVersionOffset = builder.CreateString(pluginVersion) #pyright: ignore[reportUnknownMemberType]
        targetLocalIpOffset:Optional[int] = None
        if targetLocalIp is not None:
            targetLocalIpOffset = builder.CreateString(targetLocalIp) #pyright: ignore[reportUnknownMemberType]
        deviceIdOffset:Optional[int] = None
        if deviceId is not None:
            deviceIdOffset = builder.CreateString(deviceId) #pyright: ignore[reportUnknownMemberType]

        # Setup the data vectors
        rasChallengeOffset = builder.CreateByteVector(rsaChallenge) #pyright: ignore[reportUnknownMemberType]

        # Build the handshake syn
        HandshakeSyn.Start(builder)
        HandshakeSyn.AddPrinterId(builder, printerIdOffset)
        HandshakeSyn.AddPrivateKey(builder, privateKeyOffset)
        HandshakeSyn.AddIsPrimaryConnection(builder, isPrimarySession)
        HandshakeSyn.AddPluginVersion(builder, pluginVersionOffset)
        HandshakeSyn.AddSummonMethod(builder, summonMethod)
        HandshakeSyn.AddServerHost(builder, serverHostType)
        HandshakeSyn.AddIsCompanion(builder, isCompanion)
        HandshakeSyn.AddIsDockerContainer(builder, isDockerContainer)
        if targetLocalIpOffset is not None:
            HandshakeSyn.AddLocalDeviceIp(builder, targetLocalIpOffset)
        HandshakeSyn.AddLocalHttpProxyPort(builder, localHttpProxyPort)
        HandshakeSyn.AddRsaChallenge(builder, rasChallengeOffset)
        HandshakeSyn.AddRasChallengeVersion(builder, rasKeyVersionInt)
        HandshakeSyn.AddOsType(builder, osType)
        HandshakeSyn.AddReceiveCompressionType(builder, receiveCompressionType)
        if deviceIdOffset is not None:
            HandshakeSyn.AddDeviceId(builder, deviceIdOffset)
        synOffset = HandshakeSyn.End(builder)

        # Create and return.
        return OctoStreamMsgBuilder.CreateOctoStreamMsgAndFinalize(builder, MessageContext.MessageContext.HandshakeSyn, synOffset)


    @staticmethod
    def CreateBuffer(size:int) -> octoflatbuffers.Builder:
        return octoflatbuffers.Builder(size)


    @staticmethod
    def CreateOctoStreamMsgAndFinalize(builder:octoflatbuffers.Builder, contextType:int, contextOffset:int) -> Tuple[Buffer, int, int]:
        # Create the message
        OctoStreamMessage.Start(builder)
        OctoStreamMessage.AddContextType(builder, contextType)
        OctoStreamMessage.AddContext(builder, contextOffset)
        streamMsgOffset = OctoStreamMessage.End(builder)

        # Finalize the message. We use the size prefixed
        builder.FinishSizePrefixed(streamMsgOffset) #pyright: ignore[reportUnknownMemberType]

        # Instead of using Output, which will create a copy of the buffer that's trimmed, we return the fully built buffer
        # with the header offset set and size. Flatbuffers are built backwards, so there's usually space in the front were we can add data
        # without creating a new buffer!
        # Note that the buffer is a bytearray
        buffer = Buffer(builder.Bytes)
        msgStartOffsetBytes = builder.Head()
        return (buffer, msgStartOffsetBytes, len(buffer) - msgStartOffsetBytes)


    @staticmethod
    def BytesToString(buf:Any) -> Optional[str]:
        # The default value for optional strings is None
        # So, we handle it.
        if buf is None:
            return None
        return buf.decode("utf-8")
