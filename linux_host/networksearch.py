import os
import ssl
import time
import json
import random
import string
import socket
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as mqtt

from octoeverywhere.buffer import Buffer
from octoeverywhere.websocketimpl import Client
from octoeverywhere.interfaces import IWebSocketClient, WebSocketOpCode

# A helper class that's the result of a network search.
class ElegooNetworkSearchResult:
    def __init__(self, ip:str, tooManyClients:bool, mainboardMac:Optional[str]) -> None:
        self.Ip = ip
        self.TooManyClients = tooManyClients
        self.MainboardMac = mainboardMac


# A helper class that's the result of a network validation.
class NetworkValidationResult:
    def __init__(self,
                 isBambu:bool=True, # True if this is a Bambu printer, False if it's an Elegoo printer.
                 # Bambu specific results
                 failedToConnect:bool=False, failedAuth:bool=False, failSn:bool=False, exception:Optional[Exception]=None, bambuRtspUrl:Optional[str]=None,
                 # Elegoo specific results
                 wsConnected:bool=False, tooManyClients:bool=False, mainboardMac:Optional[str]=None
                 ) -> None:
        self.IsBambu = isBambu
        # Bambu specific results
        self.FailedToConnect = failedToConnect
        self.FailedAuth = failedAuth
        self.FailedSerialNumber = failSn
        self.Exception = exception
        # If none, the printer doesn't support RTSP.
        # If empty string, the LAN Mode Liveview is not turned on.
        # If a URL, the printer is ready to stream.
        self.BambuRtspUrl = bambuRtspUrl
        # Elegoo specific results
        self.WsConnected:bool = wsConnected
        self.TooManyClients:bool = tooManyClients
        self.MainboardMac:Optional[str] = mainboardMac


    def Success(self) -> bool:
        if self.Exception is not None:
            return False
        if self.IsBambu:
            # Defines success for bambu
            return not self.FailedToConnect and not self.FailedAuth and not self.FailedSerialNumber
        # Defines success for elegoo
        return self.WsConnected and not self.TooManyClients and self.MainboardMac is not None


# A helper class that allows for validating and or searching for Moonraker or Bambu printers on the local LAN.
class NetworkSearch:

    # The default port all Bambu printers will run MQTT on.
    c_BambuDefaultPortStr = "8883"
    # The default port the elegoo WebSocket & http server runs on.
    c_ElegooDefaultPortStr = "3030"


    # Scans the local IP LAN subset for Bambu servers that successfully authorize given the access code and printer sn.
    # Thread count and delay can be used to control how aggressive the scan is.
    @staticmethod
    def ScanForInstances_Bambu(
                logger:logging.Logger,
                accessCode:str,
                printerSn:str,
                portStr:Optional[str]=None,
                ipHint:Optional[str]=None,
                threadCount:Optional[int]=None,
                delaySec:float=0.0
            ) -> List[str]:
        def callback(ip:str):
            return NetworkSearch.ValidateConnection_Bambu(logger, ip, accessCode, printerSn, portStr, timeoutSec=5)
        # We want to return if any one IP is found, since there can only be one printer that will match the printer 100% correct.
        return NetworkSearch._ScanForInstances(logger, callback, returnAfterNumberFound=1, threadCount=threadCount, perThreadDelaySec=delaySec, ipHint=ipHint)


    # Scans the local IP LAN subset for Elegoo 3D printers.
    # Thread count and delay can be used to control how aggressive the scan is.
    # If a mainboardMac is specified, only printers with that mainboardMac will be considered.
    @staticmethod
    def ScanForInstances_Elegoo(
                logger:logging.Logger,
                mainboardMac:Optional[str]=None,
                portStr:Optional[str]=None,
                ipHint:Optional[str]=None,
                threadCount:Optional[int]=None,
                delaySec:float=0.0
            ) -> List[ElegooNetworkSearchResult]:

        # First, define our result list and our test function callback.
        foundPrinters:dict[str, NetworkValidationResult] = {}
        def callback(ip:str):
            result = NetworkSearch.ValidateConnection_Elegoo(logger, ip, portStr, timeoutSec=2)
            # We want to keep track of successful printers and ones we know are Elegoo printers, but we can't connect to.
            if result.MainboardMac is not None or result.TooManyClients:
                foundPrinters[ip] = result
            return result

        # If we have a target mac, no need to return after the first one is found.
        # Otherwise we find everything we can.
        returnAfterNumberFound = 0
        if mainboardMac is not None:
            returnAfterNumberFound = 1

        # First we use the special Elegoo OS search that uses a UDP broadcast.
        NetworkSearch._ScanForElegooOsIps(logger, callback, returnAfterNumberFound=returnAfterNumberFound, threadCount=threadCount, perThreadDelaySec=delaySec, ipHint=ipHint)

        # See if we found anything.
        if len(foundPrinters) == 0:
            # If we didn't find anything, try the older network search.
            NetworkSearch._ScanForInstances(logger, callback, returnAfterNumberFound=returnAfterNumberFound, threadCount=threadCount, perThreadDelaySec=delaySec, ipHint=ipHint)

            # If we still didnt' find anything, give up.
            if len(foundPrinters) == 0:
                return []

        # If we are looking for a specific mainboard id, we need to check the results.
        if mainboardMac is not None:
            for ip, result in foundPrinters.items():
                if result.MainboardMac is not None and result.MainboardMac.lower() == mainboardMac.lower():
                    return [ElegooNetworkSearchResult(ip, result.TooManyClients, result.MainboardMac)]
            return []

        # If we are looking for all printers, we can return all the results.
        ret:List[ElegooNetworkSearchResult] = []
        for ip, result in foundPrinters.items():
            ret.append(ElegooNetworkSearchResult(ip, result.TooManyClients, result.MainboardMac))
        return ret


    # The final two steps can happen in different orders, so we need to wait for both the sub success and state object to be received.
    @staticmethod
    def _BambuConnectionDone(data:Dict[str,Any], client:mqtt.Client) -> bool:
        if "SnSubSuccess" in data and data["SnSubSuccess"] is True and "GotStateObj" in data and data["GotStateObj"] is True:
            data["Event"].set()
            client.disconnect()
            return True
        return False


    # Given the ip, accessCode, printerSn, and optionally port, this will check if the printer is connectable.
    # Returns a NetworkValidationResult with the results.
    @staticmethod
    def ValidateConnection_Bambu(logger:logging.Logger, ipOrHostname:str, accessCode:str, printerSn:str, portStr:Optional[str]=None, timeoutSec:float=5.0) -> NetworkValidationResult:
        client:mqtt.Client = None # pyright: ignore[reportAssignmentType]
        try:
            if portStr is None:
                portStr = NetworkSearch.c_BambuDefaultPortStr
            port = int(portStr)
            logger.debug(f"Testing for Bambu on {ipOrHostname}:{port}")
            result = {}
            result["Event"] = threading.Event()
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata=result) # pyright: ignore[reportPrivateImportUsage]
            client.tls_set(tls_version=ssl.PROTOCOL_TLS, cert_reqs=ssl.CERT_NONE) # pyright: ignore[reportUnknownMemberType]
            client.tls_insecure_set(True)
            client.username_pw_set("bblp", accessCode)

            def connect(client:mqtt.Client, userdata:Dict[Any, Any], flags:Any, reason_code:mqtt.ReasonCode, properties:Any): # pyright: ignore[reportPrivateImportUsage]
                # If auth is wrong, we will get a connect callback with a failure "Not authorized"
                if reason_code.is_failure:
                    logger.debug(f"Bambu {ipOrHostname} connection failure: {reason_code}")
                    client.disconnect()
                    userdata["Event"].set()
                    return

                # If the connection was successful, the auth was valid.
                logger.debug(f"Bambu {ipOrHostname} connected.")
                userdata["IsAuthorized"] = True

                # Try to sub, to make sure the SN is correct.
                # For most bambu printers, the socket will disconnect if this fails, it doesn't bother to send the sub failed message.
                (result, mid) = client.subscribe(f"device/{printerSn}/report")
                if result != mqtt.MQTT_ERR_SUCCESS or mid is None:
                    logger.debug(f"Bambu {ipOrHostname} failed to send subscribe request.")
                    client.disconnect()
                    userdata["Event"].set()
                userdata["ReportMid"] = mid

            def disconnect(client:Any, userdata:Dict[str, Any], disconnect_flags:Any, reason_code:Any, properties:Any):
                logger.debug(f"Bambu {ipOrHostname} disconnected.")
                userdata["Event"].set()

            def subscribe(client:Any, userdata:Dict[str, Any], mid:Any, reason_code_list:List[mqtt.ReasonCode], properties:Any): # pyright: ignore[reportPrivateImportUsage]
                if "ReportMid" in userdata and mid == userdata["ReportMid"]:
                    # If this is the sub report, check the status and disconnect.
                    failedSn = False
                    for r in reason_code_list:
                        if r.is_failure:
                            # On any failure, report it and disconnect.
                            logger.debug(f"Bambu {ipOrHostname} Sub response for the report subscription reports failure. {r}")
                            failedSn = True
                    if not failedSn:
                        # Note we are now subed.
                        userdata["SnSubSuccess"] = True
                        logger.debug(f"Bambu {ipOrHostname} Sub success.")
                        # Push the message to get the full state, this is needed on teh P1 and A1
                        client.publish(f"device/{printerSn}/request", json.dumps( { "pushing": {"sequence_id": "0", "command": "pushall"}}))
                        # Check if we are done, this will disconnect if we are.
                        NetworkSearch._BambuConnectionDone(userdata, client)

            def message(client:Any, userdata:Dict[str, Any], mqttMsg:mqtt.MQTTMessage):
                # When we get a message, check if it is a state object.
                # We need info from the state object, and also it's a good validation the system is healthy.
                try:
                    msg = json.loads(mqttMsg.payload)
                    if "print" in msg:
                        printMsg = msg["print"]
                        # We dont have a 100% great way to know if this is a fully sync message.
                        # For now, we use this stat. The message we get from a P1P has 59 members in the root, so we use 40 as mark.
                        # Note we use this same value in BambuClient._OnMessage
                        if len(printMsg) > 40:
                            # Indicate we got the state object.
                            userdata["GotStateObj"] = True
                            # Try to parse the rtsp url if the printer has one.
                            ipCam = printMsg.get("ipcam", None)
                            rtspUrl = None
                            if ipCam is not None:
                                rtspUrl = ipCam.get("rtsp_url", None)
                                userdata["BambuRtspUrl"] = rtspUrl
                            # Report we got the full sync object and see if we are done.
                            logger.debug(f"Bambu {ipOrHostname} got a full state sync message. RTSP URL: {rtspUrl}")
                            # Check if we are done, this will disconnect if we are.
                            NetworkSearch._BambuConnectionDone(userdata, client)
                        else:
                            logger.debug(f"Bambu {ipOrHostname} got a state message, but it was too small to be a full message.")
                except Exception as e:
                    logger.debug(f"Bambu {ipOrHostname} - message failure {e}")

            # Setup functions and connect.
            client.on_connect = connect
            client.on_disconnect = disconnect
            client.on_subscribe = subscribe
            client.on_message = message

            # Try to connect, this will throw if it fails to find any server to connect to.
            failedToConnect = True
            try:
                logger.debug(f"Connecting to Bambu on {ipOrHostname}:{port}...")
                client.connect(ipOrHostname, port, keepalive=60)
                failedToConnect = False
                client.loop_start()
            except Exception as e:
                logger.debug(f"Bambu {ipOrHostname} - connection failure {e}")
            logger.debug(f"Connection exit for Bambu on {ipOrHostname}:{port}")

            # Wait for the timeout.
            if not failedToConnect:
                result["Event"].wait(timeoutSec)

            # Walk though the connection and see how far we got.
            failedAuth = True
            failedSn = True
            rtspUrl = None

            if "IsAuthorized" in result:
                failedAuth = False
            # We need both the sub success message and a successful state sync to consider this success.
            if "SnSubSuccess" in result and "GotStateObj" in result:
                failedSn = False
            # Optional - Get the URL if there was one detected.
            if "BambuRtspUrl" in result:
                rtspUrl = result["BambuRtspUrl"]

            return NetworkValidationResult(isBambu=True, failedToConnect=failedToConnect, failedAuth=failedAuth, failSn=failedSn, bambuRtspUrl=rtspUrl)

        except Exception as e:
            return NetworkValidationResult(isBambu=True, exception=e)
        finally:
            # Ensure we alway clean up.
            try:
                client.disconnect()
            except Exception:
                pass
            try:
                client.loop_stop()
            except Exception:
                pass


    # Given the ip and optionally a mac address, this will check if the printer is connectable.
    # Returns a NetworkValidationResult with the results.
    @staticmethod
    def ValidateConnection_Elegoo(logger:logging.Logger, ipOrHostname:str, portStr:Optional[str]=None, timeoutSec:float=2.0) -> NetworkValidationResult:
        try:
            # Setup the connection functions.
            if portStr is None:
                portStr = NetworkSearch.c_ElegooDefaultPortStr
            port = int(portStr)
            logger.debug(f"Testing for Elegoo printer on {ipOrHostname}:{port}")
            result:dict[str, Any] = {}
            result["Event"] = threading.Event()

            def onWsOpen(ws:IWebSocketClient):
                # We found an open websocket!
                logger.debug(f"Elegoo {ipOrHostname} websocket connected.")
                result["WsConnected"] = True
                # We want the Attributes message which will contain the main board id.
                # We need to send any command to get that message back, so we just send this.
                ws.Send(Buffer(json.dumps(
                {
                    "Id": "",
                    "Data": {
                        "Cmd": 1, # This is the command to get the machine data.
                        "Data": {},
                        "RequestID": ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(32)), # A 32 char random request id.
                        "MainboardID": "",
                        "TimeStamp": int(time.time() / 1000), # Current time in seconds.
                        "From": 1
                    }
                }).encode("utf-8")))

            def onWsData(ws:IWebSocketClient, message:Buffer, opCode:WebSocketOpCode):
                # We got a message back! We expect this to be the response to what we asked for.
                try:
                    msgStr = message.GetBytesLike().decode("utf-8")
                    logger.debug(f"Elegoo {ipOrHostname} ws msg: %s", msgStr)
                    msg = json.loads(msgStr)

                    # After we send our first message, the printer will send back a response as well as some broadcast messages.
                    # One of those broadcast messages is the attributes message, which contains the mainboard id.
                    attr = msg.get("Attributes", None)
                    if attr is None:
                        return
                    mainboardMac = attr.get("MainboardMAC", None)
                    if mainboardMac is not None:
                        logger.debug(f"Elegoo {ipOrHostname} found mainboard id: {mainboardMac}")
                        result["MainboardMac"] = mainboardMac

                    # Once we have seen the mainboard id, we are done.
                    result["Event"].set()
                except Exception as e:
                    logger.debug(f"Elegoo {ipOrHostname} ws msg error. {e}")

            def onWsClose(ws:IWebSocketClient):
                # The websocket closed, ensure we set the done event.
                logger.debug(f"Elegoo {ipOrHostname} ws closed.")
                result["Event"].set()

            def onWsError(ws:IWebSocketClient, exception:Exception):
                # The websocket hit an error, ensure we set the done event.
                exceptionStr = str(exception)
                logger.debug(f"Elegoo {ipOrHostname} ws error. %s", exceptionStr)
                if "too many client" in exceptionStr.lower():
                    logger.debug(f"Elegoo {ipOrHostname} - too many clients error.")
                    result["TooManyClients"] = True
                result["Event"].set()

            # Try to connect, this will throw if it fails to find any server to connect to.
            failedToConnect = True
            url = f"ws://{ipOrHostname}:{port}/websocket"
            client:Client = None # pyright: ignore[reportAssignmentType]
            try:
                logger.debug(f"Connecting to Elegoo on {url}...")
                client = Client(url, onWsOpen=onWsOpen, onWsData=onWsData, onWsClose=onWsClose, onWsError=onWsError)
                # We must run async, so we don't block this testing thread.
                client.RunAsync()
                failedToConnect = False
            except Exception as e:
                logger.debug(f"Elegoo {url} - connection failure {e}")

            # Wait for the timeout.
            if not failedToConnect:
                result["Event"].wait(timeoutSec)

            # Ensure the websocket is closed.
            try:
                client.Close()
            except Exception as e:
                logger.debug(f"Elegoo {url} - close exception {e}")
            logger.debug(f"Connection exit for Elegoo on {url}")

            # Walk though the connection and see how far we got.
            wsConnected = False
            tooManyClients = False
            mainboardMac = None

            if "WsConnected" in result:
                wsConnected = True
            # This is special to Elegoo printers, it indicates there was a printer there, but there were too many active clients.
            if "TooManyClients" in result:
                tooManyClients = True
            # Get the mainboard id if we have it.
            if "MainboardMac" in result:
                mainboardMac = result["MainboardMac"]
            return NetworkValidationResult(isBambu=False, wsConnected=wsConnected, tooManyClients=tooManyClients, mainboardMac=mainboardMac)
        except Exception as e:
            return NetworkValidationResult(isBambu=False, exception=e)


    # Scans the IP subset for server instances.
    # testConFunction must be a function func(ip:str) -> NetworkValidationResult
    # Returns a list of IPs that reported Success() == True
    @staticmethod
    def _ScanForInstances(
            logger:logging.Logger,
            testConFunction:Callable[[str], NetworkValidationResult],
            returnAfterNumberFound:int=0,
            threadCount:Optional[int]=None,
            perThreadDelaySec:float=0.0,
            ipHint:Optional[str]=None
            ) -> List[str]: # type: ignore
        foundIps:List[str] = []
        try:
            # Try to get the local IP of this device. Note this will fail in docker.
            localIp = NetworkSearch._TryToGetLocalIp()
            if localIp is None or len(localIp) == 0:
                # If we failed to get it, check if we have an ip hint. If so, use it.
                if ipHint is not None and len(ipHint) > 0:
                    # If we have a hint, use it as the local IP.
                    logger.debug(f"Using IP hint as local IP: {ipHint}")
                    localIp = ipHint
                else:
                    logger.debug("Failed to get local IP")
                    return foundIps
            logger.debug(f"Local IP found as: {localIp}")
            if ":" in localIp:
                logger.info("IPv6 addresses aren't supported for local discovery.")
                return foundIps
            lastDot = localIp.rfind(".")
            if lastDot == -1:
                logger.info("Failed to find last dot in local IP?")
                return foundIps
            ipPrefix = localIp[:lastDot+1]

            # In the past, we did this wide with 255 threads.
            # We got some feedback that the system was hanging on lower powered systems, but then I also found a bug where
            # if an exception was thrown in the thread, it would hang the system.
            # We also saw on lower powered systems we hit max thread limits, since each of these thread might spawn threads in their tests.
            # I fixed that but also lowered the concurrent thread count to 50, which seems more comfortable.
            totalThreads = 50
            if threadCount is not None:
                totalThreads = threadCount

            # For the same reasons above, we always limit the number of threads on low resources devices.
            if NetworkSearch.IsLowResourceDevice():
                logger.debug("Low resource device detected, limiting threads to 30.")
                totalThreads = 30

            outstandingIpsToCheck:List[str] = []
            counter = 0
            while counter < 255:
                # The first IP will be 1, the last 255
                counter += 1
                outstandingIpsToCheck.append(ipPrefix + str(counter))

            # Start the threads
            # We must use arrays so they get captured by ref in the threads.
            doneThreads = [0]
            hasFoundRequestedNumberOfIps = [False]
            threadLock = threading.Lock()
            doneEvent = threading.Event()
            counter = 0
            while counter < totalThreads:
                def threadFunc(threadId:int):
                    ip = "none"
                    try:
                        # Loop until we run out of IPs or the test is done by the bool flag.
                        while True:
                            # Get the next IP
                            with threadLock:
                                # If there are no IPs left, this thread is done.
                                if len(outstandingIpsToCheck) == 0:
                                    # This will invoke the finally block.
                                    return
                                # If enough IPs have been found, we are done.
                                if hasFoundRequestedNumberOfIps[0] is True:
                                    return
                                # Get the next IP.
                                ip = outstandingIpsToCheck.pop(0)

                            # This is a quick fix to slow down the scan so it doesn't eat a lot of CPU load on the device while the printer is off
                            # and the plugin is trying to find it. But it's important this scan also be fast, for the installer.
                            if perThreadDelaySec > 0:
                                time.sleep(perThreadDelaySec)

                            # Outside of lock, test the IP
                            result = testConFunction(ip)

                            # re-lock and set the result.
                            with threadLock:
                                # If successful, add the IP to the found list.
                                if result.Success():
                                    # Ensure we haven't already found the requested number of IPs,
                                    # because then the result list might have already been returned
                                    # and we don't want to mess with it.
                                    if hasFoundRequestedNumberOfIps[0] is True:
                                        return

                                    # Add the IP to the list
                                    foundIps.append(ip)

                                    # Test if we have found all of the IPs we wanted to find.
                                    if returnAfterNumberFound != 0 and len(foundIps) >= returnAfterNumberFound:
                                        hasFoundRequestedNumberOfIps[0] = True
                                        # We set this now, which allows the function to return the result list
                                        # but the other threads will run until the current test ip is done.
                                        # That's ok since we protect the result list from being added to.
                                        doneEvent.set()
                    except Exception as e:
                        # Report the error.
                        logger.error(f"Server scan failed for {ip} "+str(e))
                    finally:
                        # Important - when we leave for any reason, mark this thread done.
                        with threadLock:
                            doneThreads[0] += 1
                            logger.debug(f"Thread {threadId} done. Done: {doneThreads[0]}; Total: {totalThreads}")
                            # If all of the threads are done, we are done.
                            if doneThreads[0] == totalThreads:
                                doneEvent.set()
                t = threading.Thread(target=threadFunc, args=(counter,))
                t.start()
                counter += 1
            doneEvent.wait()
            return foundIps
        except Exception as e:
            logger.error("Failed to scan for server instances. "+str(e))
        return foundIps


    @staticmethod
    def _TryToGetLocalIp() -> str:
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


    @staticmethod
    def IsLowResourceDevice() -> bool:
        try:
            # This only works on Linux
            result = os.popen('free -t -m').readlines()
            line = result[1]
            parts = line.split()
            totalRamMb = int(parts[1])
            # If the total memory is less than 1Gb, we consider this a low resource device.
            return totalRamMb < 1024
        except Exception:
            pass
        return False


    # This does Elegoo specific logic to scan for IPs on a network.
    # It needs to behave the same way that _ScanForInstances works.
    @staticmethod
    def _ScanForElegooOsIps(
            logger:logging.Logger,
            testConFunction:Callable[[str], NetworkValidationResult],
            returnAfterNumberFound:int=0,
            threadCount:Optional[int]=None,
            perThreadDelaySec:float=0.0,
            ipHint:Optional[str]=None
        ) -> List[str]:

        foundIps:List[str] = []
        try:
            # This function uses the UDP broadcast system to find any Elegoo printer on the network.
            # Doc https://github.com/cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0/blob/main/SDCP(Smart%20Device%20Control%20Protocol)_V3.0.0_EN.md#device-discovery-description
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

                # We want to send the message a few times, to ensure we hear all printers.
                # So we set the timeout to 1s, and broadcast 3 times.
                s.settimeout(1.0)
                attempt = 0
                while attempt < 3:
                    attempt += 1
                    # Send the discovery message
                    s.sendto(b"M99999", ("255.255.255.255", 3000))

                    # Wait for responses until the timeout.
                    try:
                        while True:
                            data, _ = s.recvfrom(1000)

                            # Try to process the response.
                            responseStr = data.decode("utf-8")
                            logger.debug(f"We got the following as a result of the elegoo udp discovery broadcast. {responseStr}")
                            response = json.loads(responseStr, strict=False)
                            data = response.get("Data", None)
                            if data is None:
                                logger.info(f"We got a response back from the elegoo broadcast, but it had no data object. {responseStr}")
                                continue
                            ip:Optional[str] = data.get("MainboardIP", None)
                            mainboardId:Optional[str] = data.get("MainboardID", None)
                            if ip is None or mainboardId is None:
                                logger.info(f"We got a response back from the elegoo broadcast, but it had no MainboardIP or MainboardID. {responseStr}")
                                continue

                            # Ensure we didn't discover this IP already successfully.
                            # Since we loop the broadcast, we will hear from each printer a few times.
                            alreadyFound = False
                            for foundIp in foundIps:
                                if ip == foundIp:
                                    alreadyFound = True
                                    break
                            if alreadyFound:
                                continue

                            # Since we are testing against the mainboard MAC, we need to use the Websocket connection function.
                            # This also ensures that our normal websocket will be able to connect, just like this one.
                            # ALSO - More than just CC 3D printers will response, some resin printers will as well. So we need to use the WS to ensure it's a CC.
                            # TODO - In the past we used the mainboard ID, but it looks like it changed. Maybe we can use it in the future?
                            result = testConFunction(ip)
                            if result.Success():
                                # Add the IP to the list.
                                foundIps.append(ip)
                                logger.debug(f"Found Elegoo OS printer at {ip} with mainboard id {mainboardId}")

                                # If we are looking for a specific mainboard id, check if it matches.
                                if returnAfterNumberFound != 0 and len(foundIps) >= returnAfterNumberFound:
                                    return foundIps

                            # Continue the search until we hit the read timeout.
                    except socket.timeout:
                        pass
        except Exception as e:
            logger.error(f"Failed to scan for Elegoo OS IPs: {e}")
        return foundIps
