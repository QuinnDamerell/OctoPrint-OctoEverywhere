import socket
import fcntl
import struct

class LocalIpHelper:

    @staticmethod
    def TryToGetLocalIp():
        # We try to find an ip in some priority ordering of interfaces.
        # The call will fail and return empty string if there is no IP for the interface
        # So we will test the interfaces we know going down the priority list, until we find 
        # an ip or not.
        #
        # I know at least on OctoPi, there's a 'wlan0' and a 'eth0'
        #
        interfaces = ["wlan0", "wlan1", "eth0", "eth1"]

        # Roll the dice to see if we find an ip!
        for inter in interfaces:
            result = LocalIpHelper.TryToGetIpAddress(inter)
            if len(result) > 0:
                return result
            
        # If we fail, return empty string.
        return ""

    @staticmethod
    def TryToGetIpAddress(ifname):
        try:
            return LocalIpHelper.GetIpAddress(ifname)
        except Exception as _:
            return ""

    @staticmethod
    def GetIpAddress(ifname):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return socket.inet_ntoa(fcntl.ioctl(
            s.fileno(),
            0x8915,  # SIOCGIFADDR
            struct.pack('256s', ifname[:15].encode('utf-8'))
        )[20:24])