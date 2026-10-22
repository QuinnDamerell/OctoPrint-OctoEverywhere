import base64
import logging
import unittest
from unittest.mock import patch

from octoeverywhere.buffer import Buffer
from octoeverywhere.compression import Compression, CompressionContext
from octoeverywhere.Proto.DataCompression import DataCompression
from octoeverywhere.zstandarddictionary import ZStandardDictionary

try:
    import zstandard as zstd
except ImportError:
    zstd = None


# Frozen with python-zstandard 0.22.0 (libzstd 1.5.5), using the existing
# dictionary ID 1 and level 3. Keep these bytes fixed when upgrading the codec:
# same-version round trips alone cannot detect breaking older peers.
# Streaming chunks were captured after FLUSH_BLOCK, before ending the frame,
# matching the relay's per-message flush and exact uncompressed-size contract.
LEGACY_SINGLE = base64.b64decode(
    "KLUv/WEBAAiFAQBDAQQqWi1sO81/YhyGUX23+nUCCPy1WJgHpRj5zcGmxtgyaWPWn0/aaL0JFle2xAc="
)
# ZstdNet 1.5.7 Compressor.Wrap(PAYLOADS[0]), matching the service's
# CompressionPool.RentZstdCompressor single-message path without a dictionary.
SERVER_SINGLE = base64.b64decode(
    "KLUv/WAACGUCAOQDeyJzdGF0dXMiOmUiOiJwcmludGluZyIsInByb2dyZXNzIjowLjI1LCJmaWxl"
    "bmFtdGVzdC5nY29kZSJ9fQoDALVYgisnxV6xggQ="
)
LEGACY_STREAM = tuple(base64.b64decode(value) for value in (
    "KLUv/QFYAYQBAEMBBCpaLWw7zX9iHIZRfbf6dQII/LVYmAelGPnNwabG2DJpY9afT9povQkWV7bEBw==",
    "bAAAGHs1MALM0AD5+wH8Cw==",
    "VAgABBAAAQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyAhIiMkJSYnKCkqKywtLi8wMTIz"
    "NDU2Nzg5Ojs8PT4/QEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaW1xdXl9gYWJjZGVmZ2hpamts"
    "bW5vcHFyc3R1dnd4eXp7fH1+f4CBgoOEhYaHiImKi4yNjo+QkZKTlJWWl5iZmpucnZ6foKGio6Sl"
    "pqeoqaqrrK2ur7CxsrO0tba3uLm6u7y9vr/AwcLDxMXGx8jJysvMzc7P0NHS09TV1tfY2drb3N3e"
    "3+Dh4uPk5ebn6Onq6+zt7u/w8fLz9PX29/j5+vv8/f7/AQAA/QPqmgI=",
))
PAYLOADS = (
    b'{"status":{"state":"printing","progress":0.25,"filename":"test.gcode"}}\n' * 32,
    b'{"status":{"state":"printing","progress":0.50,"filename":"test.gcode"}}\n' * 32,
    bytes(range(256)) * 3,
)


@unittest.skipIf(zstd is None, "zstandard is optional; CI installs the real codec")
class TestCompressionCompatibility(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test-compression-compatibility")
        for owner in (Compression, ZStandardDictionary):
            singleton = patch.object(owner, "_Instance", None)
            singleton.start()
            self.addCleanup(singleton.stop)
        with patch("octoeverywhere.compression.multiprocessing.cpu_count", return_value=3):
            self.compression = Compression(self.logger, "")
        Compression._Instance = self.compression
        self.assertTrue(self.compression.CanUseZStandardLib)
        self.assertEqual(ZStandardDictionary.Get().PreTrainedDict.dict_id(), 1)

    def test_decodes_legacy_single_message(self):
        with CompressionContext(self.logger) as context:
            result = self.compression.Decompress(
                context, Buffer(LEGACY_SINGLE), len(PAYLOADS[0]), True, DataCompression.ZStandard
            )
            self.assertEqual(result.Get(), PAYLOADS[0])

    def test_decodes_server_single_message_without_dictionary(self):
        with CompressionContext(self.logger) as context:
            result = self.compression.Decompress(
                context, Buffer(SERVER_SINGLE), len(PAYLOADS[0]), True, DataCompression.ZStandard
            )
            self.assertEqual(result.Get(), PAYLOADS[0])

    def test_decodes_legacy_stream_without_waiting_for_frame_end(self):
        with CompressionContext(self.logger) as context:
            for index, (compressed, expected) in enumerate(zip(LEGACY_STREAM, PAYLOADS)):
                result = self.compression.Decompress(
                    context, Buffer(compressed), len(expected), index == len(PAYLOADS) - 1, DataCompression.ZStandard
                )
                self.assertEqual(result.Get(), expected)

    def test_single_message_preserves_wire_metadata_and_dictionary(self):
        with CompressionContext(self.logger) as encoder, CompressionContext(self.logger) as decoder:
            encoder.SetTotalCompressedSizeOfData(len(PAYLOADS[0]))
            result = self.compression.Compress(encoder, Buffer(PAYLOADS[0]))
            self.assertEqual(result.CompressionType, 3)
            self.assertEqual(result.UncompressedSize, len(PAYLOADS[0]))
            frame = zstd.get_frame_parameters(result.Bytes.Get())
            self.assertEqual(frame.dict_id, 1)
            self.assertEqual(frame.content_size, len(PAYLOADS[0]))
            decoded = self.compression.Decompress(
                decoder, result.Bytes, result.UncompressedSize, True, result.CompressionType
            )
            self.assertEqual(decoded.Get(), PAYLOADS[0])

    def test_stream_messages_decode_individually_with_known_or_unknown_total(self):
        for knownSize in (False, True):
            with self.subTest(known_size=knownSize):
                with CompressionContext(self.logger) as encoder, CompressionContext(self.logger) as decoder:
                    if knownSize:
                        encoder.SetTotalCompressedSizeOfData(sum(len(payload) for payload in PAYLOADS))
                    for index, payload in enumerate(PAYLOADS):
                        result = self.compression.Compress(encoder, Buffer(payload))
                        decoded = self.compression.Decompress(
                            decoder, result.Bytes, len(payload), index == len(PAYLOADS) - 1, result.CompressionType
                        )
                        self.assertEqual(decoded.Get(), payload)

    def test_pooled_codecs_can_start_fresh_after_unfinished_stream(self):
        with CompressionContext(self.logger) as encoder, CompressionContext(self.logger) as decoder:
            result = self.compression.Compress(encoder, Buffer(PAYLOADS[0]))
            self.assertEqual(
                self.compression.Decompress(decoder, result.Bytes, len(PAYLOADS[0]), False, result.CompressionType).Get(),
                PAYLOADS[0],
            )
            compressor = encoder.Compressor
            decompressor = decoder.Decompressor
        with CompressionContext(self.logger) as encoder, CompressionContext(self.logger) as decoder:
            encoder.SetTotalCompressedSizeOfData(len(PAYLOADS[2]))
            result = self.compression.Compress(encoder, Buffer(PAYLOADS[2]))
            decoded = self.compression.Decompress(decoder, result.Bytes, len(PAYLOADS[2]), True, result.CompressionType)
            self.assertEqual(decoded.Get(), PAYLOADS[2])
            self.assertIs(encoder.Compressor, compressor)
            self.assertIs(decoder.Decompressor, decompressor)


if __name__ == "__main__":
    unittest.main()
