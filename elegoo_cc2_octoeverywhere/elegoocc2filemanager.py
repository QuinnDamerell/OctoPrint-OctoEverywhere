import logging
import threading
from typing import Any, Dict, List, Optional

from octoeverywhere.sentry import Sentry

from .elegoocc2client import ElegooCc2Client
from .elegoocc2models import FileInfo, PrinterState
from .interfaces import IFileManager


class ElegooCc2FileManager(IFileManager):

    _Instance: "ElegooCc2FileManager" = None #pyright: ignore[reportAssignmentType]

    @staticmethod
    def Init(logger:logging.Logger) -> None:
        ElegooCc2FileManager._Instance = ElegooCc2FileManager(logger)


    @staticmethod
    def Get() -> "ElegooCc2FileManager":
        return ElegooCc2FileManager._Instance


    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger
        self.Files:List[FileInfo] = []
        self.Lock = threading.Lock()
        self.SyncThread:Optional[threading.Thread] = None


    def Sync(self) -> None:
        with self.Lock:
            if self.SyncThread is None:
                self.SyncThread = threading.Thread(target=self._SyncThread, name="ElegooCc2FileManagerSyncThread")
                self.SyncThread.start()


    def GetFileInfoFromState(self, printerState:PrinterState) -> Optional[FileInfo]:
        fileName = printerState.FileName
        if fileName is None or len(fileName) == 0:
            fileName = printerState.MostRecentPrintInfo.FileName
        return self.GetFileInfo(fileName)


    def GetFileInfo(self, fileName:Optional[str]) -> Optional[FileInfo]:
        if fileName is None or len(fileName) == 0:
            return None

        fileNameLower = fileName.lower()
        with self.Lock:
            for f in self.Files:
                if f.FileNameLower == fileNameLower:
                    return f
        return None


    def _SyncThread(self) -> None:
        try:
            self.Logger.debug("Starting Elegoo CC2 file manager sync.")
            currentFileCount = 0
            with self.Lock:
                currentFileCount = len(self.Files)

            self._DoFileSystemSync()
            self._SyncExtraFileInfo()

            with self.Lock:
                self.Logger.debug("Elegoo CC2 file manager sync complete. %s files added.", len(self.Files) - currentFileCount)
        except Exception as e:
            Sentry.OnException("Exception in ElegooCc2FileManager.", e)
        finally:
            with self.Lock:
                self.SyncThread = None
            self.Logger.debug("Elegoo CC2 file manager sync thread complete.")


    def _DoFileSystemSync(self) -> None:
        try:
            result = ElegooCc2Client.Get().SendRequest(1044, {"storage_media": "local", "path": "/", "page": 1, "page_size": 100})
            if result is None or result.HasError():
                self.Logger.error("ElegooCc2FileManager failed to get the file list.")
                return

            r = result.GetResult()
            if r is None:
                self.Logger.error("ElegooCc2FileManager file list cmd is missing the result object.")
                return

            fileList = r.get("files", None)
            if fileList is None:
                self.Logger.debug("ElegooCc2FileManager file list was empty or unavailable.")
                return

            files:List[FileInfo] = []
            for f in fileList:
                if isinstance(f, dict):
                    files.append(FileInfo(self.Logger, f))

            with self.Lock:
                for f in files:
                    found = False
                    for s in self.Files:
                        if s.FileNameWithPath == f.FileNameWithPath:
                            found = True
                            break
                    if found is False:
                        self.Files.append(f)
        except Exception as e:
            Sentry.OnException("ElegooCc2FileManager _DoFileSystemSync", e)


    def _SyncExtraFileInfo(self) -> None:
        fileNamesAndPathsToSync:List[str] = []
        with self.Lock:
            for f in self.Files:
                if f.HasExtraFileInfo() is False:
                    fileNamesAndPathsToSync.append(f.FileNameWithPath)

        try:
            for fileNameAndPath in fileNamesAndPathsToSync:
                result = ElegooCc2Client.Get().SendRequest(1046, {"storage_media": "local", "filename": fileNameAndPath})
                if result is None or result.HasError():
                    self.Logger.error(f"Failed to get Elegoo CC2 file info for {fileNameAndPath}")
                    continue

                fileInfo:Optional[Dict[str, Any]] = result.GetResult()
                if fileInfo is None:
                    self.Logger.error(f"Failed to get Elegoo CC2 file info for {fileNameAndPath}")
                    continue

                with self.Lock:
                    for f in self.Files:
                        if f.FileNameWithPath == fileNameAndPath:
                            f.UpdateExtraFileInfo(fileInfo)
                            break
        except Exception as e:
            Sentry.OnException("ElegooCc2FileManager _SyncExtraFileInfo", e)
