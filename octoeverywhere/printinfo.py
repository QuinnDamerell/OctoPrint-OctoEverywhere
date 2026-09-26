import os
import json
import time
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


# The goal of this class is to keep track of info about the current print.
# This is needed because sometimes we only get the info once, like at the start of a print, and then we want to keep it around for future notifications.
# This class also writes out to disk, so for hosts where the host can crash or be restarted mid print, the print info can be recovered.
class PrintInfo:

    # Required Json Vars
    c_PrintCookieKey = "PrintCookie"
    c_PrintIdKey = "PrintId"
    c_PrintStartTimeSecKey = "PrintStartTimeSec"

    # Optional
    c_FileNameKey = "FileName"
    c_FileSizeInKBytes = "FileSizeKBytes"
    c_EstFilamentUsageMm = "EstFilamentUsageMm"
    c_EstFilamentWeightMg = "EstFilamentWeightMg"
    c_FinalPrintDurationSec = "FinalPrintDurationSec"

    # Given a file path, this loads a print info if possible.
    # Returns None on failure.
    @staticmethod
    def LoadFromFile(logger:logging.Logger, filePath:str) -> Optional["PrintInfo"]:
        try:
            with open(filePath, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Ensure it has the required vars.
                if PrintInfo.c_PrintIdKey not in data  or PrintInfo.c_PrintCookieKey not in data or PrintInfo.c_PrintStartTimeSecKey not in data:
                    raise Exception("File loaded, but there was no Print ID")
                return PrintInfo(logger, filePath, data)
        except Exception as e:
            logger.error(f"Failed to load print info from file. {e}")
        return None


    # Given a file path and required args, creates a new print context.
    # This will always return a PrintInfo! Even if it fails to write to disk.
    @staticmethod
    def CreateNew(logger:logging.Logger, filePath:str, printCookie:str, printId:str) -> "PrintInfo":
        data = {
            PrintInfo.c_PrintCookieKey : printCookie,
            PrintInfo.c_PrintIdKey : printId,
            PrintInfo.c_PrintStartTimeSecKey : time.time()
        }
        pi = PrintInfo(logger, filePath, data)
        # Save, but always return a object even if this fails.
        pi.Save()
        return pi


    def __init__(self, logger:logging.Logger, filePath:str, data:Dict[str,Any]) -> None:
        self.Logger = logger
        self.FilePath = filePath
        self.Data = data


    # Required var, this will always exist and can't be changed.
    def GetPrintId(self) -> str:
        return self.Data[PrintInfo.c_PrintIdKey]


    # Required var, this will always exist and can't be changed.
    def GetPrintCookie(self) -> str:
        return self.Data[PrintInfo.c_PrintCookieKey]


    # Always exists, but it can be updated if the platform reports an exact time.
    def GetLocalPrintStartTimeSec(self) -> float:
        return self.Data[PrintInfo.c_PrintStartTimeSecKey]
    def SetLocalPrintStartTimeSec(self, startTimeSec:float) -> None:
        if self.GetLocalPrintStartTimeSec() != startTimeSec:
            self.Data[PrintInfo.c_PrintStartTimeSecKey] = startTimeSec
            self.Save()


    # The file name is optional.
    def GetFileName(self) -> Optional[str]:
        return self.Data.get(PrintInfo.c_FileNameKey, None)
    def SetFileName(self, fileName:str) -> None:
        current = self.GetFileName()
        if current is None or current != fileName:
            self.Data[PrintInfo.c_FileNameKey] = fileName
            self.Save()


    # The file size in kbytes is optional
    def GetFileSizeKBytes(self) -> int:
        return self.Data.get(PrintInfo.c_FileSizeInKBytes, 0)
    def SetFileSizeKBytes(self, sizeBytes:int) -> None:
        if self.GetFileSizeKBytes() != sizeBytes:
            self.Data[PrintInfo.c_FileSizeInKBytes] = sizeBytes
            self.Save()


    # Estimated filament usage is optional.
    def GetEstFilamentUsageMm(self) -> int:
        return self.Data.get(PrintInfo.c_EstFilamentUsageMm, 0)
    def SetEstFilamentUsageMm(self, estMm:int) -> None:
        if self.GetEstFilamentUsageMm() != estMm:
            self.Data[PrintInfo.c_EstFilamentUsageMm] = estMm
            self.Save()


    # Estimated filament weight used is optional.
    def GetEstFilamentWeightUsageMg(self) -> int:
        return self.Data.get(PrintInfo.c_EstFilamentWeightMg, 0)
    def SetEstFilamentWeightUsageMg(self, estG:int) -> None:
        if self.GetEstFilamentWeightUsageMg() != estG:
            self.Data[PrintInfo.c_EstFilamentWeightMg] = estG
            self.Save()


    # This is only set when the print is done.
    # Returns None if there isn't one.
    def GetFinalPrintDurationSec(self) -> Optional[int]:
        return self.Data.get(PrintInfo.c_FinalPrintDurationSec, None)
    def SetFinalPrintDurationSec(self, totalDurationSec:int) -> None:
        self.Data[PrintInfo.c_FinalPrintDurationSec] = int(totalDurationSec)
        self.Save()


    # Right now this is only used by Bambu, because the printer doesn't report the
    # entire print duration or when it started. So we have to calculate it ourselves.
    def GetPrintDurationSec(self) -> int:
        # If we have a final print duration, use it.
        finalPrintDurationSec = self.GetFinalPrintDurationSec()
        if finalPrintDurationSec is not None:
            return int(finalPrintDurationSec)
        # Otherwise, use the time since start.
        return int(time.time() - self.GetLocalPrintStartTimeSec())


    def Save(self) -> bool:
        tempFilePath = None
        try:
            # Replace the saved context only after the new file is complete, so a failed write doesn't lose the print.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(self.FilePath), delete=False) as f:
                tempFilePath = f.name
                json.dump(self.Data, f)
            os.replace(tempFilePath, self.FilePath)
            return True
        except Exception as e:
            self.Logger.error(f"Failed to write print context to file. {e}")
        finally:
            if tempFilePath is not None and os.path.exists(tempFilePath):
                try:
                    os.remove(tempFilePath)
                except Exception as e:
                    self.Logger.warning("Failed to clean up temporary print context: %s", e)
        return False


# The goal of this class is to manage the current print info.
# Ideally, the info should always be in memory, so we don't have to read it from disk.
# But if the host crashes, we can recover the print info from disk.
# This class also cleans up and old print info contexts on disk.
class PrintInfoManager:

    c_ContextsFolder = "PrintInfos"

    _Instance:"PrintInfoManager" = None #pyright: ignore[reportAssignmentType]

    @staticmethod
    def Init(logger:logging.Logger, localStorageFolderPath:str):
        PrintInfoManager._Instance = PrintInfoManager(logger, localStorageFolderPath)


    @staticmethod
    def Get():
        return PrintInfoManager._Instance


    def __init__(self, logger:logging.Logger, localStorageFolderPath:str) -> None:
        self.Logger = logger
        self.ContextFolderPath = os.path.join(localStorageFolderPath, PrintInfoManager.c_ContextsFolder)
        Path(self.ContextFolderPath).mkdir(parents=True, exist_ok=True)
        self.CurrentContext:Optional[PrintInfo] = None


    # Given a print cookie, get a print info.
    # This print cookie should be as unique as possible, so print's dont get mixed up.
    # Lookups must not delete other contexts, since status requests can arrive with a different or stale cookie.
    # Returns None if no context is found for the given cookie.
    def GetPrintInfo(self, printCookie:Optional[str]) -> Optional[PrintInfo]:
        try:
            # If there's no cookie, return None.
            if not isinstance(printCookie, str) or len(printCookie) == 0:
                self.Logger.debug("GetPrintInfo called with no cookie.")
                return None

            # First, see if the current context matches.
            c = self.CurrentContext
            if c is not None and c.GetPrintCookie() == printCookie:
                return c

            # Read only the requested context, and keep the current one if the lookup fails.
            fullPath = os.path.join(self.ContextFolderPath, self._GetPrintCookieFileName(printCookie))
            if not os.path.isfile(fullPath):
                return None
            context = PrintInfo.LoadFromFile(self.Logger, fullPath)
            if context is not None and context.GetPrintCookie() == printCookie:
                self.CurrentContext = context
                return context
        except Exception as e:
            self.Logger.error(f"Exception in PrintContextTracker.GetContext: {e}")
        return None


    # Clears all print infos. Note this should only be used when we absolutely know this is a new print start,
    # like on a new print start or something.
    def ClearAllPrintInfos(self) -> None:
        # Clear memory too, so printing the same filename again creates a new print id.
        self.CurrentContext = None
        try:
            dirAndFiles = os.listdir(self.ContextFolderPath)
            for name in dirAndFiles:
                fullPath = os.path.join(self.ContextFolderPath, name)
                self._DeleteFile(fullPath)
        except Exception as e:
            self.Logger.error(f"Exception in PrintContextTracker.ClearAllPrintInfos: {e}")


    # Creates a new Print Info and returns it.
    # This will always return a new PrintInfo, even if it fails to write to disk.
    def CreateNewPrintInfo(self, printCookie:str, printId:str) -> PrintInfo:
        if not isinstance(printCookie, str) or len(printCookie) == 0:
            raise ValueError("Can't create print info without a print cookie.")
        fullPath = os.path.join(self.ContextFolderPath, self._GetPrintCookieFileName(printCookie))
        self.CurrentContext = PrintInfo.CreateNew(self.Logger, fullPath, printCookie, printId)
        return self.CurrentContext


    def _GetPrintCookieFileName(self, printCookie:str) -> str:
        return f"{printCookie}.json"


    def _DeleteFile(self, filePath:str):
        try:
            os.remove(filePath)
        except Exception as e:
            self.Logger.error(f"Exception in PrintContextTracker._DeleteFile: {e}")
