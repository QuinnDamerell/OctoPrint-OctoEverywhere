import os
import sys
import logging
import logging.handlers

class LoggerInit:

    # Sets up and returns the main logger object
    @staticmethod
    def GetLogger(config, klipperLogDir) -> logging.Logger:
        logger = logging.getLogger()

        # From the possible logging values, read the current value from the config.
        # If there is no value or it doesn't map, use the default.
        logLevelMap = {
                'DEBUG': logging.DEBUG,
                'INFO' : logging.INFO,
                'WARNING': logging.WARNING,
                'ERROR': logging.ERROR,
        }
        logLevel = logLevelMap.get(config.Get("log", "level", "INFO").upper(), logging.INFO)
        logger.setLevel(logLevel)

        # Define our format
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

        # Setup logging to standard out.
        std = logging.StreamHandler(sys.stdout)
        std.setFormatter(formatter)
        logger.addHandler(std)

        # Setup the file logger
        file = logging.handlers.RotatingFileHandler(os.path.join(klipperLogDir, "octoeverywhere.log"), maxBytes=20000000, backupCount=5)
        file.setFormatter(formatter)
        logger.addHandler(file)

        return logger
