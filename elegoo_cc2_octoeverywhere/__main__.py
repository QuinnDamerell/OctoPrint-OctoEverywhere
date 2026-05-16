import sys
from typing import Optional

from linux_host.startup import ConfigDataTypes, Startup

from .elegoocc2host import ElegooCc2Host

if __name__ == '__main__':

    s = Startup()

    jsonConfigStr:Optional[str] = None
    try:
        (jsonConfigStr, jsonConfig) = s.GetJsonFromArgs(sys.argv)

        ServiceName = s.GetConfigVarAndValidate(jsonConfig, "ServiceName", ConfigDataTypes.String)
        VirtualEnvPath = s.GetConfigVarAndValidate(jsonConfig, "VirtualEnvPath", ConfigDataTypes.Path)
        RepoRootFolder = s.GetConfigVarAndValidate(jsonConfig, "RepoRootFolder", ConfigDataTypes.Path)
        LocalFileStoragePath = s.GetConfigVarAndValidate(jsonConfig, "LocalFileStoragePath", ConfigDataTypes.Path)
        LogFolder = s.GetConfigVarAndValidate(jsonConfig, "LogFolder", ConfigDataTypes.Path)
        ConfigFolder = s.GetConfigVarAndValidate(jsonConfig, "ConfigFolder", ConfigDataTypes.Path)
        InstanceStr = s.GetConfigVarAndValidate(jsonConfig, "CompanionInstanceIdStr", ConfigDataTypes.String)
        IsDockerContainer = s.GetConfigVarAndValidate(jsonConfig, "IsDockerContainer", ConfigDataTypes.Bool, defaultValue=False)
    except Exception as e:
        s.PrintErrorAndExit(f"Exception while loading json config. Error:{str(e)}, Config: {jsonConfigStr}")

    devConfig_CanBeNone = s.GetDevConfigIfAvailable(sys.argv)

    try:
        host = ElegooCc2Host(ConfigFolder, LogFolder, devConfig_CanBeNone) #pyright: ignore[reportArgumentType,reportPossiblyUnboundVariable]
        host.RunBlocking(ConfigFolder, LocalFileStoragePath, RepoRootFolder, IsDockerContainer, devConfig_CanBeNone) #pyright: ignore[reportArgumentType,reportPossiblyUnboundVariable]
    except Exception as e:
        s.PrintErrorAndExit(f"Exception leaked from main Elegoo CC2 host class. Error:{str(e)}")

    sys.exit(1)
