import logging
import threading
import requests

# A common class to cache http sessions per host.
# This makes the connections more efficient as we can reuse the connections and the session isn't created every time.
class HttpSessions:

    _Instance = None

    @staticmethod
    def Init(logger:logging.Logger):
        HttpSessions._Instance = HttpSessions(logger)


    @staticmethod
    def Get():
        return HttpSessions._Instance


    def __init__(self, logger:logging.Logger):
        self.Logger = logger
        self.Sessions = {}
        self.SessionsLock = threading.Lock()


    # Returns a Session given the url or host.
    # If the url is relative, it can be passed directly.
    # If the url is absolute, the host will be extracted and used.
    @staticmethod
    def GetSession(hostOrUrl:str) -> requests.Session:
        #pylint: disable=protected-access
        return HttpSessions.Get()._GetSession(hostOrUrl)


    def _GetSession(self, hostOrUrl:str) -> requests.Session:
        # Get the root host from what's passed.
        host = ""
        if hostOrUrl.startswith('/'):
            # There's no way to specify a port, so all relative urls are assumed to be on the same host.
            host = "relative"
        else:
            # Extract only the host.
            # Examples can be:
            #   https://127.0.0.1/
            #   http://127.0.0.1
            #   http://test.local:80/path
            #   ws://test.local:80/path
            protocolStart = hostOrUrl.find("://")
            if protocolStart == -1:
                self.Logger.error("Invalid url passed to GetSession: " + hostOrUrl)
                host = "unknown"
            else:
                # Skip past the protocol and find the host end
                protocolStart += 3
                hostEnd = hostOrUrl.find("/", protocolStart)
                if hostEnd == -1:
                    # This means the url is "http://test.local" or "http://test.local:80"
                    hostEnd = len(hostOrUrl)
                host = hostOrUrl[:hostEnd]

        # If one exists, we don't need to lock.
        s = self.Sessions.get(host, None)
        if s is not None:
            return s

        with self.SessionsLock:
            # Check again after locking
            s = self.Sessions.get(host, None)
            if s is not None:
                return s

            # Create a new session.
            self.Logger.info(f"Creating new session for {host}")
            s = requests.Session()

            # We need to be really careful of setting any params, since they will apply to all requests.
            # But, from debugging we found that the requests lib takes time on every call to try to merge env vars for proxies and such.
            # We don't need that, so we can just set it to False. Is saves about 20ms per request.
            s.trust_env = False

            # Set the session and return it!
            self.Sessions[host] = s
            return s
