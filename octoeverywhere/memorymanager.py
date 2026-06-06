import sys
import ctypes
import logging
from typing import Optional, Tuple


# A class that sets how much data is allowed to be used by the system.
class MemoryManager:

    # These are the dynamic memory limits.
    # They default to the lower limits, but the class might bump them based on the system's available memory and other factors.
    KB = 1024
    MB = 1024 * KB
    GB = 1024 * MB

    # This is the max size anyone single message can be.
    # This should stay in sync with the server size max.
    Global_MaxSingleChunkSizeBytes = int(4.5 * MB)

    # This is the max size of the pending buffer list. If the pending buffer list exceeds this size, the read thread will block until it goes back down.
    # This is to prevent memory issues if the producer is producing data faster than the consumer can consume it.
    # We need to make sure we think about low memory devices, where we don't want to eat RAM.
    HttpStreamAccumulationReader_MaxPendingBufferSizeBytes = 5 * MB

    # This is the max size of the buffer that will be returned from a single Read call to HttpStreamAccumulationReader
    # MUST BE LESS THAN OR EQUAL TO Global_MaxSingleChunkSizeBytes
    HttpStreamAccumulationReader_MaxReturnBufferSizeBytes = Global_MaxSingleChunkSizeBytes

    # This is the max size each body read will be. Since we are making local calls, most of the time we will always get this full amount as long as theres more body to read.
    # This size is a little under the max read buffer on the server, allowing the server to handle the buffers with no copies.
    #
    # 3/24/24 - We did a lot of direct download testing to tweak this buffer size and the server read size, these were the best values able to hit about 223mbps.
    # With the current values, the majority of the time is spent sending the data on the websocket.
    #
    # But NOTE! This size is the actual size that will be allocated for the read buffer (in the stream class) and then the buffer is sliced by how much
    # is read. So we can't make this value too large, or we will be allocating big buffers.
    # MUST BE LESS THAN OR EQUAL TO Global_MaxSingleChunkSizeBytes
    OctoWebStreamHttpHelper_DefaultBodyReadSizeBytes = 1 * MB

    # This is the max amount we will read in one chunk for multipart frames.
    # Multipart frames are used for things like webcam streaming, where we read a chunk of the frame, send it to the server, and then read the next chunk until we have read the full frame.
    # This allows us to stream large frames without allocating a buffer for the full frame.
    # MUST BE LESS THAN OR EQUAL TO Global_MaxSingleChunkSizeBytes
    OctoWebStreamHttpHelper_MaxMultipartReadSizeBytes = 3 * MB

    # This is the largest chunk we will return for a single quickcam stream frame.
    # MUST BE LESS THAN OR EQUAL TO Global_MaxSingleChunkSizeBytes
    QuickCam_MaxStreamChunkSizeBytes = 3 * MB


    _Instance:"MemoryManager" = None #pyright: ignore[reportAssignmentType]

    @staticmethod
    def Init(logger: logging.Logger):
        MemoryManager._Instance = MemoryManager(logger)


    @staticmethod
    def Get() -> "MemoryManager":
        return MemoryManager._Instance


    def __init__(self, logger: logging.Logger) -> None:
        self.Logger = logger

        try:
            # Get the system's total memory and free memory, so we can scale our limits based on the device.
            (totalBytes, freeBytes) = self._GetSystemMemoryInfoBytes()
            if totalBytes is None or freeBytes is None:
                self.Logger.info("MemoryManager - Failed to read system memory info, using default limits.")
                return
            totalGb = totalBytes / MemoryManager.GB
            freeGb = freeBytes / MemoryManager.GB
            self.Logger.info(f"MemoryManager - System memory: total: {totalGb:.2f}GB, free: {freeGb:.2f}GB")

            # We default to lower limits, so if the memory isn't above the higher thresholds, we just return.
            if totalGb < 2 or freeGb < 1:
                return

            # Set the higher limits.
            MemoryManager.HttpStreamAccumulationReader_MaxPendingBufferSizeBytes = 10 * MemoryManager.MB
            MemoryManager.OctoWebStreamHttpHelper_DefaultBodyReadSizeBytes = 2 * MemoryManager.MB
            MemoryManager.OctoWebStreamHttpHelper_MaxMultipartReadSizeBytes = MemoryManager.Global_MaxSingleChunkSizeBytes
            MemoryManager.QuickCam_MaxStreamChunkSizeBytes = MemoryManager.Global_MaxSingleChunkSizeBytes
        except Exception as e:
            self.Logger.warning(f"MemoryManager failed to read system memory info during initialization. {e}")
        finally:
            # Check the limits of things that can't be larger than Global_MaxSingleChunkSizeBytes
            # If this happened, things will break.
            self._ValidateHardLimits()


    def _ValidateHardLimits(self) -> None:
        MemoryManager.HttpStreamAccumulationReader_MaxReturnBufferSizeBytes = self._ValidateBufferSizeLessThanPerSendMax("HttpStreamAccumulationReader_MaxReturnBufferSizeBytes", MemoryManager.HttpStreamAccumulationReader_MaxReturnBufferSizeBytes)
        MemoryManager.OctoWebStreamHttpHelper_DefaultBodyReadSizeBytes = self._ValidateBufferSizeLessThanPerSendMax("OctoWebStreamHttpHelper_DefaultBodyReadSizeBytes", MemoryManager.OctoWebStreamHttpHelper_DefaultBodyReadSizeBytes)
        MemoryManager.OctoWebStreamHttpHelper_MaxMultipartReadSizeBytes = self._ValidateBufferSizeLessThanPerSendMax("OctoWebStreamHttpHelper_MaxMultipartReadSizeBytes", MemoryManager.OctoWebStreamHttpHelper_MaxMultipartReadSizeBytes)
        MemoryManager.QuickCam_MaxStreamChunkSizeBytes = self._ValidateBufferSizeLessThanPerSendMax("QuickCam_MaxStreamChunkSizeBytes", MemoryManager.QuickCam_MaxStreamChunkSizeBytes)


    def _ValidateBufferSizeLessThanPerSendMax(self, varName:str, sizeBytes:int) -> int:
        if sizeBytes > MemoryManager.Global_MaxSingleChunkSizeBytes:
            self.Logger.warning(f"MemoryManager - {varName} requested buffer size of {sizeBytes} bytes is larger than the global max single chunk size of {MemoryManager.Global_MaxSingleChunkSizeBytes} bytes. Setting it to the global max.")
            return MemoryManager.Global_MaxSingleChunkSizeBytes
        return sizeBytes


    # Returns a tuple of (totalBytes, freeBytes) for the system.
    # If the values can't be read for any reason, the matching value will be None.
    def _GetSystemMemoryInfoBytes(self) -> Tuple[Optional[int], Optional[int]]:
        if sys.platform.startswith("win"):
            return self._GetWindowsMemoryInfoBytes()
        # Everything else (Linux, including the Raspberry Pi and other SBCs) reads from /proc/meminfo.
        return self._GetLinuxMemoryInfoBytes()


    # Reads the total and available memory on Linux from /proc/meminfo.
    def _GetLinuxMemoryInfoBytes(self) -> Tuple[Optional[int], Optional[int]]:
        totalBytes:Optional[int] = None
        freeBytes:Optional[int] = None
        # /proc/meminfo reports values in kB, one stat per line, like "MemTotal:        8123456 kB".
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                # We need at least the name and the value.
                if len(parts) < 2:
                    continue
                name = parts[0]
                # MemAvailable is the kernel's best estimate of memory available for new processes without swapping,
                # which is a better measure of "free" memory than MemFree. Fall back to MemFree if it's not present.
                if name == "MemTotal:":
                    totalBytes = int(parts[1]) * 1024
                elif name == "MemAvailable:":
                    freeBytes = int(parts[1]) * 1024
                elif name == "MemFree:" and freeBytes is None:
                    freeBytes = int(parts[1]) * 1024
        return (totalBytes, freeBytes)


    # Reads the total and available memory on Windows via the GlobalMemoryStatusEx API.
    def _GetWindowsMemoryInfoBytes(self) -> Tuple[Optional[int], Optional[int]]:
        # The MEMORYSTATUSEX structure the Windows API fills out for us.
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

            def __init__(self) -> None:
                super().__init__()
                # The API requires us to set the size of the struct before the call.
                self.dwLength = ctypes.sizeof(type(self))

        stat = MEMORYSTATUSEX()
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)) == 0: #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
            raise ctypes.WinError() #pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
        return (int(stat.ullTotalPhys), int(stat.ullAvailPhys))
