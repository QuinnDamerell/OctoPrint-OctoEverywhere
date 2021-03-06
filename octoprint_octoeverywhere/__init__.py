# coding=utf-8
from __future__ import absolute_import
import logging
import threading
import random
import string

from .octoeverywhereimpl import OctoEverywhere
import octoprint.plugin

class OctoeverywherePlugin(octoprint.plugin.StartupPlugin,
							octoprint.plugin.SettingsPlugin,
							octoprint.plugin.AssetPlugin,
							octoprint.plugin.TemplatePlugin,
							octoprint.plugin.WizardPlugin):

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
		return False

	# Increment this if we need to pop the wizard again.
	def get_wizard_version(self):
		return 2

	def get_wizard_details(self):
		return {"AddPrinterUrl": self._settings.get(["AddPrinterUrl"])}

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

		# Ensure they key is created here, so make sure that it is always created before
		# Any of the UI queries for it.
		self.EnsureAndGetPrinterId()

	# Call when the system is ready and running
	def on_after_startup(self):
		# Spin off a thread for us to operate on.
		self._logger.info("After startup called. Strating workder thread.")
		main_thread = threading.Thread(target=self.main)
		main_thread.daemon = True
		main_thread.start()

	# The length the printer ID should be.
	c_OctoEverywherePrinterIdLength = 40
	# The url for the add printer process.
	c_OctoEverywhereAddPrinterUrl = "https://octoeverywhere.com/getstarted?printerid="

	# Returns a new printer Id. This needs to be crypo-random to make sure it's not
	# predictable.
	def GeneratePrinterId(self):
		return ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(self.c_OctoEverywherePrinterIdLength))

	# Ensures we have generated a printer id and returns it.
	def EnsureAndGetPrinterId(self):
		# Try to get the current.
		currentId = self._settings.get(["PrinterKey"])

		# Make sure the current ID is valid.
		if currentId == None or len(currentId) < self.c_OctoEverywherePrinterIdLength:
			# Create and save the new value
			self._logger.info("Old printer id of length " + str(len(currentId)) + " is invlaid, regenerating.")
			currentId = self.GeneratePrinterId()
			self._logger.info("New printer id is: "+currentId)

		# Always update the settings, so they are always correct.
		self._settings.set(["AddPrinterUrl"], self.c_OctoEverywhereAddPrinterUrl + currentId, force=True)
		self._settings.set(["PrinterKey"], currentId, force=True)
		self._settings.save(force=True)
		return currentId

	# Sends a UI popup message for various uses.
	# title - string, the title text.
	# text  - string, the message.
	# type  - string, [notice, info, success, error] the type of message shown.
	# audioHide - bool, indicates if the message should auto hide.
	def ShowUiPopup(self, title, text, type, autoHide):
		data = {"title": title, "text": text, "type": type, "autoHide": autoHide}
		self._plugin_manager.send_plugin_message("octoeverywhere_ui_popup_msg", data)

	# Our main worker
	def main(self):
		self._logger.info("Main thread starting")

		try:
			# Get or create a printer id.
			printerId = self.EnsureAndGetPrinterId()

			# For now, we will always use 8080 for mjpg-streamer. This is the default port and I think
			# most all configs will run on it. Ideally we would pull this from the config.
			mjpgStreamerLocalPort = 8080

			# Run!
			OctoEverywhereWsUri = "wss://octoeverywhere.com/octoclientws"
			oe = OctoEverywhere(OctoEverywhereWsUri, self.OctoPrintLocalPort, mjpgStreamerLocalPort, printerId, self._logger, self, self._plugin_version)
			oe.RunBlocking()		
		except Exception as e:
			self._logger.error("Exception thrown out of main runner. "+str(e))

__plugin_name__ = "OctoEverywhere!"
__plugin_pythoncompat__ = ">=2.7,<4" # py 2.7 or 3

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = OctoeverywherePlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
	}
