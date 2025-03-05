import logging
import threading
import time

from octoeverywhere.sentry import Sentry
from octoeverywhere.compat import Compat
from octoeverywhere.octohttprequest import OctoHttpRequest
from octoeverywhere.octohttprequest import PathTypes
from octoeverywhere.WebStream.octoheaderimpl import HeaderHelper
from octoeverywhere.WebStream.octoheaderimpl import BaseProtocol
from octoeverywhere.octostreammsgbuilder import OctoStreamMsgBuilder
from octoeverywhere.compression import Compression, CompressionContext

# This class caches the web relay resources, since some of them can take a bit to load.
class Slipstream:

    # Helpful for debugging slipstream.
    DebugLog = False

    # A const that defines the common cache path for the index.
    # This is a special case for the index, since we ignore query parameters and anchors for the cache lookup logic.
    IndexCachePath = "/"

    # This is the path the index redirects to, so it will be cached as well.
    IndexRedirectCachePath = "/network-device-manager/network/control"

    # The runtime js file has some sub js files that will be loaded.
    JsRuntimeFilePrefix = "runtime."

    # Logic for a static singleton
    _Instance = None

    # This list contains partial paths to anything we want to search for in the index
    # and cache if we can find it. The search will find the query parameter used in the index,
    # so these will be up-to-date caches of files the index depends on.
    OptionalPartialCachePaths = [
        # Common JS files, super important for portal load.
        # These are partials, because they have unique ids for cache busting.
        JsRuntimeFilePrefix,
        "main.",
        "polyfills.",
        "styles.",
        "assets/iconfont/iconfont.css",
        "/assets/i18n/network-en.json"
    ]


    @staticmethod
    def Init(logger:logging.Logger):
        Slipstream._Instance = Slipstream(logger)
        Compat.SetSlipstream(Slipstream._Instance)


    @staticmethod
    def Get():
        return Slipstream._Instance


    def __init__(self, logger:logging.Logger):
        self.Logger = logger

        self.Lock = threading.Lock()
        self.IsRefreshing = False
        self.Cache = {}


    # !!! Interface Function For Slipstream in Compat Layer !!!
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

        # We have our path, check if it's in the map
        with self.Lock:
            if path in self.Cache:
                # Note that this object can be updated!
                # There's only once case right now, there's logic that will compare the cache header and convert the
                # Object into a 304 response, which will strip some headers and the body buffer.
                self._DebugLog("Slipstream returning cached content for "+path)
                return self.Cache[path]

        # Otherwise return cache miss.
        return None


    # !!! Interface Function For Slipstream in Compat Layer !!!
    # Starts a async thread to update the index cache.
    def UpdateCache(self, delayMs=1000):
        try:
            th = threading.Thread(target=self._UpdateCacheThread, args=(delayMs,), name="SlipstreamIndexRefresh")
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

        # We need to body string to process the index.
        indexBodyStr = self._GetDecodedBody(indexResult)
        if indexBodyStr is None:
            return

        # Add the index to the cache so it's ready now.
        with self.Lock:
            self.Cache[Slipstream.IndexCachePath] = indexResult

        # Set the result to None to make sure we don't use it anymore.
        indexResult = None

        # Elegoo also often redirects to a sub page that's the index as well, so cache that too.
        resultRedirectIndex = self._GetCacheReadyOctoHttpResult(Slipstream.IndexRedirectCachePath)
        if resultRedirectIndex is not None:
            # Add it to our cache.
            with self.Lock:
                self.Cache[Slipstream.IndexRedirectCachePath] = resultRedirectIndex

        # Now process the index to see if there's more we should cache.
        # We explicitly look for known files in the index should reference that are large.
        # If we don't find them, no big deal.
        for subPath in Slipstream.OptionalPartialCachePaths:
            # This function will try to find the full url or path in the index body, including the query string.
            fullPath = self.TryToFindFullUrl(indexBodyStr, subPath)

            # No big deal if it can't be found.
            if fullPath is None:
                continue

            # Ensure it starts with a /
            if fullPath.startswith("/") is False:
                fullPath = "/" + fullPath

            # If we find it, try to cache it.
            result = self._GetCacheReadyOctoHttpResult(fullPath)
            if result is None:
                continue

            # Add it to our cache.
            with self.Lock:
                self.Cache[fullPath] = result

        # Finally, try to see if we can find any sub JS files.
        self._TryToFindSubJsFiles()

        size = 0
        with self.Lock:
            size = len(self.Cache)

        self.Logger.info("Slipstream took "+str(time.time()-start)+f" to fully update the cache of {size} files.")


    # On success returns the fully ready OctoHttpResult object.
    # On failure, returns None
    def _GetCacheReadyOctoHttpResult(self, url) -> OctoHttpRequest.Result:
        success = False
        try:
            # Take the starting time.
            start = time.time()

            # Build the headers using the header helper, which will set the common required headers.
            # Notice that we don't give this function the information to set the x-forwarded-for-host header, and thus
            # any hardcoded domains in these cached files will be wrong. However, all of the files we cache don't use the
            # x-forwarded-for-host header, so it doesn't matter. Only the APIs use them to generate the correct links.
            headers = HeaderHelper.GatherRequestHeaders(self.Logger, None, BaseProtocol.Http)

            # Make the call using our helper.
            octoHttpResult = OctoHttpRequest.MakeHttpCall(self.Logger, url, PathTypes.Relative, "GET", headers)

            # Check for success
            if octoHttpResult is None or octoHttpResult.StatusCode != 200:
                self.Logger.error("Slipstream failed to make the http request for "+url)
                return None

            # Remove any headers we don't want.
            contentLength = None
            headers = octoHttpResult.Headers
            for key in headers:
                keyLower = key.lower()
                if keyLower == "content-length":
                    contentLength = int(headers[key])

            # We need to find this to make sure the body read is the correct length.
            if contentLength is None:
                self.Logger.error("Slipstream failed to find content-length header in response for "+url)
                return None

            # Set the cache header
            octoHttpResult.Headers["x-oe-slipstream-plugin"] = "1"

            # Set a short cache on the files, so the browser doesn't have to re-fetch them.
            octoHttpResult.Headers["cache-control"] = "max-age=3600"

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
            ogSize = len(buffer)
            compressStart = time.time()
            compressResult = None
            with CompressionContext(self.Logger) as compressionContext:
                # It's important to set the full compression size, because the compression system will use
                # it to better optimize the compression and know that we will be sending the full data.
                compressionContext.SetTotalCompressedSizeOfData(len(buffer))
                compressResult = Compression.Get().Compress(compressionContext, buffer)
                buffer = compressResult.Bytes

            # Set the buffer into the response so the http request logic can use it.
            octoHttpResult.SetFullBodyBuffer(buffer, compressResult.CompressionType, ogSize)

            requestDuration = compressStart - start
            compressDuration = time.time() - compressStart
            self._DebugLog("Slipstream Cached [request:"+str(format(requestDuration, '.3f'))+", compression:"+str(format(compressDuration, '.3f'))+"] ["+str(ogSize)+"->"+str(len(buffer))+" "+format(((len(buffer)/ogSize)*100), '.3f')+"%] "+url)

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


    def _GetDecodedBody(self, octoHttpResult:OctoHttpRequest.Result):
        # This isn't efficient, but since this is a background thread it's fine.
        # After we add it to the dict, we shouldn't mess with it at all.
        fullBodyBuffer = octoHttpResult.FullBodyBuffer
        if fullBodyBuffer is None:
            self.Logger.error("Slipstream index got successfully but there's no body buffer?")
            return None
        indexBodyBuffer = bytearray()
        indexBodyBuffer[:] = fullBodyBuffer

        # It's no ideal that we need to de-compress this, but it's fine since we are in the background.
        bodyStr = None
        with CompressionContext(self.Logger) as compressionContext:
            # For decompression, we give the pre-compressed size and the compression type. The True indicates this it the only message, so it's all here.
            indexBodyBytes = Compression.Get().Decompress(compressionContext, indexBodyBuffer, octoHttpResult.BodyBufferPreCompressSize, True, octoHttpResult.BodyBufferCompressionType)
            bodyStr = indexBodyBytes.decode(encoding="utf-8")
        return bodyStr


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


    # The runtime js file has some sub js files that will be loaded.
    # We try to find them best effort.
    def _TryToFindSubJsFiles(self):
        # Under lock, try to grab the runtime js file and decode it to text.
        runTimeJs:str = None
        with self.Lock:
            for (k, v) in self.Cache.items():
                if k.find(Slipstream.JsRuntimeFilePrefix) != -1:
                    runTimeJs =  self._GetDecodedBody(v)
                    break

        if runTimeJs is None:
            return

        # This is best effort parsing.
        try:
            # The best way I can find to parse this is to look for ".js", where there is only one of, and then find the {} before it.
            jsIndex = runTimeJs.find("\".js\"")
            if jsIndex == -1:
                return
            # Find the start of the object.
            openBrace = runTimeJs.rfind("{", 0, jsIndex)
            if openBrace == -1:
                return
            # Find the end of the object.
            closeBrace = runTimeJs.find("}", openBrace)
            if closeBrace == -1:
                return
            # Move the open brace to the start of the object.
            openBrace += 1

            # Get the object.
            # Ex 624:"18af8cc177954af2df59",832:"5dc3707b100471f6e9d3"
            subJsObject = runTimeJs[openBrace:closeBrace]

            # Parse the object.
            parts = subJsObject.split(",")
            for part in parts:
                fileParts = part.split(":")
                if len(fileParts) != 2:
                    continue
                # Get the key.
                key = fileParts[0]
                # Get the value.
                value = fileParts[1]
                value = value.replace("\"", "")
                urlPath = f"/{key}.{value}.js"

                # Try to cache it.
                result = self._GetCacheReadyOctoHttpResult(urlPath)
                if result is None:
                    continue
                # Add it to our cache.
                with self.Lock:
                    self.Cache[urlPath] = result

        except Exception as e:
            self._DebugLog("Slipstream failed to parse runtime js file for sub js files. e:"+str(e))


    def _DebugLog(self, msg:str):
        if Slipstream.DebugLog:
            self.Logger.debug(msg)
