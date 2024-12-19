import os
import sys
import json
import time
import signal
import base64
import logging
import traceback
import subprocess

#
# This docker host is the entry point for the docker container.
# Unlike the other host, this host doesn't run the service, it invokes the bambu or companion host.
#

from linux_host.startup import Startup
from linux_host.config import Config

from bambu_octoeverywhere.bambucloud import BambuCloud

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
        logger.info(f"Env Vars: {os.environ}")
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
        # Step 1: Ensure all required vars are set.
        #
        #

        # The serial number is always required, in both Bambu Cloud and local connection mode.
        # So we always get that first.
        printerSn = os.environ.get("SERIAL_NUMBER", None)
        if printerSn is not None:
            logger.info(f"Setting Serial Number: {printerSn}")
            config.SetStr(Config.SectionBambu, Config.BambuPrinterSn, printerSn)
        # Ensure something is set now.
        if config.GetStr(Config.SectionBambu, Config.BambuPrinterSn, None) is None:
            logger.error("")
            logger.error("")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("               You must provide your printer's Serial Number.")
            logger.error("Use `docker run -e SERIAL_NUMBER=<token>` or add it to your docker-compose file.")
            logger.error("")
            logger.error("      To find your Serial Number -> https://octoeverywhere.com/s/bambu-sn")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("")
            logger.error("")
            # Sleep some, so we don't restart super fast and then exit.
            time.sleep(5.0)
            sys.exit(1)

        # The access code is also always required, in both Bambu Cloud and local connection mode.
        # In cloud mode the we can get the access code from the service, but it's often wrong...?
        # If there is a arg passed, always update or set it.
        # This allows users to update the values after the image has ran the first time.
        accessCode = os.environ.get("ACCESS_CODE", None)
        if accessCode is not None:
            logger.info(f"Setting Access Code: {accessCode}")
            config.SetStr(Config.SectionBambu, Config.BambuAccessToken, accessCode)
        # Ensure something is set now.
        if config.GetStr(Config.SectionBambu, Config.BambuAccessToken, None) is None:
            logger.error("")
            logger.error("")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("                  You must provide your printer's Access Code.")
            logger.error("  Use `docker run -e ACCESS_CODE=<code>` or add it to your docker-compose file.")
            logger.error("")
            logger.error("      To find your Access Code -> https://octoeverywhere.com/s/access-code")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("")
            logger.error("")
            # Sleep some, so we don't restart super fast and then exit.
            time.sleep(5.0)
            sys.exit(1)

        # For now, we also need the user to supply the printer's IP address in both modes, since we can't auto scan the network in docker.
        # We also need this for the Bambu Cloud mode, since we can't get it from the Bambu Cloud API and we can't scan for the printer.
        printerId = os.environ.get("PRINTER_IP", None)
        if printerId is not None:
            logger.info(f"Setting Printer IP: {printerId}")
            config.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, printerId)
        # Ensure something is set now.
        if config.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None) is None:
            logger.error("")
            logger.error("")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("                  You must provide your printer's IP Address.")
            logger.error(" Use `docker run -e PRINTER_IP=<ip address>` or add it to your docker-compose file.")
            logger.error("")
            logger.error("    To find your printer's IP Address -> https://octoeverywhere.com/s/bambu-ip")
            logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.error("")
            logger.error("")
            # Sleep some, so we don't restart super fast and then exit.
            time.sleep(5.0)
            sys.exit(1)

        # The port is always the same, so we just set the known Bambu Lab printer port.
        if config.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, None) is None:
            config.SetStr(Config.SectionCompanion, Config.CompanionKeyPort, "8883")

        #
        #
        # Step 2: Determine the connection mode. Bambu Cloud or Local.
        #
        #
        # Bambu updated the printer and broke LAN access unless the printer is in LAN mode.
        # The work around was to connect to the Bambu Cloud instead of directly to the printer.
        # The biggest downside of this is that we need to get the user's email address and password for Bambu Cloud.
        # BUT the user can also do the LAN only mode, if they want to.
        #
        # It seems that bambu may have walked back on the no LAN access, so for now we default to the local mode, which is preferred.
        useBambuCloud = False
        envVarBambuCloudEmailKey = "BAMBU_CLOUD_ACCOUNT_EMAIL"
        envVarBambuCloudPasswordKey = "BAMBU_CLOUD_ACCOUNT_PASSWORD"
        envVarConnectionModeKey = "CONNECTION_MODE"

        # First, we will see if the user explicitly set the mode.
        connectionModeVar = os.environ.get(envVarConnectionModeKey, None)
        if connectionModeVar is not None:
            # Ensure the passed value is what we expect.
            connectionModeVar = connectionModeVar.lower().strip()
            if connectionModeVar == "cloud" or connectionModeVar == "bambucloud":
                useBambuCloud = True
            elif connectionModeVar == "local":
                useBambuCloud = False
            else:
                logger.error("")
                logger.error("")
                logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
                logger.error("                     Invalid CONNECTION_MODE value passed.")
                logger.error("")
                logger.error("  To connect via the Bambu Cloud use the value of `cloud`.")
                logger.error("  To make a local connection via your local network use the value of `local`.")
                logger.error("")
                logger.error("  Use `docker run -e CONNECTION_MODE=<mode>` or add it to your docker-compose file.")
                logger.error("")
                logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
                logger.error("")
                logger.error("")
                # Sleep some, so we don't restart super fast and then exit.
                time.sleep(5.0)
                sys.exit(1)
            logger.info(f"Connection mode specified as: {connectionModeVar}. Use Cloud Connection Mode: {useBambuCloud}")
        else:
            # If the user didn't explicitly pick a mode, we will figure it out based on what they passed.
            # This is a legacy flag, check if first.
            lanOnlyMode = os.environ.get("LAN_ONLY_MODE", None)
            if lanOnlyMode is not None:
                useBambuCloud = not bool(lanOnlyMode.lower().strip() in ("true", "1", "yes"))
                logger.info(f"LAN_ONLY_MODE specified as: {lanOnlyMode}. Use Cloud Connection Mode: {useBambuCloud}")
            else:
                # If there are no explicit flags, check if it's set in the config.
                configMode = config.GetStr(Config.SectionBambu, Config.BambuConnectionMode, None)
                if configMode is not None:
                    if configMode == "cloud":
                        useBambuCloud = True
                    elif configMode == "local":
                        useBambuCloud = False
                    else:
                        logger.error(f"Invalid Bambu Connection Mode in config: {configMode}, defaulting to local.")
                        useBambuCloud = False
                    logger.info(f"Connection mode found in the OctoEverywhere config as `{configMode}`.")
                else:
                    # There's nothing passed and nothing in the config, figure it out based on if they user passed the email and password.
                    if os.environ.get(envVarBambuCloudEmailKey, None) is not None or os.environ.get(envVarBambuCloudPasswordKey, None) is not None:
                        useBambuCloud = True
                        logger.info("Bambu cloud email or password supplied, so we will use Bambu Cloud connection mode.")
                    else:
                        useBambuCloud = False
                        logger.info("No bambu cloud email or password passed, so we will use the preferred local connection mode.")
        logger.info(f"If you want to change the connection mode, use `{envVarConnectionModeKey}` which can be set to 'cloud' or 'local'.")

        # Explicitly set the mode in the config.
        config.SetStr(Config.SectionBambu, Config.BambuConnectionMode, "cloud" if useBambuCloud else "local")

        #
        #
        # Step 3: Get any required Bambu Cloud values.
        #
        #
        if useBambuCloud:
            # In Bambu Cloud mode, we need the user's email and password.
            bambuCloud = BambuCloud(logger, config)
            # Get any existing values.
            (bambuCloudEmail, bambuCloudPassword) = bambuCloud.GetContext(expectContextToExist=False)
            bambuCloudEmail = os.environ.get("BAMBU_CLOUD_ACCOUNT_EMAIL", bambuCloudEmail)
            bambuCloudPassword = os.environ.get("BAMBU_CLOUD_ACCOUNT_PASSWORD", bambuCloudPassword)

            # Ensure the context is already set or the user passed the email and password.
            if bambuCloudEmail is None:
                logger.error("")
                logger.error("")
                logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
                logger.error("               You must provide your Bambu Cloud account email address.")
                logger.error("Use `docker run -e BAMBU_CLOUD_ACCOUNT_EMAIL=<email>` or add it to your docker-compose file.")
                logger.error("")
                logger.error("         Your Bambu email address and password are KEPT LOCALLY, encrypted on disk")
                logger.error("                 and are NEVER SENT to the OctoEverywhere service.")
                logger.error("")
                logger.error("       For Help And More Details -> https://octoeverywhere.com/s/bambu-setup")
                logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
                logger.error("")
                logger.error("")
                # Sleep some, so we don't restart super fast and then exit.
                time.sleep(5.0)
                sys.exit(1)
            if bambuCloudPassword is None:
                logger.error("")
                logger.error("")
                logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
                logger.error("                     You must provide your Bambu Cloud account password.")
                logger.error("Use `docker run -e BAMBU_CLOUD_ACCOUNT_PASSWORD=<password>` or add it to your docker-compose file.")
                logger.error("")
                logger.error("          Your Bambu email address and password are KEPT LOCALLY, encrypted on disk")
                logger.error("                     and are NEVER SENT to the OctoEverywhere service.")
                logger.error("")
                logger.error("          For Help And More Details -> https://octoeverywhere.com/s/bambu-setup")
                logger.error("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
                logger.error("")
                logger.error("")
                # Sleep some, so we don't restart super fast and then exit.
                time.sleep(5.0)
                sys.exit(1)

            # Update the context now, since it might have changed.
            logger.info(f"Setting Bambu Cloud Context: {bambuCloudEmail}")
            if bambuCloud.SetContext(bambuCloudEmail, bambuCloudPassword) is False:
                # This should never happen. If it does allow the setup to continue, but log the error.
                logger.error("Failed to set the Bambu Cloud context.")

            # The region is optional.
            bambuCloudRegion = os.environ.get("BAMBU_CLOUD_REGION", None)
            if bambuCloudRegion is not None:
                bambuCloudRegion = bambuCloudRegion.lower().strip()
                if bambuCloudRegion != "china":
                    logger.warning("The BAMBU_CLOUD_REGION should only be set to 'china' if the account is in the China region. For all other accounts it should not be set.")
                logger.info(f"Setting Bambu Cloud Region To: {bambuCloudRegion}")
                config.SetStr(Config.SectionBambu, Config.BambuCloudRegion, bambuCloudRegion)
            # Ensure something is set now.
            if config.GetStr(Config.SectionBambu, Config.BambuCloudRegion, None) is None:
                logger.info("Setting Bambu Cloud to the default value for world wide accounts.")
                config.SetStr(Config.SectionBambu, Config.BambuCloudRegion, "worldwide")

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
        }

        # Convert the launch string into what's expected.
        launchConfigStr = json.dumps(launchConfig)
        logger.info(f"Launch config: {launchConfigStr}")
        base64EncodedLaunchConfig =  base64.urlsafe_b64encode(bytes(launchConfigStr, "utf-8")).decode("utf-8")

        # Setup a ctl-c handler, so the docker container can be closed easily.
        def signal_handler(sig, frame):
            logger.info("OctoEverywhere Bambu Connect docker container stop requested")
            sys.exit(0)
        signal.signal(signal.SIGINT, signal_handler)

        # Instead of running the plugin in our process, we decided to launch a different process so it's clean and runs
        # just like the plugin normally runs.
        pythonPath = os.path.join(virtualEnvPath, os.path.join("bin", "python3"))
        logger.info(f"Launch PY path: {pythonPath}")
        result:subprocess.CompletedProcess = subprocess.run([pythonPath, "-m", "bambu_octoeverywhere", base64EncodedLaunchConfig], check=False)

        # Normally the process shouldn't exit unless it hits a bad error.
        if result.returncode == 0:
            logger.info(f"Bambu Connect plugin exited. Result: {result.returncode}")
        else:
            logger.error(f"Bambu Connect plugin exited with an error. Result: {result.returncode}")

    except Exception as e:
        LogException("Exception while bootstrapping up OctoEverywhere Bambu Connect.", e)

    # Sleep for a bit, so if we are restarted we don't do it instantly.
    time.sleep(3)
    sys.exit(1)
