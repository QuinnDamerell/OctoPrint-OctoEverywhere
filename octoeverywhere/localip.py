import socket
from typing import Optional

# A helper class to try to detect the local IP of the device.
class LocalIpHelper:

    s_ConnectionTargetIpOverride:Optional[str] = None


    # This is set for companions, where the local IP we want is not the IP of this device.
    @staticmethod
    def SetConnectionTargetIpOverride(ip:str) -> None:
        LocalIpHelper.s_ConnectionTargetIpOverride = ip


    # This returns the local IP address of the connection target.
    # If this is a plugin running on the same device, it should be the device IP.
    # If this a companion plugin, it should be the IP of the main OctoEverywhere plugin device.
    @staticmethod
    def TryToGetLocalIpOfConnectionTarget() -> str:
        # If there is an override, we use that.
        # This is what's used in the companion plugin modes.
        if LocalIpHelper.s_ConnectionTargetIpOverride is not None:
            return LocalIpHelper.s_ConnectionTargetIpOverride

        # If there's no override, the connection target is this device, so get its local IP.
        return LocalIpHelper.TryToGetLocalIpOfThisDevice()



    @staticmethod
    def TryToGetLocalIpOfThisDevice() -> str:
        # Find the local IP. Works on Windows and Linux. Always gets the correct routable IP.
        # https://stackoverflow.com/a/28950776
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ip = ""
        try:
            # doesn't even have to be reachable
            s.connect(('1.1.1.1', 1))
            ip = s.getsockname()[0]
        except Exception:
            pass
        finally:
            s.close()
        return str(ip)
