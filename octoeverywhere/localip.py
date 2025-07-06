import socket
from typing import Optional

# A helper class to try to detect the local IP of the device.
class LocalIpHelper:


    s_LocalIpOverride:Optional[str] = None


    @staticmethod
    def SetLocalIpOverride(ip:str):
        LocalIpHelper.s_LocalIpOverride = ip


    @staticmethod
    def TryToGetLocalIp() -> str:
        # If there is an override, use it. This happens on the companion for example, since the "local ip" we want for the device is not
        # this plugin device's IP.
        if LocalIpHelper.s_LocalIpOverride is not None:
            return LocalIpHelper.s_LocalIpOverride

        # Find the local IP. Works on Windows and Linux. Always gets the correct routable IP.
        # https://stackoverflow.com/a/28950776
        ip = ""
        try:
            # doesn't even have to be reachable
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("1.1.1.1", 1))
                ip = s.getsockname()[0]
        except Exception:
            pass
        return str(ip)
