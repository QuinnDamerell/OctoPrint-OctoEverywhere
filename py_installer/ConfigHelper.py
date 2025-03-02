import os

from linux_host.config import Config

from .Logging import Logger
from .Context import Context

# Since the installer shares the common config class as the plugin, this helper helps the installer access it.
# Mostly, since the config is held in memory in the plugin, changes made by the installer should only hold the Config
# class for a short time and then flush it, and then ensure the plugin restarts shortly after it's touched by the installer.
class ConfigHelper:

    #
    # Frontend
    #

    # Given a context, this will try to find the config file and see if the frontend data is in it.
    # If any data is found, it will be returned. If there's no config or the data doesn't exist, it will return None.
    # Returns (portStr:str, frontendHint:str (can be None))
    @staticmethod
    def TryToGetFrontendDetails(context:Context):
        try:
            # Load the config, if this returns None, there is no existing file.
            c = ConfigHelper._GetConfig(context)
            if c is None:
                return (None, None)
            # Use a default of None so if they don't exist, they aren't added to the config.
            frontendPortStr = c.GetStr(Config.RelaySection, Config.RelayFrontEndPortKey, None)
            frontendTypeHint = c.GetStr(Config.RelaySection, Config.RelayFrontEndTypeHintKey, None)
            return (frontendPortStr, frontendTypeHint)
        except Exception as e:
            # There have been a few reports of this file being corrupt, so if it is, we will just fail and rewrite it.
            Logger.Warn("Failed to parse frontend details from existing config. "+str(e))
        return (None, None)


    # Writes the frontend details to the config file
    @staticmethod
    def WriteFrontendDetails(context:Context, portStr:str, frontendHint_CanBeNone:str):
        try:
            # Load the config, force it to be created if it doesn't exist.
            c = ConfigHelper._GetConfig(context, createIfNotExisting=True)
            # Write the new values
            c.SetStr(Config.RelaySection, Config.RelayFrontEndPortKey, portStr)
            c.SetStr(Config.RelaySection, Config.RelayFrontEndTypeHintKey, frontendHint_CanBeNone)
        except Exception as e:
            Logger.Error("Failed to write frontend details to config. "+str(e))
            raise Exception("Failed to write frontend details to config") from e


    #
    # Companion And Bambu Connect
    #

    # Given a context, this will try to find the config file and see if the companion data is in it.
    # These vars are shared for the companion and bambu connect logic.
    # If any data is found, it will be returned. If there's no config or the data doesn't exist, it will return None.
    # Returns (ipOrHostname:str, portStr:str)
    @staticmethod
    def TryToGetCompanionDetails(context:Context = None, configFolderPath:str = None):
        try:
            # Load the config, if this returns None, there is no existing file.
            c = ConfigHelper._GetConfig(context, configFolderPath)
            if c is None:
                return (None, None)
            # Use a default of None so if they don't exist, they aren't added to the config.
            ipOrHostname = c.GetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, None)
            portStr = c.GetStr(Config.SectionCompanion, Config.CompanionKeyPort, None)
            return (ipOrHostname, portStr)
        except Exception as e:
            # There have been a few reports of this file being corrupt, so if it is, we will just fail and rewrite it.
            Logger.Warn("Failed to parse companion details from existing config. "+str(e))
        return (None, None)


    # Writes the companion details to the config file
    @staticmethod
    def WriteCompanionDetails(context:Context, ipOrHostname:str, portStr:str):
        try:
            # Load the config, force it to be created if it doesn't exist.
            c = ConfigHelper._GetConfig(context, createIfNotExisting=True)
            # Write the new values
            c.SetStr(Config.SectionCompanion, Config.CompanionKeyIpOrHostname, ipOrHostname)
            c.SetStr(Config.SectionCompanion, Config.CompanionKeyPort, portStr)
        except Exception as e:
            Logger.Error("Failed to write companion details to config. "+str(e))
            raise Exception("Failed to write companion details to config") from e


    #
    # Bambu Connect Only
    #

    # Given a context, this will try to find the config file and see if the bambu data is in it.
    # If any data is found, it will be returned. If there's no config or the data doesn't exist, it will return None.
    # Returns (accessToken:str, printerSn:str)
    @staticmethod
    def TryToGetBambuData(context:Context = None, configFolderPath:str = None):
        try:
            # Load the config, if this returns None, there is no existing file.
            c = ConfigHelper._GetConfig(context, configFolderPath)
            if c is None:
                return (None, None)
            # Use a default of None so if they don't exist, they aren't added to the config.
            accessToken = c.GetStr(Config.SectionBambu, Config.BambuAccessToken, None)
            printerSn = c.GetStr(Config.SectionBambu, Config.BambuPrinterSn, None)
            return (accessToken, printerSn)
        except Exception as e:
            # There have been a few reports of this file being corrupt, so if it is, we will just fail and rewrite it.
            Logger.Warn("Failed to parse bambu details from existing config. "+str(e))
        return (None, None)


    # Writes the bambu details to the config file
    @staticmethod
    def WriteBambuDetails(context:Context, accessToken:str, printerSn:str):
        try:
            # Load the config, force it to be created if it doesn't exist.
            c = ConfigHelper._GetConfig(context, createIfNotExisting=True)
            # Write the new values
            c.SetStr(Config.SectionBambu, Config.BambuAccessToken, accessToken)
            c.SetStr(Config.SectionBambu, Config.BambuPrinterSn, printerSn)
            # The installer can only setup local connections right now, which is preferred since cloud doesn't work well.
            c.SetStr(Config.SectionBambu, Config.BambuConnectionMode, Config.BambuConnectionModeDefault)
        except Exception as e:
            Logger.Error("Failed to write bambu details to config. "+str(e))
            raise Exception("Failed to write bambu details to config") from e


    #
    # Elegoo Only
    #


    @staticmethod
    def TryToGetElegooData(context:Context = None, configFolderPath:str = None) -> str:
        try:
            # Load the config, if this returns None, there is no existing file.
            c = ConfigHelper._GetConfig(context, configFolderPath)
            if c is None:
                return None
            # Use a default of None so if they don't exist, they aren't added to the config.
            return c.GetStr(Config.SectionElegoo, Config.ElegooMainboardId, None)
        except Exception as e:
            # There have been a few reports of this file being corrupt, so if it is, we will just fail and rewrite it.
            Logger.Warn("Failed to parse elegoo details from existing config. "+str(e))
        return None


    # Writes the bambu details to the config file
    @staticmethod
    def WriteElegooDetails(context:Context, mainboardId:str):
        try:
            # Load the config, force it to be created if it doesn't exist.
            c = ConfigHelper._GetConfig(context, createIfNotExisting=True)
            # Write the new values
            c.SetStr(Config.SectionElegoo, Config.ElegooMainboardId, mainboardId)
        except Exception as e:
            Logger.Error("Failed to write elegoo details to config. "+str(e))
            raise Exception("Failed to write elegoo details to config") from e


    #
    # Helpers
    #

    # Given a context or folder path, this will return if there's any existing config file yet or not.
    @staticmethod
    def DoesConfigFileExist(context:Context = None, configFolderPath:str = None) -> bool:
        configFilePath = None
        if context is not None:
            configFilePath = ConfigHelper.GetConfigFilePath(context)
        elif configFolderPath is not None:
            configFilePath = ConfigHelper.GetConfigFilePath(configFolderPath=configFolderPath)
        else:
            raise Exception("DoesConfigFileExist no context or file path passed.")
        return os.path.exists(configFilePath) and os.path.isfile(configFilePath)


    # Given a context or config file path, this returns file path of the config.
    # If the context is missing the ConfigFolder, None is returned.
    @staticmethod
    def GetConfigFilePath(context:Context = None, configFolderPath:str = None):
        if context is not None:
            if context.ConfigFolder is None:
                # Don't throw here, return None and let the caller handle it, incase it's ok to not have a config folder set.
                return None
            return Config.GetConfigFilePath(context.ConfigFolder)
        if configFolderPath is not None:
            return Config.GetConfigFilePath(configFolderPath)
        raise Exception("GetConfigFilePath was passed no config or config folder path.")


    # Given a context or folder path, this returns a config object if it exists.
    # If the file doesn't exist and createIfNotExisting is False, None is returned.
    # Otherwise a new config will be created.
    @staticmethod
    def _GetConfig(context:Context = None, configFolderPath:str = None, createIfNotExisting:bool = False):
        if ConfigHelper.DoesConfigFileExist(context, configFolderPath) is False:
            if createIfNotExisting:
                # Fallthrough, the Config class will create a file if none exists.
                Logger.Debug("Creating main plugin config file.")
            else:
                return None
        # Get the config folder path.
        if configFolderPath is None:
            configFolderPath = context.ConfigFolder
        if configFolderPath is None:
            raise Exception("_GetConfig was called with an invalid context and now config folder path.")
        # Open or create the config.
        return Config(configFolderPath)
