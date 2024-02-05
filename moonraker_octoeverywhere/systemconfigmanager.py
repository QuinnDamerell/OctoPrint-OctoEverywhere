import os
import subprocess

class SystemConfigManager:

    # This can't change or it will break old installs.
    c_updateConfigFileName = "octoeverywhere-system.cfg"

    # We use config files to integrate into moonraker's update manager, which allows our plugin repo to stay updated.
    # This also write a block that's used to allow the announcement system to show updates from our repo.
    # This function ensures they exist and are up to date. If not, they are fixed.
    @staticmethod
    def EnsureUpdateManagerFilesSetup(logger, klipperConfigDir, serviceName, pyVirtEnvRoot, repoRoot):

        # Special case for K1 and K1 max setups. If the service file name is the special init.d name, we can just use
        # the started "octoeverywhere" and the update manager will find the right service to manage.
        if serviceName.startswith("S66"):
            serviceName = "octoeverywhere"

        # Create the expected update config contents
        # Note that the update_manager extension name and the managed_services names must match, and the must match the systemd service file name.
        d = {
            'RepoRootFolder': repoRoot,
            'ServiceName' : serviceName,
            'pyVirtEnvRoot' : pyVirtEnvRoot,
        }
        expectedUpdateFileContent = '''\
[update_manager {ServiceName}]
type: git_repo
# Using `channel: beta` makes moonraker only update to the lasted tagged commit on the branch. Which lets us control releases.
channel: beta
path: {RepoRootFolder}
origin: https://github.com/QuinnDamerell/OctoPrint-OctoEverywhere.git
# env is deprecated, but we must keep it around for now, for older installs.
env: {pyVirtEnvRoot}/bin/python
virtualenv: {pyVirtEnvRoot}
requirements: requirements.txt
# system_dependencies is newer and replaces install_script for the list of system deps. But for now we need both for older installs.
system_dependencies: moonraker-system-dependencies.json
install_script: install.sh
managed_services:
  {ServiceName}

# This allows users of OctoEverywhere to get announcements from the system.
[announcements]
subscriptions:
    octoeverywhere
'''.format(**d)

        # Create the expected file path of our update config.
        oeUpdateConfigFile = os.path.join(klipperConfigDir, SystemConfigManager.c_updateConfigFileName)

        # Ensure that the main moonraker config file has the include for our sub config file
        SystemConfigManager._ensureMoonrakerConfigHasUpdateConfigInclude(klipperConfigDir, logger)

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

        # Whenever we update the file on disk, also restart moonraker so that it reads it and
        # pull the update information into the update manager. It's safe to restart moonraker during a print
        # so this won't effect anything.
        logger.info("No config file was found on disk, so we are going to attempt to restart moonraker.")
        try:
            SystemConfigManager._RunShellCommand("systemctl restart moonraker")
        except Exception as e:
            logger.warn("Failed to restart moonraker service. "+str(e))


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
        includeText = "[include "+SystemConfigManager.c_updateConfigFileName+"]"
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


    @staticmethod
    def _RunShellCommand(cmd):
        status = subprocess.call(cmd, shell=True)
        if status != 0:
            raise Exception("Command "+cmd+" failed to execute. Code: "+str(status))
