import sys
import json

from linux_host.startup import Startup
from linux_host.startup import ConfigDataTypes

from .moonrakerhost import MoonrakerHost

if __name__ == '__main__':

    # This is a helper class, to keep the startup logic common.
    s = Startup()

    # Try to parse the config
    jsonConfig = None
    try:
        # Get the json from the process args.
        jsonConfig = s.GetJsonFromArgs(sys.argv)

        #
        # 1) Parse the common, required args.
        #
        ServiceName = s.GetConfigVarAndValidate(jsonConfig, "ServiceName", ConfigDataTypes.String)
        VirtualEnvPath = s.GetConfigVarAndValidate(jsonConfig, "VirtualEnvPath", ConfigDataTypes.Path)
        RepoRootFolder = s.GetConfigVarAndValidate(jsonConfig, "RepoRootFolder", ConfigDataTypes.Path)
        LocalFileStoragePath = s.GetConfigVarAndValidate(jsonConfig, "LocalFileStoragePath", ConfigDataTypes.Path)
        # These var names changed to support other plugin types like Bambu, but we must keep them around for older installs.
        KlipperConfigFolder = s.GetConfigVarAndValidate(jsonConfig, "ConfigFolder", ConfigDataTypes.Path, "KlipperConfigFolder")
        KlipperLogFolder = s.GetConfigVarAndValidate(jsonConfig, "LogFolder", ConfigDataTypes.Path, "KlipperLogFolder")

        #
        # 2) Parse the IsCompanion flag, this will determine which other vars are required.
        #    Note that for older plugin installs, the IsCompanion flag won't exist, implying False.
        #
        IsCompanion = False
        if "IsCompanion" in jsonConfig:
            IsCompanion = jsonConfig["IsCompanion"]
        IsCompanion = bool(IsCompanion)

        #
        # 3) Now parse the required vars based on the IsCompanion flag state.
        #
        MoonrakerConfigFile = None
        #CompanionInstanceIdStr = None

        if IsCompanion:
            # We don't use this right now, but we have it if we need it.
            #CompanionInstanceIdStr = s.GetConfigVarAndValidate(jsonConfig, "CompanionInstanceIdStr", ConfigDataTypes.String)
            pass
        else:
            MoonrakerConfigFile = s.GetConfigVarAndValidate(jsonConfig, "MoonrakerConfigFile", ConfigDataTypes.Path)

    except Exception as e:
        s.PrintErrorAndExit(f"Exception while loading json config. Error:{str(e)} Config:{json.dumps(jsonConfig)}")

    # For debugging, we also allow an optional dev object to be passed.
    devConfig_CanBeNone = s.GetDevConfigIfAvailable(sys.argv)

    # Run!
    try:
        # Create and run the main host!
        host = MoonrakerHost(KlipperConfigFolder, KlipperLogFolder, devConfig_CanBeNone)
        host.RunBlocking(KlipperConfigFolder, IsCompanion, LocalFileStoragePath, ServiceName, VirtualEnvPath, RepoRootFolder,
                        MoonrakerConfigFile, devConfig_CanBeNone)
    except Exception as e:
        s.PrintErrorAndExit(f"Exception leaked from main moonraker host class. Error:{str(e)}")

    # If we exit here, it's due to an error, since RunBlocking should be blocked forever.
    sys.exit(1)
