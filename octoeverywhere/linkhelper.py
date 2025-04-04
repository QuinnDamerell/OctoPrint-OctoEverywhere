import time
import logging
import threading
from typing import Tuple

from .httpsessions import HttpSessions

class LinkHelper:

    # A static var that allows us to know if we have already done the short code link logic.
    # This is used to prevent the short code from being printed multiple times, like when the plugin is re-connected
    s_HasRunShortCodeLinkLogic = False

    # Checks with the service to see if the printer is setup on a account.
    # Returns a tuple of two values
    #   1 - bool - Is the printer connected to the service
    #   2 - string - If the printer is setup on an account, the printer name.
    @staticmethod
    def IsPrinterConnectedToAnAccount(logger:logging.Logger, printerId:str):
        # Adding retry logic, since one call can fail if the server is updating or whatever.
        attempt = 0
        while True:

           # Keep track of attempts and timeout if there have been too many.
            attempt += 1
            if attempt > 5:
                logger.error(f"Failed to query current printer info from service after {attempt} attempts.")
                return (False, None)

           # Delay after the first attempt
            if attempt > 1:
                logger.debug("Failed to get short code, trying again in just a second...")
                time.sleep(2.0 * attempt)

            try:
                # Query the printer status.
                url = "https://octoeverywhere.com/api/printer/info"
                r = HttpSessions.GetSession(url).post(url, json={"Id": printerId}, timeout=20)

                logger.debug("OE Printer info API Result: "+str(r.status_code))
                # If the status code is above 500, retry.
                if r.status_code >= 500:
                    raise Exception(f"Failed call with status code {r.status_code}")

                # Anything else we report as not connected.
                if r.status_code != 200:
                    return (False, None)

                # On success, try to parse the response and see if it's connected.
                jResult = r.json()
                logger.debug("OE Printer API info; Name:"+jResult["Result"]["Name"] + " HasOwners:" +str(jResult["Result"]["HasOwners"]))

                # Only return the name if there the printer is linked to an account.
                printerName = None
                if jResult["Result"]["HasOwners"] is True:
                    printerName = jResult["Result"]["Name"]
                return (True, printerName)
            except Exception as e:
                logger.debug("Exception trying to get printer info. "+str(e))
                logger.warning("Failed to get printer info from service, trying again in just a second...")


    # Given the printer id, this will generate and return a short code if possible.
    # Returns the short code string and the amount of time it's valid for in seconds.
    # Returns None if it fails.
    @staticmethod
    def GetLinkShortCode(logger:logging.Logger, printerId:str) -> Tuple[str, int]:
        # To make the setup easier, we will present the user with a short code if we can get one.
        # If not, fallback to the full URL.
        # Add retry logic to handle server update cases.
        attempt = 0
        while True:
            attempt += 1
            if attempt > 3:
                return (None, 0)

            # Delay after the first attempt
            if attempt > 1:
                logger.debug("Failed to get short code, trying again in just a second...")
                time.sleep(2.0)

            try:
                url = "https://octoeverywhere.com/api/shortcode/create"
                r = HttpSessions.GetSession(url).post(url, json={"Type": 1, "PrinterId": printerId}, timeout=10.0)
                if r.status_code != 200:
                    raise Exception(f"Invalid status code: {r.status_code}")
                jsonResponse = r.json()

                # Just do a raw parse, this will throw if it fails.
                codeStr = jsonResponse["Result"]["Code"]
                if len(codeStr) == 0:
                    raise Exception("Empty code string returned.")
                validForSeconds = int(jsonResponse["Result"]["ValidForSeconds"])
                if validForSeconds <= 0:
                    raise Exception("Invalid valid for seconds returned.")
                # Return the code and the amount of time it's valid for.
                return (codeStr, validForSeconds)
            except Exception as e:
                logger.debug("Exception trying to get short code. "+str(e))


    # Given the printer id this will print a QR code directly to the console.
    @staticmethod
    def PrintLinkUrlQrCodeToConsole(logger:logging.Logger, printerId:str, source:str=None) -> bool:
        try:
            # Update the source
            if source is None:
                source = ""
            source += "qrcode"

            # Include here, so we only include if we use this function.
            # pylint: disable=import-outside-toplevel
            import qrcode
            qr = qrcode.QRCode(
                version=1,    # Version sets the size of the QR code.
                box_size=1,   # This sets the size of each box in the QR code.
                border=4,     # This sets the border size (minimum is 4).
            )
            qr.add_data(LinkHelper.GetAddPrinterUrl(printerId, source))
            qr.make(fit=True)
            qr.print_ascii()
            return True
        except Exception as e:
            logger.error("Failed to print QR code to console. "+str(e))
        return False


    # Given the printer id, this will return the full URL to the setup page.
    @staticmethod
    def GetAddPrinterUrl(printerId:str, source:str=None) -> str:
        # By default this should return a non decorated, since it's printed directly to the user.
        # If a source is set, we will add it to the URL.
        extraArgs = ""
        if source is not None:
            extraArgs = "&source="+source
        return f"https://octoeverywhere.com/getstarted?printerid={printerId}{extraArgs}"


    # This will async run a thread that will provide the user with a link to the printer.
    @staticmethod
    def RunLinkPluginConsolePrinterAsync(logger:logging.Logger, printerId:str, source:str=None) -> None:
        t = threading.Thread(target=LinkHelper._RunLinkPluginConsolePrinterAsync, args=(logger, printerId, source))
        t.daemon = True
        t.start()


    # Used by the plugins if they connect to the service and there's no account setup.
    @staticmethod
    def _RunLinkPluginConsolePrinterAsync(logger:logging.Logger, printerId:str, source:str=None):
        # This is kicked off when the plugin first connects to the service.
        # In most cases, the user has just installed the plugin and is setting it up, so we want to make the process as easy as possible.
        # So for a bit we will use the short code setup method, and then fallback to the full plugin id URL.
        try:
            # Only try the short code logic once per process run.
            # This is so plugin reconnects don't do this, since it's unlikely a user would be looking after.
            if LinkHelper.s_HasRunShortCodeLinkLogic is False:
                LinkHelper.s_HasRunShortCodeLinkLogic = True
                if LinkHelper._DoShortCodeLinkLogic(logger, printerId) is True:
                    return

            # If the short code logic didn't work, we will just print the full URL.
        except Exception as e:
            logger.error("Failed to run link plugin console printer. "+str(e))

        # If there's an error or the short code times out, fallback to the full URL.
        logger.warning("")
        logger.warning("")
        logger.warning("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        logger.warning("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        logger.warning("          This Plugin Isn't Connected To OctoEverywhere!          ")
        logger.warning("     Use the following link to finish your 20 second setup:")
        logger.warning("     %s", LinkHelper.GetAddPrinterUrl(printerId))
        logger.warning("")
        logger.warning("")
        logger.warning("          -- Or use your phone to scan this QR code --                  ")
        # Print the QR code to the console.
        LinkHelper.PrintLinkUrlQrCodeToConsole(logger, printerId, source)
        logger.warning("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        logger.warning("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        logger.warning("")
        logger.warning("")


    @staticmethod
    def _DoShortCodeLinkLogic(logger:logging.Logger, printerId:str, source:str=None) -> bool:
        # Since the codes are valid for 15 minutes, that's long enough and we don't really need to loop.
        # But we use this loop for control logic, so we can just keep it here.
        attempt = 0
        while True:
            # Check if the printer is connected to an account.
            # Remember the printerName is only set if the printer is connected to an account and has a name.
            isConnectedToService, printerName = LinkHelper.IsPrinterConnectedToAnAccount(logger, printerId)
            if isConnectedToService and printerName is not None and len(printerName) > 0:
                # The printer is connected to an account, so we don't need to do anything else.
                logger.info("This plugin is now connected to your account as: %s", printerName)
                return True

            # Only allow this logic to run once, then fallback.
            attempt += 1
            if attempt > 1:
                logger.debug("Short code timed out, going back the full printer URL.")
                return False

            # Get the short code and how long it's valid for.
            shortCode, validForSeconds = LinkHelper.GetLinkShortCode(logger, printerId)
            if shortCode is None:
                logger.warning("Failed to get short code.")
                return False

            logger.warning("")
            logger.warning("")
            logger.warning("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.warning("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.warning("              This Plugin Isn't Connected To OctoEverywhere!             ")
            logger.warning("   To finish your setup, go the this website and enter the code below.   ")
            logger.warning("")
            logger.warning("              Website: https://octoeverywhere.com/code", )
            logger.warning("              Code:    %s", shortCode)
            logger.warning("")
            logger.warning("")
            logger.warning("               -- Or use your phone to scan this QR code --              ")
            # Print the QR code to the console.
            LinkHelper.PrintLinkUrlQrCodeToConsole(logger, printerId, source)
            logger.warning("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.warning("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.warning("")
            logger.warning("")

            # Wait for the time it was valid for.
            # Remove 20 seconds to make sure it doesn't expire before the user sees it.
            time.sleep(validForSeconds - 20)
