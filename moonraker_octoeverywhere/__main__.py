import os
import sys
import json
import base64

from .moonrakerhost import MoonrakerHost

if __name__ == '__main__':
    # The config and settings path is passed as the first arg when the service runs.
    # This allows us to run multiple services instances, each pointing at it's own config.
    if len(sys.argv) < 1:
        print("ERROR! - Program and json settings path passed to service.")
        sys.exit(1)

    # The second arg should be a json string, which has all of our params.
    if len(sys.argv) < 2:
        print("ERROR! - No settings json passed to service..")
        sys.exit(1)

    # Try to parse the config
    try:
        # The args are passed as a urlbase64 encoded string, to prevent issues with passing some chars as args.
        argsJsonBase64 = sys.argv[1]
        jsonStr = base64.urlsafe_b64decode(bytes(argsJsonBase64, "utf-8")).decode("utf-8")
        print("Loading Service Config: "+jsonStr)

        # Parse the config.
        config = json.loads(jsonStr)
        KlipperConfigFolder = config["KlipperConfigFolder"]
        MoonrakerConfigFile = config["MoonrakerConfigFile"]
        KlipperLogFolder = config["KlipperLogFolder"]
        LocalFileStoragePath = config["LocalFileStoragePath"]
        ServiceName = config["ServiceName"]
        VirtualEnvPath = config["VirtualEnvPath"]
        RepoRootFolder = config["RepoRootFolder"]

        # There are two modes, one is local running with moonraker the other is a remote observer.
        # If this is an observer, MoonrakerConfigFile will not exits.
        IsObserver = False
        ObserverConfigFilePath = None
        ObserverInstanceIdStr = None
        if "IsObserver" in config:
            IsObserver = config["IsObserver"]
        IsObserver = bool(IsObserver)
        if IsObserver:
            ObserverConfigFilePath = config["ObserverConfigFilePath"]
            ObserverInstanceIdStr = config["ObserverInstanceIdStr"]

        # Check paths always exist.
        if os.path.exists(KlipperConfigFolder) is False:
            print("Error - KlipperConfigFolder path doesn't exist.")
            sys.exit(1)
        if os.path.exists(KlipperLogFolder) is False:
            print("Error - KlipperLogFolder path doesn't exist.")
            sys.exit(1)
        if os.path.exists(LocalFileStoragePath) is False:
            print("Error - LocalFileStoragePath path doesn't exist.")
            sys.exit(1)
        if os.path.exists(VirtualEnvPath) is False:
            print("Error - VirtualEnvPath path doesn't exist.")
            sys.exit(1)
        if os.path.exists(RepoRootFolder) is False:
            print("Error - RepoRootFolder path doesn't exist.")
            sys.exit(1)

        # These paths are dependent on the mode
        if IsObserver:
            # This will not be set.
            MoonrakerConfigFile = None

            # These are required.
            if os.path.exists(ObserverConfigFilePath) is False:
                print("Error - ObserverConfigFilePath path doesn't exist.")
                sys.exit(1)
            if ObserverInstanceIdStr is None or len(ObserverInstanceIdStr) == 0:
                print("Error - ObserverInstanceIdStr doesn't exist.")
                sys.exit(1)
        else:
            # These will not exist
            ObserverConfigFilePath = None
            ObserverInstanceIdStr = None

            # This is required
            if os.path.exists(MoonrakerConfigFile) is False:
                print("Error - MoonrakerConfigFile path doesn't exist.")
                sys.exit(1)

    except Exception as e:
        print("ERROR! - Exception while parsing service config. "+str(e))
        sys.exit(1)

    # For debugging, we also allow an optional dev object to be passed.
    devConfig_CanBeNone = None
    try:
        if len(sys.argv) > 2:
            devConfig_CanBeNone = json.loads(sys.argv[2])
            print("Using dev config: "+sys.argv[2])
    except Exception as e:
        print("ERROR! - Failed to parse dev config arg! "+str(e))

    # Create and run the main host!
    host = MoonrakerHost(KlipperConfigFolder, KlipperLogFolder, devConfig_CanBeNone)
    host.RunBlocking(KlipperConfigFolder, IsObserver, LocalFileStoragePath, ServiceName, VirtualEnvPath, RepoRootFolder,
                     MoonrakerConfigFile,
                     ObserverConfigFilePath, ObserverInstanceIdStr,
                     devConfig_CanBeNone)

    # If we exit here, it's due to an error, since RunBlocking should be blocked forever.
    sys.exit(1)
