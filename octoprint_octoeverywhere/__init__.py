# coding=utf-8
from __future__ import absolute_import

### (Don't forget to remove me)
# This is a basic skeleton for your plugin's __init__.py. You probably want to adjust the class name of your plugin
# as well as the plugin mixins it's subclassing from. This is really just a basic skeleton to get you started,
# defining your plugin as a template plugin, settings and asset plugin. Feel free to add or remove mixins
# as necessary.
#
# Take a look at the documentation on what other plugin mixins are available.

import octoprint.plugin

class OctoeverywherePlugin(octoprint.plugin.SettingsPlugin,
                           octoprint.plugin.AssetPlugin,
                           octoprint.plugin.TemplatePlugin):

	##~~ SettingsPlugin mixin

	def get_settings_defaults(self):
		return dict(
			PrinterKey="testkey"
		)

	##~~ AssetPlugin mixin

	def get_assets(self):
		# Define your plugin's asset files to automatically include in the
		# core UI here.
		return dict(
			js=["js/OctoEverywhere.js"],
			css=["css/OctoEverywhere.css"],
			less=["less/OctoEverywhere.less"]
		)

	##~~ Softwareupdate hook

	def get_update_information(self):
		# Define the configuration for your plugin to use with the Software Update
		# Plugin here. See https://docs.octoprint.org/en/master/bundledplugins/softwareupdate.html
		# for details.
		return dict(
			OctoEverywhere=dict(
				displayName="Octoeverywhere Plugin",
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


__plugin_name__ = "Octo Everywhere!"
# Starting with OctoPrint 1.4.0 OctoPrint will also support to run under Python 3 in addition to the deprecated
# Python 2. New plugins should make sure to run under both versions for now. Uncomment one of the following
# compatibility flags according to what Python versions your plugin supports!
__plugin_pythoncompat__ = ">=3,<4" # only python 3

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = OctoeverywherePlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
	}

