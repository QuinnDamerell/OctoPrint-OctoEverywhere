import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
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
    c_ErrorMaxChars = 240


    @staticmethod
    def ParsePathArg(args:Optional[Dict[str, Any]]) -> Tuple[Optional[VirtualFilePath], Optional[str]]:
        if args is None:
            return (None, FileSystemCommandHelper.MissingPathError())

        path = FileSystemCommandHelper.GetFirstArg(args, "path", "filepath", "file")
        if path is None or len(str(path)) == 0:
            return (None, FileSystemCommandHelper.MissingPathError())

        normalized = str(path).replace("\\", "/").strip()
        while normalized.startswith("/"):
            normalized = normalized[1:]
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        normalized = normalized.rstrip("/")

        if len(normalized) == 0:
            return (None, FileSystemCommandHelper.MissingPathError())

        parts = normalized.split("/")
        for part in parts:
            if len(part) == 0 or part == "." or part == "..":
                return (None, FileSystemCommandHelper.InvalidPathError())

        root = parts[0].lower()
        if root != FileSystemCommandHelper.c_VirtualGcodeRoot:
            return (None, f"Unsupported path root '{parts[0]}'. Use 'path=gcode/<file>'.")

        relativePath = "/".join(parts[1:])
        if len(relativePath) == 0:
            return (None, "Invalid path. Include a file under 'gcode', e.g. 'path=gcode/example.gcode'.")

        return (VirtualFilePath(FileSystemCommandHelper.c_VirtualGcodeRoot, relativePath), None)


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
    def MissingPathError() -> str:
        return "Missing path. Add query parameter 'path=gcode/<file>'."


    @staticmethod
    def InvalidPathError() -> str:
        return "Invalid path. Use 'gcode/<file>' without '.', '..', empty segments, or a trailing slash."


    @staticmethod
    def MissingPlatformHandlerError(commandName:str) -> str:
        return f"{commandName} unavailable. No platform command handler is registered."


    @staticmethod
    def MissingUploadBodyError() -> str:
        return "files-upload requires raw file bytes in the request body."


    @staticmethod
    def EmptyUploadBodyError() -> str:
        return "files-upload received an empty body. Send raw file bytes."


    @staticmethod
    def UnsupportedPlatformError(platformName:str) -> str:
        return f"File commands are unsupported on {platformName}. Use OctoPrint or Moonraker for files-list, files-upload, files-download, and files-delete."


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
    def BuildFileUploadSuccess(platformName:str, parsedPath:VirtualFilePath, uploadSizeBytes:int, printerStatusCode:int, bodyBytes:bytes, requestFields:Dict[str, str]) -> CommandResponse:
        fileName, parentPath = parsedPath.FileNameAndParent()
        result:Dict[str, Any] = {
            "Platform": platformName,
            "Path": parsedPath.FullPath(),
            "Root": parsedPath.Root,
            "RelativePath": parsedPath.RelativePath,
            "ParentPath": parentPath,
            "FileName": fileName,
            "SizeBytes": uploadSizeBytes,
            "PrinterResponseStatusCode": printerStatusCode,
        }
        if len(requestFields) > 0:
            result["RequestFields"] = dict(requestFields)
        printerResponse = FileSystemCommandHelper._DecodeSuccessBody(bodyBytes)
        if printerResponse is not None:
            result["PrinterResponse"] = printerResponse
        return CommandResponse.Success(result)


    @staticmethod
    def BuildFileDeleteSuccess(platformName:str, parsedPath:VirtualFilePath, printerStatusCode:int, bodyBytes:bytes) -> CommandResponse:
        fileName, parentPath = parsedPath.FileNameAndParent()
        result:Dict[str, Any] = {
            "Platform": platformName,
            "Path": parsedPath.FullPath(),
            "Root": parsedPath.Root,
            "RelativePath": parsedPath.RelativePath,
            "ParentPath": parentPath,
            "FileName": fileName,
            "PrinterResponseStatusCode": printerStatusCode,
        }
        printerResponse = FileSystemCommandHelper._DecodeSuccessBody(bodyBytes)
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
    def _DecodeSuccessBody(bodyBytes:bytes) -> Optional[Any]:
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


class VirtualFileSystemTree:
    def __init__(self) -> None:
        self.Root:Dict[str, Any] = {
            "Type": "folder",
            "Name": "",
            "Path": "",
            "Children": [
                {
                    "Type": "folder",
                    "Name": FileSystemCommandHelper.c_VirtualGcodeRoot,
                    "Path": FileSystemCommandHelper.c_VirtualGcodeRoot,
                    "Children": []
                }
            ]
        }
        self._folderByPath:Dict[str, Dict[str, Any]] = {
            "": self.Root,
            FileSystemCommandHelper.c_VirtualGcodeRoot: self.Root["Children"][0],
        }


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
            "Path": virtualPath,
            "Children": []
        }
        if metadata is not None and len(metadata) > 0:
            folder["Metadata"] = metadata
        parent["Children"].append(folder)
        self._folderByPath[virtualPath] = folder
        return folder


    def AddFile(self, virtualPath:str, metadata:Optional[Dict[str, Any]]=None) -> None:
        virtualPath = self._NormalizeVirtualPath(virtualPath)
        parentPath, name = self._SplitParent(virtualPath)
        parent = self.AddFolder(parentPath)

        item:Dict[str, Any] = {
            "Type": "file",
            "Name": name,
            "Path": virtualPath,
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
    def FromMoonrakerFileList(fileList:List[Dict[str, Any]]) -> Dict[str, Any]:
        tree = VirtualFileSystemTree()
        for item in fileList:
            if not isinstance(item, dict):
                continue
            relativePath = item.get("path", None)
            if relativePath is None or len(str(relativePath)) == 0:
                continue
            metadata:Dict[str, Any] = {
                "SizeBytes": item.get("size", None),
                "ModifiedTimeSec": item.get("modified", None),
                "Permissions": item.get("permissions", None),
                "Metadata": item,
            }
            tree.AddFile(FileSystemCommandHelper.c_VirtualGcodeRoot + "/" + str(relativePath).replace("\\", "/"), metadata)
        return tree.Serialize()


    @staticmethod
    def FromOctoPrintFileList(files:List[Dict[str, Any]]) -> Dict[str, Any]:
        tree = VirtualFileSystemTree()
        for item in files:
            if isinstance(item, dict):
                FileSystemTreeBuilder._AddOctoPrintItem(tree, item)
        return tree.Serialize()


    @staticmethod
    def _AddOctoPrintItem(tree:VirtualFileSystemTree, item:Dict[str, Any]) -> None:
        itemPath = item.get("path", item.get("name", None))
        if itemPath is None or len(str(itemPath)) == 0:
            return

        itemType = str(item.get("type", "")).lower()
        children = item.get("children", None)
        isFolder = itemType == "folder" or isinstance(children, list)
        virtualPath = FileSystemCommandHelper.c_VirtualGcodeRoot + "/" + str(itemPath).replace("\\", "/").strip("/")
        metadata:Dict[str, Any] = {
            "SizeBytes": item.get("size", None),
            "ModifiedTimeSec": item.get("date", None),
            "Hash": item.get("hash", None),
            "Origin": item.get("origin", None),
            "TypePath": item.get("typePath", None),
            "Refs": item.get("refs", None),
            "GcodeAnalysis": item.get("gcodeAnalysis", None),
            "Print": item.get("print", None),
            "Metadata": item,
        }

        if isFolder:
            tree.AddFolder(virtualPath, metadata)
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        FileSystemTreeBuilder._AddOctoPrintItem(tree, child)
            return

        tree.AddFile(virtualPath, metadata)
