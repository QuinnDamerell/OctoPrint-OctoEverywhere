import logging
import os
import sys
import time

from linux_host.config import Config


class PrusaLinkBootstrap:

    @staticmethod
    def Bootstrap(logger:logging.Logger, config:Config) -> None:

        printerIp = os.environ.get("PRINTER_IP", None)
        if printerIp is not None:
            logger.info(f"Setting Printer IP: {printerIp}")
            config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, printerIp)
        printerIp = config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        if printerIp is None:
            logger.error("")
            logger.error("")
            logger.error("")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("                    You must provide your Prusa Link's IP address.")
            logger.error("    Use `docker run -e PRINTER_IP=<ip address>` or add it to your docker-compose file.")
            logger.error("")
            logger.error("           To find your IP address -> https://octoeverywhere.com/s/prusa-link-ip")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("")
            logger.error("")
            time.sleep(5.0)
            sys.exit(1)
        logger.info(f"Target Printer IP: {printerIp}")

        printerPort = os.environ.get("PRUSALINK_PORT", None)
        if printerPort is not None:
            logger.info(f"Setting Prusa Link Port: {printerPort}")
            config.SetStr(Config.SectionCompanion, Config.CompanionKeyPort, printerPort)
        printerPort = config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, None)
        if printerPort is None:
            printerPort = Config.PrusaLinkDefaultPortStr
            logger.info(f"Setting The Default Prusa Link Port: {printerPort}")
            config.SetStr(Config.SectionCompanion, Config.CompanionKeyPort, printerPort)
        try:
            portInt = int(printerPort)
            if portInt <= 0 or portInt > 65535:
                raise ValueError("port out of range")
        except Exception:
            logger.error("")
            logger.error("")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("                 The Prusa Link port must be a number between 1 and 65535.")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("")
            logger.error("")
            time.sleep(5.0)
            sys.exit(1)
        logger.info(f"Target Prusa Link Port: {printerPort}")

        #
        # Handle Auth
        #
        apiKey = os.environ.get("API_KEY", os.environ.get("APIKEY", None))
        if apiKey is not None:
            logger.info("Setting Prusa Link API key auth.")
            config.SetStr(Config.SectionPrusaLink, Config.PrusaLinkApiKey, apiKey)
            config.SetStr(Config.SectionPrusaLink, Config.PrusaLinkUsername, None)
            config.SetStr(Config.SectionPrusaLink, Config.PrusaLinkPassword, None)
        username = os.environ.get("USERNAME", None)
        if username is not None:
            logger.info(f"Setting Prusa Link Username: {username}")
            config.SetStr(Config.SectionPrusaLink, Config.PrusaLinkUsername, username)
            config.SetStr(Config.SectionPrusaLink, Config.PrusaLinkApiKey, None)
        password = os.environ.get("PASSWORD", None)
        if password is not None:
            logger.info("Setting Prusa Link Password.")
            config.SetStr(Config.SectionPrusaLink, Config.PrusaLinkPassword, password)
            config.SetStr(Config.SectionPrusaLink, Config.PrusaLinkApiKey, None)

        # Now check what we have.
        apiKey = config.GetStr(Config.SectionPrusaLink, Config.PrusaLinkApiKey, None)
        username = config.GetStr(Config.SectionPrusaLink, Config.PrusaLinkUsername, None)
        password = config.GetStr(Config.SectionPrusaLink, Config.PrusaLinkPassword, None)
        if apiKey is None and password is None and username is None:
            logger.error("")
            logger.error("")
            logger.error("")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("               You provided your Prusa Link username/password or API key.")
            logger.error("            Use `docker run -e API_KEY=<api_key>` or `-e API_KEY=<api_key>`.")
            logger.error("                                          OR")
            logger.error("           Use `docker run -e USERNAME=<username>` or `-e USERNAME=<username>`.")
            logger.error("           Use `docker run -e PASSWORD=<password>` or `-e PASSWORD=<password>`.")
            logger.error("")
            logger.error("    Need Help? Visit https://octoeverywhere.com/s/prusa-link-api-key for more information.")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("")
            logger.error("")
            time.sleep(5.0)
            sys.exit(1)

        # If we have an API key, use that as the auth mode.
        if apiKey is not None:
            logger.info("Using Prusa Link API key auth.")
            config.SetStr(Config.SectionPrusaLink, Config.PrusaLinkAuthMode, Config.PrusaLinkAuthModeApiKey)
            return

        # Otherwise use a username and password.
        logger.info("Setting Prusa Link username/password auth.")
        config.SetStr(Config.SectionPrusaLink, Config.PrusaLinkAuthMode, Config.PrusaLinkAuthModePassword)
        if password is None or username is None:
            logger.error("")
            logger.error("")
            logger.error("")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("               You provided your Prusa Link username/password or API key.")
            logger.error("            Use `docker run -e API_KEY=<api_key>` or `-e API_KEY=<api_key>`.")
            logger.error("                                          OR")
            logger.error("           Use `docker run -e USERNAME=<username>` or `-e USERNAME=<username>`.")
            logger.error("           Use `docker run -e PASSWORD=<password>` or `-e PASSWORD=<password>`.")
            logger.error("")
            logger.error("    Need Help? Visit https://octoeverywhere.com/s/prusa-link-api-key for more information.")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("")
            logger.error("")
            time.sleep(5.0)
            sys.exit(1)
