import ssl
import json
import socket
import logging
import threading
from typing import List

import paho.mqtt.client as mqtt

class NetworkValidationResult:
    def __init__(self, failedToConnect:bool = False, failedAuth:bool = False, failSn:bool = False, exception:Exception = None, bambuRtspUrl = None) -> None:
        self.FailedToConnect = failedToConnect
        self.FailedAuth = failedAuth
        self.FailedSerialNumber = failSn
        self.Exception = exception
        # Only used for Bambu printers.
        # If none, the printer doesn't support RTSP.
        # If empty string, the LAN Mode Liveview is not turned on.
        # If a URL, the printer is ready to stream.
        self.BambuRtspUrl = bambuRtspUrl


    def Success(self) -> bool:
        return not self.FailedToConnect and not self.FailedAuth and not self.FailedSerialNumber and self.Exception is None


# A helper class that allows for validating and or searching for Moonraker or Bambu printers on the local LAN.
class NetworkSearch:

    # The default port all Bambu printers will run MQTT on.
    c_BambuDefaultPortStr = "8883"


    # Scans the local IP LAN subset for Bambu servers that successfully authorize given the access code and printer sn.
    @staticmethod
    def ScanForInstances_Bambu(logger:logging.Logger, accessCode:str, printerSn:str, portStr:str = None) -> List[str]:
        def callback(ip:str):
            return NetworkSearch.ValidateConnection_Bambu(logger, ip, accessCode, printerSn, portStr, timeoutSec=5)
        # We want to return if any one IP is found, since there can only be one printer that will match the printer 100% correct.
        return NetworkSearch._ScanForInstances(logger, callback, returnAfterNumberFound=1)


    # The final two steps can happen in different orders, so we need to wait for both the sub success and state object to be received.
    @staticmethod
    def _BambuConnectionDone(data:dict, client:mqtt.Client) -> bool:
        if "SnSubSuccess" in data and data["SnSubSuccess"] is True and "GotStateObj" in data and data["GotStateObj"] is True:
            data["Event"].set()
            client.disconnect()
            return True
        return False


    # Given the ip, accessCode, printerSn, and optionally port, this will check if the printer is connectable.
    # Returns a NetworkValidationResult with the results.
    @staticmethod
    def ValidateConnection_Bambu(logger:logging.Logger, ipOrHostname:str, accessCode:str, printerSn:str, portStr:str = None, timeoutSec:float = 5.0) -> NetworkValidationResult:
        client:mqtt.Client = None
        try:
            if portStr is None:
                portStr = NetworkSearch.c_BambuDefaultPortStr
            port = int(portStr)
            logger.debug(f"Testing for Bambu on {ipOrHostname}:{port}")
            result = {}
            result["Event"] = threading.Event()
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata=result)
            client.tls_set(tls_version=ssl.PROTOCOL_TLS, cert_reqs=ssl.CERT_NONE)
            client.tls_insecure_set(True)
            client.username_pw_set("bblp", accessCode)

            def connect(client:mqtt.Client, userdata:dict, flags, reason_code:mqtt.ReasonCode, properties):
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

            def disconnect(client, userdata:dict, disconnect_flags, reason_code, properties):
                logger.debug(f"Bambu {ipOrHostname} disconnected.")
                userdata["Event"].set()

            def subscribe(client, userdata:dict, mid, reason_code_list:List[mqtt.ReasonCode], properties):
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

            def message(client, userdata:dict, mqttMsg:mqtt.MQTTMessage):
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

            return NetworkValidationResult(failedToConnect, failedAuth, failedSn, bambuRtspUrl=rtspUrl)

        except Exception as e:
            return NetworkValidationResult(exception=e)
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


    # Scans the IP subset for server instances.
    # testConFunction must be a function func(ip:str) -> NetworkValidationResult
    # Returns a list of IPs that reported Success() == True
    @staticmethod
    def _ScanForInstances(logger:logging.Logger, testConFunction, returnAfterNumberFound = 0) -> List[str]:
        foundIps = []
        try:
            localIp = NetworkSearch._TryToGetLocalIp()
            if localIp is None or len(localIp) == 0:
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
            # I fixed that but also lowered the concurrent thread count to 100, which seems more comfortable.
            totalThreads = 100
            outstandingIpsToCheck = []
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
                def threadFunc(threadId):
                    try:
                        # Loop until we run out of IPs or the test is done by the bool flag.
                        while True:
                            # Get the next IP
                            ip = "none"
                            with threadLock:
                                # If there are no IPs left, this thread is done.
                                if len(outstandingIpsToCheck) == 0:
                                    # This will invoke the finally block.
                                    return
                                # If enough IPs have been found, we are done.
                                if hasFoundRequestedNumberOfIps[0] is True:
                                    return
                                # Get the next IP.
                                ip = outstandingIpsToCheck.pop()

                            # Outside of lock, test the IP
                            result = testConFunction(ip)

                            # re-lock and set the result.
                            with threadLock:
                                # If successful, add the IP to the found list.
                                if result.Success():
                                    # Enure we haven't already found the requested number of IPs,
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
        return ip
