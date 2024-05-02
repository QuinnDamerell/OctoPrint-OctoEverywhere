import logging
from ftplib import FTP_TLS
import ssl

from octoeverywhere.sentry import Sentry
from octoeverywhere.commandhandler import FileDetails

from linux_host.config import Config


# When using Implicit FTP, the socket needs to be wrapped in SSL automatically before calling
# login. The default FTP_TLS class doesn't do that, so this small helper/wrapper does it for us.
class ImplicitFTP_TLS(FTP_TLS):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None


    @property
    def sock(self):
        return self._sock


    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value


# Does all things file related.
class FileManager:

    _Instance = None

    @staticmethod
    def Init(logger:logging.Logger, config:Config):
        FileManager._Instance = FileManager(logger, config)


    def __init__(self, logger:logging.Logger, config:Config) -> None:
        self.Logger = logger
        self.Config = config


    @staticmethod
    def Get():
        return FileManager._Instance


    # Must return a list of FileDetails or None.
    def GetFiles(self):
        try:
            # Try to get the FTP connection.
            ftp = self._GetFtpConnection()
            if ftp is None:
                self.Logger.warn("FileManager.GetFiles failed to get a FTP connection.")
                return None

            # We need to cd into the directory where files are stored on the device.
            try:
                ftp.cwd("cache")
            except Exception as e:
                # If we failed, the directory probably doesn't exist. List the dirs/files for debugging.
                for fileName in ftp.nlst():
                    self.Logger.info(f" ftp dir list: {fileName}")
                Sentry.Exception("FileManager.GetFiles failed to cd into the cached directory.", e)
                return None

            # The Bambu printers don't support the MLSD command, so we have to use the NLST command.
            returnFiles = []
            for name in ftp.nlst():
                if name.lower().strip().endswith(".gcode"):
                    returnFiles.append(FileDetails(name))
            return returnFiles
        except Exception as e:
            Sentry.Exception("GetFails failed", e)
        return None


    def _GetFtpConnection(self) -> FTP_TLS:
        try:
            # Get the server details.
            ipOrHostname = self.Config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
            accessToken = self.Config.GetStr(Config.SectionBambu, Config.BambuAccessToken, None)
            if ipOrHostname is None or accessToken is None:
                self.Logger.error("FileManager failed to get a IP or access token from the config.")
                return None

            # Try to connect to the server.
            # Use a decent timeout, since the server might be slow to respond.
            ftp = ImplicitFTP_TLS(timeout=20.0)
            #ftp.debug(2)
            ftp.connect(ipOrHostname, 990)
            # Use the access token to login
            ftp.login("bblp", accessToken)
            # Ensure we call this, to make sure all data channels are encrypted.
            ftp.prot_p()

            # Return on success.
            return ftp
        except Exception as e:
            Sentry.Exception("Failed to connect to FTP server.", e)
        return None