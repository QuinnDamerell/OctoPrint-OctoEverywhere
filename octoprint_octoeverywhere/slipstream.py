import threading
import time
import zlib

from octoeverywhere.sentry import Sentry
from octoeverywhere.compat import Compat
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.octohttprequest import PathTypes
from octoeverywhere.WebStream.octoheaderimpl import HeaderHelper
from octoeverywhere.WebStream.octoheaderimpl import BaseProtocol
from octoeverywhere.octostreammsgbuilder import OctoStreamMsgBuilder

from .localauth import LocalAuth

# From our telemetry, we can see that the initial index request is a big factor in the portal's load time.
# Since the index has dynamic plugin content, it can't be cached on the client. Until the client can load the index, it's the single
# bottleneck of the load time. It takes OctoPrint 500ms to fulfill the index http request on a good day, but averages more around 1.5-2.0s!!
# To solve this, we will try to make an early call to get the index and cache it in memory. The on the next request for the index we will return
# it and make a new request for the index. This should work well, because the only time the index should change is between boots, which we will for sure
# have to re-pull the cache.
#
# Note that the index caching here only works because of the login check js we include in our plugin's static js file. Since the call to the index will
# validate the session and redirect to the login page if it's not valid, our cache won't do that. So our little js script will load in and do that check for us.
#
# We also pre-compress the response, so we can use a better compression quality and we don't have to compress it in realtime.
#
# This class has also been expanded to cache static resources required by the index that are large.
class Slipstream:
    # A const that defines the common cache path for the index.
    # This is a special case for the index, since we ignore query parameters and anchors for the cache lookup logic.
    IndexCachePath = "/"

    # This list contains partial paths to anything we want to search for in the index
    # and cache if we can find it. The search will find the query parameter used in the index,
    # so these will be up-to-date caches of files the index depends on.
    #
    # This list is made of things that are large but also things that the UI depends upon to load.
    # We want to cache them to make the portal load as fast as possible, but we don't want to cache everything.
    #
    # Note that none of these files can depend on the x-forwarded-for-host header to be set with the correct domain,
    # because right now it's not set at all. As of now, only the OctoPrint APIs depend on the x-forwarded-for-host header
    # to set some absolute URLs correctly.
    OptionalPartialCachePaths = [
        # Common JS files, super important for portal load.
        "static/webassets/packed_libs.js",
        "static/webassets/packed_client.js",
        "static/webassets/packed_plugins.js",
        "static/webassets/packed_core.js",

        # Common css files, very important as well.
        "static/webassets/packed_libs.css",
        "static/webassets/packed_plugins.css",
        "static/webassets/packed_core.css",

        # Common fonts, somewhat important
        "static/vendor/font-awesome-5.15.1/webfonts/fa-brands-400.woff2",
        "static/vendor/font-awesome-5.15.1/webfonts/fa-brands-400.woff",
        "static/vendor/font-awesome-5.15.1/webfonts/fa-regular-400.woff2",
        "static/vendor/font-awesome-5.15.1/webfonts/fa-regular-400.woff",
        "static/vendor/font-awesome-5.15.1/webfonts/fa-solid-900.woff2",
        "static/vendor/font-awesome-5.15.1/webfonts/fa-solid-900.woff",
        "static/vendor/font-awesome-3.2.1/fonts/fontawesome-webfont.woff",
    ]


    # Logic for a static singleton
    _Instance = None


    @staticmethod
    def Init(logger):
        Slipstream._Instance = Slipstream(logger)
        # Since OctoPrint supports this, add it to our compat layer
        Compat.SetSlipstream(Slipstream._Instance)


    @staticmethod
    def Get():
        return Slipstream._Instance


    def __init__(self, logger):
        self.Logger = logger

        self.Lock = threading.Lock()
        self.IsRefreshing = False
        self.Cache = {}

        # Kick off a thread to grab the initial index, no delay we build the cache ASAP.
        # Note on server boot this index cache call can take a long time (25-30s)
        self.UpdateCache(0)


    # If available for the given URL, this will returned the cached and ready to go OctoHttpResult.
    # Otherwise returns None
    def GetCachedOctoHttpResult(self, httpInitialContext):
        # Note that all of our URL caching logic is case sensitive! (because URLs are)

        # Get the path.
        # If the path is empty, it's a protocol error. The upstream will handle it.
        path = OctoStreamMsgBuilder.BytesToString(httpInitialContext.Path())
        if path is None:
            return None

        # For now, only handle relative requests. (I don't think an absolute would ever be used.)
        if httpInitialContext.PathType() != PathTypes.Relative:
            return None

        # Remove any anchors.
        posOfHashtag = path.find('#')
        if posOfHashtag != -1:
            path = path[:posOfHashtag]

        # Special case for the index.
        # If the path is only / OR the second char is a ?, this must be the index.
        # For the index only we ignore query params.
        posOfQuestionMark = path.find('?')
        if path == "/" or posOfQuestionMark == 1:
            # Set the path to our well known index cache key.
            path = Slipstream.IndexCachePath
            posOfQuestionMark = -1

            # Each time the index is requested, kick off a thread to refresh the cache.
            # We request a 20s delay so that the rest of the portal load isn't effected by the cache refreshing.
            self.UpdateCache(20000)

            # Special case for the index page.
            # Normally the call to the index is responsible for redirecting the user to the login screen if the calling
            # user don't have permissions to access the OctoPrint settings, aka, they aren't logged in. Since we cache the index
            # that logic doesn't get applied. We have logic in our static JS (which is part of the index) that will check for a not logged in user
            # and redirect them, but it's not ideal and make the first login page load take longer.
            # For that reason, we will quickly check to see if there's any OctoPrint session cookie present. If not, we know the user  isn't logged in
            # and then we WONT use the cache so the redirect happens as normal.
            # Note, this doesn't (and can't) validate if the session cookie represents a signed in user, it just can detect if there is no cookie, like on the
            # very first user visit to a printer subdomain.
            if self.HasOctoPrintSessionCookie(httpInitialContext) is False:
                self.Logger.info("Slipstream got an index request but there's no OctoPrint session cookie found, so we aren't returning a cached index.")
                return None

        # We have our path, check if it's in the map
        with self.Lock:
            if path in self.Cache:
                self.Logger.debug("Slipstream returning cached content for "+path)
                return self.Cache[path]

        # Otherwise return cache miss.
        return None


    # Starts a async thread to update the index cache.
    def UpdateCache(self, delayMs):
        try:
            th = threading.Thread(target=self._UpdateCacheThread, args=(delayMs,))
            th.start()
        except Exception as e:
            Sentry.Exception("Slipstream failed to start index refresh thread. ", e)


    # Does the index update cache work.
    def _UpdateCacheThread(self, delayMs):
        try:
            # We only need one refresh running at once.
            with self.Lock:
                if self.IsRefreshing:
                    self.Logger.info("Slipstream ignoring refresh request, a refresh is already running.")
                    return
                # Important! We must clear this!
                self.IsRefreshing = True

            # If we want to delay this update, do that delay now.
            # This is useful to defer the index refresh until after a portal load, to reduce noise.
            if delayMs > 0:
                time.sleep(delayMs/1000)

            # Do work
            self._GetAndProcessIndex()

        except Exception as e:
            Sentry.Exception("Slipstream failed to update cache.", e)
        finally:
            with self.Lock:
                # It's important we always clear this flag.
                self.IsRefreshing = False


    def _GetAndProcessIndex(self):
        start = time.time()

        # Start by trying to get the index.
        indexResult = self._GetCacheReadyOctoHttpResult(Slipstream.IndexCachePath)

        # On failure leave.
        if indexResult is None:
            return

        # On success, copy the index's body buffer.
        # This isn't efficient, but since this is a background thread it's fine.
        # After we add it to the dict, we shouldn't mess with it at all.
        fullBodyBuffer = indexResult.FullBodyBuffer
        if fullBodyBuffer is None:
            self.Logger.error("Slipstream index got successfully but there's no body buffer?")
            return
        indexBodyBuffer = bytearray()
        indexBodyBuffer[:] = fullBodyBuffer

        # Add the index to the cache so it's ready now.
        with self.Lock:
            self.Cache[Slipstream.IndexCachePath] = indexResult

        # Set the result to None to make sure we don't use it anymore.
        indexResult = None

        # Now process the index to see if there's more we should cache.
        # We explicitly look for known files in the index should reference that are large.
        # If we don't find them, no big deal.
        # It's no ideal that we need to de-compress this, but it's fine since we are in the background.
        # PY2 zlib.decompress can't accept a bytearray, so we must convert them before compressing.
        # This isn't ideal, but not a big deal since this is in the background.
        indexBodyStr = zlib.decompress(indexBodyBuffer).decode(errors="ignore")
        for subPath in Slipstream.OptionalPartialCachePaths:
            # This function will try to find the full url or path in the index body, including the query string.
            fullPath = self.TryToFindFullUrl(indexBodyStr, subPath)

            # No big deal if it can't be found.
            if fullPath is None:
                continue

            # If we find it, try to cache it.
            result = self._GetCacheReadyOctoHttpResult(fullPath)
            if result is None:
                continue

            # Add it to our cache.
            with self.Lock:
                self.Cache[fullPath] = result

        self.Logger.info("Slipstream took "+str(time.time()-start)+" to fully update the cache")


    # On success returns the fully ready OctoHttpResult object.
    # On failure, returns None
    def _GetCacheReadyOctoHttpResult(self, url):
        success = False
        try:
            # Take the starting time.
            start = time.time()

            # Build the headers using the header helper, which will set the common required headers.
            # Notice that we don't give this function the information to set the x-forwarded-for-host header, and thus
            # any hardcoded domains in these cached files will be wrong. However, all of the files we cache don't use the
            # x-forwarded-for-host header, so it doesn't matter. Only the APIs use them to generate the correct links.
            headers = HeaderHelper.GatherRequestHeaders(self.Logger, None, BaseProtocol.Http)

            # We need to use the local auth helper to add a auth header to the call so it doesn't fail due to unauthed.
            LocalAuth.Get().AddAuthHeader(headers)

            # Make the call using our helper.
            octoHttpResult = OctoHttpRequest.MakeHttpCall(self.Logger, url, PathTypes.Relative, "GET", headers)

            # Check for success
            if octoHttpResult is None or octoHttpResult.StatusCode != 200:
                self.Logger.error("Slipstream failed to make the http request for "+url)
                return None

            # Remove any headers we don't want.
            setCookieKey = None
            contentLength = None
            headers = octoHttpResult.Headers
            for key in headers:
                keyLower = key.lower()
                if keyLower == "content-length":
                    contentLength = int(headers[key])
                elif keyLower == "set-cookie":
                    setCookieKey = key

            # We need to find this to make sure the body read is the correct length.
            if contentLength is None:
                self.Logger.error("Slipstream failed to find content-length header in response for "+url)
                return None

            # We remove Set-Cookie so no session gets applied from this cached item.
            if setCookieKey is not None:
                del octoHttpResult.Headers[setCookieKey]

            # Set the cache header
            octoHttpResult.Headers["x-oe-slipstream-plugin"] = "1"

            # Since the request is setup to use streaming mode, we need to read in the entire contents and store them
            # because when the function exist the request will be closed and the stream will no longer be able to be read.
            # This is actually a good idea, so the request connection doesn't hang around for a long time.
            buffer = None
            try:
                octoHttpResult.ReadAllContentFromStreamResponse(self.Logger)
                buffer = octoHttpResult.FullBodyBuffer
            except Exception as e:
                self.Logger.error("Slipstream failed to read index buffer for "+url+", e:"+str(e))
                return None

            # Since we are using the FullBodyBuffer, the content length header must exactly match the actual buffer size before compression.
            if len(buffer) != contentLength:
                self.Logger.error("Slipstream read a a body of different size then the content length. url:"+url+" body:"+str(len(buffer))+" cl:"+str(contentLength))
                return None

            # Do the compression.
            # See the compression chat in the main http stream class for tradeoffs about complexity.
            ogSize = len(buffer)
            compressStart = time.time()
            buffer = zlib.compress(buffer, 7)

            # Set the buffer into the response so the http request logic can use it.
            octoHttpResult.SetFullBodyBuffer(buffer, True, ogSize)

            requestDuration = compressStart - start
            compressDuration = time.time() - compressStart
            self.Logger.debug("Slipstream Cached [request:"+str(format(requestDuration, '.3f'))+", compression:"+str(format(compressDuration, '.3f'))+"] ["+str(ogSize)+"->"+str(len(buffer))+" "+format(((len(buffer)/ogSize)*100), '.3f')+"%] "+url)

            # Return the result on success.
            success = True
            return octoHttpResult

        except Exception as e:
            self.Logger.error("Slipstream failed to cache url. url:"+url+" error:"+str(e))
        finally:
            # On all exits, if not successful, remove this entry from the cache so it doesn't get stale.
            if success is False:
                self.RemoveCacheIfExists(url)
        return None


    def RemoveCacheIfExists(self, url):
        with self.Lock:
            if url in self.Cache:
                del self.Cache[url]


    # Given the full index body and some fragment of a URL, this will try to find the entire URL and return it.
    # Returns None on failure.
    def TryToFindFullUrl(self, indexBody, urlFragment):
        # To start, try to find any of it.
        start = indexBody.find(urlFragment)
        if start == -1:
            return None
        # Now we know the URl must be enclosed in " ", so look for them.
        endQuote = indexBody.find('"', start)
        if endQuote == -1:
            return None
        openQuote = indexBody.rfind('"', 0, start)
        if openQuote == -1:
            return None

        # Exclude the quote from the substr
        openQuote += 1

        urlLen = endQuote - openQuote
        if urlLen < 0 or urlLen > 500:
            return None

        # Return the URL
        return indexBody[openQuote:endQuote]


    # Returns True if a OctoPrint session cookie has been found, otherwise False.
    def HasOctoPrintSessionCookie(self, httpInitialContext):
        if httpInitialContext is None:
            self.Logger.info("Slipstream looking for OctoPrint session was called with no httpInitialContext.")
            return False

        # Go through all of the headers cooking for a cookie header.
        headersLen = httpInitialContext.HeadersLength()
        i = 0
        while i < headersLen:
            # Get the header
            header = httpInitialContext.Headers(i)
            i += 1

            # Get the header key value.
            name = OctoStreamMsgBuilder.BytesToString(header.Key())
            if name is None:
                continue

            nameLower = name.lower()
            if nameLower == "cookie":
                # Get the cookie value
                value = OctoStreamMsgBuilder.BytesToString(header.Value())
                if value is None:
                    self.Logger.info("Slipstream looking for OctoPrint session cookie found a cookie key with no value.")
                    return False

                # Now that we have found the cookie header, check if the session_p443 value exists.
                # The session_P443 value is the session cookie that the toradio server used in OctoPrint sets.
                valueLower = value.lower()
                if "session_p443" in valueLower:
                    return True
                return False

        # No cookie header found
        return False
