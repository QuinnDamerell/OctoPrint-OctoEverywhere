import os
import sys
import json
import time
import signal
import base64
import logging
import traceback
import subprocess

from enum import Enum

#
# This docker host is the entry point for the docker container.
# Unlike the other host, this host doesn't run the service, it invokes the bambu or companion host.
#

from linux_host.startup import Startup
from linux_host.config import Config

from .BambuBootstrap import BambuBootstrap
from .ElegooBootstrap import ElegooBootstrap
from .KlipperBootstrap import KlipperBootstrap


# Possible modes for the companion.
class CompanionMode(Enum):
    BambuConnect  = 1
    ElegooConnect = 2
    Klipper   = 3

    # Makes to str() cast not to include the class name.
    def __str__(self):
        return self.name


# pylint: disable=logging-fstring-interpolation

if __name__ == '__main__':

    # Setup a basic logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    std = logging.StreamHandler(sys.stdout)
    std.setFormatter(formatter)
    logger.addHandler(std)

    logger.info("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
    logger.info("Starting Docker OctoEverywhere Bootstrap")
    logger.info("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")

    # This is a helper class, to keep the startup logic common.
    s = Startup()

    #
    # Helper functions
    #
    def LogException(msg:str, e:Exception) -> None:
        tb = traceback.format_exc()
        exceptionClassType = "unknown_type"
        if e is not None:
            exceptionClassType = e.__class__.__name__
        logger.error(f"{msg}; {str(exceptionClassType)} Exception: {str(e)}; {str(tb)}")

    def EnsureIsPath(path: str) -> str:
        logger.info(f"Ensuring path exists: {path}")
        if path is None or not os.path.exists(path):
            raise Exception(f"Path does not exist: {path}")
        return path

    def CreateDirIfNotExists(path: str) -> None:
        if not os.path.exists(path):
            os.makedirs(path)

    try:
        # First, read the required env vars that are set in the dockerfile.
        virtualEnvPath = EnsureIsPath(os.environ.get("VENV_DIR", None))
        repoRootPath = EnsureIsPath(os.environ.get("REPO_DIR", None))
        dataPath = EnsureIsPath(os.environ.get("DATA_DIR", None))

        # For Bambu Connect, the config sits int the data dir.
        configPath = dataPath

        # Create the config object, which will read an existing config or make a new one.
        # If this is the first run, there will be no config file, so we need to create one.
        logger.info(f"Init config object: {configPath}")
        config = Config(configPath)

        #
        #
        # Step 1: Figure out the Companion mode.
        #
        #

        # Default to Bambu Connect, since it existed before we supported different modes.
        mode:CompanionMode = CompanionMode.BambuConnect
        modeStr = os.environ.get("COMPANION_MODE", None)
        if modeStr is not None:
            modeStr = modeStr.lower()
            if modeStr == "bambu":
                mode = CompanionMode.BambuConnect
            elif modeStr == "elegoo":
                mode = CompanionMode.ElegooConnect
            elif modeStr == "klipper":
                mode = CompanionMode.Klipper
            logger.info(f"Companion mode: {mode}")
        else:
            logger.info("No companion mode set, defaulting to Bambu Connect.")

        #
        #
        # Step 1: Ensure all required vars are set.
        #
        #
        if mode == CompanionMode.BambuConnect:
            BambuBootstrap.Bootstrap(logger, config)
        elif mode == CompanionMode.ElegooConnect:
            ElegooBootstrap.Bootstrap(logger, config)
        elif mode == CompanionMode.Klipper:
            KlipperBootstrap.Bootstrap(logger, config)
        else:
            raise Exception(f"Invalid companion mode: {mode}")

        # Create the rest of the required dirs based in the data dir, since it's persistent.
        localStoragePath = os.path.join(dataPath, "octoeverywhere-store")
        CreateDirIfNotExists(localStoragePath)
        logDirPath = os.path.join(dataPath, "logs")
        CreateDirIfNotExists(logDirPath)

        # Build the launch string
        launchConfig = {
            "ServiceName" : "octoeverywhere", # Since there's only once service, use the default name.
            "CompanionInstanceIdStr" : "1",   # Since there's only once service, use the default service id.
            "VirtualEnvPath" : virtualEnvPath,
            "RepoRootFolder" : repoRootPath,
            "LocalFileStoragePath" : localStoragePath,
            "LogFolder" : logDirPath,
            "ConfigFolder" : configPath,
            'IsCompanion' : True,
        }

        # Convert the launch string into what's expected.
        launchConfigStr = json.dumps(launchConfig)
        logger.info(f"Launch config: {launchConfigStr}")
        base64EncodedLaunchConfig =  base64.urlsafe_b64encode(bytes(launchConfigStr, "utf-8")).decode("utf-8")

        # Setup a ctl-c handler, so the docker container can be closed easily.
        def signal_handler(sig, frame):
            logger.info("OctoEverywhere Connect Docker - container stop requested")
            sys.exit(0)
        signal.signal(signal.SIGINT, signal_handler)

        # Get the correct package to launch.
        pyPackage = "bambu_octoeverywhere"
        if mode == CompanionMode.ElegooConnect:
            pyPackage = "elegoo_octoeverywhere"
        elif mode == CompanionMode.Klipper:
            pyPackage = "moonraker_octoeverywhere"

        # Instead of running the plugin in our process, we decided to launch a different process so it's clean and runs
        # just like the plugin normally runs.
        pythonPath = os.path.join(virtualEnvPath, os.path.join("bin", "python3"))
        logger.info(f"Launch PY path: {pythonPath}")
        result:subprocess.CompletedProcess = subprocess.run([pythonPath, "-m", pyPackage, base64EncodedLaunchConfig], check=False)

        # Normally the process shouldn't exit unless it hits a bad error.
        if result.returncode == 0:
            logger.info(f"OctoEverywhere Connect Docker - plugin exited. Result: {result.returncode}")
        else:
            logger.error(f"OctoEverywhere Connect Docker - plugin exited with an error. Result: {result.returncode}")

    except Exception as e:
        LogException("Exception while bootstrapping up OctoEverywhere Connect.", e)

    # Sleep for a bit, so if we are restarted we don't do it instantly.
    time.sleep(3)
    sys.exit(1)
