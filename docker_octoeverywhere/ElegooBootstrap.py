import os
import sys
import time
import logging

from linux_host.config import Config

class ElegooBootstrap:

    @staticmethod
    def Bootstrap(logger:logging.LoggerAdapter, config:Config) -> None:

        # These are constants that are always used for Elegoo connect.
        # The Elegoo OS webserver is on 3030
        config.SetStr(Config.SectionCompanion, Config.CompanionKeyPort, "3030")
        config.SetStr(Config.RelaySection, Config.RelayFrontEndPortKey, "3030")

        # The printer's IP is required.
        printerIp = os.environ.get("ELEGOO_PRINTER_IP", None)
        if printerIp is not None:
            logger.info(f"Setting Printer IP: {printerIp}")
            config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, printerIp)
        # Ensure something is set now.
        if config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None) is None:
            logger.error("")
            logger.error("")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("                      You must provide your printer's IP Address.")
            logger.error(" Use `docker run -e ELEGOO_PRINTER_IP=<ip address>` or add it to your docker-compose file.")
            logger.error("")
            logger.error("        To find your printer's IP Address -> https://octoeverywhere.com/s/elegoo-ip")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("")
            logger.error("")
            # Sleep some, so we don't restart super fast and then exit.
            time.sleep(5.0)
            sys.exit(1)
