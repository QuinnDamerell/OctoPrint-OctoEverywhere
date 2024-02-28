import os
import sys
import json
import base64
from enum import Enum


class ConfigDataTypes(Enum):
    String = 1
    Path = 2
    Bool = 3

#
# Helper functions for the service startup.
#
class Startup:

    # A common error printing function.
    def PrintErrorAndExit(self, msg:str):
        print(f"\r\nPlugin Init Error - {msg}", file=sys.stderr)
        print( "\r\nPlease contact support so we can fix this for you! support@octoeverywhere.com", file=sys.stderr)
        sys.exit(1)


    # Given the process args, this returns the json config.
    def GetJsonFromArgs(self, argv):
        # The config and settings path is passed as the first arg when the service runs.
        # This allows us to run multiple services instances, each pointing at it's own config.
        if len(argv) < 1:
            self.PrintErrorAndExit("No program and json settings path passed to service")

        # The second arg should be a json string, which has all of our params.
        if len(argv) < 2:
            self.PrintErrorAndExit("No json settings path passed to service")

        # Try to parse the config
        jsonConfigStr = None
        try:
            # The args are passed as a urlbase64 encoded string, to prevent issues with passing some chars as args.
            argsJsonBase64 = argv[1]
            jsonConfigStr = base64.urlsafe_b64decode(bytes(argsJsonBase64, "utf-8")).decode("utf-8")
            print("Loading Service Config: "+jsonConfigStr)
            return json.loads(jsonConfigStr)
        except Exception as e:
            self.PrintErrorAndExit("Failed to get json from cmd args. "+str(e))
        return None


    # If there was a dev config passed, this parses it and returns the json object.
    def GetDevConfigIfAvailable(self, argv):
        try:
            if len(argv) > 2:
                devConfigJson = json.loads(argv[2])
                print("Using dev config: "+argv[2])
                return devConfigJson
        except Exception as e:
            self.PrintErrorAndExit(f"Exception while DEV CONFIG. Error:{str(e)}, Config: {argv[2]}")
        return None


    # A helper to get a specific value from the json config.
    # oldVarName allows us to stay compat with older installs.
    def GetConfigVarAndValidate(self, jsonConfig, varName:str, dataType:ConfigDataTypes, oldVarName:str = None):
        var = None
        if varName in jsonConfig:
            var = jsonConfig[varName]
        elif oldVarName in jsonConfig:
            var = jsonConfig[oldVarName]
        else:
            raise Exception(f"{varName} isn't found in the json jsonConfig.")

        if var is None:
            raise Exception(f"{varName} returned None when parsing json jsonConfig.")

        if dataType == ConfigDataTypes.String or dataType == ConfigDataTypes.Path:
            var = str(var)
            if len(var) == 0:
                raise Exception(f"{varName} is an empty string.")

            if dataType == ConfigDataTypes.Path:
                if os.path.exists(var) is False:
                    raise Exception(f"{varName} is a path, but the path wasn't found.")

        elif dataType == ConfigDataTypes.Bool:
            var = bool(var)

        else:
            raise Exception(f"{varName} has an invalid jsonConfig data type. {dataType}")
        return var
