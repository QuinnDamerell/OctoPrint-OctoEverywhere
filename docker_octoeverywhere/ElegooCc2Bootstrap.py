import logging
import os
import sys
import time

from linux_host.config import Config


class ElegooCc2Bootstrap:

    @staticmethod
    def Bootstrap(logger:logging.Logger, config:Config) -> None:

        # These are constants that are always used for Elegoo CC2 connect.
        # The CC2 runs an MQTT broker on port 1883
        # Always set these to ensure they override any other settings from other companion modes.
        config.SetStr(Config.SectionElegoo, Config.ElegooPrinterProtocol, Config.ElegooPrinterProtocolCc2)
        config.SetStr(Config.SectionCompanion, Config.CompanionKeyPort, "1883")

        # The printer IP is always required.
        printerIp = os.environ.get("PRINTER_IP", None)
        if printerIp is not None:
            logger.info(f"Setting Printer IP: {printerIp}")
            config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, printerIp)
        printerIp = config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        if printerIp is None:
            logger.error("")
            logger.error("")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("           You must provide your Elegoo Centauri Carbon 2 printer's IP address.")
            logger.error("    Use `docker run -e PRINTER_IP=<ip address>` or add it to your docker-compose file.")
            logger.error("")
            logger.error("         To find your printer's IP address -> https://octoeverywhere.com/s/cc2-ip")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("")
            logger.error("")
            time.sleep(5.0)
            sys.exit(1)
        logger.info(f"Target Printer IP: {printerIp}")

        # The access code is always required unless the access code is disabled, in which case the default value can be used.
        accessCode = os.environ.get("ACCESS_CODE", None)
        if accessCode is not None:
            logger.info(f"Setting Access Code: {accessCode}")
            config.SetStr(Config.SectionElegoo, Config.ElegooCc2AccessCode, accessCode)
        # Ensure something is set now.
        if config.GetStr(Config.SectionElegoo, Config.ElegooCc2AccessCode, None) is None:
            logger.error("")
            logger.error("")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("            You must provide your Elegoo Centauri Carbon 2 access code.")
            logger.error("     Use `docker run -e ACCESS_CODE=<code>` or add it to your docker-compose file.")
            logger.error("   If the access code is disabled on your printer, use the default value of '123456'.")
            logger.error("")
            logger.error("   To find your printer's access code -> https://octoeverywhere.com/s/cc2-access-code")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("")
            logger.error("")
            # Sleep some, so we don't restart super fast and then exit.
            time.sleep(5.0)
            sys.exit(1)

        # The serial number is not required. If given it will ensure the printer is correctly identified, if not it will be auto detected and recorded on first run.
        serialNumber = os.environ.get("SERIAL_NUMBER", None)
        if serialNumber is not None:
            logger.info(f"Setting Elegoo CC2 Serial Number: {serialNumber}")
            config.SetStr(Config.SectionElegoo, Config.ElegooCc2PrinterSn, serialNumber)
