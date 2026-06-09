import calendar
import datetime
import ftplib
import logging
import os
import ssl
import tempfile
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Dict, Iterable, List, Optional, Tuple, Union

from octoeverywhere.WebStream.uploadbody import UploadBody


@dataclass
class BambuFtpFileInfo:
    Path:str
    SizeBytes:Optional[int] = None
    ModifiedTimeSec:Optional[float] = None


class BambuFileManager:
    c_DefaultFtpPort = 990
    c_User = "bblp"
    c_UploadBlockSizeBytes = 32 * 1024
    c_DownloadBlockSizeBytes = 512 * 1024
    c_MaxListDepth = 8
    c_MaxListEntries = 5000
    c_PrintableExtensions = (".gcode", ".3mf", ".bgcode")
    c_SkipRecursiveDirs = {
        "image",
        "ipcam",
        "logger",
        "log",
        "logs",
        "timelapse",
        "timelapses",
        "video",
        "videos",
    }


    def __init__(self, logger:logging.Logger, host:Optional[str], accessCode:Optional[str], port:int=c_DefaultFtpPort, ftpFactory:Optional[Callable[[], Any]]=None) -> None:
        self.Logger = logger
        self.Host = host
        self.AccessCode = accessCode
        self.Port = port
        self.FtpFactory = ftpFactory


    def ListPrintableFiles(self) -> List[BambuFtpFileInfo]:
        def _list(ftp:ftplib.FTP_TLS) -> List[BambuFtpFileInfo]:
            files:List[BambuFtpFileInfo] = []
            self._ListRecursive(ftp, "", 0, files)
            return files
        return self._RunWithConnection(_list)


    def UploadFile(self, printerPath:str, uploadBody:UploadBody) -> str:
        printerPath = self._NormalizePrinterPath(printerPath)
        expectedSize = uploadBody.UploadBytesReceivedSoFar

        def _upload(ftp:ftplib.FTP_TLS) -> str:
            self._EnsureParentDir(ftp, printerPath)
            try:
                with uploadBody.OpenForRequest() as requestBody:
                    if requestBody is None:
                        raise BambuFtpsError("Upload body was empty.", isInvalidPath=True)
                    reader = self._AsBinaryReader(requestBody)
                    return ftp.storbinary("STOR " + printerPath, reader, blocksize=BambuFileManager.c_UploadBlockSizeBytes)
            except Exception as e:
                if self._IsLikelyUploadShutdownTimeout(e):
                    self._CloseConnection(ftp)
                    if self._VerifyUploadedSize(printerPath, expectedSize):
                        self.Logger.debug("Bambu FTPS upload hit an SSL shutdown timeout, but the uploaded size matched.")
                        return "226 Transfer complete"
                    self.Logger.warning("Bambu FTPS upload hit an SSL shutdown timeout and size verification was unavailable. Treating the upload as complete.")
                    return "226 Transfer complete"
                raise
        return self._RunWithConnection(_upload)


    def DownloadFileToTemp(self, printerPath:str) -> Tuple[str, int]:
        printerPath = self._NormalizePrinterPath(printerPath)
        tempFile = tempfile.NamedTemporaryFile(prefix="oe-bambu-download-", suffix=".tmp", mode="w+b", delete=False)
        tempPath = tempFile.name
        success = False
        try:
            def _download(ftp:ftplib.FTP_TLS) -> None:
                ftp.retrbinary("RETR " + printerPath, tempFile.write, blocksize=BambuFileManager.c_DownloadBlockSizeBytes)
            self._RunWithConnection(_download)
            tempFile.flush()
            sizeBytes = os.fstat(tempFile.fileno()).st_size
            success = True
            return (tempPath, sizeBytes)
        finally:
            try:
                tempFile.close()
            except Exception:
                pass
            if success is False:
                self._DeleteFileIfExists(tempPath)


    def DeleteFile(self, printerPath:str) -> str:
        printerPath = self._NormalizePrinterPath(printerPath)

        def _delete(ftp:ftplib.FTP_TLS) -> str:
            return ftp.delete(printerPath)
        return self._RunWithConnection(_delete)


    def _RunWithConnection(self, callback:Callable[[ftplib.FTP_TLS], Any]) -> Any:
        ftp = self._OpenConnection()
        if isinstance(ftp, BambuFtpsError):
            raise ftp
        try:
            return callback(ftp)
        except BambuFtpsError:
            raise
        except Exception as e:
            raise self._MapException(e) from e
        finally:
            self._CloseConnection(ftp)


    def _OpenConnection(self) -> Union[ftplib.FTP_TLS, "BambuFtpsError"]:
        if self.Host is None or len(str(self.Host).strip()) == 0:
            raise BambuFtpsError("Missing printer IP or hostname for Bambu FTPS.", isConnectionError=True)
        if self.AccessCode is None or len(str(self.AccessCode).strip()) == 0:
            raise BambuFtpsError("Missing Bambu access code for FTPS.", isAuthError=True)

        try:
            if self.FtpFactory is not None:
                ftp = self.FtpFactory()
            else:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                ftp = ImplicitFTP_TLS(context=context)
            setPasv = getattr(ftp, "set_pasv", None)
            if callable(setPasv):
                setPasv(True)
            ftp.connect(host=self.Host, port=self.Port, timeout=15)
            ftp.login(user=BambuFileManager.c_User, passwd=self.AccessCode)
            ftp.prot_p()
            return ftp
        except Exception as e:
            raise self._MapException(e) from e


    def _CloseConnection(self, ftp:Any) -> None:
        try:
            ftp.quit()
            return
        except Exception:
            pass
        try:
            ftp.close()
        except Exception:
            pass


    def _ListRecursive(self, ftp:ftplib.FTP_TLS, path:str, depth:int, files:List[BambuFtpFileInfo]) -> None:
        if depth > BambuFileManager.c_MaxListDepth or len(files) >= BambuFileManager.c_MaxListEntries:
            return
        try:
            entries = self._ListDirectory(ftp, path)
        except BambuFtpsError as e:
            if e.IsNotFound:
                return
            raise

        for entry in entries:
            if len(files) >= BambuFileManager.c_MaxListEntries:
                return
            if entry.IsDir:
                if depth == 0 and entry.Name.lower() in BambuFileManager.c_SkipRecursiveDirs:
                    continue
                self._ListRecursive(ftp, entry.Path, depth + 1, files)
                continue
            if self._IsPrintablePath(entry.Path):
                files.append(BambuFtpFileInfo(entry.Path, entry.SizeBytes, entry.ModifiedTimeSec))


    def _ListDirectory(self, ftp:ftplib.FTP_TLS, path:str) -> List["_BambuFtpEntry"]:
        try:
            return self._ListDirectoryWithMlsd(ftp, path)
        except Exception:
            return self._ListDirectoryWithList(ftp, path)


    def _ListDirectoryWithMlsd(self, ftp:ftplib.FTP_TLS, path:str) -> List["_BambuFtpEntry"]:
        mlsd = getattr(ftp, "mlsd", None)
        if not callable(mlsd):
            raise Exception("MLSD unavailable.")

        entries:List[_BambuFtpEntry] = []
        iterable:Iterable[Tuple[str, Dict[str, str]]] = mlsd(path) if len(path) > 0 else mlsd()
        for name, facts in iterable:
            if name == "." or name == "..":
                continue
            entryType = str(facts.get("type", "")).lower()
            if entryType == "cdir" or entryType == "pdir":
                continue
            isDir = entryType == "dir"
            sizeBytes = self._ParseInt(facts.get("size", None))
            modifiedTimeSec = self._ParseMlsdModifyTime(facts.get("modify", None))
            entries.append(_BambuFtpEntry(name, self._JoinPrinterPath(path, name), isDir, sizeBytes, modifiedTimeSec))
        return entries


    def _ListDirectoryWithList(self, ftp:ftplib.FTP_TLS, path:str) -> List["_BambuFtpEntry"]:
        lines:List[str] = []
        command = "LIST" if len(path) == 0 else "LIST " + path
        try:
            ftp.retrlines(command, lines.append)
        except Exception as e:
            mapped = self._MapException(e)
            if mapped.IsNotFound:
                return []
            raise mapped from e

        entries:List[_BambuFtpEntry] = []
        for line in lines:
            entry = self._ParseListLine(path, line)
            if entry is not None:
                entries.append(entry)
        return entries


    def _ParseListLine(self, parentPath:str, line:str) -> Optional["_BambuFtpEntry"]:
        if line is None:
            return None
        line = str(line).strip()
        if len(line) == 0 or line.startswith("total "):
            return None
        parts = line.split(maxsplit=8)
        if len(parts) < 9:
            return None
        mode = parts[0]
        name = parts[8]
        if len(name) == 0 or name == "." or name == "..":
            return None
        if mode.startswith("l") and " -> " in name:
            name = name.split(" -> ", 1)[0]
        if len(name) == 0:
            return None
        isDir = mode.startswith("d")
        sizeBytes = self._ParseInt(parts[4])
        modifiedTimeSec = self._ParseListModifyTime(parts[5], parts[6], parts[7])
        return _BambuFtpEntry(name, self._JoinPrinterPath(parentPath, name), isDir, sizeBytes, modifiedTimeSec)


    def _EnsureParentDir(self, ftp:ftplib.FTP_TLS, printerPath:str) -> None:
        slash = printerPath.rfind("/")
        if slash == -1:
            return
        parentPath = printerPath[:slash]
        current = ""
        for part in parentPath.split("/"):
            if len(part) == 0:
                continue
            current = part if len(current) == 0 else current + "/" + part
            try:
                ftp.mkd(current)
            except Exception as e:
                # Most servers report 550 when the directory already exists.
                if self._LooksLikeFtpCode(e, "550"):
                    continue
                raise


    def _VerifyUploadedSize(self, printerPath:str, expectedSize:int) -> bool:
        try:
            def _size(ftp:ftplib.FTP_TLS) -> bool:
                sizeFunc = getattr(ftp, "size", None)
                if not callable(sizeFunc):
                    return False
                size = sizeFunc(printerPath)
                return size is not None and int(size) == expectedSize
            return bool(self._RunWithConnection(_size))
        except Exception:
            return False


    def _NormalizePrinterPath(self, printerPath:str) -> str:
        path = str(printerPath).replace("\\", "/").strip()
        while path.startswith("/"):
            path = path[1:]
        while "//" in path:
            path = path.replace("//", "/")
        path = path.rstrip("/")
        if len(path) == 0:
            raise BambuFtpsError("Invalid Bambu printer file path.", isInvalidPath=True)
        if "\r" in path or "\n" in path or "\x00" in path:
            raise BambuFtpsError("Invalid Bambu printer file path.", isInvalidPath=True)
        for part in path.split("/"):
            if len(part) == 0 or part == "." or part == "..":
                raise BambuFtpsError("Invalid Bambu printer file path.", isInvalidPath=True)
        return path


    def _AsBinaryReader(self, requestBody:Any) -> BinaryIO:
        if hasattr(requestBody, "read"):
            return requestBody
        return _BytesReader(bytes(requestBody))


    def _JoinPrinterPath(self, parentPath:str, name:str) -> str:
        if len(parentPath) == 0:
            return name
        return parentPath.rstrip("/") + "/" + name


    def _IsPrintablePath(self, path:str) -> bool:
        lower = path.lower()
        for ext in BambuFileManager.c_PrintableExtensions:
            if lower.endswith(ext):
                return True
        return False


    def _ParseInt(self, value:Any) -> Optional[int]:
        try:
            if value is None:
                return None
            return int(value)
        except Exception:
            return None


    def _ParseMlsdModifyTime(self, value:Optional[str]) -> Optional[float]:
        if value is None or len(value) < 14:
            return None
        try:
            dt = datetime.datetime.strptime(value[:14], "%Y%m%d%H%M%S")
            return float(calendar.timegm(dt.timetuple()))
        except Exception:
            return None


    def _ParseListModifyTime(self, month:str, day:str, yearOrTime:str) -> Optional[float]:
        try:
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            if ":" in yearOrTime:
                dt = datetime.datetime.strptime(f"{month} {day} {now.year} {yearOrTime}", "%b %d %Y %H:%M")
                if dt > now + datetime.timedelta(days=1):
                    dt = dt.replace(year=dt.year - 1)
            else:
                dt = datetime.datetime.strptime(f"{month} {day} {yearOrTime} 00:00", "%b %d %Y %H:%M")
            return float(calendar.timegm(dt.timetuple()))
        except Exception:
            return None


    def _MapException(self, e:Exception) -> "BambuFtpsError":
        msg = str(e)
        if isinstance(e, BambuFtpsError):
            return e
        if self._LooksLikeFtpCode(e, "530") or "authentication failed" in msg.lower() or "not logged in" in msg.lower():
            return BambuFtpsError("Bambu FTPS authentication failed.", isAuthError=True)
        if self._LooksLikeFtpCode(e, "550"):
            return BambuFtpsError("Bambu FTPS file or directory was not found.", isNotFound=True)
        if isinstance(e, (ConnectionError, TimeoutError, OSError)) or "timed out" in msg.lower() or "connection" in msg.lower():
            return BambuFtpsError("Unable to connect to the Bambu FTPS server: " + msg, isConnectionError=True)
        return BambuFtpsError("Bambu FTPS command failed: " + msg)


    def _LooksLikeFtpCode(self, e:Exception, code:str) -> bool:
        msg = str(e).strip()
        return msg.startswith(code) or (" " + code + " ") in msg


    def _IsLikelyUploadShutdownTimeout(self, e:Exception) -> bool:
        msg = str(e).lower().strip()
        return "read operation timed out" in msg or "ssl" in msg and "timed out" in msg


    def _DeleteFileIfExists(self, filePath:Optional[str]) -> None:
        if filePath is None:
            return
        try:
            if os.path.exists(filePath):
                os.remove(filePath)
        except Exception as e:
            self.Logger.warning("Failed to delete Bambu temp file %s. %s", filePath, str(e))


class _BambuFtpEntry:
    def __init__(self, name:str, path:str, isDir:bool, sizeBytes:Optional[int], modifiedTimeSec:Optional[float]) -> None:
        self.Name = name
        self.Path = path
        self.IsDir = isDir
        self.SizeBytes = sizeBytes
        self.ModifiedTimeSec = modifiedTimeSec


class _BytesReader:
    def __init__(self, data:bytes) -> None:
        self._data = data
        self._offset = 0


    def read(self, size:Optional[int]=-1) -> bytes:
        if size is None or size < 0:
            size = len(self._data) - self._offset
        if size == 0:
            return b""
        end = min(len(self._data), self._offset + size)
        data = self._data[self._offset:end]
        self._offset = end
        return data


class BambuFtpsError(Exception):
    def __init__(self, message:str, isAuthError:bool=False, isNotFound:bool=False, isConnectionError:bool=False, isInvalidPath:bool=False) -> None:
        super().__init__(message)
        self.Message = message
        self.IsAuthError = isAuthError
        self.IsNotFound = isNotFound
        self.IsConnectionError = isConnectionError
        self.IsInvalidPath = isInvalidPath


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    # Bambu printers use implicit FTPS on port 990. Python's FTP_TLS is explicit
    # FTPS, so wrap the control socket immediately and reuse the TLS session for
    # data sockets.
    def __init__(self, *args:Any, **kwargs:Any) -> None:
        super().__init__(*args, **kwargs)
        self._sock:Optional[ssl.SSLSocket] = None


    @property
    def sock(self) -> Optional[ssl.SSLSocket]: #type: ignore[override]
        return self._sock


    @sock.setter
    def sock(self, value:Any) -> None: #type: ignore[override]
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value


    def ntransfercmd(self, cmd: str, rest:Optional[str]=None) -> Tuple[Any, Optional[int]]: #type: ignore[override]
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            session = None
            if isinstance(self.sock, ssl.SSLSocket):
                session = self.sock.session
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=session)
        return conn, size


    def storbinary(self, cmd:str, fp:BinaryIO, blocksize:int=8192, callback:Optional[Callable[[bytes], None]]=None, rest:Optional[str]=None) -> str: #type: ignore[override]
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback is not None:
                    callback(buf)
        finally:
            conn.close()
        return self.voidresp()