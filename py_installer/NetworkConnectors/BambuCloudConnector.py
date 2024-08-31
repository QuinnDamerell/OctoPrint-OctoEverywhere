from linux_host.networksearch import NetworkSearch, NetworkValidationResult
from linux_host.bambucloud import BambuCloud, LoginStatus

from py_installer.Util import Util
from py_installer.Logging import Logger
from py_installer.Context import Context
from py_installer.ConfigHelper import ConfigHelper

# A class that helps the user discover, connect, and setup the details required to connect to a remote Bambu Lab printer.
class BambuCloudConnector:

    # Returns if this instance is setup with Bambu cloud or not.
    @staticmethod
    def IsBambuCloudSetup(context:Context = None, configFolderPath:str = None) -> bool:
        # Try to create a BambuCloud object, if it's None, then it's not setup.
        bambuCloud = BambuCloudConnector._BambuCloudObj(context, configFolderPath)
        if bambuCloud is None:
            return False
        # Try to get an existing context, if it's None, there's nothing setup.
        (u, p) = bambuCloud.GetContext()
        return u is not None and p is not None


    # If a cloud context can be created it's returned, otherwise, None
    @staticmethod
    def _BambuCloudObj(context:Context = None, configFolderPath:str = None) -> BambuCloud:
        # Load the config, if this returns None, there is no existing file.
        c = ConfigHelper.GetConfig(context, configFolderPath)
        if c is None:
            return None
        return BambuCloud.Init(Logger.GetPyLogger(), c)


    # If this runs, the user already has a bambu cloud setup or they want to create one.
    def EnsureBambuCloudConnection(self, context:Context):
        Logger.Debug("Running bambu cloud connect ensure config logic.")

        # For Bambu cloud, we need the user's email and password, SN, Access Code, and printer IP.
        ip, port = ConfigHelper.TryToGetCompanionDetails(context)
        accessCode, printerSn = ConfigHelper.TryToGetBambuData(context)
        bambuCloud = BambuCloudConnector._BambuCloudObj(context)

        # Check if we have everything.
        if bambuCloud is not None:
            (email, password) = bambuCloud.GetContext()
            if ip is not None and port is not None and accessCode is not None and printerSn is not None and email is not None and password is not None:
                Logger.Info(f"Existing bambu cloud config found. IP: {ip} - {printerSn}")
                Logger.Info("Checking if we can connect to the Bambu Cloud...")
                result = bambuCloud.Login()
                if result is LoginStatus.Success:
                    # TODO - We should check that the SN is still there and we can connect to the printer's local IP.
                    Logger.Info("Successfully connected to the Bambu Cloud!")
                    return

                # Let the user keep this connection setup, or try to set it up again.
                Logger.Blank()
                if result is LoginStatus.BadUserNameOrPassword:
                    Logger.Warn("We failed to connect to the Bambu Cloud. Your email or password is incorrect.")
                elif result is LoginStatus.TwoFactorAuthEnabled:
                    Logger.Warn("We failed to connect to the Bambu Cloud. Your account has two factor auth enabled, which Bambu doesn't allow 3rd party services to use.")
                else:
                    Logger.Warn("We failed to connect to the Bambu Cloud, for an unknown reason. Make sure your device has an internet connection")
                if Util.AskYesOrNoQuestion("Do you want to setup the Bambu Cloud connection again?") is False:
                    Logger.Info(f"Keeping the existing Bambu Cloud connection setup. {ip} - {printerSn}")
                    return

        ipOrHostname, port, accessToken, printerSn = self._SetupNewBambuCloudConnection(context)

        # Ensure the X1 camera is setup.
        # self._EnsureX1CameraSetup(None, ipOrHostname, accessToken, printerSn)

        ConfigHelper.WriteCompanionDetails(context, ipOrHostname, port)
        ConfigHelper.WriteBambuDetails(context, accessToken, printerSn)
        Logger.Blank()
        Logger.Header("Bambu Cloud Connection Successful!")
        Logger.Blank()


    # Helps the user setup the printer via the Bambu Cloud.
    def _SetupNewBambuCloudConnection(self, context:Context):
        while True:
            Logger.Blank()
            Logger.Blank()
            Logger.Blank()
            Logger.Header("##################################")
            Logger.Header("       Bambu Cloud Setup")
            Logger.Header("##################################")
            Logger.Blank()
            Logger.Info("OctoEverywhere Bambu Connect needs some information to connect to your printer.")
            Logger.Info("If you have any trouble, we are happy to help! Contact us at support@octoeverywhere.com")

            # Try to get an an existing access code or SN, so the user doesn't have to re-enter them if they are already there.
            user, password = ConfigHelper.TryToGetBambuData(context)

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

                # If there is already an access code, ask if the user wants to use it.
                if oldConfigAccessCode is not None and len(oldConfigAccessCode) > 0:
                    Logger.Info(f"Your previously entered Access Code is: '{oldConfigAccessCode}'")
                    if Util.AskYesOrNoQuestion("Do you want to continue using this Access Code?"):
                        accessCode = oldConfigAccessCode
                        break
                    # Set it to None so we wont ask again.
                    oldConfigAccessCode = None
                    Logger.Blank()
                    Logger.Blank()

                # Ask for the access code.
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

                # If there is already an sn, ask if the user wants to use it.
                if oldConfigPrinterSn is not None and len(oldConfigPrinterSn) > 0:
                    Logger.Info(f"Your previously entered Serial Number is: '{oldConfigPrinterSn}'")
                    if Util.AskYesOrNoQuestion("Do you want to continue using this Serial Number?"):
                        printerSn = oldConfigPrinterSn
                        break
                    # Set it to None so we wont ask again.
                    oldConfigPrinterSn = None
                    Logger.Blank()
                    Logger.Blank()

                # Ask for the sn.
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


    # Given either a validation result or required details, this gets the x1 carbon's ip camera state
    # and ensures it's enabled properly
    def _EnsureX1CameraSetup(self, result:NetworkValidationResult = None,  ipOrHostname:str = None, accessToken:str = None, printerSn:str = None):
        # If we didn't get passed a result, get one now.
        if result is None:
            result = NetworkSearch.ValidateConnection_Bambu(Logger.GetPyLogger(), ipOrHostname, accessToken, printerSn)
        # If we didn't get something, it's a failure.
        if result is None:
            Logger.Error("Ensure camera failed to get a validation result.")
            return
        # Ensure success.
        if result.Success() is False:
            Logger.Error("Ensure camera got a validation result that failed.")
            return
        # If the bambu rtsp url is None, this printer doesn't support RTSP.
        if result.BambuRtspUrl is None:
            Logger.Info("This printer doesn't use RTSP camera streaming, skipping setup.")
            return
        # If there is a string and the string isn't 'disable' then we are good to go.
        if len(result.BambuRtspUrl) > 0 and "disable" not in result.BambuRtspUrl:
            Logger.Info(f"RTSP Liveview is enabled. '{result.BambuRtspUrl}'")
            return
        # Tell the user how to enable it.
        Logger.Info("")
        Logger.Info("")
        Logger.Header("You need to enable 'LAN Mode Liveview' on your printer for webcam access.")
        Logger.Blank()
        Logger.Warn("Don't worry, you just need to flip a switch in the settings on your printer.")
        Logger.Warn("Follow this link for a step-by-step guide:")
        Logger.Warn("https://octoeverywhere.com/s/liveview")
        Logger.Blank()
        Util.AskYesOrNoQuestion("Did you enable 'LAN Mode Liveview'?")
