import os
import sys
import time
import logging

from linux_host.config import Config

class KlipperBootstrap:

    @staticmethod
    def Bootstrap(logger:logging.LoggerAdapter, config:Config) -> None:

        # The printer's IP is required.
        printerIp = os.environ.get("PRINTER_IP", None)
        if printerIp is not None:
            logger.info(f"Setting Printer IP: {printerIp}")
            config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, printerIp)
        # Ensure something is set now.
        printerIp = config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        if printerIp is None:
            logger.error("")
            logger.error("")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("                      You must provide your printer's IP Address.")
            logger.error("    Use `docker run -e PRINTER_IP=<ip address>` or add it to your docker-compose file.")
            logger.error("")
            logger.error("        To find your Moonraker's IP address, find the IP address of your host machine.")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("")
            logger.error("")
            # Sleep some, so we don't restart super fast and then exit.
            time.sleep(5.0)
            sys.exit(1)
        else:
            logger.info(f"Target Printer IP: {printerIp}")

        # The moonraker port is optional.
        # If it's specified, set it.
        # If not, don't change it, unless it's not set.
        printerPort = os.environ.get("MOONRAKER_PORT", None)
        if printerPort is not None:
            logger.info(f"Setting Moonraker Port: {printerPort}")
            config.SetStr(Config.SectionCompanion, Config.CompanionKeyPort, printerPort)
        # Ensure something is set, if not, set it to the default.
        printerPort = config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, None)
        if printerPort is None:
            logger.info("Setting The Default Moonraker Port: 7125")
            config.SetStr(Config.SectionCompanion, Config.CompanionKeyPort, "7215")
        else:
            logger.info(f"Target Moonraker Port: {printerPort}")

        # The webserver port is optional.
        # If it's specified, set it.
        # If not, don't change it, unless it's not set.
        webserverPort = os.environ.get("WEBSERVER_PORT", None)
        if webserverPort is not None:
            logger.info(f"Setting Webserver Port: {webserverPort}")
            config.SetStr(Config.RelaySection, Config.RelayFrontEndPortKey, webserverPort)
        # Ensure something is set, if not, set it to the default.
        webserverPort = config.GetStr(Config.RelaySection, Config.RelayFrontEndPortKey, None)
        if webserverPort is None:
            logger.info("Setting The Default Webserver Port: 7125")
            config.SetStr(Config.RelaySection, Config.RelayFrontEndPortKey, "80")
        else:
            logger.info(f"Target Webserver Port: {webserverPort}")
