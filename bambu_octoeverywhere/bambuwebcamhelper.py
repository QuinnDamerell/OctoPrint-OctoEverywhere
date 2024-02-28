import logging


#from octoeverywhere.sentry import Sentry
from octoeverywhere.webcamhelper import WebcamSettingItem#, WebcamHelper

# This class implements the webcam platform helper interface for bambu.
class BambuWebcamHelper():

    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger


    # !! Interface Function !!
    # This must return an array of WebcamSettingItems.
    # Index 0 is used as the default webcam.
    # The order the webcams are returned is the order the user will see in any selection UIs.
    # Returns None on failure.
    def GetWebcamConfig(self):
        return [WebcamSettingItem("Default", "self.SnapshotUrl", "self.StreamUrl", "self.FlipH", "self.FlipV", "self.Rotation")]

        # # Kick the settings worker since the webcam was accessed.
        # self.KickOffWebcamSettingsUpdate()

        # # Grab the lock to see what we should be returning.
        # with self.ResultsLock:
        #     # If auto settings are enabled, return any cached auto settings that we found.
        #     # If we have anything, make a copy of the array and return it.
        #     if self.EnableAutoSettings:
        #         if len(self.AutoSettingsResults) != 0:
        #             results = []
        #             for i in self.AutoSettingsResults:
        #                 results.append(i)
        #             return results
        #     # If we don't have auto settings enabled or we don't have any results, return what we have in memory.
        #     # This will either be the default values or a values that the user has set.
        #     item = WebcamSettingItem("Default", self.SnapshotUrl, self.StreamUrl, self.FlipH, self.FlipV, self.Rotation)
        #     # Validate the settings, but always return them.
        #     item.Validate(self.Logger)
        #     return [
        #         item
        #     ]
