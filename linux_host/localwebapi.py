import json
import time
import logging
import threading

from http.server import HTTPServer, BaseHTTPRequestHandler

from octoeverywhere.sentry import Sentry

from linux_host.config import Config

# Creates a simple web server for plugins and systems to use.
class LocalWebApi:

    # The singleton instance of the LocalWebApi.
    _Instance:"LocalWebApi" = None #pyright: ignore[reportAssignmentType]

    @staticmethod
    def Init(logger:logging.Logger, pluginId:str, config:Config) -> None:
        LocalWebApi._Instance = LocalWebApi(logger, pluginId, config)


    @staticmethod
    def Get() -> "LocalWebApi":
        return LocalWebApi._Instance


    def __init__(self, logger:logging.Logger, pluginId:str, config:Config) -> None:
        self.Logger = logger
        self.PluginId = pluginId
        self.WebServerThread = None

        # Indicates if the plugin is connected to OctoEverywhere.
        self.IsConnectedToOctoEverywhere = False
        # Indicates if the plugin is connected to the account.
        self.IsAccountLinked = False
        # Indicates if the plugin is connected to the printer.
        self.IsConnectedToPrinter = False

        # The existence of the port enables or disables the web server.
        self.HttpPort = config.GetInt(Config.GeneralSection, Config.GeneralLocalWebApiPort, None)
        self.HostName = config.GetStr(Config.GeneralSection, Config.GeneralLocalWebApiBindIp, None)
        if self.HostName is None:
            self.HostName = "0.0.0.0"
        self._Start()


    # Called when we are connected and we know if there's an account setup with this addon
    def OnPrimaryConnectionEstablished(self, hasConnectedAccount:bool):
        self.IsAccountLinked = hasConnectedAccount
        self.IsConnectedToOctoEverywhere = True


    # Called when the OctoEverywhere connection is lost.
    def OnOctoEverywhereConnectionLost(self):
        self.IsConnectedToOctoEverywhere = False


    # Called when the printer connection changes
    def SetPrinterConnectionState(self, isConnected:bool):
        self.IsConnectedToPrinter = isConnected


    def _Start(self):
        if self.HttpPort is None or self.HttpPort <= 0:
            self.Logger.debug("Local web api is disabled, because the port is not set.")
            return
        # Start the web server worker thread.
        self.WebServerThread = threading.Thread(target=self._WebServerWorker)
        self.WebServerThread.start()


    def _WebServerWorker(self):
        backoff = 0
        while True:
            # Try to run the webserver forever.
            webServer = None
            try:
                if self.HostName is None or self.HttpPort is None:
                    self.Logger.debug("Local web api is disabled, because the host name or port is not set.")
                    return
                self.Logger.debug(f"Web Server Starting {self.HostName}:{self.HttpPort}")
                webServer = HTTPServer((self.HostName, self.HttpPort), LocalWebApi.WebServerHandler)
                self.Logger.debug(f"Web Server Started {self.HostName}:{self.HttpPort}")
                webServer.serve_forever()
            except Exception as e:
                self.Logger.error("Web server exception. "+str(e))

            # If we fail, close it.
            try:
                if webServer is not None:
                    webServer.server_close()
            except Exception as e:
                Sentry.OnException("Failed to close the addon webserver.", e)

            # Try again after some time.
            backoff = min(backoff + 1, 20)
            time.sleep(backoff * 0.5)


    class WebServerHandler(BaseHTTPRequestHandler):
        # Silence access logs
        def log_message(self, format, *args): #pylint: disable=redefined-builtin
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()

            # Build the JSON response
            localWebApi = LocalWebApi.Get()
            response = {
                "PluginId": localWebApi.PluginId,
                "IsConnectedToOctoEverywhere": localWebApi.IsConnectedToOctoEverywhere,
                "IsConnectedToPrinter": localWebApi.IsConnectedToPrinter,
                "IsAccountLinked": localWebApi.IsAccountLinked,
            }

            # Write the response
            self.wfile.write(bytes(json.dumps(response), 'utf-8'))
