# coding=utf-8


#
# This is not an installer script!
# If you're trying to install OctoEverywhere for Klipper or Bambu Connect, you want to use the ./install.sh script!
#
# This PY script is required for the OctoPrint plugin install process.
#
# If you need help, feel free to contact us at support@octoeverywhere.com
#







from setuptools import setup

# The plugin's identifier, has to be unique
plugin_identifier = "octoeverywhere"

# The plugin's python package, should be "octoprint_<plugin identifier>", has to be unique
plugin_package = "octoprint_octoeverywhere"

# The plugin's human readable name. Can be overwritten within OctoPrint's internal data via __plugin_name__ in the
# plugin module
plugin_name = "OctoEverywhere"

# The plugin's version. Can be overwritten within OctoPrint's internal data via __plugin_version__ in the plugin module
# Note that this single version string is used by all of the plugins in OctoEverywhere!
plugin_version = "3.4.0"

# The plugin's description. Can be overwritten within OctoPrint's internal data via __plugin_description__ in the plugin
# module
plugin_description = """Access OctoPrint remotely over the internet anywhere! Including full webcam streaming. Free. Simple. Secure."""

# The plugin's author. Can be overwritten within OctoPrint's internal data via __plugin_author__ in the plugin module
plugin_author = "Quinn Damerell"

# The plugin's author's mail address.
plugin_author_email = "quinnd@outlook.com"

# The plugin's homepage URL. Can be overwritten within OctoPrint's internal data via __plugin_url__ in the plugin module
plugin_url = "http://www.OctoEverywhere.com"

# The plugin's license. Can be overwritten within OctoPrint's internal data via __plugin_license__ in the plugin module
plugin_license = "AGPLv3"

# Any additional requirements besides OctoPrint should be listed here.
#
# On 4/13/2023 we updated to only support PY3, which frees us up from a lot of package issues. A lot the packages we depend on only support PY3 now.
#
# websocket_client
# 	For the websocket_client, some older versions seem to have a thread issue that causes the 24 hour disconnect logic to fail, and eventually makes the thread limit get hit.
# 	Version 1.4.0 also has an SSL error in it. https://github.com/websocket-client/websocket-client/issues/857
#	Update: We also found a bug where the ping timer doesn't get cleaned up: https://github.com/websocket-client/websocket-client/pull/918
#   Thus we need version 1.6.0 or higher.
#   The sonic pad runs python 3.7.8 as of 12/18/2023 and websocket_client>=1.7 doesn't support it. So we must keep our version at 1.6 at least for now.
# dnspython
#	We depend on a feature that was released with 2.3.0, so we need to require at least that.
#   For the same reason as websocket_client for the sonic pad, we also need to include at least 2.3.0, since 2.3.0 is the last version to support python 3.7.8.
# urllib3
#   There is a bug with parsing headers in versions older than 1.26.? (https://github.com/diyan/pywinrm/issues/269). At least 1.26.6 fixes it, ubt we decide to just stick with a newer version.
#   This must be less than 2.0.0, because 2.0.0 requires open ssl 1.1.1+, which the sonic pad doesn't have.
# zstandard
#   zstandard gives us great compression that's super fast, but it requires a native lib to installed. The PY package will come with a lib and or try to build it, but we can also install it via apt-get.
#   For the complexity, we can't list it as a required install, since it won't work on some platforms. So instead we will try to install it during runtime, and then it will be used after the following restart.
#   The package version is defined in octoeverywhere.compression.ZStandardPipPackageString
#
# Other lib version notes:
#   pillow - We don't require a version of pillow because we don't want to mess with other plugins and we use basic, long lived APIs.\
#   certifi - We use to keep certs on the device that we need for let's encrypt. So we want to keep it fresh.
#   rsa - OctoPrint 1.5.3 requires RAS>=4.0, so we must leave it at 4.0.
#   httpx - Is an asyncio http lib. It seems to be required by dnspython, but dnspython doesn't enforce it. We had a user having an issue that updated to 0.24.0, and it resolved the issue.
#   sentry-sdk - We use the same version as OctoPrint, so we don't have to worry about mismatched versions.
#
# Note! These also need to stay in sync with requirements.txt, for the most part they should be the exact same!
plugin_requires = [
    "websocket_client>=1.6.0,<1.7.99",
    "requests>=2.31.0",
    "octoflatbuffers==24.3.26",
    "pillow",
    "certifi>=2023.11.17",
    "rsa>=4.9",
    "dnspython>=2.3.0",
    "httpx>=0.24.1,<0.26.0",
    "urllib3>=1.26.18,<2.0.0",
    "sentry-sdk>=1.19.1,<2",
    #"zstandard" - optional lib see notes
    ]

### --------------------------------------------------------------------------------------------------------------------
### More advanced options that you usually shouldn't have to touch follow after this point
### --------------------------------------------------------------------------------------------------------------------

# Additional package data to install for this plugin. The sub folders "templates", "static" and "translations" will
# already be installed automatically if they exist. Note that if you add something here you'll also need to update
# MANIFEST.in to match to ensure that python setup.py sdist produces a source distribution that contains all your
# files. This is sadly due to how python's setup.py works, see also http://stackoverflow.com/a/14159430/2028598
plugin_additional_data = []

# Any additional python packages you need to install with your plugin that are not contained in <plugin_package>.*
# For OctoEverywhere, we need to include or common packages shared between hosts, so OctoPrint copies them into the package folder as well.
plugin_additional_packages = [ "octoeverywhere", "octoeverywhere.Proto", "octoeverywhere.WebStream", "octoeverywhere.Webcam" ]

# Any python packages within <plugin_package>.* you do NOT want to install with your plugin
plugin_ignored_packages = []

# Additional parameters for the call to setuptools.setup. If your plugin wants to register additional entry points,
# define dependency links or other things like that, this is the place to go. Will be merged recursively with the
# default setup parameters as provided by octoprint_setuptools.create_plugin_setup_parameters using
# octoprint.util.dict_merge.
#
# Example:
#     plugin_requires = ["someDependency==dev"]
#     additional_setup_parameters = {"dependency_links": ["https://github.com/someUser/someRepo/archive/master.zip#egg=someDependency-dev"]}
additional_setup_parameters = {}

########################################################################################################################

try:
    import octoprint_setuptools
except Exception:
    print("Could not import OctoPrint's setuptools, are you sure you are running that under "
        "the same python installation that OctoPrint is installed under?")
    import sys
    sys.exit(-1)

setup_parameters = octoprint_setuptools.create_plugin_setup_parameters(
	identifier=plugin_identifier,
	package=plugin_package,
	name=plugin_name,
	version=plugin_version,
	description=plugin_description,
	author=plugin_author,
	mail=plugin_author_email,
	url=plugin_url,
	license=plugin_license,
	requires=plugin_requires,
	additional_packages=plugin_additional_packages,
	ignored_packages=plugin_ignored_packages,
	additional_data=plugin_additional_data
)

if len(additional_setup_parameters):
    from octoprint.util import dict_merge
    setup_parameters = dict_merge(setup_parameters, additional_setup_parameters)

setup(**setup_parameters)
