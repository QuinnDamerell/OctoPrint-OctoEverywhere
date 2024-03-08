import os
import json
import time
import logging
from pathlib import Path

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
    c_FinalPrintDurationSec = "FinalPrintDurationSec"

    # Given a file path, this loads a print info if possible.
    # Returns None on failure.
    @staticmethod
    def LoadFromFile(logger:logging.Logger, filePath:str):
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
    def CreateNew(logger:logging.Logger, filePath:str, printCookie:str, printId:str):
        data = {
            PrintInfo.c_PrintCookieKey : printCookie,
            PrintInfo.c_PrintIdKey : printId,
            PrintInfo.c_PrintStartTimeSecKey : time.time()
        }
        pi = PrintInfo(logger, filePath, data)
        # Save, but always return a object even if this fails.
        pi.Save()
        return pi


    def __init__(self, logger:logging.Logger, filePath:str, data:dict) -> None:
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
    def SetLocalPrintStartTimeSec(self, startTimeSec:float) -> float:
        if self.GetLocalPrintStartTimeSec() != startTimeSec:
            self.Data[PrintInfo.c_PrintStartTimeSecKey] = startTimeSec
            self.Save()


    # The file name is optional.
    def GetFileName(self) -> str:
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


    # This is only set when the print is done.
    # Returns None if there isn't one.
    def GetFinalPrintDurationSec(self) -> int:
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
        try:
            with open(self.FilePath, "w", encoding="utf-8") as f:
                json.dump(self.Data, f)
            return True
        except Exception as e:
            self.Logger.error(f"Failed to write print context from file. {e}")
        return False


# The goal of this class is to manage the current print info.
# Ideally, the info should always be in memory, so we don't have to read it from disk.
# But if the host crashes, we can recover the print info from disk.
# This class also cleans up and old print info contexts on disk.
class PrintInfoManager:

    c_ContextsFolder = "PrintInfos"

    _Instance = None

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
        self.CurrentContext:PrintInfo = None


    # Given a print cookie, if a print info.
    # This print cookie should be as unique as possible, so print's dont get mixed up.
    # This cleans up all contexts on disk that dont match the requested cookie.
    # Returns None if no context is found for the given cookie.
    def GetPrintInfo(self, printCookie:str) -> PrintInfo:
        try:
            # If there's no cookie, return None.
            if printCookie is None:
                return None

            # First, see if the current context matches.
            c = self.CurrentContext
            if c is not None and c.GetPrintCookie() == printCookie:
                return c

            # Else, go through the files looking for the correct context.
            dirAndFiles = os.listdir(self.ContextFolderPath)
            printCookieFileName = self._GetPrintCookieFileName(printCookie)
            context = None
            # Iterate all files. Any file that doesn't match or fails to parse we delete.
            for name in dirAndFiles:
                fullPath = os.path.join(self.ContextFolderPath, name)
                if os.path.isfile(fullPath):
                    if name == printCookieFileName:
                        context = PrintInfo.LoadFromFile(self.Logger, fullPath)
                        if context is None:
                            self._DeleteFile(fullPath)
                    else:
                        self._DeleteFile(fullPath)
                else:
                    self._DeleteFile(fullPath)
            # Always replace the current context even if it's empty, so the old context is removed.
            self.CurrentContext = context
            return context
        except Exception as e:
            self.Logger.error(f"Exception in PrintContextTracker.GetContext: {e}")
        return None


    # Clears all print infos. Note this should only be used when we absolutely know this is a new print start,
    # like on a new print start or something.
    def ClearAllPrintInfos(self) -> None:
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
        try:
            fullPath = os.path.join(self.ContextFolderPath, self._GetPrintCookieFileName(printCookie))
            self.CurrentContext = PrintInfo.CreateNew(self.Logger, fullPath, printCookie, printId)
            return self.CurrentContext
        except Exception as e:
            self.Logger.error(f"Exception in PrintContextTracker.CreateNew: {e}")
        return None


    def _GetPrintCookieFileName(self, printCookie:str):
        return f"{printCookie}.json"


    def _DeleteFile(self, filePath:str):
        try:
            os.remove(filePath)
        except Exception as e:
            self.Logger.error(f"Exception in PrintContextTracker._DeleteFile: {e}")
