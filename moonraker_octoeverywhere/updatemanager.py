import os

class UpdateManager:

    # This can't change or it will break old installs.
    c_updateConfigFileName = "octoeverywhere-update.cfg"

    # We use config files to integrate into moonraker's update manager, which allows our plugin repo to stay updated.
    # This function ensures they exist and are up to date. If not, they are fixed.
    @staticmethod
    def EnsureUpdateManagerFilesSetup(logger, klipperConfigDir, serviceName, pyVirtEnvRoot, repoRoot):

        # Create the expected update config contents
        d = {
            'RepoRootFolder': repoRoot,
            'ServiceName' : serviceName,
            'pyVirtEnvRoot' : pyVirtEnvRoot,
        }
        expectedUpdateFileContent = '''\
[update_manager octoeverywhere]
type: git_repo
# Using `channel: beta` makes moonraker only update to the lasted tagged commit on the branch. Which lets us control releases.
channel: beta
path: {RepoRootFolder}
origin: https://github.com/QuinnDamerell/OctoPrint-OctoEverywhere.git
env: {pyVirtEnvRoot}/bin/python
requirements: requirements.txt
install_script: install.sh
managed_services:
  {ServiceName}
    '''.format(**d)

        # Create the expected file path of our update config.
        oeUpdateConfigFile = os.path.join(klipperConfigDir, UpdateManager.c_updateConfigFileName)

        # Ensure that the main moonraker config file has the include for our sub config file
        UpdateManager._ensureMoonrakerConfigHasUpdateConfigInclude(klipperConfigDir, logger)

        # See if there's an existing file, and if the file contents match this exactly.
        # If so, there's no need to update it.
        if os.path.exists(oeUpdateConfigFile):
            with open(oeUpdateConfigFile, "r", encoding="utf-8") as file:
                existingFileContents = file.read()
                if existingFileContents == expectedUpdateFileContent:
                    logger.info("Existing update config file found with the correct file contents.")
                    return

        # We need to create or update the file.
        with open(oeUpdateConfigFile, "w", encoding="utf-8") as file:
            file.write(expectedUpdateFileContent)
        logger.info("No update config found or it was out of date, writing a new file.")


    # This doesn't relate to the update manager, but if we put our service name in this file
    # The user can then use the UI buttons to start, restart, and stop it.
    # Details: https://moonraker.readthedocs.io/en/latest/configuration/#allowed-services
    # TODO - Eventually we will get our PR in that will add this to moonraker's default list.
    @staticmethod
    def EnsureAllowedServicesFile(logger, klipperConfigDir, serviceName):
        # Make the expected file path, it should be one folder up from the config folder
        dataRootDir = os.path.abspath(os.path.join(klipperConfigDir, os.pardir))
        allowedServiceFile = os.path.join(dataRootDir, "moonraker.asvc")

        # Test if we have a file.
        if os.path.exists(allowedServiceFile) is False:
            # This isn't the end of the world, so don't worry about it
            logger.info("Failed to find moonraker allowed services file.")
            return

        # Check if we are already in the file.
        with open(allowedServiceFile, "r", encoding="utf-8") as file:
            lines = file.readlines()
            for l in lines:
                # Use in, because the lines will have new lines and such.
                # Match case, because the entry in the file must match the service name case.
                if serviceName in l:
                    logger.info("We found our name existing in the moonraker allowed service file, so there's nothing to do.")
                    return

        # Add our name.
        with open(allowedServiceFile,'a', encoding="utf-8") as f:
            # The current format this doc is not have a trailing \n, so we need to add one.
            f.write("\n"+serviceName)
        logger.info("Our name wasn't found in moonraker's allowed service file, so we added it.")


    @staticmethod
    def _ensureMoonrakerConfigHasUpdateConfigInclude(klipperConfigDir, logger):
        # Create the path where we should find the file, and make sure it exists. If not throw, so things blow up.
        moonrakerConfigFileName = "moonraker.conf"
        moonrakerConfigFilePath = os.path.join(klipperConfigDir, moonrakerConfigFileName)
        if os.path.exists(moonrakerConfigFilePath) is False:
            raise Exception("Failed to find the "+moonrakerConfigFileName+" file in dir. Expected: "+moonrakerConfigFilePath)

        # Look for our include in the main moonraker config file.
        includeText = "[include "+UpdateManager.c_updateConfigFileName+"]"
        with open(moonrakerConfigFilePath, "r", encoding="utf-8") as file:
            lines = file.readlines()
            for l in lines:
                if includeText.lower() in l.lower():
                    logger.info("Our existing update config file include was found in the moonraker config.")
                    return

        # The text wasn't found, append it to the end.
        with open(moonrakerConfigFilePath,'a', encoding="utf-8") as f:
            f.write("\n"+includeText+"\n")
        logger.info("Our update config include was not found in the moonraker config, so we added it.")
