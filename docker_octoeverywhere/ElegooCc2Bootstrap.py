import logging
import os
import sys
import time

from linux_host.config import Config


class ElegooCc2Bootstrap:

    @staticmethod
    def Bootstrap(logger:logging.Logger, config:Config) -> None:

        # CC2 uses MQTT over TCP on 1883 for the host connection. The web UI MQTT proxy uses 9001
        # at runtime, but the host still tracks the primary MQTT port here.
        config.SetStr(Config.SectionElegoo, Config.ElegooPrinterProtocol, Config.ElegooPrinterProtocolCc2)
        config.SetStr(Config.SectionCompanion, Config.CompanionKeyPort, "1883")
        config.SetStr(Config.RelaySection, Config.RelayFrontEndPortKey, "80")

        printerIp = os.environ.get("PRINTER_IP", None)
        if printerIp is not None:
            logger.info(f"Setting Printer IP: {printerIp}")
            config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, printerIp)
        printerIp = config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
        if printerIp is None:
            logger.error("")
            logger.error("")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("             You must provide your Elegoo CC2 printer's IP address.")
            logger.error("     Use `docker run -e PRINTER_IP=<ip address>` or add it to your docker-compose file.")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("")
            logger.error("")
            time.sleep(5.0)
            sys.exit(1)
        logger.info(f"Target Printer IP: {printerIp}")

        serialNumber = os.environ.get("SERIAL_NUMBER", None)
        if serialNumber is not None:
            logger.info(f"Setting Elegoo CC2 Serial Number: {serialNumber}")
            config.SetStr(Config.SectionElegoo, Config.ElegooCc2PrinterSn, serialNumber)

        accessCode = os.environ.get("ACCESS_CODE", None)
        if accessCode is not None:
            logger.info("Setting Elegoo CC2 Access Code from environment.")
            config.SetStr(Config.SectionElegoo, Config.ElegooCc2AccessCode, accessCode)
        elif config.GetStr(Config.SectionElegoo, Config.ElegooCc2AccessCode, None) is None:
            logger.info("No Elegoo CC2 access code configured; using the factory default token.")
            config.SetStr(Config.SectionElegoo, Config.ElegooCc2AccessCode, "123456")
