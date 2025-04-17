from typing import Tuple
from linux_host.networksearch import NetworkSearch, NetworkValidationResult

from py_installer.Util import Util
from py_installer.Logging import Logger
from py_installer.Context import Context
from py_installer.ConfigHelper import ConfigHelper

# A class that helps the user discover, connect, and setup the details required to connect to a remote Elegoo OS printer.
class ElegooConnector:

    # The default port for Elegoo printers.
    c_ElegooDefaultPort = 3030


    def EnsureElegooPrinterConnection(self, context:Context) -> None:
        Logger.Debug("Running elegoo connect ensure config logic.")

        # For Elegoo printers, we need the IP or Hostname, the port is static, and the mainboard ID.
        ip, port = ConfigHelper.TryToGetCompanionDetails(context)
        mainboardMac = ConfigHelper.TryToGetElegooData(context)
        if ip is not None and port is not None and mainboardMac is not None:
            # Check if we can still connect. This can happen if the IP address changes, the user might need to setup the printer again.
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
                # Let the user keep this connection setup, or try to set it up again.
                Logger.Blank()
                Logger.Warn(f"We found your printer at {ip} but couldn't connect because too many clients are already connected.")
                Logger.Warn("You can keep the current Elegoo Connect printer setup or re-run the connection process.")
                Logger.Blank()
                if Util.AskYesOrNoQuestion("Do you want to set up the Elegoo printer connection again?") is False:
                    Logger.Info(f"Keeping the existing Elegoo printer connection setup. {ip} - {mainboardMac}")
                    return
            # If there was an exception or we never connected to the WS, we should ask the user if they want to try again.
            elif result.Exception is not None or result.WsConnected is False:
                # Let the user keep this connection setup, or try to set it up again.
                Logger.Blank()
                Logger.Warn(f"We failed to connect to your Elegoo printer at {ip}.")
                Logger.Warn("You can keep the current Elegoo Connect printer setup or re-run the connection process.")
                Logger.Blank()
                if Util.AskYesOrNoQuestion("Do you want to set up the Elegoo printer connection again?") is False:
                    Logger.Info(f"Keeping the existing Elegoo printer connection setup. {ip} - {mainboardMac}")
                    return
            elif (result.MainboardMac is not None and result.MainboardMac == mainboardMac):
                Logger.Info("Successfully connected to you Elegoo printer!")
                return
            else:
                # This means we found a printer on this ip, but the mainboard id was different?
                Logger.Warn(f"Found a printer on {ip}, but the mainboard ID was different. Expected: {mainboardMac}, Found: {result.MainboardMac}")
                Logger.Warn("Let's setup your Elegoo printer again.")

        ipOrHostname, mainboardMac = self._SetupNewElegooConnection(context)
        Logger.Info(f"You Elegoo printer was found and authentication was successful! IP: {ipOrHostname}")

        ConfigHelper.WriteCompanionDetails(context, ipOrHostname, NetworkSearch.c_ElegooDefaultPortStr)
        ConfigHelper.WriteElegooDetails(context, mainboardMac)
        Logger.Blank()
        Logger.Header("Elegoo printer connection successful!")
        Logger.Blank()


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


    # Helps the user setup a bambu connection via auto scanning or manual setup.
    # Returns (ip:str, mainboard:str)
    def _SetupNewElegooConnection(self, context:Context) -> Tuple[str, str]:
        while True:
            Logger.Blank()
            Logger.Blank()
            Logger.Blank()
            Logger.Header("##################################")
            Logger.Header("      Elegoo Printer Setup")
            Logger.Header("##################################")
            Logger.Blank()

            # Scan for the local IP subset for possible matches.
            Logger.Blank()
            Logger.Blank()
            Logger.Warn("Searching for your Elegoo printer on your network, this will take about 5 seconds...")
            results = NetworkSearch.ScanForInstances_Elegoo(Logger.GetPyLogger())

            # If there's only one result, handle things differently to make it easier.
            reTryAuto = False
            if len(results) == 1:
                result = results[0]
                if result.TooManyClients:
                    self._ShowTooManyClientsError(result.Ip)
                    continue
                # If we have a mainboard id, we are good to go.
                if result.MainboardMac is not None:
                    Logger.Info(f"Found your Elegoo printer on your network at {result.Ip}.")
                    return (result.Ip, result.MainboardMac)

            elif len(results) > 1:
                # Handle multiple results.
                Logger.Blank()
                Logger.Blank()
                Logger.Info("We found the following Elegoo printers on your network:")
                count = 0
                for result in results:
                    count+= 1
                    if result.TooManyClients:
                        Logger.Info(f"   {count}) {result.Ip} - Couldn't connect, too many connections.")
                    Logger.Info(    f"   {count}) {result.Ip} - {result.MainboardMac}")
                Logger.Info("   m) Press `m` to enter the IP address manually")

                # Ask the user to select the printer they want to connect to.
                Logger.Blank()
                while True:
                    try:
                        i = input("Please select the printer number above you want to connect to this plugin: ")
                        if i == "m" or i == "M":
                            # Enter manual IP setup mode, break out of this loop to run the logic under it.
                            break

                        # Validate the selection number
                        selection = int(i)
                        if selection < 1 or selection > len(results):
                            raise ValueError()
                        result = results[selection - 1]

                        # Check if the user selected a printer that has too many clients.
                        if result.TooManyClients:
                            # If so, tell the user how to fix it and do the auto scan again when they say they are ready.
                            self._ShowTooManyClientsError(results[selection - 1].Ip)
                            reTryAuto = True
                            break

                        # If we got a mainboard id, we are good to go.
                        if result.MainboardMac is not None:
                            return (result.Ip, result.MainboardMac)

                        # Break to the manual logic.
                        Logger.Info("The selected printer had no mainboard ID, going to manual setup.")
                        break
                    except ValueError:
                        Logger.Error("Invalid selection, please enter a number from the list.")
                        continue

            # If we should retry the auto scan, we will do so.
            if reTryAuto:
                continue

            # If we are here, we either found no printers, or the user selected to enter the IP address manually.
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
                result = NetworkSearch.ValidateConnection_Elegoo(Logger.GetPyLogger(), ip, timeoutSec=5.0)
                Logger.Blank()
                Logger.Blank()

                # If we got a mainboard id, we are good to go.
                if result.MainboardMac is not None:
                    Logger.Info(f"Found your Elegoo printer on your network at {ip}.")
                    return (ip, result.MainboardMac)

                # Handle too many clients
                if result.TooManyClients:
                    self._ShowTooManyClientsError(ip)
                    continue

                # Handle other errors.
                Logger.Error("Failed to connect to your Elegoo printer, ensure the IP address is correct and the printer is connected to the network.")
                continue
