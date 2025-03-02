import logging

from linux_host.config import Config

class KlipperBootstrap:

    @staticmethod
    def Bootstrap(logger:logging.LoggerAdapter, config:Config) -> None:

        # # The serial number is always required, in both Bambu Cloud and local connection mode.
        # # So we always get that first.
        # printerSn = os.environ.get("SERIAL_NUMBER", None)
        # if printerSn is not None:
        #     logger.info(f"Setting Serial Number: {printerSn}")
        #     config.SetStr(Config.SectionBambu, Config.BambuPrinterSn, printerSn)
        # # Ensure something is set now.
        # if config.GetStr(Config.SectionBambu, Config.BambuPrinterSn, None) is None:
        #     logger.error("")
        #     logger.error("")
        #     logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        #     logger.error("               You must provide your printer's Serial Number.")
        #     logger.error("Use `docker run -e SERIAL_NUMBER=<token>` or add it to your docker-compose file.")
        #     logger.error("")
        #     logger.error("      To find your Serial Number -> https://octoeverywhere.com/s/bambu-sn")
        #     logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        #     logger.error("")
        #     logger.error("")
        #     # Sleep some, so we don't restart super fast and then exit.
        #     time.sleep(5.0)
        #     sys.exit(1)

        logger.info("Klipper Bootstrap Complete.")
