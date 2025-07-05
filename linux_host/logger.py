import os
import sys
import logging
import logging.handlers
from typing import Optional

from .config import Config

class LoggerInit:

    # Sets up and returns the main logger object
    @staticmethod
    def GetLogger(config:Config, logDir:str, logLevelOverride:Optional[str]) -> logging.Logger:
        logger = logging.getLogger()

        # From the possible logging values, read the current value from the config.
        # If there is no value or it doesn't map, use the default.
        possibleValueList = [
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
        ]
        logLevel = config.GetStrIfInAcceptableList(Config.LoggingSection, Config.LogLevelKey, "INFO", possibleValueList)
        # GetStrIfInAcceptableList does a case insensitive check, so we need to make sure the logging case is correct.
        logLevel = logLevel.upper()

        # Check the environment variable for the log level.
        if any(os.getenv(name) is not None for name in ("DEBUG", "-DEBUG", "debug", "-debug")):
            print("Environment variable DEBUG set, setting log level to DEBUG")
            logLevel = "DEBUG"

        # Allow the dev config to override the log level.
        if logLevelOverride is not None:
            logLevelOverride = logLevelOverride.upper()
            print("Dev config override log level from "+logLevel+" to "+logLevelOverride)
            logLevel = logLevelOverride

        # Set the final log level.
        logger.setLevel(logLevel)

        # Define our format
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

        # Setup logging to standard out.
        std = logging.StreamHandler(sys.stdout)
        std.setFormatter(formatter)
        logger.addHandler(std)

        # Setup the file logger
        maxFileSizeBytes = config.GetIntIfInRange(Config.LoggingSection, Config.LogFileMaxSizeMbKey, 3, 1, 5000) * 1024 * 1024
        maxFileCount = config.GetIntIfInRange(Config.LoggingSection, Config.LogFileMaxCountKey, 1, 1, 50)
        file = logging.handlers.RotatingFileHandler(
            os.path.join(logDir, "octoeverywhere.log"),
            maxBytes=maxFileSizeBytes, backupCount=maxFileCount)
        file.setFormatter(formatter)
        logger.addHandler(file)

        return logger
