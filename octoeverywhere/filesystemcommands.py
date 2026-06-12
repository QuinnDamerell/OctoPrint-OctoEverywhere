import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import quote

from .buffer import Buffer
from .httpresult import HttpResult
from .interfaces import CommandResponse


@dataclass
class VirtualFilePath:
    Root:str
    RelativePath:str

    def FullPath(self) -> str:
        if len(self.RelativePath) == 0:
            return self.Root
        return self.Root + "/" + self.RelativePath

    def FileNameAndParent(self) -> Tuple[str, str]:
        slash = self.RelativePath.rfind("/")
        if slash == -1:
            return (self.RelativePath, "")
        return (self.RelativePath[slash + 1:], self.RelativePath[:slash])


class FileSystemCommandHelper:
    c_VirtualGcodeRoot = "gcode"
    c_VirtualConfigRoot = "config"
    c_VirtualLogsRoot = "logs"
    c_DefaultAllowedRoots:Set[str] = {c_VirtualGcodeRoot}
    # Backend root synonyms a caller might send, mapped to our virtual root names.
    c_RootAliases:Dict[str, str] = {"gcodes": c_VirtualGcodeRoot}
    c_ErrorMaxChars = 240
    c_FileReadChunkSizeBytes = 512 * 1024


    @staticmethod
    def ParsePathArg(args:Optional[Dict[str, Any]], allowedRoots:Optional[Set[str]]=None) -> Tuple[Optional[VirtualFilePath], Optional[str]]:
        if allowedRoots is None:
            allowedRoots = FileSystemCommandHelper.c_DefaultAllowedRoots

        if args is None:
            return (None, FileSystemCommandHelper.MissingPathError(allowedRoots))

        path = FileSystemCommandHelper.GetFirstArg(args, "path", "filepath", "file")
        if path is None or len(str(path)) == 0:
            return (None, FileSystemCommandHelper.MissingPathError(allowedRoots))

        normalized = str(path).replace("\\", "/").strip()
        while normalized.startswith("/"):
            normalized = normalized[1:]
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        normalized = normalized.rstrip("/")

        if len(normalized) == 0:
            return (None, FileSystemCommandHelper.MissingPathError(allowedRoots))

        parts = normalized.split("/")
        for part in parts:
            if len(part) == 0 or part == "." or part == "..":
                return (None, FileSystemCommandHelper.InvalidPathError(allowedRoots))

        root = parts[0].lower()
        if root not in allowedRoots:
            return (None, f"Unsupported path root '{parts[0]}'. Use {FileSystemCommandHelper._FormatRootUsage(allowedRoots)}.")

        relativePath = "/".join(parts[1:])
        if len(relativePath) == 0:
            return (None, f"Invalid path. Include a file under {FileSystemCommandHelper._FormatRootUsage(allowedRoots)}, e.g. 'path={FileSystemCommandHelper._ExampleForRoot(allowedRoots)}'.")

        return (VirtualFilePath(root, relativePath), None)


    @staticmethod
    def ResolveRequestedRoots(args:Optional[Dict[str, Any]], allowedRoots:List[str], aliases:Optional[Dict[str, str]]=None) -> Tuple[List[str], Optional[str]]:
        # Figure out which virtual root the caller asked for. We accept an explicit "root" arg, or fall back to
        # inferring it from the first segment of a "path" arg. When nothing is provided, we list every allowed root.
        rootArg:Optional[Any] = None
        if args is not None:
            rootArg = FileSystemCommandHelper.GetFirstArg(args, "root", "Root")
            if rootArg is None:
                pathArg = FileSystemCommandHelper.GetFirstArg(args, "path", "filepath", "file")
                if pathArg is not None:
                    rootArg = pathArg
        if rootArg is None or len(str(rootArg).strip()) == 0:
            # Return a copy so callers can't mutate the shared root list.
            return (list(allowedRoots), None)

        requestedRoot = str(rootArg).replace("\\", "/").strip().strip("/").lower()
        if "/" in requestedRoot:
            requestedRoot = requestedRoot.split("/", 1)[0]
        if aliases is not None:
            requestedRoot = aliases.get(requestedRoot, requestedRoot)

        if requestedRoot not in allowedRoots:
            return ([], f"Unsupported file root '{rootArg}'. Use {FileSystemCommandHelper._FormatRootNames(allowedRoots)}.")

        return ([requestedRoot], None)


    @staticmethod
    def BuildMultiRootFileListResponse(virtualRoots:List[str],
                                       listRoot:Callable[[str], Union[List[Dict[str, Any]], CommandResponse]],
                                       addToTree:Callable[["VirtualFileSystemTree", str, List[Dict[str, Any]]], None],
                                       logger:Any,
                                       platformName:str) -> CommandResponse:
        # Local import to avoid a circular import - commandhandler imports this module.
        #pylint: disable=import-outside-toplevel
        from .commandhandler import CommandHandler
        # Lists each requested root and merges the results into a single virtual tree.
        tree = VirtualFileSystemTree([])
        listedRootCount = 0
        for virtualRoot in virtualRoots:
            listResult = listRoot(virtualRoot)
            if isinstance(listResult, CommandResponse):
                # Preserve the old behavior for gcode failures, and return exact errors for explicitly requested roots.
                # Other roots are optional - if one fails, log it and keep going so a single bad root doesn't sink the list.
                if len(virtualRoots) == 1 or virtualRoot == FileSystemCommandHelper.c_VirtualGcodeRoot:
                    return listResult
                logger.warning("%s file-list failed for optional root %s. %s", platformName, virtualRoot, str(listResult.ErrorStr))
                continue
            addToTree(tree, virtualRoot, listResult)
            listedRootCount += 1

        if listedRootCount == 0:
            return CommandResponse.Error(CommandHandler.c_CommandError_ExecutionFailure, f"files/list failed on {platformName}: no file roots could be listed.")

        return CommandResponse.Success(tree.Serialize())


    @staticmethod
    def GetFirstArg(args:Dict[str, Any], *keys:str) -> Optional[Any]:
        for key in keys:
            if key in args:
                return args[key]
            lowerKey = key.lower()
            for existingKey, value in args.items():
                if str(existingKey).lower() == lowerKey:
                    return value
        return None


    @staticmethod
    def GetBoolArg(args:Optional[Dict[str, Any]], key:str, defaultValue:bool=False) -> bool:
        if args is None:
            return defaultValue
        value = FileSystemCommandHelper.GetFirstArg(args, key)
        if value is None:
            return defaultValue
        if isinstance(value, bool):
            return value
        valueStr = str(value).lower().strip()
        return valueStr == "true" or valueStr == "1" or valueStr == "yes"


    @staticmethod
    def ParseTailLineCountArg(args:Optional[Dict[str, Any]]) -> Tuple[Optional[int], Optional[str]]:
        if args is None:
            return (None, None)
        value = FileSystemCommandHelper.GetFirstArg(args, "tailLines", "taillines", "tail_lines", "tail-lines", "lines")
        if value is None:
            return (None, None)
        if isinstance(value, bool):
            return (None, "Invalid tailLines value. Use a non-negative integer.")
        valueStr = str(value).strip()
        if len(valueStr) == 0:
            return (None, "Invalid tailLines value. Use a non-negative integer.")
        try:
            lineCount = int(valueStr)
        except Exception:
            return (None, "Invalid tailLines value. Use a non-negative integer.")
        if lineCount < 0:
            return (None, "Invalid tailLines value. Use a non-negative integer.")
        return (lineCount, None)


    @staticmethod
    def EncodeRelativePathForUrl(relativePath:str) -> str:
        return quote(relativePath, safe="/")


    @staticmethod
    def BuildJsonHttpResult(statusCode:int, obj:Dict[str, Any], url:str) -> HttpResult:
        return HttpResult(
            statusCode,
            {
                "Content-Type": "text/json",
            },
            url,
            False,
            fullBodyBuffer=Buffer(json.dumps(obj, default=str).encode("utf-8"))
        )


    @staticmethod
    def BuildRawError(statusCode:int, errorStr:str, url:str) -> HttpResult:
        return FileSystemCommandHelper.BuildJsonHttpResult(FileSystemCommandHelper.ToHttpStatus(statusCode), {
            "Status": statusCode,
            "Error": FileSystemCommandHelper.CleanErrorForApi(errorStr),
        }, url)


    @staticmethod
    def BuildRawCommandResponse(response:CommandResponse, url:str) -> HttpResult:
        obj:Dict[str, Any] = {
            "Status": response.StatusCode
        }
        if response.ErrorStr is not None:
            obj["Error"] = FileSystemCommandHelper.CleanErrorForApi(response.ErrorStr)
        if response.ResultDict is not None:
            obj["Result"] = response.ResultDict
        return FileSystemCommandHelper.BuildJsonHttpResult(FileSystemCommandHelper.ToHttpStatus(response.StatusCode), obj, url)


    @staticmethod
    def ToHttpStatus(statusCode:int) -> int:
        if statusCode >= 100 and statusCode <= 599:
            return statusCode
        return 400


    @staticmethod
    def BuildFileResult(logger:Any, filePath:Optional[str], url:str, downloadFileName:Optional[str]=None, tailLineCount:Optional[int]=None) -> HttpResult:
        if filePath is None or len(str(filePath)) == 0:
            return FileSystemCommandHelper.BuildRawError(404, "Plugin log file was not found.", url)
        if os.path.isfile(filePath) is False:
            return FileSystemCommandHelper.BuildRawError(404, "Plugin log file was not found.", url)

        try:
            fileObj:BinaryIO = open(filePath, "rb") #pylint: disable=consider-using-with
            fileSizeBytes = os.fstat(fileObj.fileno()).st_size
            startOffset = FileSystemCommandHelper._GetTailStartOffset(fileObj, fileSizeBytes, tailLineCount)
            fileObj.seek(startOffset)
        except Exception as e:
            try:
                fileObj.close() #pyright: ignore[reportPossiblyUnboundVariable]
            except Exception:
                pass
            return FileSystemCommandHelper.BuildRawError(500, "Failed to open plugin log file: " + str(e), url)

        bytesRemaining = fileSizeBytes - startOffset

        def readLogChunk() -> Optional[Buffer]:
            nonlocal bytesRemaining
            if bytesRemaining <= 0:
                return None
            data = fileObj.read(min(FileSystemCommandHelper.c_FileReadChunkSizeBytes, bytesRemaining))
            if len(data) == 0:
                return None
            bytesRemaining -= len(data)
            return Buffer(data)

        def closeLogFile() -> None:
            try:
                fileObj.close()
            except Exception as e:
                try:
                    logger.warning("Failed to close plugin log file stream. %s", str(e))
                except Exception:
                    pass

        if downloadFileName is None or len(downloadFileName) == 0:
            downloadFileName = os.path.basename(filePath)
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Length": str(bytesRemaining),
            "Content-Disposition": "attachment; filename=\"" + FileSystemCommandHelper._HeaderQuote(downloadFileName) + "\"",
        }
        return HttpResult(200, headers, url, False, customBodyStreamCallback=readLogChunk, customBodyStreamClosedCallback=closeLogFile)


    @staticmethod
    def FindLogFilePathFromLogger(logger:Any, preferredFileName:Optional[str]=None) -> Optional[str]:
        candidates:List[str] = []
        for handler in FileSystemCommandHelper._GetLoggerHandlers(logger):
            filePath = getattr(handler, "baseFilename", None)
            if filePath is None:
                continue
            filePathStr = str(filePath)
            if os.path.isfile(filePathStr) is False:
                continue
            if preferredFileName is not None and os.path.basename(filePathStr).lower() == preferredFileName.lower():
                return filePathStr
            candidates.append(filePathStr)
        if len(candidates) > 0:
            return candidates[0]
        return None


    @staticmethod
    def BuildLogFileResultFromLogger(logger:Any, preferredFileName:Optional[str], url:str, downloadFileName:Optional[str]=None, args:Optional[Dict[str, Any]]=None) -> HttpResult:
        tailLineCount, tailLineError = FileSystemCommandHelper.ParseTailLineCountArg(args)
        if tailLineError is not None:
            return FileSystemCommandHelper.BuildRawError(400, tailLineError, url)
        filePath = FileSystemCommandHelper.FindLogFilePathFromLogger(logger, preferredFileName)
        return FileSystemCommandHelper.BuildFileResult(logger, filePath, url, downloadFileName, tailLineCount)


    @staticmethod
    def _GetLoggerHandlers(logger:Any) -> List[Any]:
        handlers:List[Any] = []
        seenHandlerIds:Set[int] = set()
        seenLoggerIds:Set[int] = set()

        def addLoggerHandlers(loggerObj:Any) -> None:
            cur = loggerObj
            while cur is not None:
                curId = id(cur)
                if curId in seenLoggerIds:
                    break
                seenLoggerIds.add(curId)
                for handler in getattr(cur, "handlers", []):
                    handlerId = id(handler)
                    if handlerId in seenHandlerIds:
                        continue
                    seenHandlerIds.add(handlerId)
                    handlers.append(handler)
                if getattr(cur, "propagate", True) is False:
                    break
                cur = getattr(cur, "parent", None)

        if logger is not None:
            addLoggerHandlers(logger)

        rootLogger = logging.getLogger()
        addLoggerHandlers(rootLogger)

        for loggerObj in logging.Logger.manager.loggerDict.values():
            if isinstance(loggerObj, logging.Logger):
                addLoggerHandlers(loggerObj)
        return handlers


    @staticmethod
    def _GetTailStartOffset(fileObj:BinaryIO, fileSizeBytes:int, tailLineCount:Optional[int]) -> int:
        if tailLineCount is None:
            return 0
        if tailLineCount <= 0:
            return fileSizeBytes
        if fileSizeBytes <= 0:
            return 0

        scanEndOffset = fileSizeBytes
        fileObj.seek(fileSizeBytes - 1)
        if fileObj.read(1) == b"\n":
            scanEndOffset -= 1
        if scanEndOffset <= 0:
            return 0

        linesFound = 0
        position = scanEndOffset
        while position > 0:
            readSize = min(FileSystemCommandHelper.c_FileReadChunkSizeBytes, position)
            position -= readSize
            fileObj.seek(position)
            chunk = fileObj.read(readSize)
            searchEnd = len(chunk)
            while searchEnd > 0:
                newlineIndex = chunk.rfind(b"\n", 0, searchEnd)
                if newlineIndex == -1:
                    break
                linesFound += 1
                if linesFound >= tailLineCount:
                    return position + newlineIndex + 1
                searchEnd = newlineIndex
        return 0


    @staticmethod
    def _HeaderQuote(value:str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


    @staticmethod
    def MissingPathError(allowedRoots:Optional[Set[str]]=None) -> str:
        if allowedRoots is None:
            allowedRoots = FileSystemCommandHelper.c_DefaultAllowedRoots
        return "Missing Path. Provide a file path like " + FileSystemCommandHelper._FormatRootUsage(allowedRoots) + "."


    @staticmethod
    def InvalidPathError(allowedRoots:Optional[Set[str]]=None) -> str:
        if allowedRoots is None:
            allowedRoots = FileSystemCommandHelper.c_DefaultAllowedRoots
        return "Invalid Path. Use " + FileSystemCommandHelper._FormatRootUsage(allowedRoots) + " without '.', '..', empty segments, or a trailing slash."


    @staticmethod
    def _FormatRootUsage(allowedRoots:Set[str]) -> str:
        examples = [f"'{root}/<file>'" for root in sorted(allowedRoots)]
        return FileSystemCommandHelper._JoinWithOr(examples) or "'<root>/<file>'"


    @staticmethod
    def _FormatRootNames(roots:List[str]) -> str:
        names = [f"'{root}'" for root in roots]
        return FileSystemCommandHelper._JoinWithOr(names) or "'<root>'"


    @staticmethod
    def _JoinWithOr(items:List[str]) -> str:
        if len(items) == 0:
            return ""
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            return items[0] + " or " + items[1]
        return ", ".join(items[:-1]) + ", or " + items[-1]


    @staticmethod
    def _FirstRootForExample(allowedRoots:Set[str]) -> str:
        if FileSystemCommandHelper.c_VirtualGcodeRoot in allowedRoots:
            return FileSystemCommandHelper.c_VirtualGcodeRoot
        roots = sorted(allowedRoots)
        if len(roots) > 0:
            return roots[0]
        return "root"


    @staticmethod
    def _ExampleForRoot(allowedRoots:Set[str]) -> str:
        # Pick a representative example file for the root so the hint matches the root type.
        root = FileSystemCommandHelper._FirstRootForExample(allowedRoots)
        exampleFiles = {
            FileSystemCommandHelper.c_VirtualGcodeRoot: "example.gcode",
            FileSystemCommandHelper.c_VirtualConfigRoot: "printer.cfg",
            FileSystemCommandHelper.c_VirtualLogsRoot: "klippy.log",
        }
        return root + "/" + exampleFiles.get(root, "example.txt")


    @staticmethod
    def MissingPlatformHandlerError(commandName:str) -> str:
        return f"{commandName} unavailable. No platform command handler is registered."


    @staticmethod
    def MissingUploadBodyError() -> str:
        return "files/upload requires raw file bytes in the request body."


    @staticmethod
    def EmptyUploadBodyError() -> str:
        return "files/upload received an empty body. Send raw file bytes."


    @staticmethod
    def UnsupportedPlatformError(platformName:str, commandName:Optional[str]=None) -> str:
        if commandName is not None:
            return f"{commandName} is unsupported on {platformName} because this platform does not expose a compatible file start surface."
        return f"File commands are unsupported on {platformName}. Use a platform with file system support for files/list, files/upload, files/download, and files/delete."


    @staticmethod
    def PrinterNotConnectedError(platformName:str, commandName:str) -> str:
        return f"{commandName} failed on {platformName}: printer host is not connected."


    @staticmethod
    def AuthFailedError(platformName:str, commandName:str) -> str:
        return f"{commandName} failed on {platformName}: authentication failed; refresh printer API credentials."


    @staticmethod
    def InvalidJsonResponseError(platformName:str, commandName:str) -> str:
        return f"{commandName} failed on {platformName}: printer returned invalid JSON."


    @staticmethod
    def BackendHttpError(platformName:str, commandName:str, statusCode:int, bodyBytes:bytes) -> str:
        detail = FileSystemCommandHelper._DecodeErrorBody(bodyBytes)
        error = f"{commandName} failed on {platformName}: HTTP {statusCode}."
        if len(detail) > 0:
            error += " " + detail
        return FileSystemCommandHelper.CleanErrorForApi(error)


    @staticmethod
    def ExceptionError(commandName:str, e:Exception) -> str:
        return FileSystemCommandHelper.CleanErrorForApi(f"{commandName} failed: {str(e)}")


    @staticmethod
    def BuildFileUploadSuccess(parsedPath:VirtualFilePath, platformPath:str, uploadSizeBytes:int, bodyBytes:bytes) -> CommandResponse:
        result:Dict[str, Any] = {
            "VirtualPath": parsedPath.FullPath(),
            "PlatformPath": platformPath,
            "SizeBytes": uploadSizeBytes,
        }
        printerResponse = FileSystemCommandHelper.DecodeSuccessBody(bodyBytes)
        if printerResponse is not None:
            result["PrinterResponse"] = printerResponse
        return CommandResponse.Success(result)


    @staticmethod
    def BuildFileDeleteSuccess(parsedPath:VirtualFilePath, platformPath:str, bodyBytes:bytes) -> CommandResponse:
        result:Dict[str, Any] = {
            "VirtualPath": parsedPath.FullPath(),
            "PlatformPath": platformPath,
        }
        printerResponse = FileSystemCommandHelper.DecodeSuccessBody(bodyBytes)
        if printerResponse is not None:
            result["PrinterResponse"] = printerResponse
        return CommandResponse.Success(result)


    @staticmethod
    def BuildFileStartSuccess(parsedPath:VirtualFilePath, platformPath:str, printerResponse:Optional[Any]=None) -> CommandResponse:
        result:Dict[str, Any] = {
            "VirtualPath": parsedPath.FullPath(),
            "PlatformPath": platformPath,
        }
        if printerResponse is not None:
            result["PrinterResponse"] = printerResponse
        return CommandResponse.Success(result)


    @staticmethod
    def CleanErrorForApi(errorStr:str) -> str:
        error = " ".join(str(errorStr).split())
        if len(error) == 0:
            error = "Unknown error."
        if len(error) > FileSystemCommandHelper.c_ErrorMaxChars:
            error = error[:FileSystemCommandHelper.c_ErrorMaxChars - 3].rstrip() + "..."
        return error


    @staticmethod
    def _DecodeErrorBody(bodyBytes:bytes) -> str:
        if bodyBytes is None or len(bodyBytes) == 0:
            return ""
        bodyText = bodyBytes.decode("utf-8", errors="replace").strip()
        if len(bodyText) == 0:
            return ""
        try:
            parsed = json.loads(bodyText)
            detail = FileSystemCommandHelper._ExtractErrorDetail(parsed)
            if len(detail) > 0:
                return detail
            return json.dumps(parsed, default=str)
        except Exception:
            return bodyText


    @staticmethod
    def DecodeSuccessBody(bodyBytes:bytes) -> Optional[Any]:
        if bodyBytes is None or len(bodyBytes) == 0:
            return None
        bodyText = bodyBytes.decode("utf-8", errors="replace").strip()
        if len(bodyText) == 0:
            return None
        try:
            return json.loads(bodyText)
        except Exception:
            return FileSystemCommandHelper.CleanErrorForApi(bodyText)


    @staticmethod
    def _ExtractErrorDetail(value:Any) -> str:
        if isinstance(value, dict):
            for key in ("error", "Error", "message", "Message", "detail", "Detail"):
                if key not in value:
                    continue
                detail = value[key]
                if isinstance(detail, (dict, list)):
                    nestedDetail = FileSystemCommandHelper._ExtractErrorDetail(detail)
                    if len(nestedDetail) > 0:
                        return nestedDetail
                elif detail is not None:
                    return str(detail)
        elif isinstance(value, list):
            for item in value:
                detail = FileSystemCommandHelper._ExtractErrorDetail(item)
                if len(detail) > 0:
                    return detail
        return ""


    @staticmethod
    def BuildHttpResponseBody(response:Dict[str, Any], bodyBytes:bytes) -> None:
        # Always define the defaults.
        response["BodySizeBytes"] = 0
        response["BodyAsText"] = None
        response["BodyAsJson"] = None
        response["BodyAsBase64"] = None

        # Ensure we have anything.
        if bodyBytes is None or len(bodyBytes) == 0:
            return

        # Always set the length.
        response["BodySizeBytes"] = len(bodyBytes)

        # We only want to send the body once to cut down on the size.
        # We will try to send json -> text -> bytes, in that order.
        # We must always start by decoding the bytes to text.
        bodyText:Optional[str] = None
        try:
            bodyText = bodyBytes.decode("utf-8")
        except Exception:
            pass
        if bodyText is not None:
            try:
                # If we have text, try to decode json first.
                response["BodyAsJson"] = json.loads(bodyText)
                return
            except Exception:
                pass
            # If the json decoding fails, send the text.
            response["BodyAsText"] = bodyText
            return
        # Finally, if we can't decode text, send the bytes as base64.
        response["BodyAsBase64"] = base64.b64encode(bodyBytes).decode("ascii")


class VirtualFileSystemTree:
    def __init__(self, rootNames:Optional[List[str]]=None) -> None:
        if rootNames is None:
            rootNames = [FileSystemCommandHelper.c_VirtualGcodeRoot]
        self.Root:Dict[str, Any] = {
            "Type": "folder",
            "Name": "",
            "VirtualPath": "",
            "Children": []
        }
        self._folderByPath:Dict[str, Dict[str, Any]] = {
            "": self.Root,
        }
        for rootName in rootNames:
            self.AddFolder(rootName)


    def AddFolder(self, virtualPath:str, metadata:Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
        virtualPath = self._NormalizeVirtualPath(virtualPath)
        if virtualPath in self._folderByPath:
            folder = self._folderByPath[virtualPath]
            if metadata is not None and len(metadata) > 0:
                folder["Metadata"] = metadata
            return folder

        parentPath, name = self._SplitParent(virtualPath)
        parent = self.AddFolder(parentPath)
        folder = {
            "Type": "folder",
            "Name": name,
            "VirtualPath": virtualPath,
            "Children": []
        }
        if metadata is not None and len(metadata) > 0:
            folder["Metadata"] = metadata
        parent["Children"].append(folder)
        self._folderByPath[virtualPath] = folder
        return folder


    def AddFile(self,
                virtualPath:str,
                platformPath:str,
                sizeBytes:Optional[int]=None,
                modifiedTimeSec:Optional[float]=None,
                permissions:Optional[str]=None,
                metadata:Optional[Dict[str, Any]]=None) -> None:
        virtualPath = self._NormalizeVirtualPath(virtualPath)
        parentPath, name = self._SplitParent(virtualPath)
        parent = self.AddFolder(parentPath)

        item:Dict[str, Any] = {
            "Type": "file",
            "Name": name,
            "VirtualPath": virtualPath,
            "PlatformPath": platformPath,
            "SizeBytes": sizeBytes,
            "ModifiedTimeSec": int(modifiedTimeSec) if modifiedTimeSec is not None else None,
            "Permissions": permissions,
        }
        if metadata is not None:
            for key, value in metadata.items():
                if value is not None:
                    item[key] = value
        parent["Children"].append(item)


    def Serialize(self) -> Dict[str, Any]:
        self._SortFolder(self.Root)
        rootChildren = self.Root.get("Children", [])
        return {
            "Root": rootChildren
        }


    def _SortFolder(self, folder:Dict[str, Any]) -> None:
        childrenRaw = folder.get("Children", [])
        if not isinstance(childrenRaw, list):
            return
        children:List[Dict[str, Any]] = []
        for child in childrenRaw:
            if isinstance(child, dict):
                children.append(child)
        folder["Children"] = children
        for child in children:
            if child.get("Type", None) == "folder":
                self._SortFolder(child)
        children.sort(key=lambda i: (0 if i.get("Type", "") == "folder" else 1, str(i.get("Name", "")).lower()))


    def _NormalizeVirtualPath(self, path:str) -> str:
        return path.replace("\\", "/").strip("/")


    def _SplitParent(self, path:str) -> Tuple[str, str]:
        slash = path.rfind("/")
        if slash == -1:
            return ("", path)
        return (path[:slash], path[slash + 1:])


class FileSystemTreeBuilder:
    @staticmethod
    def FromMoonrakerFileList(fileList:List[Dict[str, Any]], virtualRoot:str=FileSystemCommandHelper.c_VirtualGcodeRoot) -> Dict[str, Any]:
        tree = VirtualFileSystemTree([virtualRoot])
        FileSystemTreeBuilder.AddMoonrakerFileListToTree(tree, fileList, virtualRoot)
        return tree.Serialize()


    @staticmethod
    def AddMoonrakerFileListToTree(tree:VirtualFileSystemTree, fileList:List[Dict[str, Any]], virtualRoot:str) -> None:
        tree.AddFolder(virtualRoot)
        for item in fileList:
            if not isinstance(item, dict):
                continue
            # Get the required basic info
            relativePath = item.get("path", None)
            if relativePath is None or len(str(relativePath)) == 0:
                continue
            relativePath = str(relativePath).replace("\\", "/")
            sizeBytes = item.get("size", None)
            modifiedTimeSec = item.get("modified", None)
            permissions = item.get("permissions", None)

            # Send the rest of the item as metadata, but remove anything we already set.
            metadata = {}
            for key, value in item.items():
                if key not in ("path", "size", "modified", "permissions"):
                    metadata[key] = value

            tree.AddFile(virtualRoot + "/" + relativePath, relativePath, sizeBytes, modifiedTimeSec, permissions, metadata)


    @staticmethod
    def FromOctoPrintFileList(files:List[Dict[str, Any]]) -> Dict[str, Any]:
        tree = VirtualFileSystemTree()
        FileSystemTreeBuilder.AddOctoPrintFileListToTree(tree, files)
        return tree.Serialize()


    @staticmethod
    def AddOctoPrintFileListToTree(tree:VirtualFileSystemTree, files:List[Dict[str, Any]]) -> None:
        tree.AddFolder(FileSystemCommandHelper.c_VirtualGcodeRoot)
        for item in files:
            if isinstance(item, dict):
                FileSystemTreeBuilder._AddOctoPrintItem(tree, item)


    @staticmethod
    def AddOctoPrintLogListToTree(tree:VirtualFileSystemTree, logs:List[Dict[str, Any]]) -> None:
        tree.AddFolder(FileSystemCommandHelper.c_VirtualLogsRoot)
        for item in logs:
            if not isinstance(item, dict):
                continue
            name = item.get("name", None)
            if name is None or len(str(name)) == 0:
                continue
            platformPath = str(name).replace("\\", "/").strip("/")
            if len(platformPath) == 0:
                continue

            sizeBytes = item.get("size", None)
            modifiedTimeSec = item.get("date", None)

            metadata = {}
            refs = item.get("refs", None)
            if refs is not None:
                metadata["Refs"] = refs

            tree.AddFile(FileSystemCommandHelper.c_VirtualLogsRoot + "/" + platformPath, platformPath, sizeBytes, modifiedTimeSec, None, metadata)


    @staticmethod
    def _AddOctoPrintItem(tree:VirtualFileSystemTree, item:Dict[str, Any]) -> None:
        itemPath = item.get("path", item.get("name", None))
        if itemPath is None or len(str(itemPath)) == 0:
            return

        itemType = str(item.get("type", "")).lower()
        children = item.get("children", None)
        isFolder = itemType == "folder" or isinstance(children, list)
        platformPath = str(itemPath).replace("\\", "/").strip("/")
        virtualPath = FileSystemCommandHelper.c_VirtualGcodeRoot + "/" + platformPath

        # Handle folders
        if isFolder:
            tree.AddFolder(virtualPath)
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        FileSystemTreeBuilder._AddOctoPrintItem(tree, child)
            return

        # Handle files.
        # Get the required basic info
        sizeBytes = item.get("size", None)
        modifiedTimeSec = item.get("date", None)
        permissions = item.get("permissions", None)

        # Only send a subset of the extra data as metadata, because there can be a ton.
        metadata = {}
        metadataKeys = {
            "hash": "Hash",
            "origin": "Origin",
            "display": "Display",
        }
        for key in item.keys():
            keyLower = str(key).lower()
            if keyLower in metadataKeys:
                metadata[metadataKeys[keyLower]] = item[key]

        # Add the file.
        tree.AddFile(virtualPath, platformPath, sizeBytes, modifiedTimeSec, permissions, metadata)
