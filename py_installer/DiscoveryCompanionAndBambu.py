import os

from .Logging import Logger
from .Context import Context
from .Util import Util
from .ConfigHelper import ConfigHelper

# This class does the same function as the Discovery class, but for companion or Bambu Connect plugins.
# Note that "Bambu Connect" is really just a type of companion plugin, but we use different names so it feels correct.
class DiscoveryCompanionAndBambu:

    # This is the base data folder name that will be used, the plugin id suffix will be added to end of it.
    # The folders will always be in the user's home path.
    # These MUST start with a . and be LOWER CASE for the matching logic below to work correctly!
    # The primary instance (id == "1") will have no "-#" suffix on the folder or service name.
    c_CompanionPluginDataRootFolder_Lower = ".octoeverywhere-companion"
    c_BambuPluginDataRootFolder_Lower = ".octoeverywhere-bambu"


    def Discovery(self, context:Context):
        Logger.Debug("Starting companion discovery.")

        # Used for printing the type, like "would you like to install a new {pluginTypeStr} plugin?"
        pluginTypeStr = "Bambu Connect" if context.IsBambuSetup else "Companion"

        # Look for existing companion or bambu data installs.
        existingCompanionFolders = []
        # Sort so the folder we find are ordered from 1-... This makes the selection process nicer, since the ID == selection.
        fileAndDirList = sorted(os.listdir(context.UserHomePath))
        for fileOrDirName in fileAndDirList:
            # Use starts with to see if it matches any of our possible folder names.
            # Since each setup only targets companion or bambu connect, only pick the right folder type.
            fileOrDirNameLower = fileOrDirName.lower()
            if context.IsCompanionSetup:
                if fileOrDirNameLower.startswith(DiscoveryCompanionAndBambu.c_CompanionPluginDataRootFolder_Lower):
                    existingCompanionFolders.append(fileOrDirName)
                    Logger.Debug(f"Found existing companion data folder: {fileOrDirName}")
            elif context.IsBambuSetup:
                if fileOrDirNameLower.startswith(DiscoveryCompanionAndBambu.c_BambuPluginDataRootFolder_Lower):
                    existingCompanionFolders.append(fileOrDirName)
                    Logger.Debug(f"Found existing bambu data folder: {fileOrDirName}")
            else:
                raise Exception("DiscoveryCompanionAndBambu used in non companion or bambu connect context.")

        # If there's an existing folders, ask the user if they want to use them.
        if len(existingCompanionFolders) > 0:
            count = 1
            Logger.Blank()
            Logger.Header(f"Existing {pluginTypeStr} Plugins Found")
            Logger.Blank()
            Logger.Info( "If you want to update or recover an existing plugin enter the Plugin ID from the list below.")
            Logger.Info( "                          - or - ")
            Logger.Info(f"If you want to install a new {pluginTypeStr} plugin, enter 'n'.")
            Logger.Blank()
            Logger.Info("Options:")
            for folder in existingCompanionFolders:
                instanceId = self._GetCompanionOrBambuIdFromFolderName(folder)
                # Try to parse the config, if there is one and it's valid.
                ip, port = ConfigHelper.TryToGetCompanionDetails(configFolderPath=os.path.join(context.UserHomePath, folder))
                if ip is None and port is None:
                    Logger.Info(f"  {count}) Plugin ID {instanceId} - Path: {folder}")
                else:
                    Logger.Info(f"  {count}) Plugin ID {instanceId} - {ip}:{port}")
                count += 1
            Logger.Info(f"  n) Setup a new {pluginTypeStr} plugin instance")
            Logger.Blank()
            # Ask the user which number they want.
            responseInt = -1
            isFirstPrint = True
            while True:
                try:
                    if isFirstPrint:
                        isFirstPrint = False
                    else:
                        Logger.Warn( "If you need help, contact us! https://octoeverywhere.com/support")
                    response = input("Enter a Plugin ID from the list above or 'n': ")
                    response = response.lower().strip()
                    # If the response is n, fall through.
                    if response == "n":
                        break
                    # Parse the input and -1 it, so it aligns with the array length.
                    tempInt = int(response.lower().strip()) - 1
                    if tempInt >= 0 and tempInt < len(existingCompanionFolders):
                        responseInt = tempInt
                        break
                    Logger.Blank()
                    Logger.Warn("Invalid number selection, try again.")
                except Exception as _:
                    Logger.Blank()
                    Logger.Warn("Invalid input, try again.")

            # If there is a response, the user selected an instance.
            if responseInt != -1:
                # Use this instance
                self._SetupContextFromVars(context, existingCompanionFolders[responseInt])
                Logger.Info(f"Existing {pluginTypeStr} plugin selected. Path: {context.CompanionDataRoot}, Id: {context.CompanionInstanceId}")
                return

        # Create a new instance path. Either there is no existing data path or the user wanted to create a new one.
        # There is a special case for instance ID "1", we use no suffix. All others will have the suffix.
        newId = str(len(existingCompanionFolders) + 1)
        folderNameRoot = DiscoveryCompanionAndBambu.c_BambuPluginDataRootFolder_Lower if context.IsBambuSetup else DiscoveryCompanionAndBambu.c_CompanionPluginDataRootFolder_Lower
        fullFolderName = folderNameRoot if newId == Context.CompanionPrimaryInstanceId else f"{folderNameRoot}-{newId}"
        self._SetupContextFromVars(context, fullFolderName)
        Logger.Info(f"Creating a new {pluginTypeStr} plugin data path. Path: {context.CompanionDataRoot}, Id: {context.CompanionInstanceId}")
        return


    def _SetupContextFromVars(self, context:Context, folderName:str):
        # First, ensure we can parse the id and set it.
        context.CompanionInstanceId = self._GetCompanionOrBambuIdFromFolderName(folderName)

        # Make the full path
        context.CompanionDataRoot = os.path.join(context.UserHomePath, folderName)

        # Ensure the file exists and we have permissions
        Util.EnsureDirExists(context.CompanionDataRoot, context, True)


    # Returns the instance id, for primary instances, this returns "1"
    def _GetCompanionOrBambuIdFromFolderName(self, folderName:str):
        folderName_lower = folderName.lower()

        # If the folder name starts with any of these, then its a folder we can get the instance for.
        # We will get the suffix for the folder path and then figure out the id.
        folderSuffix = None
        if folderName_lower.startswith(DiscoveryCompanionAndBambu.c_CompanionPluginDataRootFolder_Lower) is True:
            folderSuffix = folderName_lower[len(DiscoveryCompanionAndBambu.c_CompanionPluginDataRootFolder_Lower):]
        elif folderName_lower.startswith(DiscoveryCompanionAndBambu.c_BambuPluginDataRootFolder_Lower) is True:
            folderSuffix = folderName_lower[len(DiscoveryCompanionAndBambu.c_BambuPluginDataRootFolder_Lower):]
        else:
            Logger.Error(f"We tried to get an companion or bambu connect ID from a non-companion or bambu connect data folder. {folderName}")
            raise Exception("We tried to get an companion or bambu connect ID from a non-companion or bambu connect data folder")

        # If there is no suffix, this is the primary instance
        if folderSuffix is None or len(folderSuffix) == 0:
            return Context.CompanionPrimaryInstanceId

        # Otherwise, remove the - and return the id.
        if folderSuffix.startswith("-") is False:
            Logger.Error(f"We tried to get an companion or bambu connect ID but the suffix didn't start with a -. {folderName}")
            raise Exception("We tried to get an companion or bambu connect ID but the suffix didn't start with a -")
        return folderSuffix[1:]
