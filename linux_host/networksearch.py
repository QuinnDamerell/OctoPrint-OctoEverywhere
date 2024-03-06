import ssl
import socket
import logging
import threading
from typing import List

import paho.mqtt.client as mqtt

class NetworkValidationResult:
    def __init__(self, failedToConnect:bool = False, failedAuth:bool = False, failSn:bool = False, exception:Exception = None) -> None:
        self.FailedToConnect = failedToConnect
        self.FailedAuth = failedAuth
        self.FailedSerialNumber = failSn
        self.Exception = exception


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
        return NetworkSearch._ScanForInstances(logger, callback)


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
                        userdata["SnSubSuccess"] = True
                        logger.debug(f"Bambu {ipOrHostname} Sub success, the serial number is good")
                    client.disconnect()
                    userdata["Event"].set()

            # Setup functions and connect.
            client.on_connect = connect
            client.on_disconnect = disconnect
            client.on_subscribe = subscribe

            # Try to connect, this will throw if it fails to find any server to connect to.
            failedToConnect = True
            try:
                client.connect(ipOrHostname, port, keepalive=60)
                failedToConnect = False
                client.loop_start()
            except Exception as e:
                logger.debug(f"Bambu {ipOrHostname} - connection failure {e}")

            # Wait for the timeout.
            if not failedToConnect:
                result["Event"].wait(timeoutSec)

            # Walk though the connection and see how far we got.
            failedAuth = True
            failedSn = True
            if "IsAuthorized" in result:
                failedAuth = False
            if "SnSubSuccess" in result:
                failedSn = False

            return NetworkValidationResult(failedToConnect, failedAuth, failedSn)

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
    def _ScanForInstances(logger:logging.Logger, testConFunction) -> List[str]:
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

            counter = 0
            doneThreads = [0]
            totalThreads = 255
            threadLock = threading.Lock()
            doneEvent = threading.Event()
            while counter <= totalThreads:
                fullIp = ipPrefix + str(counter)
                def threadFunc(ip):
                    try:
                        result = testConFunction(ip)
                        with threadLock:
                            if result.Success():
                                foundIps.append(ip)
                            doneThreads[0] += 1
                            if doneThreads[0] == totalThreads:
                                doneEvent.set()
                    except Exception as e:
                        logger.error(f"Server scan failed for {ip} "+str(e))
                t = threading.Thread(target=threadFunc, args=[fullIp])
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
