import json
import logging

from octoeverywhere.sentry import Sentry

from .moonrakerclient import MoonrakerClient

# Implements logic that deals with the moonraker database.
class MoonrakerDatabase:

    def __init__(self, logger:logging.Logger, printerId:str, pluginVersion:str) -> None:
        self.Logger = logger
        self.PrinterId = printerId
        self.PluginVersion = pluginVersion


    def EnsureOctoEverywhereDatabaseEntry(self):
        # Useful for debugging.
        # self._Debug_EnumerateDataBase()

        # We use a few database entries under our own name space to share information with apps and other plugins.
        # Note that since these are used by 3rd party systems, they must never change. We also use this for our frontend.
        result = MoonrakerClient.Get().SendJsonRpcRequest("server.database.post_item",
        {
            "namespace": "octoeverywhere",
            "key": "public.printerId",
            "value": self.PrinterId
        })
        if result.HasError():
            self.Logger.error("Ensure database entry item post failed. "+result.GetLoggingErrorStr())
            return
        result = MoonrakerClient.Get().SendJsonRpcRequest("server.database.post_item",
        {
            "namespace": "octoeverywhere",
            "key": "public.pluginVersion",
            "value": self.PluginVersion
        })
        if result.HasError():
            self.Logger.error("Ensure database entry item plugin version failed. "+result.GetLoggingErrorStr())
            return
        self.Logger.debug("Ensure database items posted successfully.")


    def _Debug_EnumerateDataBase(self):
        try:
            result = MoonrakerClient.Get().SendJsonRpcRequest("server.database.list")
            if result.HasError():
                self.Logger.error("_Debug_EnumerateDataBase failed to list. "+result.GetLoggingErrorStr())
                return
            nsList = result.GetResult()["namespaces"]
            for n in nsList:
                result = MoonrakerClient.Get().SendJsonRpcRequest("server.database.get_item",
                    {
                        "namespace": n
                    })
                if result.HasError():
                    self.Logger.error("_Debug_EnumerateDataBase failed to get items for "+n+". "+result.GetLoggingErrorStr())
                    return
                self.Logger.debug("Database namespace "+n+" : "+json.dumps(result.GetResult(), indent=4, separators=(", ", ": ")))
        except Exception as e:
            Sentry.Exception("_Debug_EnumerateDataBase exception.", e)
