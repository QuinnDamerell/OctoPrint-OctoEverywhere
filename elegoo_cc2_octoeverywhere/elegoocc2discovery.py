import json
import logging
import socket
import time
from typing import Any, Dict, List, Optional

from octoeverywhere.sentry import Sentry


class ElegooCc2DiscoveryResult:
    def __init__(self, ip:str, serialNumber:Optional[str], tokenStatus:Optional[int], lanStatus:Optional[int], hostName:Optional[str], machineModel:Optional[str]) -> None:
        self.Ip = ip
        self.SerialNumber = serialNumber
        self.TokenStatus = tokenStatus
        self.LanStatus = lanStatus
        self.HostName = hostName
        self.MachineModel = machineModel


class ElegooCc2Discovery:

    c_DiscoveryPort = 52700

    @staticmethod
    def Discover(logger:logging.Logger, ipOrHostname:Optional[str]=None, timeoutSec:float=3.0) -> List[ElegooCc2DiscoveryResult]:
        results:List[ElegooCc2DiscoveryResult] = []
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.settimeout(timeoutSec)

                target = "255.255.255.255" if ipOrHostname is None else ipOrHostname
                payload = json.dumps({"id": 0, "method": 7000}).encode("utf-8")
                s.sendto(payload, (target, ElegooCc2Discovery.c_DiscoveryPort))

                start = time.time()
                while time.time() - start < timeoutSec:
                    try:
                        data, addr = s.recvfrom(4096)
                    except socket.timeout:
                        break

                    result = ElegooCc2Discovery._ParseDiscoveryResponse(logger, data, addr[0])
                    if result is None:
                        continue

                    alreadyFound = False
                    for existing in results:
                        if existing.Ip == result.Ip or (existing.SerialNumber is not None and existing.SerialNumber == result.SerialNumber):
                            alreadyFound = True
                            break
                    if alreadyFound is False:
                        results.append(result)

                    # Directed discovery only expects one response.
                    if ipOrHostname is not None:
                        break
        except Exception as e:
            Sentry.OnExceptionNoSend("Elegoo CC2 discovery failed.", e)
            logger.debug("Elegoo CC2 discovery failed. %s", e)
        return results


    @staticmethod
    def _ParseDiscoveryResponse(logger:logging.Logger, data:bytes, ip:str) -> Optional[ElegooCc2DiscoveryResult]:
        try:
            msg:Dict[str, Any] = json.loads(data.decode("utf-8"))
            result = msg.get("result", None)
            if not isinstance(result, dict):
                logger.debug("Elegoo CC2 discovery response missing result object: %s", msg)
                return None
            return ElegooCc2DiscoveryResult(
                ip,
                result.get("sn", None),
                result.get("token_status", None),
                result.get("lan_status", None),
                result.get("host_name", None),
                result.get("machine_model", None),
            )
        except Exception as e:
            logger.debug("Elegoo CC2 discovery parse failed. %s", e)
            return None
