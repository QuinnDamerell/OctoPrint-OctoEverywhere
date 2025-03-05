from linux_host.networksearch import NetworkSearch, NetworkValidationResult

from py_installer.Util import Util
from py_installer.Logging import Logger
from py_installer.Context import Context
from py_installer.ConfigHelper import ConfigHelper

# A class that helps the user discover, connect, and setup the details required to connect to a remote Elegoo OS printer.
class ElegooConnector:

    # The default port for Elegoo printers.
    c_ElegooDefaultPort = 3030


    def EnsureElegooPrinterConnection(self, context:Context):
        Logger.Debug("Running elegoo connect ensure config logic.")

        # For Elegoo printers, we need the IP or Hostname, the port is static, and the mainboard ID.
        ip, port = ConfigHelper.TryToGetCompanionDetails(context)
        mainboardId = ConfigHelper.TryToGetElegooData(context)
        if ip is not None and port is not None and mainboardId is not None:
            # Check if we can still connect. This can happen if the IP address changes, the user might need to setup the printer again.
            Logger.Debug(f"Existing Elegoo config found. IP: {ip} - {mainboardId}")
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
                    Logger.Info(f"Keeping the existing Elegoo printer connection setup. {ip} - {mainboardId}")
                    return
            # If there was an exception or we never connected to the WS, we should ask the user if they want to try again.
            elif result.Exception is not None or result.WsConnected is False:
                # Let the user keep this connection setup, or try to set it up again.
                Logger.Blank()
                Logger.Warn(f"We failed to connect to your Elegoo printer at {ip}.")
                Logger.Warn("You can keep the current Elegoo Connect printer setup or re-run the connection process.")
                Logger.Blank()
                if Util.AskYesOrNoQuestion("Do you want to set up the Elegoo printer connection again?") is False:
                    Logger.Info(f"Keeping the existing Elegoo printer connection setup. {ip} - {mainboardId}")
                    return
            elif (result.MainboardId is not None and result.MainboardId == mainboardId):
                Logger.Info("Successfully connected to you Elegoo printer!")
                return
            else:
                # This means we found a printer on this ip, but the mainboard id was different?
                Logger.Warn(f"Found a printer on {ip}, but the mainboard ID was different. Expected: {mainboardId}, Found: {result.MainboardId}")
                Logger.Warn("Let's setup your Elegoo printer again.")

        ipOrHostname, mainboardId = self._SetupNewElegooConnection(context)
        Logger.Info(f"You Elegoo printer was found and authentication was successful! IP: {ipOrHostname}")

        ConfigHelper.WriteCompanionDetails(context, ipOrHostname, NetworkSearch.c_ElegooDefaultPortStr)
        ConfigHelper.WriteElegooDetails(context, mainboardId)
        Logger.Blank()
        Logger.Header("Elegoo printer connection successful!")
        Logger.Blank()


    # Shows the user a message that there are too many clients connected to the printer and how to fix it.
    def _ShowTooManyClientsError(self, ip:str):
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
    def _SetupNewElegooConnection(self, context:Context):
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
            if len(results) == 1:
                result = results[0]
                if result.TooManyClients:
                    self._ShowTooManyClientsError(result.Ip)
                    continue

                Logger.Info(f"Found your Elegoo printer on your network at {result.Ip}.")
                return (result.Ip, result.MainboardId)

            elif len(results) > 1:
                # Handle multiple results.
                Logger.Blank()
                Logger.Blank()
                Logger.Info("We found the following Elegoo printers on your network:")
                for result in results:
                    if result.TooManyClients:
                        Logger.Info(f"   {result.Ip} - Couldn't connect, too many connections.")
                    Logger.Info(f"   {result.Ip} - {result.MainboardId}")
                if not Util.AskYesOrNoQuestion("Are the Access Code and Serial Number correct?"):
                    # Loop back to the very top, to restart the entire setup, allowing the user to enter their values again.
                    Logger.Blank()
                    Logger.Blank()
                    Logger.Blank()
                    Logger.Blank()
                    continue

            # We didnt't find any printer, enter manual IP setup mode.
            while True:
                Logger.Blank()
                Logger.Blank()
                Logger.Info("We can't automatically find your printer, we can get the IP address manually.")
                Logger.Info("Your Elegoo printer's IP address can be found using the screen on your printer:")
                Logger.Info("   - Press the gear icon second in the vertical left icon menu.")
                Logger.Info("   - Press the 'Network' tab at the top of the screen.")
                Logger.Info("   - Ensure Wi-Fi is on and the printer is connected to your network.")
                Logger.Info("   - The IP address is under your connected network.")
                Logger.Blank()
                Logger.Info("The IP's format is a numerical xxx.xxx.xxx.xxx, such as 192.168.1.15 or 10.0.0.122")
                Logger.Blank()
                Logger.Info("If you need help finding your printer's IP address, visit:")
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
                if result.MainboardId is not None:
                    Logger.Info(f"Found your Elegoo printer on your network at {ip}.")
                    return (ip, result.MainboardId)

                # Handle too many clients
                if result.TooManyClients:
                    self._ShowTooManyClientsError(ip)
                    continue

                # Handle other errors.
                Logger.Error("Failed to connect to your Elegoo printer, ensure the IP address is correct and the printer is connected to the network.")
                continue
