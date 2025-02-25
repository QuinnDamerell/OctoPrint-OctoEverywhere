import sys

from linux_host.startup import Startup
from linux_host.startup import ConfigDataTypes

from .elegoohost import ElegooHost

if __name__ == '__main__':

    # This is a helper class, to keep the startup logic common.
    s = Startup()

    # Try to parse the config
    jsonConfigStr = None
    try:
        # Get the json from the process args.
        jsonConfig = s.GetJsonFromArgs(sys.argv)

        #
        # Parse the common, required args.
        #
        ServiceName = s.GetConfigVarAndValidate(jsonConfig, "ServiceName", ConfigDataTypes.String)
        VirtualEnvPath = s.GetConfigVarAndValidate(jsonConfig, "VirtualEnvPath", ConfigDataTypes.Path)
        RepoRootFolder = s.GetConfigVarAndValidate(jsonConfig, "RepoRootFolder", ConfigDataTypes.Path)
        LocalFileStoragePath = s.GetConfigVarAndValidate(jsonConfig, "LocalFileStoragePath", ConfigDataTypes.Path)
        LogFolder = s.GetConfigVarAndValidate(jsonConfig, "LogFolder", ConfigDataTypes.Path)
        ConfigFolder = s.GetConfigVarAndValidate(jsonConfig, "ConfigFolder", ConfigDataTypes.Path)
        InstanceStr   = s.GetConfigVarAndValidate(jsonConfig, "CompanionInstanceIdStr",  ConfigDataTypes.String)

    except Exception as e:
        s.PrintErrorAndExit(f"Exception while loading json config. Error:{str(e)}, Config: {jsonConfigStr}")

    # For debugging, we also allow an optional dev object to be passed.
    devConfig_CanBeNone = s.GetDevConfigIfAvailable(sys.argv)

    # Run!
    try:
        # Create and run the main host!
        host = ElegooHost(ConfigFolder, LogFolder, devConfig_CanBeNone)
        host.RunBlocking(ConfigFolder, LocalFileStoragePath, RepoRootFolder, devConfig_CanBeNone)
    except Exception as e:
        s.PrintErrorAndExit(f"Exception leaked from main elegoo host class. Error:{str(e)}")

    # If we exit here, it's due to an error, since RunBlocking should be blocked forever.
    sys.exit(1)
