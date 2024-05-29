import time
import logging

import dns.resolver

# Created to the DNS resolution of our URLS when the websocket claims it can't connect due to DNS issues.
class DnsTest:

    def __init__(self, logger: logging.Logger) -> None:
        self.Logger = logger


    def RunTestSync(self) -> None:
        try:
            self.Logger.info("DNS test starting.")
            # Try to get the cname, which should be octoeverywhere-v1.trafficmanager.net
            self._TestUrl("starport-v1.octoeverywhere.com", "CNAME")
            # Try to resolve the octoeverywhere-v1.trafficmanager.net, which should resolve to a cluster hostname.
            self._TestUrl("octoeverywhere-v1.trafficmanager.net", "CNAME")
            # Try to resolve the octoeverywhere-v1.trafficmanager.net, which should resolve to a cluster ip.
            self._TestUrl("octoeverywhere-v1.trafficmanager.net", "A")
            # This should do the same as above and resolve the IP.
            self._TestUrl("starport-v1.octoeverywhere.com", "A")

            # For fun, also do the root and some others.
            self._TestUrl("octoeverywhere.com", "CNAME")
            self._TestUrl("octoeverywhere.com", "A")
            self._TestUrl("printer-events-v1-oeapi.octoeverywhere.com")
            self._TestUrl("gadget-v1-oeapi.octoeverywhere.com")

            # Also, run some DNS names we expect to be valid.
            self._TestUrl("google.com", "A")
            self._TestUrl("bing.com", "A")
            self.Logger.info("DNS test done.")
        except Exception as e:
            self.Logger.error(f"RunTestSync test failed. {e}")


    def _TestUrl(self, url: str, recordType:str = "A") -> None:
        try:
            self.Logger.debug(f"Starting DNS resolve test for {url} with record type {recordType}")
            startSec = time.time()
            dnsResolver = dns.resolver.Resolver()
            dnsResolver.timeout = 5.0 # Timeout in seconds.
            result = dnsResolver.query(url, recordType)
            resolveTimeSec = time.time() - startSec
            c = 0
            for r in result:
                c += 1
                self.Logger.info(f"[{c}/{len(result)}] Resolved {url}:{recordType} to {r} in {resolveTimeSec:.3f} seconds.")
            if len(result) == 0:
                self.Logger.info(f"[?/?] FAILED TO RESOLVE {url}:{recordType} in {resolveTimeSec:.3f} seconds - no result returned.")
        except Exception as e:
            self.Logger.info(f"TestUrl test failed. Url: {url}, Error: {e}")
