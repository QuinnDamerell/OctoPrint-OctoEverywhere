import platform
import logging
from typing import Dict, Optional

from .mdns import MDns
from .buffer import BufferOrNone
from .compat import Compat
from .localip import LocalIpHelper
from .httpresult import HttpResult
from .httpsessions import HttpSessions
from .octostreammsgbuilder import OctoStreamMsgBuilder

from .Proto.PathTypes import PathTypes
from .Proto.HttpInitialContext import HttpInitialContext


class OctoHttpRequest:
    LocalHttpProxyPort = 80
    LocalHttpProxyIsHttps = False
    LocalOctoPrintPort = 5000
    LocalHostAddress = "127.0.0.1"
    DisableHttpRelay = False
    LocalHostUseHttps = False

    @staticmethod
    def SetLocalHttpProxyPort(port:int) -> None:
        OctoHttpRequest.LocalHttpProxyPort = port
    @staticmethod
    def GetLocalHttpProxyPort() -> int:
        return OctoHttpRequest.LocalHttpProxyPort

    @staticmethod
    def SetLocalHttpProxyIsHttps(isHttps:bool) -> None:
        OctoHttpRequest.LocalHttpProxyIsHttps = isHttps
    @staticmethod
    def GetLocalHttpProxyIsHttps() -> bool:
        return OctoHttpRequest.LocalHttpProxyIsHttps

    @staticmethod
    def SetLocalOctoPrintPort(port:int) -> None:
        OctoHttpRequest.LocalOctoPrintPort = port
    @staticmethod
    def GetLocalOctoPrintPort() -> int:
        return OctoHttpRequest.LocalOctoPrintPort

    @staticmethod
    def SetLocalHostAddress(address:str) -> None:
        OctoHttpRequest.LocalHostAddress = address
    @staticmethod
    def GetLocalhostAddress() -> str:
        return OctoHttpRequest.LocalHostAddress

    @staticmethod
    def SetLocalHostUseHttps(address:bool):
        OctoHttpRequest.LocalHostUseHttps = address
    @staticmethod
    def GetLocalHostUseHttps() -> bool:
        return OctoHttpRequest.LocalHostUseHttps

    @staticmethod
    def SetDisableHttpRelay(disableHttpRelay:bool) -> None:
        OctoHttpRequest.DisableHttpRelay = disableHttpRelay
    @staticmethod
    def GetDisableHttpRelay() -> bool:
        return OctoHttpRequest.DisableHttpRelay


    # Based on the URL passed, this will return PathTypes.Relative or PathTypes.Absolute
    @staticmethod
    def GetPathType(url:str) -> int:
        if url.find("://") != -1:
            # If there is a protocol, it's for sure absolute.
            return PathTypes.Absolute
        # TODO - It might be worth to add some logic to try to detect no protocol hostnames, like test.com/helloworld.
        return PathTypes.Relative


    # Handles making all http calls out of the plugin to OctoPrint or other services running locally on the device or
    # even on other devices on the LAN.
    #
    # The main point of this function is to abstract away the logic around relative paths, absolute URLs, and the fallback logic
    # we use for different ports. See the comments in the function for details.
    @staticmethod
    def MakeHttpCallOctoStreamHelper(logger:logging.Logger, httpInitialContext:HttpInitialContext, method:str, headers:Dict[str, str], data:BufferOrNone=None) -> Optional[HttpResult]:
        # Get the vars we need from the octostream initial context.
        path = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Path())
        if path is None:
            raise Exception("Http request has no path field in open message.")
        pathType = httpInitialContext.PathType()

        # Make the common call.
        return OctoHttpRequest.MakeHttpCall(logger, path, pathType, method, headers, data)


    # allowRedirects should be false for all proxy calls. If it's true, then the content returned might be from a redirected URL and the actual URL will be incorrect.
    # Instead, the system needs to handle the redirect 301 or 302 call as normal, sending it back to the caller, and allowing them to follow the redirect if needed.
    # The X-Forwarded-Host header will tell the OctoPrint server the correct place to set the location redirect header.
    # However, for calls that aren't proxy calls, things like local snapshot requests and such, we want to allow redirects to be more robust.
    @staticmethod
    def MakeHttpCall(logger:logging.Logger, pathOrUrl:str, pathOrUrlType:int, method:str, headers:Optional[Dict[str, str]]=None, data:BufferOrNone=None, allowRedirects:bool=False, timeoutSec:Optional[float]=None) -> Optional[HttpResult]:
        # First of all, we need to figure out what the URL is. There are two options
        #
        # 1) Absolute URLs
        # These are the easiest, because we just want to make a request to exactly what the absolute URL is. These are used
        # when the OctoPrint portal is trying to make an local LAN http request to the same device or even a different device.
        # For these to work properly on a remote browser, the OctoEverywhere service will detect and convert the URLs in to encoded relative
        # URLs for the portal. This ensures when the remote browser tries to access the HTTP endpoint, it will hit OctoEverywhere. The OctoEverywhere
        # server detects the special relative URL, decodes the absolute URL, and sends that in the OctoMessage as "AbsUrl". For these URLs we just try
        # to hit them and we take whatever we get, we don't care if fails or not.
        #
        # 2) Relative Urls
        # These Urls are the most common, standard URLs. The browser makes the relative requests to the same hostname:port as it's currently
        # on. However, for our setup its a little more complex. The issue is the OctoEverywhere plugin not knowing how the user's system is setup.
        # The plugin can with 100% certainty query and know the port OctoPrint's http server is running on directly. So we do that to know exactly what
        # OctoPrint server to talk to. (consider there might be multiple instances running on one device.)
        #
        # But, the other most common use case for http calls are the webcam streams to mjpegstreamer. This is the tricky part. There are two ways it can be
        # setup. 1) the webcam stream uses an absolute local LAN url with the ip and port. This is covered by the absolute URL system above. 2) The webcam stream
        # uses a relative URL and haproxy handles detecting the webcam path to send it to the proper mjpegstreamer instance. This is the tricky one, because we can't
        # directly query or know what the correct port for haproxy or mjpegstreamer is. We could look at the configs, but a user might not setup the configs in the
        # standard places. So to fix the issue, we use logic in the frontend JS to determine if a web browser is connecting locally, and if so what the port is. That gives
        # use a reliable way to know what port haproxy is running on. It sends that to the plugin, which is then given here as `localHttpProxyPort`.
        #
        # The last problem is knowing which calls should be sent to OctoPrint directly and which should be sent to haproxy. We can't rely on any URL matching, because
        # the user can setup the webcam stream to start with anything they want. So the method we use right now is to simply always request to OctoPrint first, and if we
        # get a 404 back try the haproxy. This adds a little bit of unneeded overhead, but it works really well to cover all of the cases.

        # Setup the protocol we need to use for the http proxy. We need to use the same protocol that was detected.
        localServiceProtocol = "http://"
        if OctoHttpRequest.LocalHostUseHttps:
            localServiceProtocol = "https://"
        httpProxyProtocol = "http://"
        if OctoHttpRequest.LocalHttpProxyIsHttps:
            httpProxyProtocol = "https://"

        # Figure out the main and fallback url.
        url = ""
        fallbackUrl:Optional[str] = None
        fallbackWebcamUrl:Optional[str] = None
        fallbackLocalIpDirectServicePortSuffix:Optional[str] = None
        fallbackLocalIpHttpProxySuffix:Optional[str] = None
        if pathOrUrlType == PathTypes.Relative:

            # Note!
            # These URLs are very closely related to the logic in the OctoWebStreamWsHelper class and should stay in sync!

            # Fluidd seems to have a bug where the default webcam streaming value is .../webcam?action...
            # but crowsnest will send a redirect to .../webcam/?action...
            # To prevent that redirect hop every time the camera is loaded, we will try to correct it.
            # We should remove this eventually when the bug has been fixed for long enough.
            if pathOrUrl.startswith("/webcam?action"):
                pathOrUrl = pathOrUrl.replace("/webcam?action", "/webcam/?action")

            # The main URL is directly to this OctoPrint instance
            # This URL will only every be http, it can't be https.
            url = localServiceProtocol + OctoHttpRequest.LocalHostAddress + ":" + str(OctoHttpRequest.LocalOctoPrintPort) + pathOrUrl

            # The fallback URL is to where we think the http proxy port is.
            # For this address, we need set the protocol correctly depending if the client detected https
            # or not.
            fallbackUrl = httpProxyProtocol + OctoHttpRequest.LocalHostAddress + ":" +str(OctoHttpRequest.LocalHttpProxyPort) + pathOrUrl

            # Special case for systems with an API router (only moonraker as of now)
            # If the API router wants to redirect the URL, it must be tried first, since the default URL
            # might also work, but might be incorrect.
            apiRouteHandler = Compat.GetApiRouterHandler()
            if apiRouteHandler is not None:
                reroutedUrl = apiRouteHandler.MapRelativePathToAbsolutePathIfNeeded(pathOrUrl, "http://")
                if reroutedUrl is not None:
                    # If we got a redirect URL, make sure it's the first URL, and use the default URL as the fallback.
                    fallbackUrl = url
                    url = reroutedUrl

            # If the two URLs above don't work, we will try to call the server using the local IP since the server might not be bound to localhost.
            # Note we only build the suffix part of the string here, because we don't want to do the local IP detection if we don't have to.
            # Also note this will only work for OctoPrint pages.
            # This case only seems to apply to OctoPrint instances running on Windows.
            fallbackLocalIpDirectServicePortSuffix = ":" + str(OctoHttpRequest.LocalOctoPrintPort) + pathOrUrl
            fallbackLocalIpHttpProxySuffix =  ":" + str(OctoHttpRequest.LocalHttpProxyPort) + pathOrUrl

            # If all else fails, and because this logic isn't perfect, yet, we will also try to fallback to the assumed webcam port.
            # This isn't a great thing though, because more complex webcam setups use different ports and more than one instance.
            # Only setup this URL if the path starts with /webcam, which again isn't a great indicator because it can change per user.
            webcamUrlIndicator = "/webcam"
            pathLower = pathOrUrl.lower()
            if pathLower.startswith(webcamUrlIndicator):
                # We need to remove the /webcam* since we are trying to talk directly to mjpg-streamer
                # We do want to keep the second / though.
                secondSlash = pathOrUrl.find("/", 1)
                if secondSlash != -1:
                    webcamPath = pathOrUrl[secondSlash:]
                    fallbackWebcamUrl = "http://" + OctoHttpRequest.LocalHostAddress + ":8080" + webcamPath

        elif pathOrUrlType == PathTypes.Absolute:
            # For absolute URLs, only use the main URL and set it be exactly what was requested.
            url = pathOrUrl

            # The only exception to this is for mdns local domains. So here's the hard part. On most systems, mdns works for the
            # requests lib and everything will work. However, on some systems mDNS isn't support and the call will fail. On top of that, mDNS
            # is super flakey, and it will randomly stop working often. For both of those reasons, we will check if we find a local address, and try
            # to resolve it manually. Our logic has a cache and local disk backup, so if mDNS is being flakey, our logic will recover it.
            localResolvedUrl = MDns.Get().TryToResolveIfLocalHostnameFound(url)
            if localResolvedUrl is not None:
                # The function will only return back the full URL if a local hostname was found and it was able to resolve to an IP.
                # In this case, use our local IP result first, and then set the requested as the fallback.
                # This should be better, because it will use our already resolved IP url first, and if for some reason it fails, we still try the
                # OG URL.
                fallbackUrl = url
                url = localResolvedUrl
        else:
            raise Exception("Http request got a message with an unknown path type. "+str(pathOrUrlType))

        # Ensure if there's no data we don't set it. Sometimes our json message parsing will leave an empty
        # bytearray where it should be None.
        if data is not None and len(data) == 0:
            data = None

        # All of the users of MakeHttpCall don't handle compressed responses.
        # For OctoStream request, this header is already set in GatherRequestHeaders, but for things like webcam snapshot requests and such, it's not set.
        # Beyond nothing handling compressed responses, since the call is almost always over localhost, there's no point in doing compression, since it mainly just helps in transmit less data.
        # Thus, for all calls, we set the Accept-Encoding to identity, telling the server no response compression is allowed.
        # This is important for somethings like camera-streamer, which will use gzip by default. (which is also silly, because it's sending jpegs and jmpeg streams?)
        if headers is None:
            headers = {}
        headers["Accept-Encoding"] = "identity"

        # First, try the main URL.
        # For the first main url, we set the main response to None and is fallback to False.
        ret = OctoHttpRequest.MakeHttpCallAttempt(logger, "Main request", method, url, headers, data, None, False, fallbackUrl, allowRedirects, timeoutSec=timeoutSec)
        # If the function reports the chain is done, the next fallback URL is invalid and we should always return
        # whatever is in the Response, even if it's None.
        if ret.IsChainDone:
            return ret.Result

        # We should have a fallback url if we are here.
        if fallbackUrl is None:
            logger.error("Main request failed and no fallback URL was provided. This is a critical error and should be reported to the OctoEverywhere team.")
            return ret.Result

        # We keep track of the main response, if all future fallbacks fail. (This can be None)
        mainResult = ret.Result

        # Main failed, try the fallback, which should be the http proxy.
        ret = OctoHttpRequest.MakeHttpCallAttempt(logger, "Http proxy fallback", method, fallbackUrl, headers, data, mainResult, True, fallbackLocalIpHttpProxySuffix, allowRedirects, timeoutSec=timeoutSec)
        # If the function reports the chain is done, the next fallback URL is invalid and we should always return
        # whatever is in the Response, even if it's None.
        if ret.IsChainDone:
            return ret.Result

        # Try to get the local IP of this device and try to use the same ports with it.
        # We build these full URLs after the failures so we don't try to get the local IP on every call.
        localIp = LocalIpHelper.TryToGetLocalIpOfConnectionTarget()

        # We should have a fallbackLocalIpHttpProxySuffix if we are here.
        if fallbackLocalIpHttpProxySuffix is None:
            logger.error("Main request failed and no fallbackLocalIpHttpProxySuffix was provided. This is a critical error and should be reported to the OctoEverywhere team.")
            return ret.Result

        # With the local IP, first try to use the http proxy URL, since it's the most likely to be bound to the public IP and not firewalled.
        # It's important we use the right http proxy protocol with the http proxy port.
        localIpFallbackUrl = httpProxyProtocol + localIp + fallbackLocalIpHttpProxySuffix
        ret = OctoHttpRequest.MakeHttpCallAttempt(logger, "Local IP Http Proxy Fallback", method, localIpFallbackUrl, headers, data, mainResult, True, fallbackLocalIpDirectServicePortSuffix, allowRedirects, timeoutSec=timeoutSec)
        # If the function reports the chain is done, the next fallback URL is invalid and we should always return
        # whatever is in the Response, even if it's None.
        if ret.IsChainDone:
            return ret.Result

        # We should have a fallbackLocalIpHttpProxySuffix if we are here.
        if fallbackLocalIpDirectServicePortSuffix is None:
            logger.error("Main request failed and no fallbackLocalIpOctoPrintPortSuffix was provided. This is a critical error and should be reported to the OctoEverywhere team.")
            return ret.Result

        # Now try the OcotoPrint direct port with the local IP.
        localIpFallbackUrl = "http://" + localIp + fallbackLocalIpDirectServicePortSuffix
        ret = OctoHttpRequest.MakeHttpCallAttempt(logger, "Local IP fallback", method, localIpFallbackUrl, headers, data, mainResult, True, fallbackWebcamUrl, allowRedirects, timeoutSec=timeoutSec)
        # If the function reports the chain is done, the next fallback URL is invalid and we should always return
        # whatever is in the Response, even if it's None.
        if ret.IsChainDone:
            return ret.Result

        # We should have a fallbackWebcamUrl if we are here.
        if fallbackWebcamUrl is None:
            logger.error("Main request failed and no fallbackWebcamUrl was provided. This is a critical error and should be reported to the OctoEverywhere team.")
            return ret.Result

        # If all others fail, try the hardcoded webcam URL.
        # Note this has to be last, because there commonly isn't a fallbackWebcamUrl, so it will stop the
        # chain of other attempts.
        ret = OctoHttpRequest.MakeHttpCallAttempt(logger, "Webcam hardcode fallback", method, fallbackWebcamUrl, headers, data, mainResult, True, None, allowRedirects, timeoutSec=timeoutSec)

        # No matter what, always return the result now.
        return ret.Result


    # Returned by a single http request attempt.
    # IsChainDone - indicates if the fallback chain is done and the response should be returned
    # Result - is the final result. Note the result can be unsuccessful or even `None` if everything failed.
    class AttemptResult():
        def __init__(self, isChainDone:bool, result:Optional[HttpResult]):
            self.isChainDone = isChainDone
            self.result = result

        @property
        def IsChainDone(self) -> bool:
            return self.isChainDone

        @property
        def Result(self) -> Optional[HttpResult]:
            return self.result


    # This function should always return a AttemptResult object.
    @staticmethod
    def MakeHttpCallAttempt(logger:logging.Logger, attemptName:str, method:str, url:str, headers:Optional[Dict[str,str]], data:BufferOrNone, mainResult:Optional[HttpResult], isFallback:bool, nextFallbackUrl:Optional[str], allowRedirects:bool=False, timeoutSec:Optional[float]=None) -> AttemptResult:
        # The requests lib can accept any "byte like" object. We use this to force the type to be bytes, so pyright is happy.
        dataBuffer:Optional[bytes] = None if data is None else data.GetBytesLike() #pyright: ignore[reportAssignmentType]

        response = None
        try:
            # Try to make the http call.
            #
            # Note we use a long timeout because some api calls can hang for a while.
            # For example when plugins are installed, some have to compile which can take some time.
            # timeout note! This value also effects how long a body read can be. This can effect unknown body chunk stream reads can hang while waiting on a chunk.
            # But whatever this timeout value is will be the max time a body read can take, and then the chunk will fail and the stream will close.
            timeoutSec = 1800 if timeoutSec is None else timeoutSec

            # See the note about allowRedirects above MakeHttpCall.
            #
            # It's important to set the `verify` = False, since if the server is using SSL it's probably a self-signed cert.
            #
            # We always set stream=True because we use the iter_content function to read the content.
            # This means that response.content will not be valid and we will always use the iter_content. But it also means
            # iter_content will ready into memory on demand and throw when the stream is consumed. This is important, because
            # our logic relies on the exception when the stream is consumed to end the http response stream.
            response = HttpSessions.GetSession(url).request(method, url, headers=headers, data=dataBuffer, timeout=timeoutSec, allow_redirects=allowRedirects, stream=True, verify=False)
        except Exception as e:
            logger.debug("%s http URL threw an exception: %s", attemptName, e)

        # We have seen when making absolute calls to some lower end devices, like external IP cameras, they can't handle the number of headers we send.
        # So if any call fails due to 431 (headers too long) we will retry the call with no headers at all. Note this will break most auth, but
        # most of these systems don't need auth headers or anything.
        # Strangely this seems to only work on Linux, where as on Windows the request.request function will throw a 'An existing connection was forcibly closed by the remote host' error.
        # Thus for windows, if the response is ever null, try again. This isn't ideal, but most windows users are just doing dev anyways.
        if response is not None and response.status_code == 431 or (platform.system() == "Windows" and response is None):
            if response is not None and response.status_code == 431:
                logger.info(url + " http call returned 431, too many headers. Trying again with no headers.")
            else:
                logger.warning(url + " http call returned no response on Windows. Trying again with no headers.")
            try:
                response = HttpSessions.GetSession(url).request(method, url, headers={}, data=dataBuffer, timeout=1800, allow_redirects=False, stream=True, verify=False)
            except Exception as e:
                logger.info(attemptName + " http NO HEADERS URL threw an exception: "+str(e))

        # Check if we got a valid response.
        if response is not None and response.status_code != 404:
            # We got a valid response, we are done.
            # Return true and the result object, so it can be returned.
            return OctoHttpRequest.AttemptResult(True, HttpResult.BuildFromRequestLibResponse(response, url, isFallback))

        # Check if we have another fallback URL to try.
        if nextFallbackUrl is not None:
            # We have more fallbacks to try.
            # Return false so we keep going, but also return this response if we had one. This lets
            # use capture the main result object, so we can use it eventually if all fallbacks fail.
            if response is None:
                return OctoHttpRequest.AttemptResult(False, None)
            return OctoHttpRequest.AttemptResult(False, HttpResult.BuildFromRequestLibResponse(response, url, isFallback))

        # We don't have another fallback, so we need to end this.
        if mainResult is not None:
            # If we got something back from the main try, always return it (we should only get here on a 404)
            logger.info(attemptName + " failed and we have no more fallbacks. Returning the main URL response.")
            return OctoHttpRequest.AttemptResult(True, mainResult)
        else:
            if response is not None:
                logger.debug("%s failed and we have no more fallbacks. We DON'T have a main response.", attemptName)
                return OctoHttpRequest.AttemptResult(True, HttpResult.BuildFromRequestLibResponse(response, url, isFallback))

            # Otherwise return the failure.
            logger.debug("%s failed and we have no more fallbacks. We DON'T have a main response.", attemptName)
            return OctoHttpRequest.AttemptResult(True, None)
