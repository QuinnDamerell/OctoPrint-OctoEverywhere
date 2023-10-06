import os
import threading

from octoeverywhere.websocketimpl import Client

from .Util import Util
from .Logging import Logger
from .Context import Context
from .ObserverConfigFile import ObserverConfigFile

# The goal of this class is the take the context object from the Discovery Gen2 phase to the Phase 3.
class Configure:

    def Run(self, context:Context):

        Logger.Header("Starting configuration...")

        if context.IsObserverSetup:
            # For observer setups, there is no local moonraker config file, so things are setup differently.
            # The plugin data folder, which is normally the root printer data folder for that instance, becomes our per instance
            # observer folder.
            context.PrinterDataFolder = context.ObserverDataPath
            # We mock the same layout as the moonraker folder structure, to keep things common.
            context.PrinterDataConfigFolder = ObserverConfigFile.GetConfigFolderPathFromDataPath(context.PrinterDataFolder)
            # Set the path to where the observer config file will be.
            # This path is shared by the plugin logic, so it can't change!
            context.ObserverConfigFilePath = ObserverConfigFile.GetConfigFilePathFromDataPath(context.PrinterDataFolder)
            # Make a logs folder, so it's found bellow
            Util.EnsureDirExists(os.path.join(context.PrinterDataFolder, "logs"), context, True)
        else:
            # For now we assume the folder structure is the standard Klipper folder config,
            # thus the full moonraker config path will be .../something_data/config/moonraker.conf
            # Based on that, we will define the config folder and the printer data root folder.
            context.PrinterDataConfigFolder = Util.GetParentDirectory(context.MoonrakerConfigFilePath)
            context.PrinterDataFolder = Util.GetParentDirectory(context.PrinterDataConfigFolder)
            Logger.Debug("Printer data folder: "+context.PrinterDataFolder)


        serviceSuffixStr = ""
        if context.IsObserverSetup:
            # For observers, we use the observer id, with a unique prefix to separate it from any possible local moonraker installs
            # Note that the moonraker service suffix can be numbers or letters, so we use the same rules.
            serviceSuffixStr = f"-observer{context.ObserverInstanceId}"
        else:
            # Now we need to figure out the instance suffix we need to use.
            # To keep with Kiauh style installs, each moonraker instances will be named moonraker-<number or name>.service.
            # If there is only one moonraker instance, the name is moonraker.service.
            # Default to empty string, which means there's no suffix and only one instance.
            serviceFileNameNoExtension = context.MoonrakerServiceFileName.split('.')[0]
            if '-' in serviceFileNameNoExtension:
                moonrakerServiceSuffix = serviceFileNameNoExtension.split('-')
                serviceSuffixStr = "-" + moonrakerServiceSuffix[1]

        # This is the name of our service we create. If the port is the default port, use the default name.
        # Otherwise, add the port to keep services unique.
        context.ServiceName = "octoeverywhere"+serviceSuffixStr
        context.ServiceFilePath = os.path.join(Util.SystemdServiceFilePath, context.ServiceName+".service")

        # Since the moonraker config folder is unique to the moonraker instance, we will put our storage in it.
        # This also prevents the user from messing with it accidentally.
        context.LocalFileStorageFolder = os.path.join(context.PrinterDataFolder, "octoeverywhere-store")

        # Ensure the storage folder exists and is owned by the correct user.
        Util.EnsureDirExists(context.LocalFileStorageFolder, context, True)

        # There's not a great way to find the log path from the config file, since the only place it's located is in the systemd file.
        context.PrinterDataLogsFolder = None

        # First, we will see if we can find a named folder relative to this folder.
        context.PrinterDataLogsFolder = os.path.join(context.PrinterDataFolder, "logs")
        if os.path.exists(context.PrinterDataLogsFolder) is False:
            # Try an older path
            context.PrinterDataLogsFolder = os.path.join(context.PrinterDataFolder, "klipper_logs")
            if os.path.exists(context.PrinterDataLogsFolder) is False:
                # Failed, make a folder in the printer data root.
                context.PrinterDataLogsFolder = os.path.join(context.PrinterDataFolder, "octoeverywhere-logs")
                # Create the folder and force the permissions so our service can write to it.
                Util.EnsureDirExists(context.PrinterDataLogsFolder, context, True)

        # Finally, if this is an observer setup, we need the user to tell us where moonraker is installed
        # and need to write the observer config file.
        if context.IsObserverSetup:
            self._EnsureObserverConfigure(context)

        # Report
        Logger.Info(f'Configured. Service: {context.ServiceName}, Path: {context.ServiceFilePath}, LocalStorage: {context.LocalFileStorageFolder}, Config Dir: {context.PrinterDataConfigFolder}, Logs: {context.PrinterDataLogsFolder}')


    def _EnsureObserverConfigure(self, context:Context):
        Logger.Debug("Running observer ensure config logic.")

        # See if there's a valid config already.
        ip, port = ObserverConfigFile.TryToParseConfig(context.ObserverConfigFilePath)
        if ip is not None and port is not None:
            Logger.Info(f"Existing observer config file found. IP: {ip}:{port}")
            return

        # We need to get the ip and port.
        ipOrHostname = ""
        port = "7125"

        while True:
            try:
                Logger.Blank()
                Logger.Blank()
                Logger.Blank()
                Logger.Header("##################################")
                Logger.Header("   Moonraker Companion Setup")
                Logger.Header("##################################")
                Logger.Blank()
                Logger.Info("For OctoEverywhere Companion to work, it needs to know how to connect to the Klipper device on your network.")
                Logger.Info("If you have any trouble, we are happy to help! Contact us at support@octoeverywhere.com")
                Logger.Blank()
                Logger.Info("Please enter the IP address or Hostname of the device running Klipper/Moonraker/Mainsail/Fluidd.")
                Logger.Info("The IP address might look something like `192.168.1.5` or a Hostname might look like `klipper.local`")
                ipOrHostname = input("Enter the IP or Hostname: ")
                ipOrHostname = ipOrHostname.lower().strip()
                Logger.Blank()
                Logger.Info("Please enter the port Moonraker is running on.")
                Logger.Info("If you don't know the port or want to use the default port (7125), press enter.")
                port = input("Enter Moonraker Port: ")
                if len(port) == 0:
                    port = "7125"

                Logger.Blank()
                url = f"ws://{ipOrHostname}:{port}"
                Logger.Info(f"Trying to connect to Moonraker via {url} ...")

                # Try to connect to the websocket and try to make sure it's moonraker.
                doneEvent = threading.Event()
                lock = threading.Lock()
                result = {}
                def OnOpened(ws):
                    pass
                def OnMsg(ws, msg):
                    with lock:
                        if "success" in result:
                            return
                        try:
                            # Try to see if the message looks like one of the first moonraker messages.
                            msgStr = msg.decode('utf-8')
                            if "moonraker" in msgStr.lower():
                                result["success"] = True
                                doneEvent.set()
                        except Exception:
                            pass
                def OnClosed(ws):
                    doneEvent.set()
                def OnError(ws, exception):
                    with lock:
                        result["exception"] = exception
                    doneEvent.set()
                ws = Client(f"{url}/websocket", onWsOpen=OnOpened, onWsMsg=OnMsg, onWsError=OnError, onWsClose=OnClosed)
                ws.RunAsync()

                # Wait for the event or a timeout.
                doneEvent.wait(10.0)

                # Get the results before we close.
                capturedSuccess = False
                capturedEx = None
                with lock:
                    if result.get("success", None) is not None:
                        capturedSuccess = result["success"]
                    if result.get("exception", None) is not None:
                        capturedEx = result["exception"]

                # Ensure the ws is closed
                try:
                    ws.Close()
                except Exception:
                    pass

                # Handle the result.
                if capturedSuccess:
                    if ObserverConfigFile.WriteIpAndPort(context, context.ObserverConfigFilePath, ipOrHostname, port) is False:
                        raise Exception("Failed to write observer config.")
                    Logger.Blank()
                    Logger.Header("Moonraker connection successful!")
                    Logger.Blank()
                    return
                else:
                    Logger.Blank()
                    Logger.Blank()
                    if capturedEx is not None:
                        Logger.Error("Moonraker connection failed.")
                    else:
                        Logger.Error("Moonraker connection timed out.")
                    Logger.Warn("Make sure the device is powered on, has an network connection, and the ip is correct.")
                    if capturedEx is not None:
                        Logger.Warn(f"Error {str(capturedEx)}")
                    Logger.Blank()
                    Logger.Blank()
            except Exception as e:
                Logger.Warn("Failed to setup moonraker, try again. "+str(e))
