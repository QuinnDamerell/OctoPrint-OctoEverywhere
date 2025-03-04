import logging
import threading

from typing import List

from octoeverywhere.sentry import Sentry

from .elegooclient import ElegooClient
from .elegoomodels import PrinterState


# Holds data about a file on the printer.
# Note that some of the fields can be None if we can't get them, and they can become non-None later.
# But once a field is set, it can't be changed.
class FileInfo:

    def __init__(self, logger:logging.Logger, fileDirInfo:dict) -> None:
        self.FileNameWithPath:str = fileDirInfo.get("name", None)
        # Get a version of the file name without the path.
        folderIndex = self.FileNameWithPath.rfind("/")
        if folderIndex != -1:
            self.FileName = self.FileNameWithPath[folderIndex + 1:]
        else:
            logger.warning(f"Failed to remove the folder from {self.FileNameWithPath}")
            self.FileName = self.FileNameWithPath
        self.FileNameLower = self.FileName.lower()

        self.CreateTimeSec:int = fileDirInfo.get("CreateTime", None)
        self.TotalLayers:int = fileDirInfo.get("TotalLayers", None)
        self.FileSizeKb:int = None
        fileSizeBytes = fileDirInfo.get("FileSize", None)
        if fileSizeBytes is not None:
            self.FileSizeKb = int(fileSizeBytes / 1024)

        # These are usually 0
        self.LayerHeight:int = fileDirInfo.get("LayerHeight", None)
        self.EstFilamentLength:int = fileDirInfo.get("EstFilamentLength", None)

        # These come from the extra file info.
        self.EstPrintTimeSec:int = None
        self.EstFilamentWeightMg:int = None


    # Returns true if we have all of the file info.
    def HasExtraFileInfo(self) -> bool:
        # If we have either, we at a response, but we might not get both.
        return self.EstPrintTimeSec is not None or self.EstFilamentWeightMg is not None


    def UpdateExtraFileInfo(self, fileInfo:dict) -> None:
        self.EstPrintTimeSec = fileInfo.get("EstTime", None)
        weightG = fileInfo.get("EstWeight", None)
        if weightG is not None:
            self.EstFilamentWeightMg = int(weightG * 1000)


# The file manager and cache class for the Elegoo printer.
class ElegooFileManager:

    _Instance = None

    @staticmethod
    def Init(logger:logging.Logger):
        ElegooFileManager._Instance = ElegooFileManager(logger)


    @staticmethod
    def Get():
        return ElegooFileManager._Instance


    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger

        self.Files:List[FileInfo] = []
        self.Lock = threading.Lock()
        self.SyncThread:threading.Thread = None


    # Kicks off an async sync of the file manager.
    def Sync(self):
        # If there's no sync thread, start one now.
        with self.Lock:
            if self.SyncThread is None:
                self.SyncThread = threading.Thread(target=self._SyncThread, name="ElegooFileManagerSyncThread")
                self.SyncThread.start()


    # Returns the file info for the current print.
    def GetFileInfoFromState(self, printerState:PrinterState) -> FileInfo:
        fileName = printerState.FileName
        if fileName is None or len(fileName) == 0:
            fileName = printerState.MostRecentPrintInfo.FileName
        return self.GetFileInfo(fileName)


    # Given a file name, get the file info.
    def GetFileInfo(self, fileName:str) -> FileInfo:
        # Ensure there's a name.
        if fileName is None or len(fileName) == 0:
            return None

        # Try to find it using the lower case search.
        fileNameLower = fileName.lower()
        with self.Lock:
            for f in self.Files:
                if f.FileNameLower == fileNameLower:
                    return f
        return None


    def _SyncThread(self):
        try:
            self.Logger.debug("Starting file manager sync.")
            currentFileCount = 0
            with self.Lock:
                currentFileCount = len(self.Files)

            # First, sync the current file list.
            self._DoFileSystemSync()

            # Now, sync the file details for any file that needs it.
            self._SyncExtraFileInfo()

            with self.Lock:
                self.Logger.debug(f"File manager sync complete. {len(self.Files)-currentFileCount} files added.")
        except Exception as e:
            Sentry.Exception("Exception in ElegooFileManager.", e)
        finally:
            # When the sync thread is done, set it to None.
            with self.Lock:
                self.SyncThread = None
            self.Logger.debug("File manager sync thread complete.")


    def _DoFileSystemSync(self):
        try:
            # This command gets the current file list.
            result = ElegooClient.Get().SendRequest(258, {"Url": "/local"})
            if result is None or result.HasError():
                self.Logger.error("ElegooFileManager failed to get the file list.")
                return

            # Get the data object from the result.
            r = result.GetResult()
            data = r.get("Data", None)
            if data is None:
                self.Logger.error("ElegooFileManager file list cmd is missing the first data object.")
                return
            data = data.get("Data", None)
            if data is None:
                self.Logger.error("ElegooFileManager file list cmd is missing the second data object.")
                return
            fileList = data.get("FileList", None)
            if fileList is None:
                self.Logger.error("ElegooFileManager file list cmd is missing the FileList.")
                return

            # Build a local file list.
            files:List[FileInfo] = []
            for f in fileList:
                fileInfo = FileInfo(self.Logger, f)
                files.append(fileInfo)

            # Merge this list with anything we currently have.
            with self.Lock:
                # For each new file...
                for f in files:
                    found = False
                    for s in self.Files:
                        # Match the names
                        if s.FileNameWithPath == f.FileNameWithPath:
                            # The file seems to exist, but we want to make sure it wasn't re-uploaded or something.
                            if s.FileNameWithPath != f.FileNameWithPath :
                                # Delete the current file and replace it with the new one.
                                self.Files.remove(s)
                                self.Files.append(f)
                            # Set the found flag since we are good.
                            found = True
                            break
                    # If we didn't find the file, add it.
                    if found is False:
                        self.Files.append(f)
        except Exception as e:
            Sentry.Exception("_DoFileSystemSync", e)


    def _SyncExtraFileInfo(self):
        # Under lock, get any files we need to get info for.
        fileNamsAndPathsToSync = []
        with self.Lock:
            for f in self.Files:
                if f.HasExtraFileInfo() is False:
                    fileNamsAndPathsToSync.append(f.FileNameWithPath)

        try:
            for fileNameAndPath in fileNamsAndPathsToSync:
                # This command gets the file info just for this file.
                result = ElegooClient.Get().SendRequest(260, {"Url": fileNameAndPath})
                if result is None or result.HasError():
                    self.Logger.error(f"Failed to get file info for {fileNameAndPath}")
                    continue

                # Get the data object from the result.
                r = result.GetResult()
                data = r.get("Data", None)
                if data is None:
                    self.Logger.error(f"Failed to get file info for {fileNameAndPath}")
                    continue
                data = data.get("Data", None)
                if data is None:
                    self.Logger.error(f"Failed to get file info for {fileNameAndPath}")
                    continue
                fileInfo = data.get("FileInfo", None)
                if fileInfo is None:
                    self.Logger.error(f"Failed to get file info for {fileNameAndPath}")
                    continue

                # Update the file info.
                with self.Lock:
                    for f in self.Files:
                        if f.FileNameWithPath == fileNameAndPath:
                            f.UpdateExtraFileInfo(fileInfo)
                            break
        except Exception as e:
            Sentry.Exception("Error in _SyncFileInfo", e)
