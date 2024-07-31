import re
import sys
import logging
import platform
import subprocess

from .sentry import Sentry

# A class that tries to get a unique id per device that doesn't change, ideally even when the OS is re-installed.
# Inspired by https://github.com/keygen-sh/py-machineid/blob/master/machineid/__init__.py
class DeviceId:

    _Instance = None

    @staticmethod
    def Init(logger: logging.Logger):
        DeviceId._Instance = DeviceId(logger)


    @staticmethod
    def Get():
        return DeviceId._Instance


    def __init__(self, logger: logging.Logger) -> None:
        self.Logger = logger


    # Get's a unique ID for the platform. The ID should be unique per platform and ideally not change even when the OS is re-installed.
    # This ID can't not be written to disk it must come from the system level some how.
    # If nothing can be found, None is return.
    def GetId(self) -> str:
        try:
            return self._GetIdInternal()
        except Exception as e:
            Sentry.Exception("Exception in DeviceId.GetId", e)
        return None


    def _GetIdInternal(self) -> str:
        # We have a few options to get a unique id for the device.
        # Try each possible method and return the first one that works.
        # We prefix each system, to ensure there are no collisions

        # Mac
        if sys.platform == "darwin":
            fid = self._RunCmd("ioreg -d2 -c IOPlatformExpertDevice | awk -F\\\" '/IOPlatformUUID/{print $(NF-1)}'")
            if fid is not None:
                self.Logger.debug(f"Found device id from darwin device id: {fid}")
                return self._BuildId("darwin", fid)

        # Windows
        if sys.platform in ('win32', 'cygwin', 'msys'):
            self.Logger.debug("Windows is not supported in DeviceId right now.")
            return None

        # Linux
        if sys.platform.startswith("linux"):
            fid = self._ReadFile("/var/lib/dbus/machine-id")
            if fid is not None:
                self.Logger.debug(f"Found device id from /var/lib/dbus/machine-id: {fid}")
                return self._BuildId("linux-mi", fid)

            fid = self._ReadFile('/etc/machine-id')
            if fid is not None:
                self.Logger.debug(f"Found device id from /etc/machine-id: {fid}")
                return self._BuildId("linux-mie", fid)

            group = self._ReadFile('/proc/self/cgroup')
            if group is not None and 'docker' in group:
                fid = self._RunCmd("head -1 /proc/self/cgroup | cut -d/ -f3")
            if fid is not None:
                self.Logger.debug(f"Found device id from docker cgroup: {fid}")
                return self._BuildId("linux-d", fid)

            mountInfo = self._ReadFile('/proc/self/mountinfo')
            if mountInfo and 'docker' in mountInfo:
                fid = self._RunCmd("grep -oP '(?<=docker/containers/)([a-f0-9]+)(?=/hostname)' /proc/self/mountinfo")
            if fid is not None:
                self.Logger.debug(f"Found device id from docker mountinfo: {fid}")
                return self._BuildId("linux-dm", fid)

            if 'microsoft' in platform.uname().release:
                fid = self._RunCmd("powershell.exe -ExecutionPolicy bypass -command '(Get-CimInstance -Class Win32_ComputerSystemProduct).UUID'")
            if fid is not None:
                self.Logger.debug(f"Found device id from wsl UUID: {fid}")
                return self._BuildId("wsl", fid)

        # BSD
        if sys.platform.startswith(('openbsd', 'freebsd')):
            fid = self._ReadFile("/etc/hostid")
            if fid is not None:
                self.Logger.debug(f"Found device id from /etc/hostid: {fid}")
                return self._BuildId("bsd-h", fid)

            fid = self._RunCmd('kenv -q smbios.system.uuid')
            if fid is not None:
                self.Logger.debug(f"Found device id from kenv -q smbios.system.uuid: {fid}")
                return self._BuildId("bsd-k", fid)

        self.Logger.warn(f"Found device ID not found on platform: {sys.platform}")
        return None


    # If the file exists and is readable, returns the body.
    # Otherwise None
    def _ReadFile(self, path: str) -> str:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return None


    # Runs the command and returns stdout.
    # Otherwise None
    def _RunCmd(self, cmd: str) -> str:
        try:
            return subprocess.run(cmd, shell=True, capture_output=True, check=True, encoding="utf-8").stdout.strip()
        except Exception:
            return None


    # Normalize the id to remove any whitespace or control characters.
    # We add a prefix for each method to ensure they don't collide.
    def _BuildId(self, method:str, fid: str) -> str:
        return method + "-" + re.sub(r'[\x00-\x1f\x7f-\x9f\s]', '', fid).strip()
