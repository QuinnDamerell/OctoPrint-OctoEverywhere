import os
import sys
import json
import time
import zlib
import logging
import threading
import subprocess
import multiprocessing

from .sentry import Sentry
from .zstandarddictionary import ZStandardDictionary

from .Proto.DataCompression import DataCompression


# A return type for the compression operation.
class CompressionResult:
    def __init__(self, b: bytes, duration:float, compressionType: DataCompression) -> None:
        self.Bytes = b
        self.CompressionType = compressionType
        self.CompressionTimeSec = duration


# The compression context should match the lifespan of the compression operation for a set of data.
# For example, one websocket should use the same compression context, so it uses one compression stream.
# This class is not thread safe PER OPERATION so it must only be used by one thread per operation.
# So only one thread can be doing compression, but another thread can be doing decompression.
# This class rents shared resources, so it should be used in with the `with` statement in PY to make sure it's cleaned up.
class CompressionContext:

    # This is the default value used by the zstandard to indicate the full size of the data is unknown.
    TOTAL_SIZE_UNKNOWN = -1


    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger
        self.ResourceLock = threading.Lock()
        self.IsClosed = False

        # Compression - can't be shared to be thread safe
        self.Compressor = None
        self.StreamWriter = None
        self.CompressionByteBuffer:bytes = None
        # The compression is more efficient if we know the size of the data of the og data.
        self.CompressionTotalSizeOfDataBytes:int = CompressionContext.TOTAL_SIZE_UNKNOWN

        # Decompression - can't be shared to be thread safe
        self.Decompressor = None
        self.StreamReader = None
        self.DecompressionByteBuffer:bytes = None


    def __del__(self):
        # Ensure exit was called before the object is destroyed.
        # This ensures we always return the compression contexts
        try:
            self.__exit__(None, None, None)
        except Exception as e:
            Sentry.Exception("CompressionContext had an exception on object delete", e)


    def __enter__(self):
        return self


    def __exit__(self, exc_type, exc_value, traceback):
        # Free anything that has been allocated in reverse order.
        # We use a lock to ensure we don't leak any of the resources, especially the rented ones.
        streamWriter = None
        compressor = None
        streamReader = None
        decompressor = None

        with self.ResourceLock:
            self.IsClosed = True

            streamWriter = self.StreamWriter
            compressor = self.Compressor
            self.StreamWriter = None
            self.Compressor = None
            self.CompressionByteBuffer = None

            streamReader = self.StreamReader
            decompressor = self.Decompressor
            self.StreamReader = None
            self.Decompressor = None
            self.DecompressionByteBuffer = None

        # Exit them outside of the lock
        if streamWriter is not None:
            streamWriter.__exit__(exc_type, exc_value, traceback)
        if compressor is not None:
            Compression.Get().ReturnZStandardCompressor(compressor)
        if streamReader is not None:
            streamReader.__exit__(exc_type, exc_value, traceback)
        if decompressor is not None:
            Compression.Get().ReturnZStandardDecompressor(decompressor)


    # Ideally, we want to tell the system how much data is being compressed in total.
    def SetTotalCompressedSizeOfData(self, totalSizeBytes:int):
        if self.StreamWriter is not None:
            raise Exception("CompressionContext SetTotalSizeOfData tried to be set after compression started")
        self.CompressionTotalSizeOfDataBytes = totalSizeBytes


    # This is the callback from stream_writer that get called when it has data to write.
    def write(self, data):
        # A bytearray is a better option if we are continuously appending data, since we can allocate a bigger buffer
        # and copy into it. But 99% of the time we are only doing one compress callback at a time, in which case it's
        # better to just take the buffer given to us and use it.
        if self.CompressionByteBuffer is None:
            self.CompressionByteBuffer = data
        else:
            self.CompressionByteBuffer += data


    # Compresses the data.
    # Returns a successful CompressionResult or throws
    def Compress(self, data:bytes) -> CompressionResult:
        # Ensure we are setup.
        startSec = time.time()
        with self.ResourceLock:
            if self.IsClosed:
                raise Exception("The compression context is closed, we can't compress data")
            if self.Compressor is None:
                self.Compressor = Compression.Get().RentZStandardCompressor()
                if self.Compressor is None:
                    raise Exception("CompressionContext failed to rent a compressor")

        # After a lot of testing, we found that the streaming compression about 80% slower, but that's only 0.1ms in most cases.
        # But if it's an actual stream AND WE ARE DOING MULTIPLE COMPRESSES, it can compress UP TO 300% TIMES BETTER, for example with websocket messages.
        # If we are only doing one (big) compress, then there's no big compression gain, so we only take a time hit.
        #
        # Thus, as a good middle ground, if the buffer input is the exact size as we know the full length is, we do a one time compress.
        if self.CompressionTotalSizeOfDataBytes == len(data):
            return CompressionResult(self.Compressor.compress(data), time.time() - startSec, DataCompression.ZStandard)

        # If the data is size is unknown or this buffer is smaller than it, it's most likely a stream, so the streaming setup works much better.
        # Since we are passing the size if known, we can't call flush(zstd.FLUSH_FRAME), since the size indicates the expected full frame size.
        with self.ResourceLock:
            if self.IsClosed:
                raise Exception("The compression context is closed, we can't start a stream writer")
            if self.StreamWriter is None:
                self.StreamWriter = self.Compressor.stream_writer(self, size=self.CompressionTotalSizeOfDataBytes)

        # Compress this chunk.
        self.StreamWriter.write(data)

        # We call flush to get the output that can be independently decompressed, but we don't use the
        # zstd.FLUSH_FRAME flag. If we used the zstd.FLUSH_FRAME, we would have to make sure the entire length is written.
        self.StreamWriter.flush()

        # Capture the buffer of the written data.
        if self.CompressionByteBuffer is None:
            raise Exception("CompressionContext failed to get a buffer of the compressed data")
        resultBuffer = self.CompressionByteBuffer
        self.CompressionByteBuffer = None

        # Done
        return CompressionResult(resultBuffer, time.time() - startSec, DataCompression.ZStandard)


    # This is the callback from stream_reader that get called when it needs more data to read.
    def read(self, readSizeBytes:int) -> bytes:
        if self.DecompressionByteBuffer is None:
            # This is bad. If we return bytes(), which is what is normally done when the stream has ended, it will prevent
            # the stream_reader from ever reading again. In our case, we should never hit this, because we don't know how much
            # more of the stream there is to read.
            # We prevent this from happening by calling read with exactly the uncompressed size of the data. This means that the read
            # loop will consume the full buffer, but then never come back for more because it's output all it should have.
            raise Exception("CompressionContext read ran out of buffer to read so the stream will be terminated early.")
            #return bytes()

        # If the read size is the same as the buffer, we will consume it all at once.
        if readSizeBytes >= len(self.DecompressionByteBuffer):
            ret = self.DecompressionByteBuffer
            self.DecompressionByteBuffer = None
            return ret

        # Otherwise, we will consume the exact amount we are asked for.
        ret = self.DecompressionByteBuffer[:readSizeBytes]
        self.DecompressionByteBuffer = self.DecompressionByteBuffer[readSizeBytes:]
        return ret


    # Given a byte buffer, decompresses the stream and returns the bytes.
    def Decompress(self, data:bytes, thisMsgUncompressedDataSize:int, isLastMessage:bool) -> bytes:
        # Ensure we are setup.
        isFirstMessage = False
        with self.ResourceLock:
            if self.IsClosed:
                raise Exception("The compression context is closed, we can't decompress data")
            if self.Decompressor is None:
                isFirstMessage = True
                self.Decompressor = Compression.Get().RentZStandardDecompressor()
                if self.Decompressor is None:
                    raise Exception("CompressionContext failed to rent a decompressor")

        # Same the the compressor, if this is the first and only message, we use the one time decompress.
        # This is faster because for some reason using the stream version of the API for just one message is slower.
        if isFirstMessage and isLastMessage:
            return self.Decompressor.decompress(data)

        # If the data is size is unknown or this buffer is smaller than it, it's most likely a stream, so the streaming setup works much better.
        # Since we are passing the size if known, we can't call flush(zstd.FLUSH_FRAME), since the size indicates the expected full frame size.
        with self.ResourceLock:
            if self.IsClosed:
                raise Exception("The compression context is closed, we can't start a stream reader")
            if self.StreamReader is None:
                self.StreamReader = self.Decompressor.stream_reader(self)

        # Set the buffer for the decompressor to be read by the read() function
        self.DecompressionByteBuffer = data

        # NOTE! It's important to read exactly the amount we are expecting and nothing more.
        # The reason is explained in the read() function
        return self.StreamReader.read(thisMsgUncompressedDataSize)


# A helper class to handle compression for streams.
class Compression:

    # Defines the min size a buffer must be before we compress it.
    # There's some small size that's not worth the time to compress, and also compressing it usually makes it bigger.
    # That said, zstandard actually does quite well with small payloads, so we can set this quite low.
    MinSizeToCompress = 200

    # Since zstandard can't be a required dep since it will fail on some platforms, we try to install it via the runtime or
    # the linux installer if possible. Due to that, this is the package version string they will use ty to to install it.
    # We currently have this set to 21, which still supports PY3.7, which is from 2019.
    # THIS MUST STAY IN SYNC WITH THE VERSION IN THE Dockerfile and the GitHub actions linter file.
    ZStandardPipPackageString = "zstandard>=0.21.0,<0.23.0"
    ZStandardMinCoreCountForInstall = 3

    _Instance = None

    @staticmethod
    def Init(logger: logging.Logger, localFileStoragePath:str):
        Compression._Instance = Compression(logger, localFileStoragePath)


    @staticmethod
    def Get():
        return Compression._Instance


    def __init__(self, logger: logging.Logger, localFileStoragePath:str) -> None:
        self.Logger = logger
        self.LocalFileStoragePath = localFileStoragePath
        self.ZStandardCompressorPool = []
        self.ZStandardCompressorPoolLock = threading.Lock()
        self.ZStandardCompressorCreatedCount = 0

        self.ZStandardDecompressorPool = []
        self.ZStandardDecompressorPoolLock = threading.Lock()
        self.ZStandardDecompressorCreatedCount = 0

        # Determine the thread count we will allow zstandard to use.
        # If there are 3 or less cores, we will only use one thread.
        # If there are 4 or more cores, we will use all but 2.
        self.ZStandardThreadCount = 1
        cpuCores = multiprocessing.cpu_count()
        if cpuCores <= 3:
            self.ZStandardThreadCount = 1
        else:
            self.ZStandardThreadCount = cpuCores - 2

        # Always init the zstandard singleton, even if we aren't using zstandard.
        ZStandardDictionary.Init(logger)

        # Try to load the zstandard library, if it fails, we won't use it.
        # Some systems don't have the native lib this will try to load, so we will fall back to zlib.
        self.CanUseZStandardLib = False
        try:
            #pylint: disable=import-outside-toplevel,unused-import
            import zstandard as zstd

            # Since we are using zlib, try to load the pre-trained dictionary.
            # This will throw if it fails, and we must load this dict to use zstandard, because the server expects it.
            ZStandardDictionary.Get().InitPreComputedDict()

            # Only set this flag after everything is setup and good.
            self.CanUseZStandardLib = True
            self.Logger.info(f"Compression is using zstandard with {self.ZStandardThreadCount} threads")

            # Once the state is set, make a few compressors and decompressors so they are cached and ready to go.
            c = self.RentZStandardCompressor()
            c2 = self.RentZStandardCompressor()
            self.ReturnZStandardCompressor(c)
            self.ReturnZStandardCompressor(c2)

            d = self.RentZStandardDecompressor()
            d2 = self.RentZStandardDecompressor()
            self.ReturnZStandardDecompressor(d)
            self.ReturnZStandardDecompressor(d2)
        except Exception as e:
            self.Logger.info(f"Failed to load the zstandard lib, so we won't use it. Error: {e}")

        # If we can't use zstandard, we assume it's not installed since it doesn't install as a required dependency.
        # In that case, we will use this function to try to install it async, and it will be used on the next restart.
        # But, if the system has two or less cores, dont try to install, because it's probably not powerful enough to use it.
        if self.CanUseZStandardLib is False and cpuCores >= Compression.ZStandardMinCoreCountForInstall:
            self._TryInstallZStandardIfNeededAsync()


    # Given a buffer of data, compress it using the best available compression library.
    def Compress(self, compressionContext:CompressionContext, data: bytes) -> CompressionResult:
        # If we have zstandard lib, use that, since it's better.
        if self.CanUseZStandardLib:
            # If we are training, submit the data to be sampled.
            # ZStandardDictionary.Get().SubmitData(data)
            return compressionContext.Compress(data)

        # If we can't use zStandard lib, fallback to zlib
        startSec = time.time()
        compressed = zlib.compress(data, 3)
        return CompressionResult(compressed, time.time() - startSec, DataCompression.Zlib)


    # Given a buffer of data and the compression type, decompresses it.
    def Decompress(self, compressionContext:CompressionContext, data:bytes, thisMsgUncompressedDataSize:int, isLastMessage:bool, compressionType: DataCompression) -> bytes:
        # Decompress depending on what type of compression was used.
        if compressionType == DataCompression.Zlib:
            return zlib.decompress(data)
        elif compressionType == DataCompression.ZStandard:
            if self.CanUseZStandardLib is False:
                raise Exception("We tried to decompress data using DataCompression.ZStandard, but we can't support that library on this system.")
            return compressionContext.Decompress(data, thisMsgUncompressedDataSize, isLastMessage)
            # This is logic we use if we want to train the zstandard lib.
            # data = compressionContext.Decompress(data, thisMsgUncompressedDataSize, isLastMessage)
            # ZStandardDictionary.Get().SubmitData(data)
            # return data
        else:
            raise Exception(f"Unknown compression type: {compressionType}")


    # Returns a compressor or None if it fails to load.
    # The compressor warps the zstandard lib context, they are reusable but not thread safe.
    def RentZStandardCompressor(self):
        if self.CanUseZStandardLib is False:
            return None
        try:
            with self.ZStandardCompressorPoolLock:
                if len(self.ZStandardCompressorPool) > 0:
                    return self.ZStandardCompressorPool.pop()

                # Report how many we have created for leak detection.
                self.ZStandardCompressorCreatedCount += 1
                if self.ZStandardCompressorCreatedCount > 40:
                    self.Logger.warn(f"Compression zstandard compressor pool has created {self.ZStandardCompressorCreatedCount} items, there might be a leak")

                #pylint: disable=import-outside-toplevel
                import zstandard as zstd
                # We must use the pre-trained dict, since the service uses it as well and it must match.
                return zstd.ZstdCompressor(threads=self.ZStandardThreadCount, dict_data=ZStandardDictionary.Get().PreTrainedDict)
        except Exception as e:
            self.Logger.error(f"Failed to rent zstandard compressor. Error: {e}")
        return None


    # Puts the compressor back into the pool
    def ReturnZStandardCompressor(self, compressor):
        if compressor is None:
            return
        with self.ZStandardCompressorPoolLock:
            self.ZStandardCompressorPool.append(compressor)


    # Returns a decompressor or None if it fails to load.
    # The decompressor warps the zstandard lib context, they are reusable but not thread safe.
    def RentZStandardDecompressor(self):
        if self.CanUseZStandardLib is False:
            return None
        try:
            with self.ZStandardDecompressorPoolLock:
                if len(self.ZStandardDecompressorPool) > 0:
                    return self.ZStandardDecompressorPool.pop()

                # Report how many we have created for leak detection.
                self.ZStandardDecompressorCreatedCount += 1
                if self.ZStandardDecompressorCreatedCount > 40:
                    self.Logger.warn(f"Compression zstandard decompressor pool has created {self.ZStandardDecompressorCreatedCount} items, there might be a leak")

                #pylint: disable=import-outside-toplevel
                import zstandard as zstd
                # We must use the pre-trained dict, since the service uses it as well and it must match.
                return zstd.ZstdDecompressor(dict_data=ZStandardDictionary.Get().PreTrainedDict)
        except Exception as e:
            self.Logger.error(f"Failed to rent zstandard decompressor. Error: {e}")
        return None


    # Puts the decompressor back into the pool
    def ReturnZStandardDecompressor(self, decompressor):
        if decompressor is None:
            return
        with self.ZStandardDecompressorPoolLock:
            self.ZStandardDecompressorPool.append(decompressor)


    # If we can't use zstandard, we assume it's not installed since it doesn't install as a required dependency.
    # In that case, we will use this function to try to install it async, and it will be used on the next restart.
    def _TryInstallZStandardIfNeededAsync(self):
        threading.Thread(target=self._TryInstallZStandardIfNeeded, daemon=True).start()


    def _TryInstallZStandardIfNeeded(self):
        lastAttemptFileName = "CompressionData.json"
        try:
            # First, see if we need to try to do this again.
            filePath = os.path.join(self.LocalFileStoragePath, lastAttemptFileName)
            if os.path.exists(filePath):
                with open(filePath, encoding="utf-8") as f:
                    data = json.load(f)
                    if "LastUpdateTimeSec" in data:
                        lastUpdateTimeSec = float(data["LastUpdateTimeSec"])
                        # If the most recent attempt was less than 30 days ago, we won't try again.
                        if time.time() - lastUpdateTimeSec < 30 * 24 * 60 * 60:
                            return

            # We are going to update, write a file now with the current time.
            with open(filePath, encoding="utf-8", mode="w") as f:
                data = {
                    "LastUpdateTimeSec": time.time()
                }
                json.dump(data, f)

            # Try to do the update now.
            # Limit the install, but give it a longer timeout since it might try to compile.
            # Use `sys.executable` to make sure we get our virtual env python.
            result = subprocess.run([sys.executable, '-m', 'pip', 'install', Compression.ZStandardPipPackageString], timeout=60.0, check=False, capture_output=True)
            if result.returncode == 0:
                self.Logger.info(f"Pip install/update of {sys.executable} {Compression.ZStandardPipPackageString} successful.")
                return
            self.Logger.info(f"Compression pip install failed. {sys.executable} {Compression.ZStandardPipPackageString}. stdout:{result.stdout} - stderr:{result.stderr}")
        except Exception as e:
            self.Logger.error(f"Compression failed to pip install zstandard lib. {e}")

#
# This is an old comment, from before zstandard lib. But it still has useful info about zlib and brotli
# For zstandard, we found that it's faster and compresses way better, especially on small messages if it can stream like the websocket.
#
# Some setups can't install brotli since it requires gcc and c++ to compile native code.
# zlib is part of PY so all plugins us it. Right now it's not worth the tradeoff from testing to enable brotli.
#
# After a good amount of testing, we found that a compression level of 3 is a good tradeoff for both.
# For small to medium size files zlib can actually be better. Brotli starts to be much better in terms of speed
# and compression for larger files. But for now given the file sizes we use here, it's not worth it.
#
# Here's a good quick benchmark on a large js file (4mb)
#2021-12-17 22:37:22,258 - octoprint.plugins.octoeverywhere - INFO - zlib level: 0 time:9.43207740784 size: 815175 og:815104
#2021-12-17 22:37:22,319 - octoprint.plugins.octoeverywhere - INFO - zlib level: 1 time:58.7220191956 size: 273923 og:815104
#2021-12-17 22:37:22,383 - octoprint.plugins.octoeverywhere - INFO - zlib level: 2 time:61.7210865021 size: 263366 og:815104
#2021-12-17 22:37:22,453 - octoprint.plugins.octoeverywhere - INFO - zlib level: 3 time:69.3519115448 size: 256257 og:815104
#2021-12-17 22:37:22,537 - octoprint.plugins.octoeverywhere - INFO - zlib level: 4 time:81.6609859467 size: 239928 og:815104
#2021-12-17 22:37:22,650 - octoprint.plugins.octoeverywhere - INFO - zlib level: 5 time:110.955953598 size: 231844 og:815104
#2021-12-17 22:37:22,803 - octoprint.plugins.octoeverywhere - INFO - zlib level: 6 time:150.192022324 size: 229684 og:815104
#2021-12-17 22:37:22,972 - octoprint.plugins.octoeverywhere - INFO - zlib level: 7 time:166.711091995 size: 229118 og:815104
#2021-12-17 22:37:23,196 - octoprint.plugins.octoeverywhere - INFO - zlib level: 8 time:221.390962601 size: 228784 og:815104
#2021-12-17 22:37:23,442 - octoprint.plugins.octoeverywhere - INFO - zlib level: 9 time:244.188070297 size: 228737 og:815104
#2021-12-17 22:37:23,477 - octoprint.plugins.octoeverywhere - INFO - brotli level: 0 time:31.9409370422 size: 280540 og:815104
#2021-12-17 22:37:23,536 - octoprint.plugins.octoeverywhere - INFO - brotli level: 1 time:56.2720298767 size: 267581 og:815104
#2021-12-17 22:37:23,611 - octoprint.plugins.octoeverywhere - INFO - brotli level: 2 time:72.9219913483 size: 245109 og:815104
#2021-12-17 22:37:23,703 - octoprint.plugins.octoeverywhere - INFO - brotli level: 3 time:86.4551067352 size: 241472 og:815104
#2021-12-17 22:37:23,874 - octoprint.plugins.octoeverywhere - INFO - brotli level: 4 time:169.479846954 size: 235446 og:815104
#2021-12-17 22:37:24,125 - octoprint.plugins.octoeverywhere - INFO - brotli level: 5 time:248.244047165 size: 219928 og:815104
#2021-12-17 22:37:24,451 - octoprint.plugins.octoeverywhere - INFO - brotli level: 6 time:321.651935577 size: 217598 og:815104
#2021-12-17 22:37:24,848 - octoprint.plugins.octoeverywhere - INFO - brotli level: 7 time:395.76292038 size: 216307 og:815104
#2021-12-17 22:37:25,334 - octoprint.plugins.octoeverywhere - INFO - brotli level: 8 time:483.689785004 size: 215660 og:815104
#2021-12-17 22:37:25,973 - octoprint.plugins.octoeverywhere - INFO - brotli level: 9 time:637.011051178 size: 214962 og:815104
#2021-12-17 22:37:30,395 - octoprint.plugins.octoeverywhere - INFO - brotli level: 10 time:4420.00603676 size: 202474 og:815104
#2021-12-17 22:37:40,826 - octoprint.plugins.octoeverywhere - INFO - brotli level: 11 time:10429.7590256 size: 198538 og:815104
# Here's a more average size file
#2021-12-17 22:45:06,278 - octoprint.plugins.octoeverywhere - INFO - zlib level: 0 time:1.84893608093 size: 13514 og:13503
#2021-12-17 22:45:06,291 - octoprint.plugins.octoeverywhere - INFO - zlib level: 1 time:1.37400627136 size: 5647 og:13503
#2021-12-17 22:45:06,298 - octoprint.plugins.octoeverywhere - INFO - zlib level: 2 time:3.87191772461 size: 5550 og:13503
#2021-12-17 22:45:06,301 - octoprint.plugins.octoeverywhere - INFO - zlib level: 3 time:1.43599510193 size: 5498 og:13503
#2021-12-17 22:45:06,304 - octoprint.plugins.octoeverywhere - INFO - zlib level: 4 time:1.70516967773 size: 5306 og:13503
#2021-12-17 22:45:06,308 - octoprint.plugins.octoeverywhere - INFO - zlib level: 5 time:2.17819213867 size: 5227 og:13503
#2021-12-17 22:45:06,312 - octoprint.plugins.octoeverywhere - INFO - zlib level: 6 time:2.08187103271 size: 5217 og:13503
#2021-12-17 22:45:06,316 - octoprint.plugins.octoeverywhere - INFO - zlib level: 7 time:2.29096412659 size: 5218 og:13503
#2021-12-17 22:45:06,320 - octoprint.plugins.octoeverywhere - INFO - zlib level: 8 time:2.12597846985 size: 5218 og:13503
#2021-12-17 22:45:06,324 - octoprint.plugins.octoeverywhere - INFO - zlib level: 9 time:2.29811668396 size: 5218 og:13503
#2021-12-17 22:45:06,327 - octoprint.plugins.octoeverywhere - INFO - brotli level: 0 time:1.26886367798 size: 5877 og:13503
#2021-12-17 22:45:06,330 - octoprint.plugins.octoeverywhere - INFO - brotli level: 1 time:1.18708610535 size: 5828 og:13503
#2021-12-17 22:45:06,334 - octoprint.plugins.octoeverywhere - INFO - brotli level: 2 time:1.77407264709 size: 5479 og:13503
#2021-12-17 22:45:06,339 - octoprint.plugins.octoeverywhere - INFO - brotli level: 3 time:2.63094902039 size: 5418 og:13503
#2021-12-17 22:45:06,345 - octoprint.plugins.octoeverywhere - INFO - brotli level: 4 time:4.88996505737 size: 5335 og:13503
#2021-12-17 22:45:06,354 - octoprint.plugins.octoeverywhere - INFO - brotli level: 5 time:6.34503364563 size: 5007 og:13503
#2021-12-17 22:45:06,364 - octoprint.plugins.octoeverywhere - INFO - brotli level: 6 time:8.3749294281 size: 5003 og:13503
#2021-12-17 22:45:06,384 - octoprint.plugins.octoeverywhere - INFO - brotli level: 7 time:18.7141895294 size: 4994 og:13503
#2021-12-17 22:45:06,411 - octoprint.plugins.octoeverywhere - INFO - brotli level: 8 time:25.6741046906 size: 4994 og:13503
#2021-12-17 22:45:06,447 - octoprint.plugins.octoeverywhere - INFO - brotli level: 9 time:33.3149433136 size: 4989 og:13503
#2021-12-17 22:45:06,499 - octoprint.plugins.octoeverywhere - INFO - brotli level: 10 time:50.5220890045 size: 4609 og:13503
#2021-12-17 22:45:06,636 - octoprint.plugins.octoeverywhere - INFO - brotli level: 11 time:135.287046432 size: 4503 og:13503
