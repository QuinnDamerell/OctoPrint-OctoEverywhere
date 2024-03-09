from linux_host.networksearch import NetworkSearch

from py_installer.Util import Util
from py_installer.Logging import Logger
from py_installer.Context import Context
from py_installer.ConfigHelper import ConfigHelper

# A class that helps the user discover, connect, and setup the details required to connect to a remote Bambu Lab printer.
class BambuConnector:


    def EnsureBambuConnection(self, context:Context):
        Logger.Debug("Running bambu connect ensure config logic.")

        # For Bambu printers, we need the IP or Hostname, the port is static,
        # and we also need the printer SN and access token.
        ip, port = ConfigHelper.TryToGetCompanionDetails(context)
        accessCode, printerSn = ConfigHelper.TryToGetBambuData(context)
        if ip is not None and port is not None and accessCode is not None and printerSn is not None:
            # Check if we can still connect. This can happen if the IP address changes, the user might need to setup the printer again.
            Logger.Info(f"Existing bambu config found. IP: {ip} - {printerSn}")
            Logger.Info("Checking if we can connect to your Bambu Lab printer...")
            result = NetworkSearch.ValidateConnection_Bambu(Logger.GetPyLogger(), ip, accessCode, printerSn, portStr=port, timeoutSec=10.0)
            if result.Success():
                Logger.Info("Successfully connected to you Bambu Lab printer!")
                return
            else:
                # Let the user keep this connection setup, or try to set it up again.
                Logger.Blank()
                Logger.Warn(f"We failed to connect or authenticate to your printer using {ip}.")
                if Util.AskYesOrNoQuestion("Do you want to setup the Bambu Lab printer connection again?") is False:
                    Logger.Info(f"Keeping the existing Bambu Lab printer connection setup. {ip} - {printerSn}")
                    return

        ipOrHostname, port, accessToken, printerSn = self._SetupNewBambuConnection()
        Logger.Info(f"You Bambu printer was found and authentication was successful! IP: {ipOrHostname}")

        ConfigHelper.WriteCompanionDetails(context, ipOrHostname, port)
        ConfigHelper.WriteBambuDetails(context, accessToken, printerSn)
        Logger.Blank()
        Logger.Header("Bambu Connection successful!")
        Logger.Blank()


    # Helps the user setup a bambu connection via auto scanning or manual setup.
    # Returns (ip:str, port:str, accessToken:str, printerSn:str)
    def _SetupNewBambuConnection(self):
        while True:
            Logger.Blank()
            Logger.Blank()
            Logger.Blank()
            Logger.Header("##################################")
            Logger.Header("     Bambu Lab Printer Setup")
            Logger.Header("##################################")
            Logger.Blank()
            Logger.Info("OctoEverywhere Bambu Connect needs to connect to your Bambu Lab printer to provide remote access.")
            Logger.Info("Bambu Connect needs your printer's Access Code and Serial Number to connect to your printer.")
            Logger.Info("If you have any trouble, we are happy to help! Contact us at support@octoeverywhere.com")

            # Get the access code.
            accessCode = None
            while True:
                Logger.Blank()
                Logger.Blank()
                Logger.Header("We need your Bambu Lab printer's Access Code to connect.")
                Logger.Info("The Access Code can be found using the screen on your printer, in the Network settings.")
                Logger.Blank()
                Logger.Warn("Follow this link for a step-by-step guide to find the Access Code for your printer:")
                Logger.Warn("https://octoeverywhere.com/s/access-code")
                Logger.Blank()
                Logger.Info("The access code is case sensitive - make sure to enter it exactly as shown on your printer.")
                Logger.Blank()
                accessCode = input("Enter your printer's Access Code: ")

                # Validate
                # The access code IS CASE SENSITIVE and can letters and numbers.
                accessCode = accessCode.strip()
                if len(accessCode) != 8:
                    if Util.AskYesOrNoQuestion(f"The Access Code should be 8 numbers, you have entered {len(accessCode)}. Do you want to try again? "):
                        continue

                retryEntry = False
                for c in accessCode:
                    if not c.isdigit() and not c.isalpha():
                        if Util.AskYesOrNoQuestion("The Access Code should only be letters and numbers, you seem to have entered something else. Do you want to try again? "):
                            retryEntry = True
                            break
                if retryEntry:
                    continue

                # Accept the input.
                break


            Logger.Blank()
            Logger.Blank()
            Logger.Blank()

            # Get the serial number.
            printerSn = None
            while True:
                Logger.Blank()
                Logger.Header("Finally, Bambu Connect needs your Bambu Lab printer's Serial Number to connect.")
                Logger.Info("The Serial Number is required for authentication when the printer's local network protocol.")
                Logger.Info("Your Serial Number and Access Code are only stored on this device and will not be uploaded.")
                Logger.Blank()
                Logger.Warn("Follow this link for a step-by-step guide to find the Serial Number for your printer:")
                Logger.Warn("https://octoeverywhere.com/s/bambu-sn")
                Logger.Blank()
                printerSn = input("Enter your printer's Serial Number: ")

                # The SN should always be upper case letters.
                printerSn = printerSn.strip().upper()

                # Validate
                # It seems the SN are 15 digits
                if len(printerSn) != 15:
                    if Util.AskYesOrNoQuestion(f"The Serial Number is usually 15 letters or numbers, you have entered {len(printerSn)}. Do you want to try again? "):
                        continue

                retryEntry = False
                for c in printerSn:
                    if not c.isdigit() and not c.isalpha():
                        if Util.AskYesOrNoQuestion("The Serial Number should only be letters and numbers, you seem to have entered something else. Do you want to try again? "):
                            retryEntry = True
                            break
                if retryEntry:
                    continue

                # Accept the input.
                break

            # Scan for the local IP subset for possible matches.
            Logger.Blank()
            Logger.Blank()
            Logger.Warn("Searching for your Bambu printer on your network, this will take about 5 seconds...")
            ips = NetworkSearch.ScanForInstances_Bambu(Logger.GetPyLogger(), accessCode, printerSn)

            Logger.Blank()
            Logger.Blank()

            # There should only be one IP found or none, because there should be no other printer that matches the same access code and printer serial number.
            if len(ips) == 1:
                ip = ips[0]
                return (ip, NetworkSearch.c_BambuDefaultPortStr, accessCode, printerSn)

            Logger.Blank()
            Logger.Blank()
            Logger.Blank()
            Logger.Error("We were unable to automatically find your printer on your network using these details:")
            Logger.Info(f"   Access Code:   {accessCode}")
            Logger.Info(f"   Serial Number: {printerSn}")
            Logger.Blank()
            Logger.Header("Make sure your printer is on a full booted and verify the values above are correct.")
            Logger.Blank()
            if not Util.AskYesOrNoQuestion("Are the Access Code and Serial Number correct?"):
                # Loop back to the very top, to restart the entire setup, allowing the user to enter their values again.
                Logger.Blank()
                Logger.Blank()
                Logger.Blank()
                Logger.Blank()
                continue

            # Enter manual IP setup mode
            while True:
                Logger.Blank()
                Logger.Blank()
                Logger.Info("Since we can't automatically find your printer, we can get the IP address manually.")
                Logger.Info("You Bambu printer's IP address can be found using the screen on your printer, in the Networking settings.")
                Logger.Blank()
                Logger.Warn("Follow this link for a step-by-step guide to find the IP address of your printer:")
                Logger.Warn("https://octoeverywhere.com/s/bambu-ip")
                Logger.Blank()
                ip = input("Enter your printer's IP Address: ")
                ip = ip.strip()
                Logger.Blank()
                Logger.Info("Trying to connect to your printer...")
                result = NetworkSearch.ValidateConnection_Bambu(Logger.GetPyLogger(), ip, accessCode, printerSn, timeoutSec=10.0)
                Logger.Blank()
                Logger.Blank()
                if result.Success():
                    return (ip, NetworkSearch.c_BambuDefaultPortStr, accessCode, printerSn)
                if result.FailedToConnect:
                    Logger.Error("Failed to connect to your Bambu printer, ensure the IP address is correct and the printer is connected to the network.")
                elif result.FailedAuth:
                    Logger.Error("Failed to connect to your Bambu printer, the Access Code was incorrect.")
                    _ = input("Press any key to continue.")
                    # Breaking this loop will return us to the main setup loop, which will do the entire access code and sn entry again.
                    break
                elif result.FailedSerialNumber:
                    Logger.Error("Failed to connect to your Bambu printer, the Serial Number was incorrect.")
                    _ = input("Press any key to continue.")
                    # Breaking this loop will return us to the main setup loop, which will do the entire access code and sn entry again.
                    break
                else:
                    Logger.Error("Failed to connect to your Bambu printer.")

                # If we got here, the IP address is wrong or something else.
                Logger.Blank()
                Logger.Info("Pick one of the following:")
                Logger.Info("   1) Enter the IP address again.")
                Logger.Info("   2) Enter your Access Code and Serial Number.")
                Logger.Blank()
                c = input("Pick one or two: ")
                try:
                    cInt = int(c.strip())
                    if cInt == 1:
                        # Restart IP entry.
                        continue
                    else:
                        # Breaking this loop will return us to the main setup loop, which will do the entire access code and sn entry again.
                        break
                except Exception:
                    # Default to a full restart.
                    # Breaking this loop will return us to the main setup loop, which will do the entire access code and sn entry again.
                    break
