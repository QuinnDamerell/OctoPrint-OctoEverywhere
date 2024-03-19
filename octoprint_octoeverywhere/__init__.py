# coding=utf-8
from __future__ import absolute_import
import threading
import socket
import time
from datetime import datetime

import flask
import requests
import octoprint.plugin

from octoeverywhere.webcamhelper import WebcamHelper
from octoeverywhere.octoeverywhereimpl import OctoEverywhere
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.notificationshandler import NotificationsHandler
from octoeverywhere.octopingpong import OctoPingPong
from octoeverywhere.telemetry import Telemetry
from octoeverywhere.sentry import Sentry
from octoeverywhere.mdns import MDns
from octoeverywhere.hostcommon import HostCommon
from octoeverywhere.Proto.ServerHost import ServerHost
from octoeverywhere.commandhandler import CommandHandler
from octoeverywhere.printinfo import PrintInfoManager
from octoeverywhere.compat import Compat


from .printerstateobject import PrinterStateObject
from .octoprintcommandhandler import OctoPrintCommandHandler
from .octoprintwebcamhelper import OctoPrintWebcamHelper
from .localauth import LocalAuth
from .slipstream import Slipstream
from .smartpause import SmartPause

class OctoeverywherePlugin(octoprint.plugin.StartupPlugin,
                            octoprint.plugin.SettingsPlugin,
                            octoprint.plugin.AssetPlugin,
                            octoprint.plugin.TemplatePlugin,
                            octoprint.plugin.WizardPlugin,
                            octoprint.plugin.SimpleApiPlugin,
                            octoprint.plugin.EventHandlerPlugin,
                            octoprint.plugin.ProgressPlugin):

    def __init__(self):
        # The host and port this octoprint instance is listening on.
        self.OctoPrintLocalPort = 80
        self.OctoPrintLocalHost = "127.0.0.1"
        # Default the handler to None since that will make the var name exist
        # but we can't actually create the class yet until the system is more initialized.
        self.NotificationHandler = None
        # Init member vars
        self.octoKey = ""
        # Indicates if OnStartup has been called yet.
        self.HasOnStartupBeenCalledYet = False
        # Indicates if there's a pending smart print notification that should be shown when the user sees the dashboard next.
        self.HasPendingSmartPauseMessage = False
        # Let the compat system know this is an OctoPrint host.
        Compat.SetIsOctoPrint(True)

     # Assets we use, just for the wizard right now.
    def get_assets(self):
        return {
            "js"  : ["js/OctoEverywhere.js"],
            "less": ["less/OctoEverywhere.less"],
            "css" : ["css/OctoEverywhere.css"]
        }

    # Return true if the wizard needs to be shown.
    def is_wizard_required(self):
        # We don't need to show the wizard if we know there are account connected.
        hasConnectedAccounts = self.GetHasConnectedAccounts()
        return hasConnectedAccounts is False

    # Increment this if we need to pop the wizard again.
    def get_wizard_version(self):
        return 10

    def get_wizard_details(self):
        # Do some sanity checking logic, since this has been sensitive in the past.
        printerUrl = self.GetAddPrinterUrl()
        if printerUrl is None:
            self._logger.error("Failed to get OctoPrinter Url for wizard.")
            printerUrl = "https://octoeverywhere.com/getstarted"
        return {"AddPrinterUrl": printerUrl + "&source=octoprint_wizard"}

    # Return the default settings.
    def get_settings_defaults(self):
        return {}

    # Return the current printer key for the settings template
    def get_template_vars(self):
        return dict(
            PrinterKey=self.GetFromSettings("PrinterKey", None),
            AddPrinterUrl=self.GetAddPrinterUrl() + "&source=octoprint_settings"
        )

    def get_template_configs(self):
        return [
            dict(type="settings", custom_bindings=False)
        ]

    ##~~ Softwareupdate hook
    def get_update_information(self):
        # Define the configuration for your plugin to use with the Software Update
        # Plugin here. See https://docs.octoprint.org/en/master/bundledplugins/softwareupdate.html
        # for details.
        return dict(
            octoeverywhere=dict(
                displayName="octoeverywhere",
                displayVersion=self._plugin_version,

                # version check: github repository
                type="github_release",
                user="QuinnDamerell",
                repo="OctoPrint-OctoEverywhere",
                current=self._plugin_version,

                # update method: pip
                pip="https://github.com/QuinnDamerell/OctoPrint-OctoEverywhere/archive/{target_version}.zip"
            )
        )

    # Called when the system is starting up.
    def on_startup(self, host, port):
        # Set the local host address to be the one passed to us from OctoPrint. Most cases the IP will be 127.0.0.1 or 0.0.0.0, which then
        # we can access OctoPrint on localhost. But in some setups OctoPrint might be bound to only one adapter, in which case we need to use it.
        # Host should always be a string, but if not, ignore it.
        if isinstance(host, str):
            # Check the host to see if it's all adapters ("0.0.0.0"). If so, change it to be localhost, since it will work.
            if "0.0.0.0" in host:
                host = host.replace("0.0.0.0", "127.0.0.1")
            # Some setups seem to send "::" as the host string, which makes an invalid address. So we will check to make sure the string
            # is at least as long as 0.0.0.0, which should be about the min valid string length. (considering 'localhost', ipv4, and ipv6 addresses)
            if len(host) < len("0.0.0.0"):
                # Ignore this bind all string, don't worry about it.
                if host != "::":
                    # In this case the default value of OctoPrintLocalHost will be used.
                    self._logger.warn("The host string from OctoPrint was too short, so it was ignored. Value: "+str(host))
            else:
                self.OctoPrintLocalHost = host
        else:
            # This the `isinstance` check will also fail on PY2, but that's ok.
            self._logger.warn("Host passed from OctoPrint wasn't a string? (or this is a PY2 setup)")

        # Get the port the server is listening on, since for some configs it's not the default.
        self.OctoPrintLocalPort = port

        # Report the current setup.
        self._logger.info("OctoPrint host:" +str(self.OctoPrintLocalHost) + " port:" + str(self.OctoPrintLocalPort))

        # Setup Sentry to capture issues.
        # We can't enable tracing or profiling in OctoPrint, because it picks up a lot of OctoPrint functions.
        Sentry.SetLogger(self._logger)
        Sentry.Setup(self._plugin_version, "octoprint", isDevMode=False, enableProfiling=False, filterExceptionsByPackage=True)

        # Setup our telemetry class.
        Telemetry.Init(self._logger)

        #
        # Due to settings bugs in OctoPrint, as much of the generated values saved into settings should be set here as possible.
        # For more details, see SaveToSettingsIfUpdated()
        #

        # Ensure they keys are created here, so make sure that they are always created before any of the UI queries for them.
        printerId = self.EnsureAndGetPrinterId()
        self.EnsureAndGetPrivateKey()

        # Ensure the plugin version is updated in the settings for the frontend.
        self.EnsurePluginVersionSet()

        # Set the printer id to Sentry.
        Sentry.SetPrinterId(printerId)

        # Init the static local auth helper
        LocalAuth.Init(self._logger, self._user_manager)

        # Init the static snapshot helper
        WebcamHelper.Init(self._logger, OctoPrintWebcamHelper(self._logger, self._settings), self.get_plugin_data_folder())

        # Init the ping helper
        OctoPingPong.Init(self._logger, self.get_plugin_data_folder(), printerId)

        # Init the mdns helper
        MDns.Init(self._logger, self.get_plugin_data_folder())

        # Init the print info manager.
        PrintInfoManager.Init(self._logger, self.get_plugin_data_folder())

        # Setup our printer state object, that implements the interface.
        printerStateObject = PrinterStateObject(self._logger, self._printer)

        # Create the notification object now that we have the logger.
        self.NotificationHandler = NotificationsHandler(self._logger, printerStateObject)
        self.NotificationHandler.SetPrinterId(printerId)
        printerStateObject.SetNotificationHandler(self.NotificationHandler)

        # Create our command handler and our platform specific command handler.
        CommandHandler.Init(self._logger, self.NotificationHandler, OctoPrintCommandHandler(self._logger, self._printer, printerStateObject, self))

        # Create the smart pause handler
        SmartPause.Init(self._logger, self._printer, self._printer_profile_manager.get_current_or_default())

        # Spin off a thread to try to resolve hostnames for logging and debugging.
        resolverThread = threading.Thread(target=self.TryToPrintHostNameIps)
        resolverThread.start()

        # Indicate this has been called and things have been inited.
        self.HasOnStartupBeenCalledYet = True


    # Call when the system is ready and running
    def on_after_startup(self):
        # Spin off a thread for us to operate on.
        self._logger.info("After startup called. Starting worker thread.")
        main_thread = threading.Thread(target=self.main)
        main_thread.daemon = True
        main_thread.start()

        # Init slipstream - This must be inited after LocalAuth since it requires the auth key.
        # Is also must be done when the OctoPrint server is ready, since it's going to kick off a thread to
        # pull and cache the index.
        Slipstream.Init(self._logger)

    #
    # Functions for the Simple API Mixin
    #
    def get_api_commands(self):
        return dict(
            # Our frontend js logic calls this API when it detects a local LAN connection and reports the port used.
            # We use the port internally as a solid indicator for what port the http proxy in front of OctoPrint is on.
            # This is required because it's common to also have webcams setup behind the http proxy and there's no other
            # way to query the port value from the system.
            setFrontendLocalPort=["port"]
        )

    def on_api_command(self, command, data):
        # Note this command is the only not handled in the api command handler.
        # But the command name must still be defined in the command handler.
        if command == "setFrontendLocalPort":
            # Ensure we can find a port.
            if "port" in data and data["port"] is not None:

                # Get vars
                port = int(data["port"])
                url = "Unknown"
                if "url" in data and data["url"] is not None:
                    url = str(data["url"])
                isHttps = False
                if "isHttps" in data and data["isHttps"] is not None:
                    isHttps = data["isHttps"]

                # Report
                self._logger.info("SetFrontendLocalPort API called. Port:"+str(port)+" IsHttps:"+str(isHttps)+" URL:"+url)

                # Save into settings only if the value has changed.
                self.SaveToSettingsIfUpdated("HttpFrontendPort", port)
                self.SaveToSettingsIfUpdated("HttpFrontendIsHttps", isHttps)

                # Update the running value.
                OctoHttpRequest.SetLocalHttpProxyPort(port)
                OctoHttpRequest.SetLocalHttpProxyIsHttps(isHttps)
            else:
                self._logger.info("SetFrontendLocalPort API called with no port.")

    def on_api_get(self, request):
        # On get requests, share some data.
        # This API is protected by the need for a OctoPrint API key
        # This API is used by apps and other system to identify the printer
        # for communication with the service. Thus these values should not be
        # modified or deleted.
        return flask.jsonify(
            PluginVersion=self._plugin_version,
            PrinterId=self.EnsureAndGetPrinterId()
        )


    #
    # Functions are for the gcode receive plugin hook
    #
    def received_gcode(self, comm, line, *args, **kwargs):
        # Blocking will block the printer commands from being handled so we can't block here!

        if line and self.NotificationHandler is not None:
            # ToLower the line for better detection.
            lineLower = line.lower()

            # M600 is a filament change command.
            # https://marlinfw.org/docs/gcode/M600.html
            # On my Pursa, I see this "fsensor_update - M600" AND this "echo:Enqueuing to the front: "M600""
            # We check for this both in sent and received, to make sure we cover all use cases. The OnFilamentChange will only allow one notification to fire every so often.
            # This m600 usually comes from when the printer sensor has detected a filament run out.
            if "m600" in lineLower or "fsensor_update" in lineLower:
                self._logger.info("Firing On Filament Change Notification From GcodeReceived: "+str(line))
                # No need to use a thread since all events are handled on a new thread.
                self.NotificationHandler.OnFilamentChange()
            else:
                # Look for a line indicating user interaction is needed.
                if "paused for user" in lineLower or "// action:paused" in lineLower:
                    self._logger.info("Firing On User Interaction Required From GcodeReceived: "+str(line))
                    # No need to use a thread since all events are handled on a new thread.
                    self.NotificationHandler.OnUserInteractionNeeded()

        # We must return line the line won't make it to OctoPrint!
        return line

    def sent_gcode(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        # Blocking will block the printer commands from being handled so we can't block here!

        # M600 is a filament change command.
        # https://marlinfw.org/docs/gcode/M600.html
        # We check for this both in sent and received, to make sure we cover all use cases. The OnFilamentChange will only allow one notification to fire every so often.
        # This M600 usually comes from filament change required commands embedded in the gcode, for color changes and such.
        if self.NotificationHandler is not None and gcode and gcode == "M600":
            self._logger.info("Firing On Filament Change Notification From GcodeSent: "+str(gcode))
            # No need to use a thread since all events are handled on a new thread.
            self.NotificationHandler.OnFilamentChange()

        # Look for positive extrude commands, so we can keep track of them for final snap and our first layer tracking logic.
        # Example cmd value: `G1 X112.979 Y93.81 E.03895`
        if self.NotificationHandler is not None and gcode and cmd and gcode == "G1":
            try:
                indexOfE = cmd.find('E')
                if indexOfE != -1:
                    endOfEValue = cmd.find(' ', indexOfE)
                    if endOfEValue == -1:
                        endOfEValue = len(cmd)
                    eValue = cmd[indexOfE+1:endOfEValue]
                    # The value will look like one of these: -.333,1.33,.33
                    # We don't care about negative values, so ignore them.
                    if eValue[0] != '-':
                        # If the value doesn't start with a 0, the float parse wil fail.
                        if eValue[0] != '0':
                            eValue = "0" + eValue
                        # Now the value should be something like 1.33 or 0.33
                        if float(eValue) > 0:
                            self.NotificationHandler.ReportPositiveExtrudeCommandSent()
            except Exception as e:
                self._logger.debug("Failed to parse gcode %s, error %s", cmd, str(e))

    def queuing_gcode(self, comm_instance, phase, cmd, cmd_type, gcode, subcode=None, tags=None, *args, **kwargs):
        # Make sure smart pause is setup, since this can be called really early on startup.
        smartPause = SmartPause.Get()
        if smartPause is None:
            return
        # Smart pause needs to keep track of the positioning mode, so it can properly resume it after a pause.
        smartPause.OnGcodeQueuing(cmd)

    def script_hook(self, comm, script_type, script_name, *args, **kwargs):
        # Make sure smart pause is setup, since this can be called really early on startup.
        smartPause = SmartPause.Get()
        if smartPause is None:
            return None
        # When we get any script hooks, allow the smart pause system to handle them, since it might
        # inject scripts for some hook types.
        return smartPause.OnScriptHook(script_type, script_name)


    #
    # Functions for the key validator hook.
    #
    def key_validator(self, api_key, *args, **kwargs):
        try:
            # Use LocalAuth to handle the request.
            return LocalAuth.Get().ValidateApiKey(api_key)
        except Exception as e:
            Sentry.Exception("key_validator failed", e)
        return None


    #
    # Functions are for the Process Plugin
    #
    # pylint: disable=arguments-renamed
    def on_print_progress(self, storage, path, progressInt):
        if self.NotificationHandler is not None:
            self.NotificationHandler.OnPrintProgress(progressInt, None)


    # A dict helper
    def _exists(self, dictObj:dict, key:str) -> bool:
        return key in dictObj and dictObj[key] is not None


    #
    # Functions for the Event Handler Mixin
    #
    # Note that on_event can actually fire before on_startup in some cases.
    #
    def on_event(self, event, payload):
        # This can be called before on_startup where things are inited.
        # Never handle anything that's sent before then.
        if self.HasOnStartupBeenCalledYet is False:
            return

        # Ensure there's a payload
        if payload is None:
            payload = {}

        # Listen for client authed events, these fire whenever a websocket opens and is auth is done.
        if event == "ClientAuthed":
            self.HandleClientAuthedEvent()

        # Only check the event after the notification handler has been created.
        # Specifically here, we have seen the Error event be fired before `on_startup` is fired,
        # and thus the handler isn't created.
        if self.NotificationHandler is None:
            return

        # Listen for the rest of these events for notifications.
        # OctoPrint Events
        if event == "PrintStarted":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            # Gather some stats from other places, if they exist.
            currentData = self._printer.get_current_data()
            fileSizeKBytes = 0
            if self._exists(currentData, "job") and self._exists(currentData["job"], "file") and self._exists(currentData["job"]["file"], "size"):
                fileSizeKBytes = int(currentData["job"]["file"]["size"]) / 1024
            totalFilamentUsageMm = 0
            if self._exists(currentData, "job") and self._exists(currentData["job"], "filament") and self._exists(currentData["job"]["filament"], "tool0") and self._exists(currentData["job"]["filament"]["tool0"], "length"):
                totalFilamentUsageMm = int(currentData["job"]["filament"]["tool0"]["length"])
            # On OctoPrint, we dont need to support print recovery, because if this process crashes so does the print.
            # So for the print cookie, we just use the current time, to make sure it's always unique.
            # See details in NotificationHandler._RecoverOrRestForNewPrint
            # TODO - With things like OctoKlipper, I'm not sure if the above is true, OctoPrint could restart and the print would still be active.
            self.NotificationHandler.OnStarted(f"{int(time.time())}", fileName, fileSizeKBytes, totalFilamentUsageMm)
        elif event == "PrintFailed":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            durationSec = self.GetDictStringOrEmpty(payload, "time")
            reason = self.GetDictStringOrEmpty(payload, "reason")
            self.NotificationHandler.OnFailed(fileName, durationSec, reason)
        elif event == "PrintDone":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            durationSec = self.GetDictStringOrEmpty(payload, "time")
            self.NotificationHandler.OnDone(fileName, durationSec)
        elif event == "PrintPaused":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            self.NotificationHandler.OnPaused(fileName)
        elif event == "PrintResumed":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            self.NotificationHandler.OnResume(fileName)

        # Printer Connection
        elif event == "Error":
            error = self.GetDictStringOrEmpty(payload, "error")
            self.NotificationHandler.OnError(error)

        # GCODE Events
        # Note most of these aren't sent when printing from the SD card
        elif event == "Waiting":
            self.NotificationHandler.OnWaiting()
        elif event == "FilamentChange":
            # We also handle some of these filament change gcode events ourselves, but since we already have
            # anti duplication logic in the notification handler for this event, might as well send it here as well.
            self.NotificationHandler.OnFilamentChange()


    def GetDictStringOrEmpty(self, d, key):
        if d[key] is None:
            return ""
        return str(d[key])


    def HandleClientAuthedEvent(self):
        # When the user is authed (opens the webpage in a new tab) we want to check if we should show the
        # finish setup message. This helps users setup the plugin if the miss the wizard or something.
        self.ShowLinkAccountMessageIfNeeded()

        # When the user sees the portal, check if we want to show the smart pause message.
        self.ShowSmartPausePopupIfNeeded()

        # Check if an update is required, if so, tell the user every time they login.
        pluginUpdateRequired = self.GetPluginUpdateRequired()
        if pluginUpdateRequired is True:
            title = "OctoEverywhere Disabled"
            message = '<strong>You need to update your OctoEverywhere plugin before you can continue using OctoEverywhere.</strong><br/><br/>We are always improving OctoEverywhere to make things faster and add features. Sometimes, that means we have to break things.'
            self.ShowUiPopup(title, message, "notice", "Learn How To Update", "https://octoeverywhere.com/pluginupdate", 0, False)


    def ShowSmartPausePopUpOnPortalLoad(self):
        # Set the flag so when the user hits the portal next, they see the popup.
        self.HasPendingSmartPauseMessage = True

        # Try to show it now as well, so it will popup if the user has the portal open.
        # If it's not open right now, then the deferred logic will handle showing it when the user opens it next.
        self.ShowSmartPausePopup()


    def ShowSmartPausePopupIfNeeded(self):
        if self.HasPendingSmartPauseMessage is False:
            return

        # Clear the flag
        self.HasPendingSmartPauseMessage = False

        # Ensure the system is still paused.
        if self._printer.is_paused() is False:
            return

        # Show it now.
        self.ShowSmartPausePopup()


    def ShowSmartPausePopup(self):
        # Show the notification, but don't auto hide it, to ensure the user sees it.
        title = "Smart Pause"
        message = "OctoEverywhere used Smart Pause to protect your print while paused. Smart Pause turned off your hotend and retracted the z-axis away from the print.<br/><br />When the printing is resumed, the hotend temp and z-axis state will automatically be restored <strong>before</strong> the print resumes."
        self.ShowUiPopup(title, message, "notice", None, None, 0, False)


    def ShowLinkAccountMessageIfNeeded(self):
        addPrinterUrl = self.GetAddPrinterUrl()
        hasConnectedAccounts = self.GetHasConnectedAccounts()
        lastInformTimeDateTime = self.GetNoAccountConnectedLastInformDateTime()
        # Check if we know there are connected accounts or not, if we have a add printer URL, and finally if there are no accounts setup yet.
        # If we don't know about connected accounts or have a printer URL, we will skip this until we know for sure.
        if hasConnectedAccounts is False and addPrinterUrl is not None:
            # We will show a popup to help the user setup the plugin every little while. I have gotten a lot of feedback from support
            # tickets indicating this is a problem, so this might help it.
            #
            # We don't want to show the message the first time we load, since the wizard should show. After that we will show it some what frequently.
            # Ideally the user will either setup the plugin or remove it so it doesn't consume server resources.
            minTimeBetweenInformsSec = 60 * 1 # Every 1 minute

            # Check the time since the last message.
            if lastInformTimeDateTime is None or (datetime.now() - lastInformTimeDateTime).total_seconds() > minTimeBetweenInformsSec:
                # Update the last show time.
                self.SetNoAccountConnectedLastInformDateTime(datetime.now())

                # Send the UI message.
                if lastInformTimeDateTime is None:
                    # Since the wizard is working now, we will skip the first time we detect this.
                    pass
                else:
                    # We want to show the finish setup message, but we only want to show it if the account is still unlinked.
                    # So we will kick off a new thread to make a http request to check before we show it.
                    t = threading.Thread(target=self.CheckIfPrinterIsSetupAndShowMessageIfNot)
                    t.start()


    # Should be called on a non-main thread!
    # Make a http request to ensure this printer is not owned and shows a pop-up to help the user finish the install if not.
    def CheckIfPrinterIsSetupAndShowMessageIfNot(self):
        try:
            # Check if this printer is owned or not.
            response = requests.post('https://octoeverywhere.com/api/printer/info', json={ "Id": self.EnsureAndGetPrinterId() }, timeout=30)
            if response.status_code != 200:
                raise Exception("Invalid status code "+str(response.status_code))

            # Parse
            jsonData = response.json()
            hasOwners = jsonData["Result"]["HasOwners"]
            self._logger.info("Printer has owner: "+str(hasOwners))

            # If we are owned, update our settings and return!
            if hasOwners is True:
                self.SetHasConnectedAccounts(True)
                return

            # Ensure the printer URL - Add our source tag to it.
            addPrinterUrl = self.GetAddPrinterUrl()
            if addPrinterUrl is None:
                return
            addPrinterUrl += "&source=plugin_popup"

            # If not, show the message.
            title = "Complete Your Setup"
            message = 'You\'re <strong>only 15 seconds</strong> away from OctoEverywhere\'s free remote access to OctoPrint from anywhere!'
            self.ShowUiPopup(title, message, "notice", "Finish Your Setup Now", addPrinterUrl, 20, False)

        except Exception as e:
            if "Temporary failure in name resolution" in str(e):
                # Ignore this temp issue.
                pass
            else:
                Sentry.Exception("CheckIfPrinterIsSetupAndShowMessageIfNot failed", e)


    # Ensures we have generated a printer id and returns it.
    def EnsureAndGetPrinterId(self):
        # Try to get the current.
        # "PrinterKey" is used by name in the static plugin JS and needs to be updated if this ever changes.
        currentId = self.GetFromSettings("PrinterKey", None)

        # Make sure the current ID is valid.
        if HostCommon.IsPrinterIdValid(currentId) is False:
            if currentId is None:
                self._logger.info("No printer id found, regenerating.")
            else:
                self._logger.info("Old printer id of length " + str(len(currentId)) + " is invalid, regenerating.")

            # Create and save the new value
            currentId = HostCommon.GeneratePrinterId()
            self._logger.info("New printer id is: "+currentId)

            # Update the printer URL whenever the id changes to ensure they always stay in sync.
            self.SetAddPrinterUrl(HostCommon.GetAddPrinterUrl(currentId, True))

            # "PrinterKey" is used by name in the static plugin JS and needs to be updated if this ever changes.
            self.SaveToSettingsIfUpdated("PrinterKey", currentId)

        # Return
        return currentId

    # Ensures we have generated a private key and returns it.
    # This key not a key used for crypo purposes, but instead generated and tied to this instance's printer id.
    # The Printer id is used to ID the printer around the website, so it's more well known. This key is stored by this plugin
    # and is only used during the handshake to send to the server. Once set it can never be changed, or the server will reject the
    # handshake for the given printer ID.
    def EnsureAndGetPrivateKey(self):
        # Try to get the current.
        currentKey = self.GetFromSettings("Pid", None)

        # Make sure the current ID is valid.
        if HostCommon.IsPrivateKeyValid(currentKey) is False:
            if currentKey is None:
                self._logger.info("No private key found, regenerating.")
            else:
                self._logger.info("Old private key of length " + str(len(currentKey)) + " is invalid, regenerating.")

            # Create and save the new value
            currentKey = HostCommon.GeneratePrivateKey()

            # Save - it's important to only call then when the key is updated, since we race the `incompleteStartup` flag
            # around the same time this is accessed. See the comment above with `incompleteStartup` for details.
            self.SaveToSettingsIfUpdated("Pid", currentKey)

        # Return
        return currentKey

    # Ensures the plugin version is set into the settings for the frontend.
    def EnsurePluginVersionSet(self):
        # We save the current plugin version into the settings so the frontend JS can get it.
        self.SaveToSettingsIfUpdated("PluginVersion", self._plugin_version)

    # Returns the frontend http port OctoPrint's http proxy is running on.
    def GetFrontendHttpPort(self):
        # Always try to get and parse the settings value. If the value doesn't exist
        # or it's invalid this will fall back to the default value.
        try:
            return int(self.GetFromSettings("HttpFrontendPort", 80))
        except Exception:
            return 80

    # Returns the if the frontend http proxy for OctoPrint is using https.
    def GetFrontendIsHttps(self):
        # Always try to get and parse the settings value. If the value doesn't exist
        # or it's invalid this will fall back to the default value.
        try:
            return self.GetFromSettings("HttpFrontendIsHttps", False)
        except Exception:
            return False

    # Interface function - Sends a UI popup message for various uses.
    # Must stay in sync with the OctoPrint handler!
    # title - string, the title text.
    # text  - string, the message.
    # type  - string, [notice, info, success, error] the type of message shown.
    # actionText - string, if not None or empty, this is the text to show on the action button or text link.
    # actionLink - string, if not None or empty, this is the URL to show on the action button or text link.
    # onlyShowIfLoadedViaOeBool - bool, if set, the message should only be shown on browsers loading the portal from OE.
    def ShowUiPopup(self, title:str, text:str, msgType:str, actionText:str, actionLink:str, showForSec:int, onlyShowIfLoadedViaOeBool:bool):
        data = {"title":title, "text":text, "type":msgType, "actionText":actionText, "actionLink":actionLink, "showForSec":showForSec, "onlyShowIfLoadedViaOeBool":onlyShowIfLoadedViaOeBool}
        self._plugin_manager.send_plugin_message("octoeverywhere_ui_popup_msg", data)

    # Fired when the connection to the primary server is established.
    # connectedAccounts - a string list of connected accounts, can be an empty list.
    def OnPrimaryConnectionEstablished(self, octoKey, connectedAccounts):
        # On connection, set if there are connected accounts. We don't want to save the email
        # addresses in the settings, since they can be read by anyone that has access to the config
        # file or any plugin.
        hasConnectedAccounts = connectedAccounts is not None and len(connectedAccounts) > 0
        self.SetHasConnectedAccounts(hasConnectedAccounts)

        # Clear out the update required flag, since we connected.
        self.SetPluginUpdateRequired(False)

        # Always set the OctoKey as well.
        self.SetOctoKey(octoKey)

    # Fired when the plugin needs to be updated before OctoEverywhere can be used again.
    # This should so a message to the user, so they know they need to update.
    def OnPluginUpdateRequired(self):
        self._logger.error("The OctoEverywhere service told us we must update before we can connect.")
        self.SetPluginUpdateRequired(True)

    # Our main worker
    def main(self):
        self._logger.info("Main thread starting")

        try:
            # Get or create a printer id.
            printerId = self.EnsureAndGetPrinterId()
            privateKey = self.EnsureAndGetPrivateKey()

            # Get the frontend http port OctoPrint or it's proxy is running on.
            # This is the port the user would use if they were accessing OctoPrint locally.
            # Normally this is port 80, but some users might configure it differently.
            frontendHttpPort = self.GetFrontendHttpPort()
            frontendIsHttps = self.GetFrontendIsHttps()
            self._logger.info("Frontend http port detected as " + str(frontendHttpPort) + ", is https? "+str(frontendIsHttps))

            # Set the ports this instance is running on
            OctoHttpRequest.SetLocalHttpProxyPort(frontendHttpPort)
            OctoHttpRequest.SetLocalOctoPrintPort(self.OctoPrintLocalPort)
            OctoHttpRequest.SetLocalHostAddress(self.OctoPrintLocalHost)
            OctoHttpRequest.SetLocalHttpProxyIsHttps(frontendIsHttps)

            # Run!
            oe = OctoEverywhere(HostCommon.c_OctoEverywhereOctoClientWsUri, printerId, privateKey, self._logger, self, self, self._plugin_version, ServerHost.OctoPrint, False)
            oe.RunBlocking()
        except Exception as e:
            Sentry.Exception("Exception thrown out of main runner.", e)

    # For logging and debugging purposes, print the IPs the hostname is resolving to.
    def TryToPrintHostNameIps(self):
        try:
            try:
                starportIp = socket.getaddrinfo('starport-v1.octoeverywhere.com', None, socket.AF_INET)[0][4][0]
                mainSiteIp = socket.getaddrinfo('octoeverywhere.com', None, socket.AF_INET)[0][4][0]
                self._logger.info("IPV4 - starport:"+str(starportIp)+" main:"+str(mainSiteIp))
            except Exception as e:
                self._logger.info("Failed to resolve host ipv4 name "+str(e))
            try:
                starportIp = socket.getaddrinfo('starport-v1.octoeverywhere.com', None, socket.AF_INET6)[0][4][0]
                mainSiteIp = socket.getaddrinfo('octoeverywhere.com', None, socket.AF_INET6)[0][4][0]
                self._logger.info("IPV6 - starport:"+str(starportIp)+" main:"+str(mainSiteIp))
            except Exception as e:
                self._logger.info("Failed to resolve host ipv6 name "+str(e))
        except Exception as _:
            pass

    #
    # Variable getters and setters.
    #

    def SetOctoKey(self, key):
        # We don't save the OctoKey to settings, keep it in memory.
        self.octoKey = key
        # We also need to set it into the notification handler.
        if self.NotificationHandler is not None:
            self.NotificationHandler.SetOctoKey(key)

    def GetOctoKey(self):
        if self.octoKey is None:
            return ""
        return self.octoKey

    def GetHasConnectedAccounts(self):
        return self.GetBoolFromSettings("HasConnectedAccounts", False)

    def SetHasConnectedAccounts(self, hasConnectedAccounts):
        self.SaveToSettingsIfUpdated("HasConnectedAccounts", hasConnectedAccounts is True)

    def GetPluginUpdateRequired(self):
        return self.GetBoolFromSettings("PluginUpdateRequired", False)

    def SetPluginUpdateRequired(self, pluginUpdateRequired):
        self.SaveToSettingsIfUpdated("PluginUpdateRequired", pluginUpdateRequired is True)

    def GetNoAccountConnectedLastInformDateTime(self):
        return self.GetFromSettings("NoAccountConnectedLastInformDateTime", None)

    def SetNoAccountConnectedLastInformDateTime(self, dateTime):
        self.SaveToSettingsIfUpdated("NoAccountConnectedLastInformDateTime", dateTime)

    # Returns None if there is no url set.
    # Note the URL will always have a ?, so it's safe to append a &source=bar on it.
    def GetAddPrinterUrl(self):
        return self.GetFromSettings("AddPrinterUrl", None)

    def SetAddPrinterUrl(self, url):
        self.SaveToSettingsIfUpdated("AddPrinterUrl", url)

    # Gets the current setting or the default value.
    def GetBoolFromSettings(self, name, default):
        value = self._settings.get([name])
        if value is None:
            return default
        return value is True

    # Gets the current setting or the default value.
    def GetFromSettings(self, name, default):
        value = self._settings.get([name])
        if value is None:
            return default
        return value

    # Saves the value into to the settings object if the value changed.
    def SaveToSettingsIfUpdated(self, name, value):
        #
        # A quick note about settings and creating / saving settings during startup!
        #
        # Notes about _settings:
        #    - The force=True MUST ALWAYS BE USED for the .set() function. This is because we don't offer any default settings in get_settings_defaults, and if we don't use the force flag
        #      the setting doesn't match an existing path is ignored.
        #    - We should only set() and save() the settings when things actually change to prevent race conditions with anything else in OctoPrint writing to or saving settings.
        #    - Ideally anything that needs to be generated and written into the settings should happen IN SYNC during the on_startup or on_after_startup calls.
        #
        # We had a bug where OctoEverywhere would put OctoPrint into Safe Mode on the next reboot. After hours of debugging
        # we realized it was because when we updated and saved settings. The OctoPrint safe mode can get triggered when the var `incompleteStartup` remains set to True in the OctoPrint config.
        # This flag is set to true on startup and then set to false after `on_after_startup` is called on all plugins. The problem was our logic in on_after_startup raced the clearing logic of
        # that flag and sometimes resulted in it not being unset.
        #
        curValue = self.GetFromSettings(name, None)
        if curValue is None or curValue != value:
            self._logger.info("Value "+str(name)+" has changed so we are updating the value in settings and saving.")
            self._settings.set([name], value, force=True)
            self._settings.save(force=True)

__plugin_name__ = "OctoEverywhere!"
__plugin_pythoncompat__ = ">=3.0,<4" # Only PY3

def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = OctoeverywherePlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.accesscontrol.keyvalidator": __plugin_implementation__.key_validator,
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.received_gcode,
        "octoprint.comm.protocol.gcode.sent": __plugin_implementation__.sent_gcode,
        "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.queuing_gcode,
        # We supply a int here to set our order, so we can be one of the first plugins to execute, to prevent issues.
        # The default order value is 1000
        "octoprint.comm.protocol.scripts": (__plugin_implementation__.script_hook, 1337),
    }
