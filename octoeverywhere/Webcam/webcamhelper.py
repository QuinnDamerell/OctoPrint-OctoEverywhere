import logging
import os
import json
from typing import List

from ..sentry import Sentry
from .webcamutil import WebcamUtil
from .quickcam import QuickCamManager
from ..octohttprequest import OctoHttpRequest
from .webcamsettingitem import WebcamSettingItem

# The point of this class is to abstract the logic that needs to be done to reliably get a webcam snapshot and stream from many types of
# printer setups. The main entry point is GetSnapshot() which will try a number of ways to get a snapshot from whatever camera system is
# setup. This includes USB based cameras, external IP based cameras, and OctoPrint instances that don't have a snapshot URL defined.
class WebcamHelper:

    # These are the headers Oracle will add to indicate a snapshot or webcam stream request.
    # These can't really change since old plugins use them.
    c_OracleSnapshotHeaderKey = "oe-snapshot"         # The existence of this header with any value will be handled as a snapshot request.
    c_OracleStreamHeaderKey = "oe-webcamstream"       # The existence of this header with any value will be handled as a stream request.
    c_OracleWebcamIndexHeaderKey = "oe-webcam-index"  # The existence and value of this header will determine the webcam index.

    # If no other index is specified, 0 is the default webcam index.
    # This assumption is also made in the service and website, so it can't change.
    c_DefaultWebcamIndex = 0

    # A header we apply to all snapshot and webcam streams so the client can get the correct transforms the user has setup.
    c_OeWebcamTransformHeaderKey = "x-oe-webcam-transform"

    # Logic for a static singleton
    _Instance = None


    @staticmethod
    def Init(logger:logging.Logger, webcamPlatformHelperInterface, pluginDataFolderPath):
        WebcamHelper._Instance = WebcamHelper(logger, webcamPlatformHelperInterface, pluginDataFolderPath)
        QuickCamManager.Init(logger, webcamPlatformHelperInterface)


    @staticmethod
    def Get():
        return WebcamHelper._Instance


    def __init__(self, logger:logging.Logger, webcamPlatformHelperInterface, pluginDataFolderPath:str):
        self.Logger = logger
        self.WebcamPlatformHelperInterface = webcamPlatformHelperInterface

        # Init local webcam settings stuffs.
        self.SettingsFilePath = os.path.join(pluginDataFolderPath, "webcam-settings.json")
        self.DefaultCameraName:str = None
        self.LocalPluginWebcamSettingsObjects:List[WebcamSettingItem] = []
        self._LoadPluginWebcamSettings()


    # Returns if flip H is set in the settings.
    def GetWebcamFlipH(self, cameraIndex:int = None):
        obj = self._GetWebcamSettingObj(cameraIndex)
        if obj is None:
            return None
        return obj.FlipH


    # Returns if flip V is set in the settings.
    def GetWebcamFlipV(self, cameraIndex:int = None):
        obj = self._GetWebcamSettingObj(cameraIndex)
        if obj is None:
            return None
        return obj.FlipV


    # Returns if rotate 90 is set in the settings.
    def GetWebcamRotation(self, cameraIndex:int = None):
        obj = self._GetWebcamSettingObj(cameraIndex)
        if obj is None:
            return None
        return obj.Rotation


    # Given a set of request headers, this determine if this is a special Oracle call indicating it's a snapshot or webcam stream.
    def IsSnapshotOrWebcamStreamOracleRequest(self, requestHeadersDict):
        return self.IsSnapshotOracleRequest(requestHeadersDict) or self.IsWebcamStreamOracleRequest(requestHeadersDict)


    # Check if the special header is set, indicating this is a snapshot request.
    def IsSnapshotOracleRequest(self, requestHeadersDict):
        return WebcamHelper.c_OracleSnapshotHeaderKey in requestHeadersDict


    # Check if the special header is set, indicating this is a webcam stream request.
    def IsWebcamStreamOracleRequest(self, requestHeadersDict):
        return WebcamHelper.c_OracleStreamHeaderKey in requestHeadersDict

    # If the header is set to specify a camera name, this returns it. Otherwise None
    def GetOracleRequestCameraIndex(self, requestHeadersDict) -> int:
        if WebcamHelper.c_OracleWebcamIndexHeaderKey in requestHeadersDict:
            return int(requestHeadersDict[WebcamHelper.c_OracleWebcamIndexHeaderKey])
        return None

    # Called by the OctoWebStreamHelper when a Oracle snapshot or webcam stream request is detected.
    # It's important that this function returns a OctoHttpRequest that's very similar to what the default MakeHttpCall function
    # returns, to ensure the rest of the octostream http logic can handle the response.
    def MakeSnapshotOrWebcamStreamRequest(self, httpInitialContext, method, sendHeaders, uploadBuffer) -> OctoHttpRequest.Result:
        cameraIndexOpt = self.GetOracleRequestCameraIndex(sendHeaders)
        if self.IsSnapshotOracleRequest(sendHeaders):
            return self.GetSnapshot(cameraIndexOpt)
        elif self.IsWebcamStreamOracleRequest(sendHeaders):
            return self.GetWebcamStream(cameraIndexOpt)
        else:
            raise Exception("Webcam helper MakeSnapshotOrWebcamStreamRequest was called but the request didn't have the oracle headers?")


    # Tries to get a webcam stream from the system using the webcam stream URL or falling back to the passed path.
    # Returns a OctoHttpResult on success and None on failure.
    #
    # On failure, this returns None. Returning None will fail out the request.
    # On success, this will return a valid OctoHttpRequest.
    def GetWebcamStream(self, cameraIndex:int = None) -> OctoHttpRequest.Result:
        # Wrap the entire result in the add transform function, so on success the header gets added.
        return self._AddOeWebcamTransformHeader(self._GetWebcamStreamInternal(cameraIndex), cameraIndex)


    def _GetWebcamStreamInternal(self, cameraIndex:int = None) -> OctoHttpRequest.Result:
        # Get the webcam settings object for this request.
        # If there are no webcams, this will return None
        webcamSettingsObj = self._GetWebcamSettingObj(cameraIndex)
        if webcamSettingsObj is None:
            return None

        # First, check if this webcam URL needs to be handled by the QuickCam system.
        result = QuickCamManager.Get().TryGetStream(webcamSettingsObj)
        if result is not None:
            return result

        # Try to get the URL from the settings.
        webcamStreamUrl = webcamSettingsObj.StreamUrl
        if webcamStreamUrl is not None:
            # Try to make a standard http call with this stream url
            # Use use this HTTP call helper system because it might be somewhat tricky to know
            # Where to actually make the webcam request in terms of IP and port.
            # We use the allow redirects flag to make the API more robust, since some webcam images might need that.
            #
            # Whatever this returns, the rest of the request system will handle it, since it's expecting the OctoHttpRequest object
            return OctoHttpRequest.MakeHttpCall(self.Logger, webcamStreamUrl, OctoHttpRequest.GetPathType(webcamStreamUrl), "GET", {}, allowRedirects=True)

        # If we can't get the webcam stream URL, return None to fail out the request.
        return None


    # Tries to get a snapshot from the system using the snapshot URL or falling back to the mjpeg stream.
    # Returns a OctoHttpResult on success and None on failure.
    #
    # On failure, this returns None. Returning None will fail out the request.
    # On success, this will return a valid OctoHttpRequest that's fully filled out. The stream will always already be fully read, and will be FullBodyBuffer var.
    def GetSnapshot(self, cameraIndex:int = None) -> OctoHttpRequest.Result:
        # Wrap the entire result in the _EnsureJpegHeaderInfo function, so ensure the returned snapshot can be used by all image processing libs.
        # Wrap the entire result in the add transform function, so on success the header gets added.
        return self._AddOeWebcamTransformHeader(self._EnsureJpegHeaderInfo(self._GetSnapshotInternal(cameraIndex)), cameraIndex)


    def _GetSnapshotInternal(self, cameraIndex:int = None) -> OctoHttpRequest.Result:
        # Get the webcam settings object for this request.
        # If there are no webcams, this will return None
        webcamSettingsObj = self._GetWebcamSettingObj(cameraIndex)
        if webcamSettingsObj is None:
            return None

        # First, check if this webcam URL needs to be handled by the QuickCam system.
        result = QuickCamManager.Get().TryToGetSnapshot(webcamSettingsObj)
        if result is not None:
            return result

        # Next, try to get the snapshot using the string defined in settings.
        snapshotUrl = webcamSettingsObj.SnapshotUrl
        if snapshotUrl is not None:
            # Try to make a standard http call with this snapshot url
            # Use use this HTTP call helper system because it might be somewhat tricky to know
            # Where to actually make the webcam request in terms of IP and port.
            # We use the allow redirects flag to make the API more robust, since some webcam images might need that.
            self.Logger.debug("Trying to get a snapshot using url: %s", snapshotUrl)
            octoHttpResult = OctoHttpRequest.MakeHttpCall(self.Logger, snapshotUrl, OctoHttpRequest.GetPathType(snapshotUrl), "GET", {}, allowRedirects=True)
            # If the result was successful, we are done.

            if octoHttpResult is not None and octoHttpResult.StatusCode == 200:
                return octoHttpResult

        # If getting the snapshot from the snapshot URL fails, try getting a single frame from the mjpeg stream
        streamUrl = webcamSettingsObj.StreamUrl
        if streamUrl is None:
            self.Logger.debug("Snapshot helper failed to get a snapshot from the snapshot URL, but we also don't have a stream URL.")
            return None
        return self._GetSnapshotFromStream(streamUrl)


    def _GetSnapshotFromStream(self, url) -> OctoHttpRequest.Result:
        try:
            # Try to connect the the mjpeg stream using the http helper class.
            # This is required because knowing the port to connect to might be tricky.
            # We use the allow redirects flag to make the API more robust, since some webcam images might need that.
            self.Logger.debug("_GetSnapshotFromStream - Trying to get a snapshot using THE STREAM URL: %s", url)
            octoHttpResult = OctoHttpRequest.MakeHttpCall(self.Logger, url, OctoHttpRequest.GetPathType(url), "GET", {}, allowRedirects=True)
            if octoHttpResult is None:
                self.Logger.debug("_GetSnapshotFromStream - Failed to make web request.")
                return None

            # Check for success.
            if octoHttpResult.StatusCode != 200:
                self.Logger.info("Snapshot fallback failed due to the http call having a bad status: "+str(octoHttpResult.StatusCode))
                return None

            # Hold the entire response in a with block, so that we we leave it will be cleaned up, since it's most likely a streaming stream.
            with octoHttpResult:
                # Use the common logic to get the snapshot from the stream.
                result = WebcamUtil.GetSnapshotFromStream(self.Logger, octoHttpResult)
                if result is None:
                    return None

                # If successful, set values to match the fixed size body and content type.
                headers = {
                    # Set the content type to the header we got from the stream chunk.
                    "content-type": result.ContentType,
                    # It's very important this size matches the body buffer we give OctoHttpRequest, or the logic in the http loop will fail because it will keep trying to read more.
                    "content-length": str(len(result.ImageBuffer))
                }
                # Return a result. Return the full image buffer which will be used as the response body.
                return OctoHttpRequest.Result(200, headers, url, True, fullBodyBuffer=result.ImageBuffer)
        except Exception as e:
            Sentry.Exception("Failed to get fallback snapshot.", e)
        return None


    # Returns the default webcam setting object or None if there isn't one.
    # If there isn't a default webcam name, it's assumed to be the first webcam returned in the list command.
    # If there are no webcams, this will return None
    def _GetWebcamSettingObj(self, cameraIndex:int = None):
        try:
            # Get the current list of webcam settings.
            webcamItems = self.ListWebcams()
            if webcamItems is None or len(webcamItems) == 0:
                return None

            # If a camera index wasn't passed, get the default index.
            if cameraIndex is None:
                cameraIndex = self.GetDefaultCameraIndex(webcamItems)

            # We will always get a default index back from the above function.
            if cameraIndex is not None and cameraIndex >= 0 and cameraIndex < len(webcamItems):
                return webcamItems[cameraIndex]

            self.Logger.warn(f"_GetWebcamSettingObj asked for {cameraIndex} but it was out of bounds. Max: {len(webcamItems)}")
            return webcamItems[WebcamHelper.c_DefaultWebcamIndex]
        except Exception as e:
            Sentry.Exception("WebcamHelper _GetWebcamSettingObj exception.", e)
        return None


    # Returns the currently known list of webcams.
    # The order they are returned is the order the use sees them.
    # The default is usually the index 0.
    def ListWebcams(self) -> List[WebcamSettingItem]:
        try:
            # Get the webcams from the platform.
            ret = self.WebcamPlatformHelperInterface.GetWebcamConfig()

            # Check if there are any plugin local items to return.
            # Note the cameras returned from ListWebcams() must always be first - the bambu logic depends on this! (see GetSnapshot_Override)
            pluginLocalWebcamItems = self.GetPluginLocalWebcamList()
            if pluginLocalWebcamItems is not None and len(pluginLocalWebcamItems) > 0:
                if ret is None:
                    ret = []
                ret.extend(pluginLocalWebcamItems)

            # Ensure we got something
            if ret is None or len(ret) == 0:
                return None
            return ret
        except Exception as e:
            Sentry.Exception("WebcamHelper ListWebcams exception.", e)
        return None


    # Checks if the result was success and if so adds the common header.
    # Returns the octoHttpResult, so the function is chainable
    def _AddOeWebcamTransformHeader(self, octoHttpResult, cameraIndex:int):
        if octoHttpResult is None or octoHttpResult.StatusCode > 300:
            return octoHttpResult

        # Default to none
        transformStr = "none"

        # If there are any settings build a string with them all contaminated.
        settings = self._GetWebcamSettingObj(cameraIndex)
        if settings.FlipH or settings.FlipV or settings.Rotation != 0:
            transformStr = ""
            if settings.FlipH:
                transformStr += "fliph "
            if settings.FlipV:
                transformStr += "flipv "
            if settings.Rotation != 0:
                transformStr += "rotate="+str(settings.Rotation)+" "

        # Set the header
        octoHttpResult.Headers[WebcamHelper.c_OeWebcamTransformHeaderKey] = transformStr
        return octoHttpResult


    # Checks if the result was success and if so checks if the image is a jpeg and if the header info is set correctly.
    # For some webcam servers, we have seen them return jpegs that have incomplete jpeg binary header data, which breaks some image processing libs.
    # This seems to break ImageSharp and it also breaks whatever Telegram uses on it's server side for processing.
    # To combat this, we will check if the image is a jpeg, and if so, ensure the header is set correctly.
    #
    # Returns the octoHttpResult, so the function is chainable
    def _EnsureJpegHeaderInfo(self, octoHttpResult:OctoHttpRequest.Result):
        # Ensure we got a result.
        if octoHttpResult is None or octoHttpResult.StatusCode > 300:
            return octoHttpResult

        # The GetSnapshot API will always return the fully buffered snapshot.
        # If there already isn't a full buffered body, make one now.
        buf = octoHttpResult.FullBodyBuffer
        if buf is None:
            # This will read the entire stream and store it into the FullBodyBuffer
            octoHttpResult.ReadAllContentFromStreamResponse(self.Logger)
            buf = octoHttpResult.FullBodyBuffer
            if buf is None:
                self.Logger.error("_EnsureJpegHeaderInfo got a null body read from ReadAllContentFromStreamResponse")
                return None

        imgBuffer = WebcamUtil.EnsureJpegHeaderInfo(self.Logger, buf)
        octoHttpResult.SetFullBodyBuffer(imgBuffer)
        return octoHttpResult


    def GetDevAddress(self):
        return OctoHttpRequest.GetLocalhostAddress()+":"+str(OctoHttpRequest.GetLocalOctoPrintPort())


    # A static helper that provides common logic to detect urls for camera-streamer.
    #
    # Both OctoPrint and Klipper are using camera-streamer for WebRTC webcam streaming. If the system is going to be WebRTC based,
    # it's going to be camera-streamer. There are a ton of other streaming types use commonly, the most common being jmpeg from server sources, as well as HLS, and more.
    #
    # This function is designed to detect the camera-streamer URLs and fix them up for our internal use. We support WebRTC via the Klipper or OctoPrint portals,
    # but for all of our service related streaming we can't support WebRTC. For things like Live Links, WebRTC would expose the WAN IP of the user's device.
    # Thus, for anything internally to OctoEverywhere, we convert camera-streamer's webrtc stream URL to jmpeg.
    #
    # If the camera-streamer webrtc stream URL is found, the correct camera-streamer jmpeg stream is returned.
    # Otherwise None is returned.
    @staticmethod
    def DetectCameraStreamerWebRTCStreamUrlAndTranslate(streamUrl:str) -> str:
        # Ensure there's something to work with
        if streamUrl is None:
            return None

        # try to find anything with /webrtc in it, which is a pretty unique signature for camera-streamer
        streamUrlLower = streamUrl.lower()
        webRtcLocation = streamUrlLower.find("/webrtc")
        if webRtcLocation == -1:
            return None

        # Since just /webrtc is vague, make sure there's no more paths after the webrtc
        forwardSlashAfterWebrtc = streamUrlLower.find('/', webRtcLocation + 1)
        if forwardSlashAfterWebrtc != -1:
            # If there's another / after the /webrtc chunk, this isn't camera streamer.
            return None

        # This is camera-streamer.
        # We want to preserver the URL before the /webrtc, and only replace the /webrtc.
        return streamUrl[:webRtcLocation] + "/stream"


    # A static helper that provides common logic to detect webcam urls missing a directory slash.
    # This works for any url that has the following format: '*webcam*?action=*'
    #
    # This is mostly a problem in Klipper, but if the webcam/?action=stream URL is formatted as 'webcam?action=stream' and the proxy was nginx, it will cause a redirect to 'webcam/?action=stream'.
    # This is ok, but it causes an extra hop before the webcam can show. Also internally this used to break the Snapshot logic, as we didn't follow redirects, so getting
    # a snapshot locally would break. We added the ability for non-proxy http calls to follow redirects, so this is no longer a problem.
    #
    # If the slash is detected to be missing, this function will return the URL with the slash added correctly.
    # Otherwise, it returns None.
    @staticmethod
    def FixMissingSlashInWebcamUrlIfNeeded(logger:logging.Logger, webcamUrl:str) -> str:
        # First, the stream must have webcam* and ?action= in it, otherwise, we don't care.
        streamUrlLower = webcamUrl.lower()
        webcamLocation = streamUrlLower.find("webcam")
        actionLocation = streamUrlLower.find("?action=")
        if webcamLocation == -1 or actionLocation == -1:
            return None

        # Next, we must we need to remember that some urls might be like 'webcam86?action=*', so we have to exclude the number.
        # We know that if we found ?action= there must be a / before the ?
        if actionLocation == 0:
            # This shouldn't happen, but we should check.
            return None
        if streamUrlLower[actionLocation-1] == '/':
            # The URL is good we know that just before ?action= there is a /
            return None

        # We know there is no slash before action, add it.
        newWebcamUrl = webcamUrl[:actionLocation] + "/" + webcamUrl[actionLocation:]
        logger.info(f"Found incorrect webcam url, updating. [{webcamUrl}] -> [{newWebcamUrl}]")
        return newWebcamUrl


    #
    # Plugin Webcam Logic.
    # The default camera is always set and stored as the name, since the camera index can change over time.
    # But it's always gotten as the index of the current list of cameras.
    #
    # The plugin can also have a local list of cameras it will add to the main get cameras result, so that users can
    # setup their own cameras in the plugin settings on the website. This is used for systems like the Bambu, where there's no other UI for settings.
    #

    # Sets the default camera name and writes it to the settings file.
    def SetDefaultCameraName(self, name:str) -> None:
        name = name.lower()
        self.DefaultCameraName = name
        self._SavePluginWebcamSettings()


    # Returns the default camera index. This will always return an int.
    # If there is not a default currently set, this returns the WebcamHelper.c_DefaultWebcamIndex, which is index 0.
    def GetDefaultCameraIndex(self, webcamItemList:List[WebcamSettingItem]) -> int:
        # If there is no name currently, the default is 0.
        if self.DefaultCameraName is None:
            return WebcamHelper.c_DefaultWebcamIndex

        # Try to find the name that was last set.
        count = 0
        for i in webcamItemList:
            if i.Name.lower() == self.DefaultCameraName:
                return count
            count += 1

        # We didn't find it, return the default.
        return WebcamHelper.c_DefaultWebcamIndex


    # Returns a list of any plugin local webcam settings objects.
    # These objects will be merged into the main list of webcams settings objects
    def GetPluginLocalWebcamList(self, returnDisabledItems:bool = False) -> List[WebcamSettingItem]:
        # If there's nothing return or we are returning everything, return the list.
        if len(self.LocalPluginWebcamSettingsObjects) == 0 or returnDisabledItems:
            return self.LocalPluginWebcamSettingsObjects
        # Otherwise return the list of enabled objects.
        ret = []
        for i in self.LocalPluginWebcamSettingsObjects:
            if i.Enabled:
                ret.append(i)
        return ret


    # Sets the local plugin webcam settings objects.
    def SetPluginLocalWebcamList(self, newList:List[WebcamSettingItem]) -> bool:
        # Validate the new list of webcam items.
        for i in newList:
            if i.Validate(self.Logger) is False:
                self.Logger.warn(f"SetPluginLocalWebcamList failed to validate the webcam settings object. {i.Name}")
                return False

        # Set the new list.
        self.LocalPluginWebcamSettingsObjects = newList

        # Save the settings
        return self._SavePluginWebcamSettings()


    # Saves the currently set plugin webcam settings to the settings file.
    def _SavePluginWebcamSettings(self) -> bool:
        try:
            # Convert the local webcam settings objects to dicts
            localWebcamSettingsDict = []
            for i in self.LocalPluginWebcamSettingsObjects:
                localWebcamSettingsDict.append(i.Serialize())

            # Create the settings object
            settings = {
                "DefaultWebcamName" : self.DefaultCameraName,
                "LocalPluginWebcamSettings": localWebcamSettingsDict
            }

            # Save
            with open(self.SettingsFilePath, encoding="utf-8", mode="w") as f:
                f.write(json.dumps(settings))
            return True
        except Exception as e:
            self.Logger.error("SetDefaultCameraName failed "+str(e))
        return False


    # Loads the current name from our settings file.
    def _LoadPluginWebcamSettings(self) -> None:
        try:
            # Default the settings.
            self.DefaultCameraName = None
            self.LocalPluginWebcamSettingsObjects = []

            # First check if there's a file.
            if os.path.exists(self.SettingsFilePath) is False:
                return

            # Try to open it and get the key. Any failure will null out the key.
            with open(self.SettingsFilePath, encoding="utf-8") as f:
                data = json.load(f)

            # Get the default webcam name.
            name:str = data.get("DefaultWebcamName", None)
            if name is not None and len(name) > 0:
                self.DefaultCameraName = name.lower()

            # Set the local plugin webcam setting items.
            items:List[WebcamSettingItem] = data.get("LocalPluginWebcamSettings", None)
            if items is not None and len(items) > 0:
                for i in items:
                    wsi = WebcamSettingItem.Deserialize(i, self.Logger)
                    if wsi is not None:
                        self.LocalPluginWebcamSettingsObjects.append(wsi)

            self.Logger.info(f"Webcam settings loaded. Default camera name: {self.DefaultCameraName}, Local Webcam Settings Items: {len(self.LocalPluginWebcamSettingsObjects)}")
        except Exception as e:
            self.Logger.error("_LoadDefaultCameraName failed "+str(e))
