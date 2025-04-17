from enum import Enum
import os
from typing import Optional, Tuple

from linux_host.config import Config

from .Logging import Logger
from .Context import Context


# Frontends that are known.
class KnownFrontends(Enum):
    Unknown  = 1
    Mainsail = 2
    Fluidd   = 3
    Creality = 4 # This is Creality's K1 default web interface (not nearly as good as the others)
    Elegoo   = 5 # This is Elegoo's default web interface.

    # Makes to str() cast not to include the class name.
    def __str__(self):
        return self.name


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
    def TryToGetFrontendDetails(context:Context) -> Tuple[Optional[str], Optional[str]]:
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
    def WriteFrontendDetails(context:Context, portStr:str, frontendHint:Optional[KnownFrontends]) -> None:
        try:
            # Load the config, force it to be created if it doesn't exist.
            c = ConfigHelper._GetConfig_CreateIfNotExisting(context)
            # Write the new values
            c.SetStr(Config.RelaySection, Config.RelayFrontEndPortKey, portStr)
            if frontendHint is not None:
                c.SetStr(Config.RelaySection, Config.RelayFrontEndTypeHintKey, str(frontendHint))
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
    def TryToGetCompanionDetails(context:Optional[Context]=None, configFolderPath:Optional[str]=None) -> Tuple[Optional[str], Optional[str]]:
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
            c = ConfigHelper._GetConfig_CreateIfNotExisting(context)
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
    def TryToGetBambuData(context:Optional[Context]=None, configFolderPath:Optional[str]=None) -> Tuple[Optional[str], Optional[str]]:
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
            c = ConfigHelper._GetConfig_CreateIfNotExisting(context)
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
    def TryToGetElegooData(context:Optional[Context]=None, configFolderPath:Optional[str]=None) -> Optional[str]:
        try:
            # Load the config, if this returns None, there is no existing file.
            c = ConfigHelper._GetConfig(context, configFolderPath)
            if c is None:
                return None
            # Use a default of None so if they don't exist, they aren't added to the config.
            return c.GetStr(Config.SectionElegoo, Config.ElegooMainboardMac, None)
        except Exception as e:
            # There have been a few reports of this file being corrupt, so if it is, we will just fail and rewrite it.
            Logger.Warn("Failed to parse elegoo details from existing config. "+str(e))
        return None


    # Writes the bambu details to the config file
    @staticmethod
    def WriteElegooDetails(context:Context, mainboardMac:str):
        try:
            # Load the config, force it to be created if it doesn't exist.
            c = ConfigHelper._GetConfig_CreateIfNotExisting(context)
            # Write the new values
            c.SetStr(Config.SectionElegoo, Config.ElegooMainboardMac, mainboardMac)
        except Exception as e:
            Logger.Error("Failed to write elegoo details to config. "+str(e))
            raise Exception("Failed to write elegoo details to config") from e

    #
    # Moonraker Only
    #

    # Given a context, this will try to find the config file and see if the moonraker data is in it.
    # If any data is found, it will be returned. If there's no config or the data doesn't exist, it will return None.
    # Returns (apiKey:str)
    @staticmethod
    def TryToGetMoonrakerDetails(context:Optional[Context]=None, configFolderPath:Optional[str]=None) -> Optional[str]:
        try:
            # Load the config, if this returns None, there is no existing file.
            c = ConfigHelper._GetConfig(context, configFolderPath)
            if c is None:
                return None
            # Use a default of None so if they don't exist, they aren't added to the config.
            apiKey = c.GetStr(Config.MoonrakerSection, Config.MoonrakerApiKey, None, True)
            return apiKey
        except Exception as e:
            # There have been a few reports of this file being corrupt, so if it is, we will just fail and rewrite it.
            Logger.Warn("Failed to parse moonraker details from existing config. "+str(e))
        return None


    # Writes the moonraker details to the config file
    @staticmethod
    def WriteMoonrakerDetails(context:Context, apiKey:Optional[str]):
        try:
            # Load the config, force it to be created if it doesn't exist.
            c = ConfigHelper._GetConfig_CreateIfNotExisting(context)
            # Write the new values
            c.SetStr(Config.MoonrakerSection, Config.MoonrakerApiKey, apiKey, True)
        except Exception as e:
            Logger.Error("Failed to write moonraker details to config. "+str(e))
            raise Exception("Failed to write moonraker details to config") from e


    #
    # Helpers
    #

    # Given a context or folder path, this will return if there's any existing config file yet or not.
    @staticmethod
    def DoesConfigFileExist(context:Optional[Context]=None, configFolderPath:Optional[str]=None) -> bool:
        configFilePath = None
        if context is not None:
            configFilePath = ConfigHelper.GetConfigFilePath(context)
        elif configFolderPath is not None:
            configFilePath = ConfigHelper.GetConfigFilePath(configFolderPath=configFolderPath)
        else:
            raise Exception("DoesConfigFileExist no context or file path passed.")
        if configFilePath is None:
            raise Exception("DoesConfigFileExist no context or file path passed.")
        return os.path.exists(configFilePath) and os.path.isfile(configFilePath)


    # Given a context or config file path, this returns file path of the config.
    # If the context is missing the ConfigFolder, None is returned.
    @staticmethod
    def GetConfigFilePath(context:Optional[Context]=None, configFolderPath:Optional[str]=None) -> Optional[str]:
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
    def _GetConfig(context:Optional[Context]=None, configFolderPath:Optional[str]=None, createIfNotExisting:bool = False) -> Optional[Config]:
        if ConfigHelper.DoesConfigFileExist(context, configFolderPath) is False:
            if createIfNotExisting:
                # Fallthrough, the Config class will create a file if none exists.
                Logger.Debug("Creating main plugin config file.")
            else:
                return None
        # Get the config folder path.
        if configFolderPath is None and context is not None:
            configFolderPath = context.ConfigFolder
        if configFolderPath is None:
            raise Exception("_GetConfig was called with an invalid context and now config folder path.")
        # Open or create the config.
        return Config(configFolderPath)


    @staticmethod
    def _GetConfig_CreateIfNotExisting(context:Optional[Context]=None, configFolderPath:Optional[str]=None) -> Config:
        # This is a helper to create the config if it doesn't exist.
        # This is used by the other functions to ensure the config exists before trying to access it.
        c = ConfigHelper._GetConfig(context, configFolderPath, True)
        if c is None:
            raise Exception("_GetConfigCreateIfNotExisting failed to create config when createIfNotExisting was set.")
        return c
