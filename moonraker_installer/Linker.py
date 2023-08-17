import os
import time
import configparser
import requests

from .Util import Util
from .Logging import Logger
from .Context import Context

# Responsible for getting the printer id from the instance's config file, checking if it's linked,
# and if not helping the user link their printer.
class Linker:

    c_MinPrinterIdLength = 40

    def Run(self, context:Context):

        # First, wait for the config file to be created and the printer ID to show up.
        printerId = None
        startTimeSec = time.time()
        Logger.Info("Waiting for the plugin to produce a printer id... (this can take a few seconds)")
        while printerId is None:
            # Give the service time to start.
            time.sleep(0.1)

            # Try to get the printer id from the secrets config file
            printerId = Linker.GetPrinterIdFromServiceSecretsConfigFile(context)

            # If we failed, try to handle the case where the service might be having an error.
            if printerId is None:
                timeDelta = time.time() - startTimeSec
                if timeDelta > 10.0:
                    Logger.Warn("The local plugin service is taking a while to start, there might be something wrong.")
                    if Util.AskYesOrNoQuestion("Do you want to keep waiting?"):
                        startTimeSec = time.time()
                        continue
                    # Handle the error and cleanup.
                    Logger.Blank()
                    Logger.Blank()
                    Logger.Error("We didn't get a response from the OctoEverywhere service when waiting for the printer id.")
                    Logger.Error("You can find service logs which might indicate the error in: "+context.PrinterDataLogsFolder)
                    Logger.Blank()
                    Logger.Blank()
                    Logger.Error("Attempting to print the service logs:")
                    # Try to print the service logs to the console.
                    Util.PrintServiceLogsToConsole(context)
                    raise Exception("Failed to read printer id from service config file.")

        # Check if the printer is already connected to an account.
        # If so, report and we don't need to do the setup.
        (isConnectedToService, printerNameIfConnectedToAccount) = self._IsPrinterConnectedToAnAccount(printerId)
        if isConnectedToService and printerNameIfConnectedToAccount is not None:
            Logger.Header("This printer is securely connected to your OctoEverywhere account as '"+str(printerNameIfConnectedToAccount)+"'")
            return

        # The printer isn't connected to an account.
        # If this is not the first time setup, ask the user if they want to do it now.
        if context.ExistingPrinterId is not None:
            Logger.Blank()
            Logger.Warn("This printer isn't connected to an OctoEverywhere account.")
            if Util.AskYesOrNoQuestion("Would you like to link it now?") is False:
                Logger.Blank()
                Logger.Header("You can connect this printer anytime, using this URL: ")
                Logger.Warn(self._GetAddPrinterUrl(printerId))
                return

        # Help the user setup the printer!
        Logger.Blank()
        Logger.Blank()
        Logger.Warn( "You're 10 seconds away from free and unlimited printer access from anywhere!")
        self._PrintShortCodeStyleOrFullUrl(printerId)
        Logger.Blank()
        Logger.Blank()

        Logger.Info("Waiting for the printer to be linked to your account...")
        startTimeSec = time.time()
        notConnectedTimeSec = time.time()
        while True:
            # Query status.
            (isConnectedToService, printerNameIfConnectedToAccount) = self._IsPrinterConnectedToAnAccount(printerId)

            if printerNameIfConnectedToAccount is not None:
                # Connected!
                Logger.Blank()
                Logger.Header("Success! This printer is securely connected to your account as '"+str(printerNameIfConnectedToAccount)+"'")
                return

            # We expect the plugin to be connected to the service. If it's not, something might be wrong.
            if isConnectedToService is False:
                notConnectedDeltaSec = time.time() - notConnectedTimeSec
                Logger.Info("Waiting for the plugin to connect to our service...")
                if notConnectedDeltaSec > 10.0:
                    Logger.Warn("It looks like your plugin hasn't connected to the service yet, which it should have by now.")
                    if Util.AskYesOrNoQuestion("Do you want to keep waiting?"):
                        notConnectedTimeSec = time.time()
                        continue
                    # Handle the Logger.Error and cleanup.
                    Logger.Blank()
                    Logger.Blank()
                    Logger.Error("The plugin hasn't connected to our service yet. Something might be wrong.")
                    Logger.Error("You can find service logs which might indicate the Logger.Error in: "+context.PrinterDataLogsFolder)
                    Logger.Blank()
                    Logger.Blank()
                    Logger.Error("Attempting to print the service logs:")
                    # Try to print the service logs to the console.
                    Util.PrintServiceLogsToConsole(context)
                    raise Exception("Failed to wait for printer to connect to service.")
            else:
                # The plugin is connected but no user account is connected yet.
                timeDeltaSec = time.time() - startTimeSec
                if timeDeltaSec > 60.0:
                    Logger.Warn("It doesn't look like this printer has been connected to your account yet.")
                    if Util.AskYesOrNoQuestion("Do you want to keep waiting?"):
                        Logger.Blank()
                        Logger.Blank()
                        self._PrintShortCodeStyleOrFullUrl(printerId)
                        Logger.Blank()
                        startTimeSec = time.time()
                        continue

                    Logger.Blank()
                    Logger.Blank()
                    Logger.Blank()
                    Logger.Warn("You can use the following URL at anytime to link this printer to your account. Or run this install script again for help.")
                    Logger.Header(self._GetAddPrinterUrl(printerId))
                    Logger.Blank()
                    Logger.Blank()
                    return

            # Sleep before trying the API again.
            time.sleep(1.0)


    def _PrintShortCodeStyleOrFullUrl(self, printerId):
        # To make the setup easier, we will present the user with a short code if we can get one.
        # If not, fallback to the full URL.
        try:
            # Try to get a short code. We do a quick timeout so if this fails, we just present the user the longer URL.
            # Any failures, like rate limiting, server error, whatever, and we just use the long URL.
            r = requests.post('https://octoeverywhere.com/api/shortcode/create', json={"Type": 1, "PrinterId": printerId}, timeout=10.0)
            if r.status_code == 200:
                jsonResponse = r.json()
                if "Result" in jsonResponse and "Code" in jsonResponse["Result"]:
                    codeStr = jsonResponse["Result"]["Code"]
                    if len(codeStr) > 0:
                        Logger.Warn("To securely link this printer to your OctoEverywhere account, go to the following website and use the code.")
                        Logger.Blank()
                        Logger.Header("Website: https://octoeverywhere.com/code")
                        Logger.Header("Code:    "+codeStr)
                        return
        except Exception:
            pass

        Logger.Warn("Use this URL to securely link this printer to your OctoEverywhere account:")
        Logger.Header(self._GetAddPrinterUrl(printerId))


    # Get's the printer id from the instances secrets config file, if the config exists.
    @staticmethod
    def GetPrinterIdFromServiceSecretsConfigFile(context:Context) -> str or None:
        # This path and name must stay in sync with where the plugin will write the file.
        oeServiceConfigFilePath = os.path.join(context.LocalFileStorageFolder, "octoeverywhere.secrets")

        # Check if there is a file. If not, it means the service hasn't been run yet and this is a first time setup.
        if os.path.exists(oeServiceConfigFilePath) is False:
            return None

        # If the file exists, try to read it.
        # If this fails, let it throw, so the user knows something is wrong.
        Logger.Debug("Found existing OctoEverywhere service secrets config.")
        try:
            config = configparser.ConfigParser(allow_no_value=True, strict=False)
            config.read(oeServiceConfigFilePath)
        except Exception as e:
            # Print the file for Logger.Debugging.
            Logger.Info("Failed to read config file. "+str(e)+ ", trying again...")
            with open(oeServiceConfigFilePath, 'r', encoding="utf-8") as f:
                Logger.Debug("file contents:"+f.read())
            return None

        # Print the raw config file for debugging issues with the config.
        try:
            with open(oeServiceConfigFilePath, 'r', encoding="utf-8") as f:
                Logger.Debug("Service secrets config contents:"+f.read())
        except Exception:
            pass

        # Look for these sections, but don't throw if they aren't there. The service first creates the file and then
        # adds these, so it might be the case that the service just hasn't created them yet.
        # These must stay in sync with the vars defined in the secrets.py class in the main module.
        section = "secrets"
        key = "printer_id"
        if config.has_section(section) is False:
            Logger.Debug("Server section not found in OE config.")
            return None
        if key not in config[section].keys():
            Logger.Debug("Printer id not found in OE config.")
            return None
        printerId = config[section][key]
        if len(printerId) < Linker.c_MinPrinterIdLength:
            Logger.Debug("Printer ID found, but the length is less than "+str(Linker.c_MinPrinterIdLength)+" chars? value:`"+printerId+"`")
            return None
        return printerId


    # Checks with the service to see if the printer is setup on a account.
    # Returns a tuple of two values
    #   1 - bool - Is the printer connected to the service
    #   2 - string - If the printer is setup on an account, the printer name.
    def _IsPrinterConnectedToAnAccount(self, printerId):
        # Adding retry logic, since one call can fail if the server is updating or whatever.
        attempt = 0
        while True:
            try:
                # Keep track of attempts and timeout if there have been too many.
                attempt += 1
                if attempt > 5:
                    Logger.Error(f"Failed to query current printer info from service after {attempt} attempts.")
                    return (False, None)

                # Query the printer status.
                r = requests.post('https://octoeverywhere.com/api/printer/info', json={"Id": printerId}, timeout=20)

                Logger.Debug("OE Printer info API Result: "+str(r.status_code))
                # If the status code is above 500, retry.
                if r.status_code >= 500:
                    raise Exception(f"Failed call with status code {r.status_code}")

                # Anything else we report as not connected.
                if r.status_code != 200:
                    return (False, None)

                # On success, try to parse the response and see if it's connected.
                jResult = r.json()
                Logger.Debug("OE Printer API info; Name:"+jResult["Result"]["Name"] + " HasOwners:" +str(jResult["Result"]["HasOwners"]))

                # Only return the name if there the printer is linked to an account.
                printerName = None
                if jResult["Result"]["HasOwners"] is True:
                    printerName = jResult["Result"]["Name"]
                return (True, printerName)
            except Exception:
                Logger.Warn("Failed to get printer info from service, trying again in just a second...")
                time.sleep(2.0 * attempt)

    def _GetAddPrinterUrl(self, printerId):
        return "https://octoeverywhere.com/getstarted?printerid="+printerId
