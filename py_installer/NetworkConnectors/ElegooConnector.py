import json
import random
import threading
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt

from elegoo_cc2_octoeverywhere.elegoocc2discovery import ElegooCc2Discovery, ElegooCc2DiscoveryResult
from linux_host.config import Config
from linux_host.networksearch import NetworkSearch, NetworkValidationResult

from py_installer.Util import Util
from py_installer.Logging import Logger
from py_installer.Context import Context, ElegooPrinterProtocols
from py_installer.ConfigHelper import ConfigHelper


class _ElegooPrinterSetupResult:
    def __init__(
        self,
        protocol:str,
        ip:str,
        mainboardMac:Optional[str]=None,
        serialNumber:Optional[str]=None,
        accessCode:Optional[str]=None,
        tokenStatus:Optional[int]=None,
        lanStatus:Optional[int]=None,
        hostName:Optional[str]=None,
        machineModel:Optional[str]=None,
        tooManyClients:bool=False
    ) -> None:
        self.Protocol = protocol
        self.Ip = ip
        self.MainboardMac = mainboardMac
        self.SerialNumber = serialNumber
        self.AccessCode = accessCode
        self.TokenStatus = tokenStatus
        self.LanStatus = lanStatus
        self.HostName = hostName
        self.MachineModel = machineModel
        self.TooManyClients = tooManyClients


class _ElegooCc2ValidationResult:
    def __init__(
        self,
        failedToConnect:bool=False,
        failedAuth:bool=False,
        registered:bool=False,
        tooManyClients:bool=False,
        registrationError:Optional[str]=None,
        exception:Optional[Exception]=None
    ) -> None:
        self.FailedToConnect = failedToConnect
        self.FailedAuth = failedAuth
        self.Registered = registered
        self.TooManyClients = tooManyClients
        self.RegistrationError = registrationError
        self.Exception = exception


    def Success(self) -> bool:
        return (
            self.Exception is None
            and self.FailedToConnect is False
            and self.FailedAuth is False
            and self.TooManyClients is False
            and self.Registered
        )


# A class that helps the user discover, connect, and setup the details required to connect to a remote Elegoo printer.
class ElegooConnector:


    def EnsureElegooPrinterConnection(self, context:Context) -> None:
        Logger.Debug("Running elegoo connect ensure config logic.")

        if self._TryExistingElegooConnection(context):
            return

        result = self._SetupNewElegooConnection()
        self._WriteElegooConfig(context, result)

        printerType = "Elegoo Centauri Carbon 2" if result.Protocol == Config.ElegooPrinterProtocolCc2 else "Elegoo Centauri Carbon"
        Logger.Info(f"Your {printerType} printer was found and authentication was successful! IP: {result.Ip}")
        Logger.Blank()
        Logger.Header(f"{printerType} printer connection successful!")
        Logger.Blank()


    def _TryExistingElegooConnection(self, context:Context) -> bool:
        ip, port = ConfigHelper.TryToGetCompanionDetails(context)
        protocol = ConfigHelper.TryToGetElegooPrinterProtocol(context)
        mainboardMac = ConfigHelper.TryToGetElegooData(context)
        accessCode, printerSn = ConfigHelper.TryToGetElegooCc2Data(context)

        if protocol == Config.ElegooPrinterProtocolCc2:
            context.ElegooPrinterProtocol = ElegooPrinterProtocols.Cc2
            if ip is None or printerSn is None:
                return False
            return self._TryExistingElegooCc2Connection(context, ip, accessCode, printerSn)

        if protocol == Config.ElegooPrinterProtocolCc1 or mainboardMac is not None:
            context.ElegooPrinterProtocol = ElegooPrinterProtocols.Cc1
            if ip is None or port is None or mainboardMac is None:
                return False
            return self._TryExistingElegooCc1Connection(context, ip, mainboardMac)

        return False


    def _TryExistingElegooCc1Connection(self, context:Context, ip:str, mainboardMac:str) -> bool:
        Logger.Debug(f"Existing Elegoo config found. IP: {ip} - {mainboardMac}")
        Logger.Info(f"Checking if we can connect to your Elegoo printer at {ip}...")
        result:NetworkValidationResult = NetworkSearch.ValidateConnection_Elegoo(Logger.GetPyLogger(), ipOrHostname=ip, timeoutSec=10.0)

        # Validate - This should never be set.
        if result.IsBambu is True:
            Logger.Error("A non-elegoo result was returned when trying to connect to the printer.")

        # This is a special case - the elegoo printers have a limited number of connections possible.
        # So if we hit this, we connected to a WS on the unique port for the known IP, but we weren't able to authenticate.
        # We will assume this means the connection is still valid.
        if result.TooManyClients:
            Logger.Blank()
            Logger.Warn(f"We found your printer at {ip} but couldn't connect because too many clients are already connected.")
            Logger.Warn("You can keep the current Elegoo Connect printer setup or re-run the connection process.")
            Logger.Blank()
            if Util.AskYesOrNoQuestion("Do you want to set up the Elegoo printer connection again?") is False:
                Logger.Info(f"Keeping the existing Elegoo printer connection setup. {ip} - {mainboardMac}")
                context.ElegooPrinterProtocol = ElegooPrinterProtocols.Cc1
                ConfigHelper.WriteElegooPrinterProtocol(context, Config.ElegooPrinterProtocolCc1)
                return True
        elif result.Exception is not None or result.WsConnected is False:
            Logger.Blank()
            Logger.Warn(f"We failed to connect to your Elegoo printer at {ip}.")
            Logger.Warn("You can keep the current Elegoo Connect printer setup or re-run the connection process.")
            Logger.Blank()
            if Util.AskYesOrNoQuestion("Do you want to set up the Elegoo printer connection again?") is False:
                Logger.Info(f"Keeping the existing Elegoo printer connection setup. {ip} - {mainboardMac}")
                context.ElegooPrinterProtocol = ElegooPrinterProtocols.Cc1
                ConfigHelper.WriteElegooPrinterProtocol(context, Config.ElegooPrinterProtocolCc1)
                return True
        elif result.MainboardMac is not None and result.MainboardMac == mainboardMac:
            Logger.Info("Successfully connected to your Elegoo printer!")
            context.ElegooPrinterProtocol = ElegooPrinterProtocols.Cc1
            ConfigHelper.WriteElegooPrinterProtocol(context, Config.ElegooPrinterProtocolCc1)
            return True
        else:
            Logger.Warn(f"Found a printer on {ip}, but the mainboard ID was different. Expected: {mainboardMac}, Found: {result.MainboardMac}")
            Logger.Warn("Let's setup your Elegoo printer again.")
        return False


    def _TryExistingElegooCc2Connection(self, context:Context, ip:str, accessCode:Optional[str], printerSn:str) -> bool:
        Logger.Debug(f"Existing Elegoo Centauri Carbon 2 config found. IP: {ip} - {printerSn}")
        Logger.Info(f"Checking if we can connect to your Elegoo Centauri Carbon 2 printer at {ip}...")

        discovery = self._DiscoverCc2AtIp(ip)
        if discovery is not None:
            if discovery.LanStatus == 0:
                self._ShowCc2LanOnlyModeRequired(ip)
                return False
            if discovery.SerialNumber is not None and discovery.SerialNumber != printerSn:
                Logger.Warn(f"Found an Elegoo Centauri Carbon 2 printer on {ip}, but the serial number was different. Expected: {printerSn}, Found: {discovery.SerialNumber}")
                Logger.Warn("Let's setup your Elegoo Centauri Carbon 2 printer again.")
                return False
            if accessCode is None and discovery.TokenStatus == 1:
                accessCode = self._AskForCc2AccessCode()
            elif accessCode is None:
                accessCode = NetworkSearch.c_ElegooCc2DefaultAccessCode
        elif accessCode is None:
            accessCode = NetworkSearch.c_ElegooCc2DefaultAccessCode

        validation = self._ValidateCc2MqttConnection(ip, printerSn, accessCode)
        if validation.Success():
            Logger.Info("Successfully connected to your Elegoo Centauri Carbon 2 printer!")
            result = _ElegooPrinterSetupResult(
                Config.ElegooPrinterProtocolCc2,
                ip,
                serialNumber=printerSn,
                accessCode=accessCode
            )
            self._WriteElegooConfig(context, result)
            return True

        Logger.Blank()
        if validation.TooManyClients:
            Logger.Warn(f"We found your Elegoo CC 2 printer at {ip} but couldn't register because too many clients are already connected.")
        elif validation.FailedAuth:
            Logger.Warn(f"We failed to authenticate to your Elegoo CC 2 printer at {ip}.")
        else:
            Logger.Warn(f"We failed to connect to your Elegoo CC 2 printer at {ip}.")
        Logger.Warn("You can keep the current Elegoo CC 2 printer setup or re-run the connection process.")
        Logger.Blank()
        if Util.AskYesOrNoQuestion("Do you want to set up your Elegoo CC 2 printer connection again?") is False:
            Logger.Info(f"Keeping the existing Elegoo CC 2 printer connection setup. {ip} - {printerSn}")
            context.ElegooPrinterProtocol = ElegooPrinterProtocols.Cc2
            ConfigHelper.WriteElegooPrinterProtocol(context, Config.ElegooPrinterProtocolCc2)
            return True
        return False


    def _WriteElegooConfig(self, context:Context, result:_ElegooPrinterSetupResult) -> None:
        if result.Protocol == Config.ElegooPrinterProtocolCc2:
            if result.SerialNumber is None or result.AccessCode is None:
                raise Exception("Elegoo Centauri Carbon 2 setup result was missing serial number or access code.")
            context.ElegooPrinterProtocol = ElegooPrinterProtocols.Cc2
            ConfigHelper.WriteCompanionDetails(context, result.Ip, NetworkSearch.c_ElegooCc2DefaultMqttPortStr)
            ConfigHelper.WriteElegooCc2Details(context, result.AccessCode, result.SerialNumber)
            return

        if result.MainboardMac is None:
            raise Exception("Elegoo setup result was missing mainboard id.")
        context.ElegooPrinterProtocol = ElegooPrinterProtocols.Cc1
        ConfigHelper.WriteCompanionDetails(context, result.Ip, NetworkSearch.c_ElegooDefaultPortStr)
        ConfigHelper.WriteElegooDetails(context, result.MainboardMac)


    # Shows the user a message that there are too many clients connected to the printer and how to fix it.
    def _ShowTooManyClientsError(self, ip:str) -> None:
        Logger.Blank()
        Logger.Blank()
        Logger.Warn(f"We found an Elegoo printer on your network at {ip}, but we couldn't connect to it because there are too many existing connections.")
        Logger.Info("Elegoo printers have a maximum number of connections that can be made to the printer at once.")
        Logger.Info("Before you can complete the OctoEverywhere setup, we need to allow the installer to connect.")
        Logger.Blank()
        Logger.Info("To close existing connections, try:")
        Logger.Info("   - Ensure you don't have the printer control webpage open in any open web browser.")
        Logger.Info("   - Ensure you aren't on the 'Device' tab in the Elegoo slicer.")
        Logger.Info("   - Restart the printer, to close old connections.")
        Logger.Blank()
        Logger.Info("Once you have closed existing connections, press y to try the Elegoo Connection connection again.")
        while True:
            Logger.Blank()
            if Util.AskYesOrNoQuestion("Would you like to try connecting again now?"):
                break
            Logger.Blank()
            Logger.Error("The Elegoo Connect installer must connect to your printer to complete the secure connection.")
            Logger.Warn("If you want to exit the setup and continue later, hold the control and press C.")
            Logger.Blank()
            Logger.Info("If need help, contact our support team support@octoeverywhere.com or join our Discord for help!")
            Logger.Blank()


    def _ShowCc2LanOnlyModeRequired(self, ip:str) -> None:
        Logger.Blank()
        Logger.Blank()
        Logger.Warn(f"We found an Elegoo Centauri Carbon 2 printer at {ip}, but it is NOT in LAN-only mode.")
        Logger.Info("OctoEverywhere Elegoo Connect requires LAN-only mode so it can securely connect directly to the printer over your network.")
        Logger.Blank()
        Logger.Info("To enable LAN-only mode, follow these steps:")
        Logger.Info("   - On the printer's touch screen, tap the gear icon to open the settings menu.")
        Logger.Info("   - Make sure your on the 'Settings' tab along the top and scroll in the list for 'LAN Only'")
        Logger.Info("   - In the Lan Only settings, enable the toggle for LAN Only mode.")
        Logger.Info("   - Take a picture or write down the Access Code.")
        Logger.Blank()
        while True:
            Logger.Blank()
            if Util.AskYesOrNoQuestion("Would you like to try connecting again now?"):
                break
            Logger.Blank()
            Logger.Error("The Elegoo Centauri Carbon 2 installer must use LAN-only mode to complete setup.")
            Logger.Warn("If you want to exit the setup and continue later, hold the control and press C.")
            Logger.Blank()


    # Helps the user setup an Elegoo connection via auto scanning or manual setup.
    def _SetupNewElegooConnection(self) -> _ElegooPrinterSetupResult:
        while True:
            Logger.Blank()
            Logger.Blank()
            Logger.Blank()
            Logger.Header("##################################")
            Logger.Header("      Elegoo Printer Setup")
            Logger.Header("##################################")
            Logger.Blank()

            Logger.Blank()
            Logger.Blank()
            Logger.Warn("Searching for Elegoo Centauri Carbon and Elegoo Centauri Carbon 2 printers on your network, this will take about 10 seconds...")
            results = self._ScanForElegooPrinters()

            reTryAuto = False
            if len(results) == 1:
                result = self._TrySetupDiscoveredElegooPrinter(results[0])
                if result is not None:
                    return result
                reTryAuto = True

            elif len(results) > 1:
                Logger.Blank()
                Logger.Blank()
                Logger.Info("We found the following Elegoo printers on your network:")
                count = 0
                for result in results:
                    count += 1
                    Logger.Info(f"   {count}) {self._GetElegooResultDisplayStr(result)}")
                Logger.Info("   m) Press `m` to enter the IP address manually")

                Logger.Blank()
                while True:
                    try:
                        i = input("Please select the printer number above you want to connect to this plugin: ")
                        if i == "m" or i == "M":
                            break

                        selection = int(i)
                        if selection < 1 or selection > len(results):
                            raise ValueError()
                        setupResult = self._TrySetupDiscoveredElegooPrinter(results[selection - 1])
                        if setupResult is not None:
                            return setupResult
                        reTryAuto = True
                        break
                    except ValueError:
                        Logger.Error("Invalid selection, please enter a number from the list.")
                        continue

            if reTryAuto:
                continue

            manualResult = self._SetupElegooConnectionManually()
            if manualResult is not None:
                return manualResult


    def _ScanForElegooPrinters(self) -> List[_ElegooPrinterSetupResult]:
        results:List[_ElegooPrinterSetupResult] = []
        cc2Results:List[_ElegooPrinterSetupResult] = []
        cc1Results:List[_ElegooPrinterSetupResult] = []

        def scanCc2() -> None:
            for result in ElegooCc2Discovery.Discover(Logger.GetPyLogger(), timeoutSec=10.0):
                cc2Results.append(self._ConvertCc2DiscoveryResult(result))

        def scanCc1() -> None:
            for result in NetworkSearch.ScanForInstances_Elegoo(Logger.GetPyLogger()):
                cc1Results.append(_ElegooPrinterSetupResult(
                    Config.ElegooPrinterProtocolCc1,
                    result.Ip,
                    mainboardMac=result.MainboardMac,
                    tooManyClients=result.TooManyClients
                ))

        cc2Thread = threading.Thread(target=scanCc2, name="ElegooCc2InstallerDiscovery")
        cc1Thread = threading.Thread(target=scanCc1, name="ElegooCc1InstallerDiscovery")
        cc2Thread.start()
        cc1Thread.start()
        cc2Thread.join()
        cc1Thread.join()

        results.extend(cc2Results)
        for result in cc1Results:
            if self._IsIpAlreadyInResults(results, result.Ip):
                continue
            results.append(result)

        return results


    def _IsIpAlreadyInResults(self, results:List[_ElegooPrinterSetupResult], ip:str) -> bool:
        for existing in results:
            if existing.Ip == ip:
                return True
        return False


    def _ConvertCc2DiscoveryResult(self, result:ElegooCc2DiscoveryResult) -> _ElegooPrinterSetupResult:
        return _ElegooPrinterSetupResult(
            Config.ElegooPrinterProtocolCc2,
            result.Ip,
            serialNumber=result.SerialNumber,
            tokenStatus=result.TokenStatus,
            lanStatus=result.LanStatus,
            hostName=result.HostName,
            machineModel=result.MachineModel
        )


    def _TrySetupDiscoveredElegooPrinter(self, result:_ElegooPrinterSetupResult) -> Optional[_ElegooPrinterSetupResult]:
        if result.Protocol == Config.ElegooPrinterProtocolCc2:
            return self._TrySetupDiscoveredElegooCc2Printer(result)
        return self._TrySetupDiscoveredElegooCc1Printer(result)


    def _TrySetupDiscoveredElegooCc1Printer(self, result:_ElegooPrinterSetupResult) -> Optional[_ElegooPrinterSetupResult]:
        if result.TooManyClients:
            self._ShowTooManyClientsError(result.Ip)
            return None
        if result.MainboardMac is not None:
            Logger.Info(f"Found your Elegoo printer on your network at {result.Ip}.")
            return result
        Logger.Info("The selected printer had no mainboard ID, going to manual setup.")
        return None


    def _TrySetupDiscoveredElegooCc2Printer(self, result:_ElegooPrinterSetupResult) -> Optional[_ElegooPrinterSetupResult]:
        if result.LanStatus == 0:
            self._ShowCc2LanOnlyModeRequired(result.Ip)
            return None

        if result.SerialNumber is None or len(result.SerialNumber) == 0:
            Logger.Error(f"We found an Elegoo Centauri Carbon 2 printer at {result.Ip}, but it didn't report a serial number.")
            return None

        accessCode = self._GetCc2AccessCodeForPrinter(result)
        while True:
            Logger.Blank()
            Logger.Info(f"Trying to connect to your Elegoo Centauri Carbon 2 printer at {result.Ip}...")
            validation = self._ValidateCc2MqttConnection(result.Ip, result.SerialNumber, accessCode)
            Logger.Blank()

            if validation.Success():
                result.AccessCode = accessCode
                Logger.Info(f"Found your Elegoo Centauri Carbon 2 printer on your network at {result.Ip}.")
                return result

            if validation.TooManyClients:
                self._ShowTooManyClientsError(result.Ip)
                return None

            if validation.FailedAuth:
                Logger.Error("Failed to authenticate to your Elegoo Centauri Carbon 2 printer. The Access Code was incorrect.")
                accessCode = self._AskForCc2AccessCode()
                continue

            Logger.Error("Failed to connect to your Elegoo Centauri Carbon 2 printer, ensure the printer is powered on and connected to your network.")
            return None


    def _SetupElegooConnectionManually(self) -> Optional[_ElegooPrinterSetupResult]:
        while True:
            Logger.Blank()
            Logger.Blank()
            Logger.Info("We cannot automatically detect your printer, so we need to enter the IP address manually. (don't worry, it's easy!)")
            Logger.Blank()
            Logger.Info("Use the display on your Elegoo 3D printer to find your IP address by following these steps:")
            Logger.Info("   - Press the gear icon in the vertical main menu icon.")
            Logger.Info("   - Press the 'Network' tab at the top of the screen.")
            Logger.Info("   - Ensure Wi-Fi is on and the printer is connected to your network.")
            Logger.Info("   - The IP address is under the connected network.")
            Logger.Blank()
            Logger.Info("The IP address format is numerical, typically in the format xxx.xxx.xxx.xxx, such as 192.168.1.15 or 10.0.0.122")
            Logger.Blank()
            Logger.Info("If you need help finding your printer's IP address, we have an in-depth guide with images:")
            Logger.Info("https://octoeverywhere.com/s/elegoo-ip")
            Logger.Blank()
            ip = input("Enter your printer's IP Address: ")
            ip = ip.strip()
            Logger.Blank()
            Logger.Info("Trying to connect to your printer...")

            cc2Discovery = self._DiscoverCc2AtIp(ip)
            if cc2Discovery is not None:
                setupResult = self._TrySetupDiscoveredElegooCc2Printer(self._ConvertCc2DiscoveryResult(cc2Discovery))
                if setupResult is not None:
                    return setupResult
                continue

            result = NetworkSearch.ValidateConnection_Elegoo(Logger.GetPyLogger(), ip, timeoutSec=5.0)
            Logger.Blank()
            Logger.Blank()

            if result.MainboardMac is not None:
                Logger.Info(f"Found your Elegoo printer on your network at {ip}.")
                return _ElegooPrinterSetupResult(Config.ElegooPrinterProtocolCc1, ip, mainboardMac=result.MainboardMac)

            if result.TooManyClients:
                self._ShowTooManyClientsError(ip)
                continue

            Logger.Error("Failed to connect to your Elegoo printer, ensure the IP address is correct and the printer is connected to the network.")
            continue


    def _DiscoverCc2AtIp(self, ip:str) -> Optional[ElegooCc2DiscoveryResult]:
        results = ElegooCc2Discovery.Discover(Logger.GetPyLogger(), ip, timeoutSec=3.0)
        if len(results) == 0:
            return None
        return results[0]


    def _GetCc2AccessCodeForPrinter(self, result:_ElegooPrinterSetupResult) -> str:
        if result.AccessCode is not None:
            return result.AccessCode
        if result.TokenStatus == 1:
            return self._AskForCc2AccessCode()
        # If the user turns off the access code, this is the default code.
        return NetworkSearch.c_ElegooCc2DefaultAccessCode


    def _AskForCc2AccessCode(self) -> str:
        while True:
            Logger.Blank()
            Logger.Header("We found your Elegoo Centauri Carbon 2! Enter the printer's Access Code to connect.")
            Logger.Blank()
            Logger.Info("To find your Elegoo Centauri Carbon 2, follow these steps:")
            Logger.Info("   - On the printer's touch screen, tap the gear icon to open the settings menu.")
            Logger.Info("   - Make sure your on the 'Settings' tab along the top and scroll in the list for 'LAN Only'")
            Logger.Info("   - In the LAN Only section, you will find the Access Code.")
            Logger.Blank()
            accessCode = input("Enter your printer's Access Code: ")
            accessCode = accessCode.strip()
            if len(accessCode) > 0:
                return accessCode
            Logger.Error("The Access Code can't be empty.")


    def _GetElegooResultDisplayStr(self, result:_ElegooPrinterSetupResult) -> str:
        if result.Protocol == Config.ElegooPrinterProtocolCc2:
            name = "Elegoo Centauri Carbon 2"
            if result.MachineModel is not None:
                name = result.MachineModel
            elif result.HostName is not None:
                name = result.HostName
            serial = result.SerialNumber if result.SerialNumber is not None else "Unknown Serial"
            mode = "Unknown network mode"
            if result.LanStatus == 1:
                mode = "LAN-only"
            elif result.LanStatus == 0:
                mode = "Cloud mode"
            return f"{name} - {result.Ip} - {serial} - {mode}"

        if result.TooManyClients:
            return f"Elegoo CC1 - {result.Ip} - Couldn't connect, too many connections."
        return f"Elegoo CC1 - {result.Ip} - {result.MainboardMac}"


    def _ValidateCc2MqttConnection(self, ipOrHostname:str, printerSn:str, accessCode:str, timeoutSec:float=10.0) -> _ElegooCc2ValidationResult:
        client:Optional[mqtt.Client] = None
        try:
            result:Dict[str, Any] = {}
            result["Event"] = threading.Event()
            clientId = f"1_PC_{random.randint(1000, 9999)}"
            requestId = f"{clientId}_req"

            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=clientId, userdata=result) # pyright: ignore[reportPrivateImportUsage]
            client.username_pw_set("elegoo", accessCode)

            def connect(client:mqtt.Client, userdata:Dict[str, Any], flags:Any, reason_code:mqtt.ReasonCode, properties:Any): # pyright: ignore[reportPrivateImportUsage]
                if reason_code.is_failure:
                    Logger.Debug(f"Elegoo Centauri Carbon 2 {ipOrHostname} MQTT connection failure: {reason_code}")
                    userdata["FailedAuth"] = True
                    userdata["Event"].set()
                    client.disconnect()
                    return

                Logger.Debug(f"Elegoo Centauri Carbon 2 {ipOrHostname} MQTT connected.")
                userdata["Connected"] = True
                topic = f"elegoo/{printerSn}/{requestId}/register_response"
                (subscribeResult, mid) = client.subscribe(topic)
                if subscribeResult != mqtt.MQTT_ERR_SUCCESS or mid is None:
                    Logger.Debug(f"Elegoo Centauri Carbon 2 {ipOrHostname} failed to subscribe to register response topic.")
                    userdata["Event"].set()
                    client.disconnect()
                    return
                userdata["RegisterSubscribeMid"] = mid

            def subscribe(client:mqtt.Client, userdata:Dict[str, Any], mid:Any, reason_code_list:List[mqtt.ReasonCode], properties:Any): # pyright: ignore[reportPrivateImportUsage]
                if userdata.get("RegisterSubscribeMid", None) != mid:
                    return

                for r in reason_code_list:
                    if r.is_failure:
                        Logger.Debug(f"Elegoo Centauri Carbon 2 {ipOrHostname} register response subscribe failed. {r}")
                        userdata["Event"].set()
                        client.disconnect()
                        return

                client.publish(
                    f"elegoo/{printerSn}/api_register",
                    json.dumps({"client_id": clientId, "request_id": requestId})
                )

            def message(client:mqtt.Client, userdata:Dict[str, Any], mqttMsg:mqtt.MQTTMessage):
                try:
                    msg = json.loads(mqttMsg.payload)
                    error = str(msg.get("error", "fail")).lower()
                    userdata["RegistrationError"] = error
                    if error == "ok":
                        userdata["Registered"] = True
                    elif "too many" in error:
                        userdata["TooManyClients"] = True
                    Logger.Debug(f"Elegoo Centauri Carbon 2 {ipOrHostname} registration response: {error}")
                except Exception as e:
                    Logger.Debug(f"Elegoo Centauri Carbon 2 {ipOrHostname} registration response parse failed. {e}")
                finally:
                    userdata["Event"].set()
                    client.disconnect()

            def disconnect(client:Any, userdata:Dict[str, Any], disconnect_flags:Any, reason_code:Any, properties:Any):
                Logger.Debug(f"Elegoo Centauri Carbon 2 {ipOrHostname} MQTT disconnected.")
                userdata["Event"].set()

            client.on_connect = connect
            client.on_subscribe = subscribe
            client.on_message = message
            client.on_disconnect = disconnect

            failedToConnect = True
            try:
                Logger.Debug(f"Connecting to Elegoo Centauri Carbon 2 on {ipOrHostname}:{NetworkSearch.c_ElegooCc2DefaultMqttPortStr}...")
                client.connect(ipOrHostname, int(NetworkSearch.c_ElegooCc2DefaultMqttPortStr), keepalive=30)
                failedToConnect = False
                client.loop_start()
            except Exception as e:
                Logger.Debug(f"Elegoo Centauri Carbon 2 {ipOrHostname} MQTT connection failure {e}")

            if not failedToConnect:
                result["Event"].wait(timeoutSec)

            return _ElegooCc2ValidationResult(
                failedToConnect=failedToConnect,
                failedAuth=bool(result.get("FailedAuth", False)),
                registered=bool(result.get("Registered", False)),
                tooManyClients=bool(result.get("TooManyClients", False)),
                registrationError=result.get("RegistrationError", None)
            )
        except Exception as e:
            return _ElegooCc2ValidationResult(exception=e)
        finally:
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    pass
                try:
                    client.loop_stop()
                except Exception:
                    pass
