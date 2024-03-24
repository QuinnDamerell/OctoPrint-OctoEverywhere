import threading
import socket

from octoeverywhere.websocketimpl import Client

from py_installer.Util import Util
from py_installer.Logging import Logger
from py_installer.Context import Context
from py_installer.ConfigHelper import ConfigHelper


# A class that helps the user discover, connect, and setup the details required to connect to a remote Moonraker server.
class MoonrakerConnector:

    def EnsureCompanionMoonrakerConnection(self, context:Context):
        Logger.Debug("Running companion ensure config logic.")

        # See if there's a valid config already.
        ip, port = ConfigHelper.TryToGetCompanionDetails(context)
        if ip is not None and port is not None:
            # Check if we can still connect. This can happen if the IP address changes, the user might need to setup the
            # printer again.
            Logger.Info(f"Existing companion config file found. IP: {ip}:{port}")
            Logger.Info("Checking if we can connect to Klipper...")
            success, _ = self._CheckForMoonraker(ip, port, 10.0)
            if success:
                Logger.Info("Successfully connected to Klipper!")
                return
            else:
                # Let the user keep this connection setup, or try to set it up again.
                Logger.Blank()
                Logger.Warn(f"No Klipper connection found at {ip}:{port}.")
                if Util.AskYesOrNoQuestion("Do you want to setup the Klipper connection again for this OctoEverywhere companion instance?") is False:
                    Logger.Info(f"Keeping the existing Klipper connection setup. {ip}:{port}")
                    return

        ip, port = self._SetupNewMoonrakerConnection()
        ConfigHelper.WriteCompanionDetails(context, ip, port)
        Logger.Blank()
        Logger.Header("Klipper connection successful!")
        Logger.Blank()


    # Helps the user setup a moonraker connection via auto scanning or manual setup.
    # Returns (ip:str, port:str)
    def _SetupNewMoonrakerConnection(self):
        Logger.Blank()
        Logger.Blank()
        Logger.Blank()
        Logger.Header("##################################")
        Logger.Header("     Klipper Companion Setup")
        Logger.Header("##################################")
        Logger.Blank()
        Logger.Info("For OctoEverywhere Companion to work, it needs to know how to connect to the Klipper device on your network.")
        Logger.Info("If you have any trouble, we are happy to help! Contact us at support@octoeverywhere.com")
        Logger.Blank()
        Logger.Info("Searching for local Klipper printers... please wait... (about 5 seconds)")
        foundIps = self._ScanForMoonrakerInstances()
        if len(foundIps) > 0:
            # Sort them so they present better.
            foundIps = sorted(foundIps)
            Logger.Blank()
            Logger.Info("Klipper was found on the following IP addresses:")
            count = 0
            for ip in foundIps:
                count += 1
                Logger.Info(f"  {count}) {ip}:7125")
            Logger.Blank()
            while True:
                response = input("Enter the number next to the Klipper instance you want to use or enter `m` to manually setup the connection: ")
                response = response.lower().strip()
                if response == "m":
                    # Break to fall through to the manual setup.
                    break
                try:
                    # Parse the input and -1 it, so it aligns with the array length.
                    tempInt = int(response.lower().strip()) - 1
                    if tempInt >= 0 and tempInt < len(foundIps):
                        return (foundIps[tempInt], "7125")
                except Exception as _:
                    Logger.Warn("Invalid input, try again.")
        else:
            Logger.Info("No local Klipper devices could be automatically found.")

        # Do the manual setup process.
        ipOrHostname = ""
        port = "7125"
        while True:
            try:
                Logger.Blank()
                Logger.Blank()
                Logger.Info("Please enter the IP address or Hostname of the device running Klipper/Moonraker/Mainsail/Fluidd.")
                Logger.Info("The IP address might look something like `192.168.1.5` or a Hostname might look like `klipper.local`")
                ipOrHostname = input("Enter the IP or Hostname: ")
                # Clean up what the user entered.
                ipOrHostname = ipOrHostname.lower().strip()
                if ipOrHostname.find("://") != -1:
                    ipOrHostname = ipOrHostname[ipOrHostname.find("://")+3:]
                if ipOrHostname.find("/") != -1:
                    ipOrHostname = ipOrHostname[:ipOrHostname.find("/")]

                Logger.Blank()
                Logger.Info("Please enter the port Moonraker is running on.")
                Logger.Info("If you don't know the port or want to use the default port (7125), press enter.")
                port = input("Enter Moonraker Port: ")
                if len(port) == 0:
                    port = "7125"

                Logger.Blank()
                Logger.Info(f"Trying to connect to Moonraker via {ipOrHostname}:{port} ...")
                success, exception = self._CheckForMoonraker(ipOrHostname, port, 10.0)

                # Handle the result.
                if success:
                    return (ipOrHostname, port)
                else:
                    Logger.Blank()
                    Logger.Blank()
                    if exception is not None:
                        Logger.Error("Klipper connection failed.")
                    else:
                        Logger.Error("Klipper connection timed out.")
                    Logger.Warn("Make sure the device is powered on, has an network connection, and the ip is correct.")
                    if exception is not None:
                        Logger.Warn(f"Error {str(exception)}")
            except Exception as e:
                Logger.Warn("Failed to setup Klipper, try again. "+str(e))


    # Given an ip or hostname and port, this will try to detect if there's a moonraker instance.
    # Returns (success:, exception | None)
    def _CheckForMoonraker(self, ip:str, port:str, timeoutSec:float = 5.0):
        doneEvent = threading.Event()
        lock = threading.Lock()
        result = {}

        # Create the URL
        url = f"ws://{ip}:{port}/websocket"

        # Setup the callback functions
        def OnOpened(ws):
            Logger.Debug(f"Test [{url}] - WS Opened")
        def OnMsg(ws, msg):
            with lock:
                if "success" in result:
                    return
                try:
                    # Try to see if the message looks like one of the first moonraker messages.
                    msgStr = msg.decode('utf-8')
                    Logger.Debug(f"Test [{url}] - WS message `{msgStr}`")
                    if "moonraker" in msgStr.lower():
                        Logger.Debug(f"Test [{url}] - Found Moonraker message, success!")
                        result["success"] = True
                        doneEvent.set()
                except Exception:
                    pass
        def OnClosed(ws):
            Logger.Debug(f"Test [{url}] - Closed")
            doneEvent.set()
        def OnError(ws, exception):
            Logger.Debug(f"Test [{url}] - Error: {str(exception)}")
            with lock:
                result["exception"] = exception
            doneEvent.set()

        # Create the websocket
        Logger.Debug(f"Checking for moonraker using the address: `{url}`")
        try:
            with Client(url, onWsOpen=OnOpened, onWsMsg=OnMsg, onWsError=OnError, onWsClose=OnClosed) as ws:
                ws.RunAsync()

                # Wait for the event or a timeout.
                doneEvent.wait(timeoutSec)

                # Get the results before we close.
                capturedSuccess = False
                capturedEx = None
                with lock:
                    if result.get("success", None) is not None:
                        capturedSuccess = result["success"]
                    if result.get("exception", None) is not None:
                        capturedEx = result["exception"]
        except Exception as e:
            Logger.Info(f"Websocket threw and exception. {e}")

        return (capturedSuccess, capturedEx)


    # Scans the subnet for Moonraker instances.
    # Returns a list of IPs where moonraker was found.
    def _ScanForMoonrakerInstances(self):
        foundIps = []
        try:
            localIp = self._TryToGetLocalIp()
            if localIp is None or len(localIp) == 0:
                Logger.Debug("Failed to get local IP")
                return foundIps
            Logger.Debug(f"Local IP found as: {localIp}")
            if ":" in localIp:
                Logger.Info("IPv6 addresses aren't supported for local discovery.")
                return foundIps
            lastDot = localIp.rfind(".")
            if lastDot == -1:
                Logger.Info("Failed to find last dot in local IP?")
                return foundIps
            ipPrefix = localIp[:lastDot+1]

            counter = 0
            doneThreads = [0]
            totalThreads = 255
            threadLock = threading.Lock()
            doneEvent = threading.Event()
            while counter <= totalThreads:
                fullIp = ipPrefix + str(counter)
                def threadFunc(ip):
                    try:
                        success, _ = self._CheckForMoonraker(ip, "7125", 5.0)
                        with threadLock:
                            if success:
                                foundIps.append(ip)
                            doneThreads[0] += 1
                            if doneThreads[0] == totalThreads:
                                doneEvent.set()
                    except Exception as e:
                        Logger.Error(f"Moonraker scan failed for {ip} "+str(e))
                t = threading.Thread(target=threadFunc, args=[fullIp])
                t.start()
                counter += 1
            doneEvent.wait()
            return foundIps
        except Exception as e:
            Logger.Error("Failed to scan for Moonraker instances. "+str(e))
        return foundIps


    def _TryToGetLocalIp(self) -> str:
        # Find the local IP. Works on Windows and Linux. Always gets the correct routable IP.
        # https://stackoverflow.com/a/28950776
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ip = None
        try:
            # doesn't even have to be reachable
            s.connect(('1.1.1.1', 1))
            ip = s.getsockname()[0]
        except Exception:
            pass
        finally:
            s.close()
        return ip
