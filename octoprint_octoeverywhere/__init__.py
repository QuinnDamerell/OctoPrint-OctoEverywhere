# coding=utf-8
from __future__ import absolute_import
import logging
import threading
import random
import string
from datetime import datetime

# Use for the simple api mixin
import flask

from .octoeverywhereimpl import OctoEverywhere
from .octohttprequest import OctoHttpRequest
from .notificationshandler import NotificationsHandler
import octoprint.plugin

class OctoeverywherePlugin(octoprint.plugin.StartupPlugin,
                            octoprint.plugin.SettingsPlugin,
                            octoprint.plugin.AssetPlugin,
                            octoprint.plugin.TemplatePlugin,
                            octoprint.plugin.WizardPlugin,
                            octoprint.plugin.SimpleApiPlugin,
                            octoprint.plugin.EventHandlerPlugin,
                            octoprint.plugin.ProgressPlugin):

    # The port this octoprint instance is listening on.
    OctoPrintLocalPort = 80

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
        return hasConnectedAccounts == False

    # Increment this if we need to pop the wizard again.
    def get_wizard_version(self):
        return 10

    def get_wizard_details(self):
        # Do some sanity checking logic, since this has been sensitive in the past.
        printerUrl = self._settings.get(["AddPrinterUrl"])
        if printerUrl == None:
            self._logger.error("Failed to get OctoPrinter Url for wizard.")
            printerUrl = "https://octoeverywhere.com/getstarted"
        return {"AddPrinterUrl": printerUrl}

    # Return the default settings.
    def get_settings_defaults(self):
        return dict(PrinterKey="", AddPrinterUrl="")

    # Return the current printer key for the settings template
    def get_template_vars(self):
        return dict(
            PrinterKey=self._settings.get(["PrinterKey"]),
            AddPrinterUrl=self._settings.get(["AddPrinterUrl"])
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
    def on_startup(self, ip, port):
        # Get the port the server is listening on, since for some configs it's not the default.
        self.OctoPrintLocalPort = port
        self._logger.info("OctoPrint port " + str(self.OctoPrintLocalPort))

        # Create the notification object.
        self.notificationHandler = NotificationsHandler()

        # Ensure they key is created here, so make sure that it is always created before
        # Any of the UI queries for it.
        self.EnsureAndGetPrinterId()

    # Call when the system is ready and running
    def on_after_startup(self):
        # Spin off a thread for us to operate on.
        self._logger.info("After startup called. Starting worker thread.")
        main_thread = threading.Thread(target=self.main)
        main_thread.daemon = True
        main_thread.start()

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
        if command == "setFrontendLocalPort":
            # Ensure we can find a port.
            if "port" in data and data["port"] != None:

                # Get vars
                port = int(data["port"])
                url = "Unknown"
                if "url" in data and data["url"] != None:
                    url = str(data["url"])
                isHttps = False
                if "isHttps" in data and data["isHttps"] != None:
                    isHttps = data["isHttps"]

                # Report
                self._logger.info("SetFrontendLocalPort API called. Port:"+str(port)+" IsHttps:"+str(isHttps)+" URL:"+url)
                # Save
                self._settings.set(["HttpFrontendPort"], port, force=True)
                self._settings.set(["HttpFrontendIsHttps"], isHttps, force=True)
                self._settings.save(force=True)
                # Update the running value.
                OctoHttpRequest.SetLocalHttpProxyPort(port)
                OctoHttpRequest.SetLocalHttpProxyIsHttps(isHttps)
            else:
                self._logger.info("SetFrontendLocalPort API called with no port.")
        else:
            self._logger.info("Unknown API command. "+command)


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
    # Functions are for the Process Plugin
    #
    def on_print_progress(self, storage, path, progressInt):
        self.notificationHandler.OnPrintProgress(progressInt)

    #
    # Functions for the Event Handler Mixin
    #
    def on_event(self, event, payload):
        # Ensure there's a payload
        if payload is None:
            payload = {}

        # Listen for client authed events, these fire whenever a websocket opens and is auth is done.
        if event == "ClientAuthed":
            self.HandleClientAuthedEvent()

        # Listen for the rest of these events for notifications.
        if event == "PrintStarted":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            self.notificationHandler.OnStarted(fileName)
        if event == "PrintFailed":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            durationSec = self.GetDictStringOrEmpty(payload, "time")
            reason = self.GetDictStringOrEmpty(payload, "reason")
            self.notificationHandler.OnFailed(fileName, durationSec, reason)
        if event == "PrintDone":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            durationSec = self.GetDictStringOrEmpty(payload, "time")
            self.notificationHandler.OnDone(fileName, durationSec)
        if event == "PrintPaused":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            self.notificationHandler.OnPaused(fileName)


    def GetDictStringOrEmpty(self, dict, key):
        if dict[key] is None:
            return ""
        return str(dict[key])

    def HandleClientAuthedEvent(self):
        hasConnectedAccounts = self.GetHasConnectedAccounts()
        addPrinterUrl = self._settings.get(["AddPrinterUrl"])
        # Check if we know there are connected accounts or not, if we have a add printer URL, and finally if there are no accounts setup yet.
        # If we don't know about connected accounts or have a printer URL, we will skip this until we know for sure.
        if hasConnectedAccounts == False and addPrinterUrl != None:
            #
            # We will only inform the user there are no connected accounts when it's first
            # detected and then every little while. We don't want to bug the user, so the time must be long.
            # Since our wizard doesn't work well, we will use this for the time being.
            #
            # Note! The time must also be longer than the primary socket refresh time, because when an account 
            # is connected in the service our plugin currently doesn't get a message. So we rely on the primary socket
            # refresh to update the value every ~48 hours.
            minTimeBetweenInformsSec = 60 * 60 * 24 * 30 # Every 30 days.

            # Check the time since the last message.
            lastInformTime = self._settings.get(["NoAccountConnectedLastInformTime"])
            if lastInformTime == None or (datetime.now() - lastInformTime).total_seconds() > minTimeBetweenInformsSec:
                # Update the last show time.
                self._settings.set(["NoAccountConnectedLastInformTime"], datetime.now(), force=True)

                # Send the UI message.
                if lastInformTime == None:
                    # Show a different messsage for the first time.
                    # Disable for now since the wizard should be working.
                    # title = "OctoEverywhere Blastoff!"
                    # message = '<br/>The OctoEverywhere plugin is up and running! Click the button below and in about <strong>15 seconds</strong> you too will enjoy free remote acccess from everywhere!<br/><br/><a class="btn btn-primary" style="color:white" target="_blank" href="'+addPrinterUrl+'">Finish Your Setup Now!&nbsp;&nbsp;<i class="fa fa-external-link"></i></a>'
                    # self.ShowUiPopup(title, message, "notice", True)
                    pass
                else:
                    title = "We Miss You"
                    message = '<br/>It only takes about <strong>15 seconds</strong> to finish the OctoEverywhere setup and you too can enjoy free remote access from everywhere!<br/><br/><a class="btn btn-primary" style="color:white" target="_blank" href="'+addPrinterUrl+'">Finish Your Setup Now!&nbsp;&nbsp;<i class="fa fa-external-link"></i></a>'
                    self.ShowUiPopup(title, message, "notice", True)
        
        # Check if an update is required, if so, tell the user everytime they login.
        pluginUpdateRequired = self.GetPluginUpdateRequired()
        if pluginUpdateRequired == True:
            title = "OctoEverywhere Disabled"
            message = '<br/><strong>You need to update your OctoEverywhere plugin before you can continue using OctoEverywhere.</strong><br/><br/>We are always improving OctoEverywhere to make things faster and add features. Sometimes, that means we have to break things. If you need info about how to update your plugin, <a target="_blank" href="https://octoeverywhere.com/pluginupdate">check this out.</i></a>'
            self.ShowUiPopup(title, message, "notice", True)

    # The length the printer ID should be.
    c_OctoEverywherePrinterIdIdealLength = 60
    c_OctoEverywherePrinterIdMinLength = 40
    # The url for the add printer process.
    c_OctoEverywhereAddPrinterUrl = "https://octoeverywhere.com/getstarted?isFromOctoPrint=true&printerid="

    # Returns a new printer Id. This needs to be crypo-random to make sure it's not
    # predictable.
    def GeneratePrinterId(self):
        return ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(self.c_OctoEverywherePrinterIdIdealLength))

    # Ensures we have generated a printer id and returns it.
    def EnsureAndGetPrinterId(self):
        # Try to get the current.
        currentId = self._settings.get(["PrinterKey"])

        # Make sure the current ID is valid.
        if currentId == None or len(currentId) < self.c_OctoEverywherePrinterIdMinLength:
            # Create and save the new value
            self._logger.info("Old printer id of length " + str(len(currentId)) + " is invlaid, regenerating.")
            currentId = self.GeneratePrinterId()
            self._logger.info("New printer id is: "+currentId)

        # Always update the settings, so they are always correct.
        self._settings.set(["AddPrinterUrl"], self.c_OctoEverywhereAddPrinterUrl + currentId, force=True)
        self._settings.set(["PrinterKey"], currentId, force=True)
        self._settings.save(force=True)
        return currentId

    # Returns the frontend http port OctoPrint's http proxy is running on.
    def GetFrontendHttpPort(self):
        # Always try to get and parse the settings value. If the value doesn't exist
        # or it's invalid this will fall back to the default value.
        try:
            return int(self._settings.get(["HttpFrontendPort"]))
        except:
            return 80

    # Returns the if the frontend http proxy for OctoPrint is using https.
    def GetFrontendIsHttps(self):
        # Always try to get and parse the settings value. If the value doesn't exist
        # or it's invalid this will fall back to the default value.
        try:
            return self._settings.get(["HttpFrontendIsHttps"])
        except:
            return False
    
    # Sends a UI popup message for various uses.
    # title - string, the title text.
    # text  - string, the message.
    # type  - string, [notice, info, success, error] the type of message shown.
    # audioHide - bool, indicates if the message should auto hide.
    def ShowUiPopup(self, title, text, type, autoHide):
        data = {"title": title, "text": text, "type": type, "autoHide": autoHide}
        self._plugin_manager.send_plugin_message("octoeverywhere_ui_popup_msg", data)

    # Fired when the connection to the primary server is established.
    # connectedAccounts - a string list of connected accounts, can be an empty list.
    def OnPrimaryConnectionEstablished(self, octoKey, connectedAccounts):
        # On connection, set if there are connected accounts. We don't want to save the email
        # addresses in the settings, since they can be read by anyone that has access to the config 
        # file or any plugin.
        hasConnectedAccounts = connectedAccounts != None and len(connectedAccounts) > 0
        self.SetHasConnectedAccounts(hasConnectedAccounts)

        # Clear out the update required flag, since we connected.
        self.SetPluginUpdateRequired(False)

        # Always set the OctoKey as well.
        self.SetOctoKey(octoKey)

        # Clear this old value.
        self._settings.set(["ConnectedAccounts"], "", force=True)

        # Save
        self._settings.save(force=True)

    # Fired when the plugin needs to be updated before OctoEverywhere can be used again.
    # This should so a message to the user, so they know they need to update.    
    def OnPluginUpdateRequired(self):
        self._logger.error("The OctoEverywhere service told us we must update before we can connect.")
        self.SetPluginUpdateRequired(True)
        self._settings.save(force=True)

    # Our main worker
    def main(self):
        self._logger.info("Main thread starting")

        try:
            # Get or create a printer id.
            printerId = self.EnsureAndGetPrinterId()

            # Get the frontend http port OctoPrint or it's proxy is running on.
            # This is the port the user would use if they were accessing OctoPrint locally.
            # Normally this is port 80, but some users might configure it differently.
            frontendHttpPort = self.GetFrontendHttpPort()
            frontendIsHttps = self.GetFrontendIsHttps()
            self._logger.info("Frontend http port detected as " + str(frontendHttpPort) + ", is https? "+str(frontendIsHttps))

            # Set the ports this instance is running on
            OctoHttpRequest.SetLocalHttpProxyPort(frontendHttpPort)
            OctoHttpRequest.SetLocalOctoPrintPort(self.OctoPrintLocalPort)
            OctoHttpRequest.SetLocalHttpProxyIsHttps(frontendIsHttps)

            # Run!
            OctoEverywhereWsUri = "wss://starport-v1.octoeverywhere.com/octoclientws"
            oe = OctoEverywhere(OctoEverywhereWsUri, printerId, self._logger, self, self, self._plugin_version)
            oe.RunBlocking()		
        except Exception as e:
            self._logger.error("Exception thrown out of main runner. "+str(e))

    #
    # Variable getters and setters.
    # 

    def SetOctoKey(self, key):
        # We don't save the OctoKey to settings, keep it in memory.
        self.octoKey = key

    def GetOctoKey(self):
        if self.octoKey == None:
            return ""
        return self.octoKey

    def GetHasConnectedAccounts(self):
        return self.GetBoolFromSettings("HasConnectedAccounts", False)    

    def SetHasConnectedAccounts(self, hasConnectedAccounts):
        self._settings.set(["HasConnectedAccounts"], hasConnectedAccounts == True, force=True)

    def GetPluginUpdateRequired(self):
        return self.GetBoolFromSettings("PluginUpdateRequired", False)   

    def SetPluginUpdateRequired(self, pluginUpdateRequired):
        self._settings.set(["PluginUpdateRequired"], pluginUpdateRequired == True, force=True)

    # Gets the current setting or the default value.
    def GetBoolFromSettings(self, name, default):
        value = self._settings.get([name])
        if value == None:
            return default
        return value == True


__plugin_name__ = "OctoEverywhere!"
__plugin_pythoncompat__ = ">=2.7,<4" # py 2.7 or 3

def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = OctoeverywherePlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
    }
