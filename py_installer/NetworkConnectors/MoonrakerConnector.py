import json
import socket
import threading
import ipaddress
from typing import Any, Optional, Tuple, List

from octoeverywhere.websocketimpl import Client
from octoeverywhere.interfaces import IWebSocketClient, WebSocketOpCode
from octoeverywhere.buffer import Buffer

from py_installer.Util import Util
from py_installer.Logging import Logger
from py_installer.Context import Context
from py_installer.ConfigHelper import ConfigHelper


# A simple helper that's returned from the moonraker connection test.
class MoonrakerConnectionResult:
    def __init__(self, success:bool, unauthorized:bool, exception:Optional[Exception]=None):
        self.Success = success
        self.Unauthorized = unauthorized
        self.Exception = exception


# A simple helper that's returned from the moonraker scan.
class MoonrakerScanResult:
    def __init__(self, ip:str, unauthorized:bool):
        self.Ip = ip
        self.Unauthorized = unauthorized


# A class that helps the user discover, connect, and setup the details required to connect to a remote Moonraker server.
class MoonrakerConnector:

    c_KlipperDefaultPortStr = "7125"

    def EnsureCompanionMoonrakerConnection(self, context:Context) -> None:
        Logger.Debug("Running companion ensure config logic.")

        # See if there's a valid config already.
        ip, port = ConfigHelper.TryToGetCompanionDetails(context)
        # The API key is optional.
        apiKey = ConfigHelper.TryToGetMoonrakerDetails(context)
        if ip is not None and port is not None:
            # Check if we can still connect. This can happen if the IP address changes, the user might need to setup the
            # printer again.
            Logger.Info(f"Existing companion config file found. IP: {ip}:{port}")
            Logger.Info("Checking if we can connect to Klipper...")
            result = self._CheckForMoonraker(ip, port, apiKey=apiKey, timeoutSec=10.0)
            if result.Success:
                Logger.Info("Successfully connected to Klipper!")
                return
            else:
                # Let the user keep this connection setup, or try to set it up again.
                Logger.Blank()
                if result.Unauthorized:
                    Logger.Warn("We were able to connect to Klipper, but the server requires auth and we don't have an API key or the API key invalid.")
                else:
                    Logger.Warn(f"We failed to connect to Klipper using {ip}:{port}.")
                Logger.Blank()
                if Util.AskYesOrNoQuestion("Do you want to set up the Klipper connection again?") is False:
                    Logger.Info(f"Keeping the existing Klipper connection. {ip}:{port}")
                    return

        # Note that often the API key will be None.
        ip, port, apiKey = self._SetupNewMoonrakerConnection()
        ConfigHelper.WriteCompanionDetails(context, ip, port)
        ConfigHelper.WriteMoonrakerDetails(context, apiKey)
        Logger.Blank()
        Logger.Header("Klipper connection successful!")
        Logger.Blank()


    # Helps the user setup a moonraker connection via auto scanning or manual setup.
    # Returns (ip:str, port:str, apiKey:str)
    def _SetupNewMoonrakerConnection(self) -> Tuple[str, str, Optional[str]]:
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
        scanResults = self._ScanForMoonrakerInstances()
        if len(scanResults) > 0:
            # Sort them so they present better.
            scanResults.sort(key=lambda x: int(ipaddress.IPv4Address(x.Ip)))

            while True:
                # Print the options.
                Logger.Blank()
                Logger.Info("Klipper was found on the following IP addresses:")
                count = 0
                for r in scanResults:
                    count += 1
                    Logger.Info(f"  {count}) {r.Ip}:{MoonrakerConnector.c_KlipperDefaultPortStr}{' - Needs API Key Authentication' if r.Unauthorized else ''}")
                Logger.Blank()

                response = input("Enter the number next to the Klipper instance you want to use or enter `m` to manually setup the connection: ")
                response = response.lower().strip()
                if response == "m":
                    # Break to fall through to the manual setup.
                    break
                try:
                    # Parse the input and -1 it, so it aligns with the array length.
                    tempInt = int(response.lower().strip()) - 1
                    if tempInt >= 0 and tempInt < len(scanResults):
                        # Get the result and see if we need to get an API key.
                        result = scanResults[tempInt]
                        apiKey = None

                        # Check if this instance requires an API key.
                        if result.Unauthorized:
                            apiKey = self._PromptForApiKeyAndValidate(result.Ip)
                            if apiKey is None:
                                Logger.Warn("That API key didn't work, try again.")
                                continue

                        # If we get here, we have a valid connection.
                        return (result.Ip, MoonrakerConnector.c_KlipperDefaultPortStr, apiKey)
                except Exception as _:
                    Logger.Warn("Invalid input, try again.")
        else:
            Logger.Info("No local Klipper devices could be automatically found.")

        # Do the manual setup process.
        ipOrHostname = ""
        port = MoonrakerConnector.c_KlipperDefaultPortStr
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
                Logger.Info(f"If you don't know the port or want to use the default port ({MoonrakerConnector.c_KlipperDefaultPortStr}), press enter.")
                port = input("Enter Moonraker Port: ")
                if len(port) == 0:
                    port = MoonrakerConnector.c_KlipperDefaultPortStr

                Logger.Blank()
                Logger.Info(f"Trying to connect to Moonraker via {ipOrHostname}:{port} ...")
                result = self._CheckForMoonraker(ipOrHostname, port, apiKey=None, timeoutSec=10.0)

                # Check if we connected, but need auth
                if result.Unauthorized:
                    apiKey = self._PromptForApiKeyAndValidate(ipOrHostname)
                    if apiKey is None:
                        Logger.Blank()
                        Logger.Blank()
                        Logger.Error("That API key didn't work, try again.")
                        continue
                    return (ipOrHostname, port, apiKey)

                # Check for success.
                if result.Success:
                    return (ipOrHostname, port, None)

                else:
                    Logger.Blank()
                    Logger.Blank()
                    if result.Exception is not None:
                        Logger.Error("Klipper connection failed.")
                    else:
                        Logger.Error("Klipper connection timed out.")
                    Logger.Warn("Make sure the device is powered on, has an network connection, and the ip is correct.")
                    if result.Exception is not None:
                        Logger.Warn(f"Error {str(result.Exception)}")
            except Exception as e:
                Logger.Warn("Failed to setup Klipper, try again. "+str(e))


    # For a given IP address, this will check if the server requires an API key.
    def _PromptForApiKeyAndValidate(self, ip:str) -> Optional[str]:
        Logger.Blank()
        Logger.Warn("This Klipper printer requires an API key for authentication.")
        Logger.Info("You can generate a Moonraker API key from either Mainsail or Fluidd, and then paste or enter it here.")
        Logger.Blank()
        apiKey = input("Please enter the API key: ")
        if apiKey is None or len(apiKey) == 0:
            return None
        result = self._CheckForMoonraker(ip, MoonrakerConnector.c_KlipperDefaultPortStr, apiKey=apiKey, timeoutSec=5.0)
        if result.Success is False:
            return None
        return apiKey


    # Given an ip or hostname and port, this will try to detect if there's a moonraker instance.
    def _CheckForMoonraker(self, ip:str, port:str, apiKey:Optional[str]=None, timeoutSec:float=5.0) -> MoonrakerConnectionResult:
        doneEvent = threading.Event()
        lock = threading.Lock()
        result:dict[str, Any] = {}

        # Create the URL
        url = f"ws://{ip}:{port}/websocket"

        # Setup the callback functions
        def OnOpened(ws:IWebSocketClient):
            Logger.Debug(f"Test [{url}] - WS Opened")
            # After the websocket is open, we must send this message to identify ourselves.
            # This is required to check if the server needs auth or not.
            # If it needs auth and we don't provide it, it will return an unauthorized failure.
            params = {
                "client_name": "OctoEverywhere",
                "version": "4.0.0",
                "type": "agent", # We must be the agent type so that we can send agent-event, aka send messages to the UI.
                "url": "https://octoeverywhere.com",
            }
            if apiKey is not None:
                Logger.Debug(f"Test [{url}] - WS Requesting identify with apiKey")
                params["api_key"] =  apiKey
            else:
                Logger.Debug(f"Test [{url}] - WS Requesting identify without apiKey")

            # Create the request object
            obj = {
                "jsonrpc": "2.0",
                "method": "server.connection.identify",
                "id": 1,
                "params": params
            }
            # Try to send. default=str makes the json dump use the str function if it fails to serialize something.
            jsonStr = json.dumps(obj, default=str)
            jsonBytes = jsonStr.encode("utf-8")
            ws.Send(Buffer(jsonBytes), 0, len(jsonBytes), False)

        def OnData(ws:IWebSocketClient, msg:Buffer, opcode:WebSocketOpCode):
            with lock:
                if "success" in result:
                    return
                try:
                    # Try to see if the message looks like one of the first moonraker messages.
                    msgStr = msg.GetBytesLike().decode('utf-8')
                    Logger.Debug(f"Test [{url}] - WS message `{msgStr}`")
                    msgStrLower = msgStr.lower()

                    # First check if there was an unauthorized error.
                    if "unauthorized" in msgStrLower:
                        Logger.Debug(f"Test [{url}] - Found unauthorized message, failure!")
                        result["unauthorized"] = True
                        result["success"] = False
                        doneEvent.set()

                    if "moonraker" in msgStrLower:
                        Logger.Debug(f"Test [{url}] - Found Moonraker message, success!")
                        result["success"] = True
                        doneEvent.set()
                except Exception:
                    pass
        def OnClosed(ws:IWebSocketClient):
            Logger.Debug(f"Test [{url}] - Closed")
            doneEvent.set()
        def OnError(ws:IWebSocketClient, exception:Exception):
            Logger.Debug(f"Test [{url}] - Error: {str(exception)}")
            with lock:
                result["exception"] = exception
            doneEvent.set()

        # Create the websocket
        capturedSuccess = False
        capturedUnauthorized = False
        capturedException = None
        Logger.Debug(f"Checking for moonraker using the address: `{url}`")
        try:
            with Client(url, onWsOpen=OnOpened, onWsData=OnData, onWsError=OnError, onWsClose=OnClosed) as ws:
                ws.SetDisableCertCheck(True)
                ws.RunAsync()

                # Wait for the event or a timeout.
                doneEvent.wait(timeoutSec)

                # Get the results before we close.
                with lock:
                    if result.get("success", None) is not None:
                        capturedSuccess = result["success"]
                    if result.get("unauthorized", None) is not None:
                        capturedUnauthorized = result["unauthorized"]
                    if result.get("exception", None) is not None:
                        capturedException = result["exception"]
        except Exception as e:
            Logger.Info(f"Websocket threw and exception. {e}")

        return MoonrakerConnectionResult(capturedSuccess, capturedUnauthorized, capturedException)


    # Scans the subnet for Moonraker instances.
    # Returns a list of IPs where moonraker was found.
    def _ScanForMoonrakerInstances(self) -> List[MoonrakerScanResult]:
        results:List[MoonrakerScanResult] = []
        try:
            localIp = self._TryToGetLocalIp()
            if localIp is None or len(localIp) == 0:
                Logger.Debug("Failed to get local IP")
                return results
            Logger.Debug(f"Local IP found as: {localIp}")
            if ":" in localIp:
                Logger.Info("IPv6 addresses aren't supported for local discovery.")
                return results
            lastDot = localIp.rfind(".")
            if lastDot == -1:
                Logger.Info("Failed to find last dot in local IP?")
                return results
            ipPrefix = localIp[:lastDot+1]

            counter = 0
            doneThreads = [0]
            totalThreads = 255
            threadLock = threading.Lock()
            doneEvent = threading.Event()
            while counter <= totalThreads:
                fullIp = ipPrefix + str(counter)
                def threadFunc(ip:str):
                    try:
                        checkResult = self._CheckForMoonraker(ip, MoonrakerConnector.c_KlipperDefaultPortStr, apiKey=None, timeoutSec=5.0)
                        with threadLock:
                            # First check if we found a server, but it was unauthorized.
                            if checkResult.Unauthorized:
                                results.append(MoonrakerScanResult(ip, True))
                            # Next check for a successful connection.
                            elif checkResult.Success:
                                # If we found a server, add it to the list.
                                results.append(MoonrakerScanResult(ip, False))
                            doneThreads[0] += 1
                            if doneThreads[0] == totalThreads:
                                doneEvent.set()
                    except Exception as e:
                        Logger.Error(f"Moonraker scan failed for {ip} "+str(e))
                t = threading.Thread(target=threadFunc, args=[fullIp])
                t.start()
                counter += 1
            doneEvent.wait()
            return results
        except Exception as e:
            Logger.Error("Failed to scan for Moonraker instances. "+str(e))
        return results


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
        return str(ip)
