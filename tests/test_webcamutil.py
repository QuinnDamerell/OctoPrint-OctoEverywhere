import io
import logging
import types
import unittest

import urllib3

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

from octoeverywhere.Webcam.webcamutil import WebcamUtil  # noqa: E402


class FragmentedStream(io.BytesIO):
    def readinto(self, target):
        return super().readinto(memoryview(target)[:7])


class RecordingStream(io.BytesIO):
    def __init__(self, data:bytes) -> None:
        super().__init__(data)
        self.ReadSizes = []


    def readinto(self, target):
        self.ReadSizes.append(len(target))
        return super().readinto(target)


class TestWebcamUtil(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("TestWebcamUtil")


    def _MakeFrame(self, data:bytes) -> bytes:
        return b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data


    def _MakeResult(self, data:bytes, raw=None):
        return types.SimpleNamespace(
            Headers={"content-type": "multipart/x-mixed-replace"},
            ResponseForBodyRead=types.SimpleNamespace(raw=raw if raw is not None else FragmentedStream(data)),
            MultipartStreamCarryOver=None,
        )


    def test_small_adjacent_frames_are_read_without_consuming_next_frame(self) -> None:
        first = b"\xff\xd8" + b"a" * 500 + b"\xff\xd9"
        second = b"\xff\xd8" + b"b" * 3900 + b"\xff\xd9"
        response = self._MakeResult(self._MakeFrame(first) + b"\r\n" + self._MakeFrame(second))
        self.assertEqual(WebcamUtil.GetSnapshotFromStream(self.logger, response).ImageBuffer.Get(), first)
        self.assertEqual(WebcamUtil.GetSnapshotFromStream(self.logger, response).ImageBuffer.Get(), second)


    def test_large_fragmented_frame_is_read_completely(self) -> None:
        data = b"\xff\xd8" + b"x" * 5000 + b"\xff\xd9"
        response = self._MakeResult(self._MakeFrame(data))
        self.assertEqual(WebcamUtil.GetSnapshotFromStream(self.logger, response).ImageBuffer.Get(), data)


    def test_urllib3_reader_preserves_adjacent_frames(self) -> None:
        data = b"\xff\xd8small jpeg\xff\xd9"
        raw = urllib3.response.HTTPResponse(body=io.BytesIO(self._MakeFrame(data) * 2), preload_content=False)
        self.addCleanup(raw.close)
        response = self._MakeResult(b"", raw)
        for _ in range(2):
            self.assertEqual(WebcamUtil.GetSnapshotFromStream(self.logger, response).ImageBuffer.Get(), data)
        self.assertIsNone(response.MultipartStreamCarryOver)


    def test_tiny_frames_carry_over_partial_headers(self) -> None:
        # Many frames fit in one header read, and the read ends partway through a later frame's headers.
        frames = [b"\xff\xd8" + bytes([ord("a") + i]) * (i + 1) + b"\xff\xd9" for i in range(8)]
        raw = urllib3.response.HTTPResponse(body=io.BytesIO(b"\r\n".join(self._MakeFrame(f) for f in frames)), preload_content=False)
        self.addCleanup(raw.close)
        response = self._MakeResult(b"", raw)
        for frame in frames:
            self.assertEqual(WebcamUtil.GetSnapshotFromStream(self.logger, response, validateMultiStreamHeader=False).ImageBuffer.Get(), frame)
        self.assertIsNone(WebcamUtil.GetSnapshotFromStream(self.logger, response, validateMultiStreamHeader=False))


    def test_large_frame_uses_one_small_header_read(self) -> None:
        # Frames are read on the hot webcam streaming path: one small header read, then the rest of the image goes straight into its buffer.
        data = b"\xff\xd8" + b"x" * 50000 + b"\xff\xd9"
        raw = RecordingStream(self._MakeFrame(data) + b"\r\n" + self._MakeFrame(data))
        response = self._MakeResult(b"", raw)
        self.assertEqual(WebcamUtil.GetSnapshotFromStream(self.logger, response).ImageBuffer.Get(), data)
        headerSize = len(self._MakeFrame(data)) - len(data)
        self.assertEqual(raw.ReadSizes, [256, len(data) - (256 - headerSize)])
        self.assertIsNone(response.MultipartStreamCarryOver)
        self.assertEqual(WebcamUtil.GetSnapshotFromStream(self.logger, response, validateMultiStreamHeader=False).ImageBuffer.Get(), data)


    def test_truncated_frame_is_not_returned(self) -> None:
        response = self._MakeResult(self._MakeFrame(b"x" * 500)[:-10])
        self.assertIsNone(WebcamUtil.GetSnapshotFromStream(self.logger, response))


    def test_unterminated_headers_still_obey_size_limit(self) -> None:
        response = self._MakeResult(b"x" * 5000)
        self.assertIsNone(WebcamUtil.GetSnapshotFromStream(self.logger, response))
        self.assertEqual(response.ResponseForBodyRead.raw.tell(), 4096)


if __name__ == "__main__":
    unittest.main()
