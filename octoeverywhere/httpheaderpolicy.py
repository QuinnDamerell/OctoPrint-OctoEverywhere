import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable, List, Mapping, Optional, Set, Tuple

from urllib3 import HTTPHeaderDict


# When an HTTP decision is unclear, follow nginx first and Cloudflare second, favoring transfer size
# and latency. Comment any trade-off where we deliberately differ or give something up.
class HttpHeaderPolicy:
    # Each HTTP leg owns its connection, framing, protocol upgrade, and proxy authentication.
    # Alt-Svc advertises an alternate origin endpoint that would bypass the remote-access relay.
    c_HopByHopHeaders = frozenset((
        "connection", "keep-alive", "proxy-connection", "proxy-authenticate",
        "proxy-authorization", "proxy-authentication-info", "te", "trailer",
        "transfer-encoding", "upgrade", "http2-settings", "alt-svc",
    ))
    c_HeaderNameCharacters = frozenset("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")

    # RFC 9112 section 5.2 lets a proxy either reject a response that folds a header across lines
    # (obs-fold) or replace each fold with a space. nginx rejects it with a 502; we replace it, which
    # keeps legacy origins working at no measurable cost.
    c_ObsFold = re.compile(r"\r?\n[ \t]+")

    # Cloudflare's published compressible types that WebStreamHttpHelper.shouldCompressBody does not
    # already match with its text/, json, xml, javascript, and svg checks. Already-compressed formats
    # (images, video, woff2) are left out because compressing them again only costs CPU.
    c_TunnelCompressibleTypes = frozenset((
        "image/x-icon", "image/vnd.microsoft.icon", "application/x-protobuf", "application/wasm",
        "multipart/bag", "multipart/mixed", "application/x-perl", "application/x-httpd-cgi",
        "font/ttf", "font/otf", "font/x-woff", "application/vnd.ms-fontobject", "application/ttf",
        "application/x-ttf", "application/otf", "application/x-otf", "application/truetype",
        "application/opentype", "application/x-opentype", "application/font-woff", "application/eot",
        "application/font", "application/font-sfnt",
    ))

    @staticmethod
    def GetHopByHopHeaderNames(headers:Iterable[Tuple[str, str]]) -> Set[str]:
        excluded:Set[str] = set(HttpHeaderPolicy.c_HopByHopHeaders)
        # Connection can nominate fields that appear before it, so collect nominations first.
        for name, value in headers:
            if name.lower() == "connection":
                excluded.update(token.strip().lower() for token in value.split(",") if token.strip())
        return excluded

    @staticmethod
    def IsValidHeader(name:str, value:str) -> bool:
        return (bool(name) and all(character in HttpHeaderPolicy.c_HeaderNameCharacters for character in name)
                and all((ord(character) >= 32 or character == "\t") and ord(character) != 127
                        and ord(character) <= 255 for character in value))

    @staticmethod
    def UnfoldHeaderValue(value:str) -> str:
        return HttpHeaderPolicy.c_ObsFold.sub(" ", value)

    @staticmethod
    def HasNoTransform(headers:Iterable[Tuple[str, str]]) -> bool:
        for name, value in headers:
            if name.lower() == "cache-control":
                if any(directive.split("=", 1)[0].strip().lower() == "no-transform" for directive in value.split(",")):
                    return True
        return False

    @staticmethod
    def IsNotModified(method:str, requestHeaders:Mapping[str, str], statusCode:int, responseHeaders:HTTPHeaderDict) -> bool:
        # With proxy_cache enabled, nginx's not_modified filter answers a validated GET/HEAD with 304
        # even when the origin ignored the validators and returned a full 200. It avoids sending the
        # whole body through the tunnel and over the internet to a client that already has it.
        # Deliberate deviation for performance: nginx only converts responses it would cache, skipping
        # Set-Cookie, Vary: *, no-cache/no-store/private, max-age=0, and expired responses. Those rules
        # protect nginx's stored cache, and the relay stores nothing. A validator that matches the fresh
        # 200 is correct whatever its cache directives say; no-cache only demands this revalidation, and
        # the 304 keeps Set-Cookie and cache fields. Applying nginx's rules would mostly cost the case
        # where this helps most: no-cache plus an ETag from an origin that ignores If-None-Match, which
        # browsers revalidate on every load.
        # Callers must not apply this to responses whose body Homeway rewrites: the client's copy may
        # predate the rewrite, so the origin's validators can't vouch for it.
        if statusCode != 200 or method.upper() not in ("GET", "HEAD"):
            return False
        ifNoneMatch:Optional[str] = None
        ifModifiedSince:Optional[str] = None
        for name, value in requestHeaders.items():
            nameLower = name.lower()
            if nameLower == "if-none-match":
                ifNoneMatch = value.strip()
            elif nameLower == "if-modified-since":
                ifModifiedSince = value.strip()
            elif nameLower in ("if-match", "if-unmodified-since"):
                # nginx would answer a failed If-Match or If-Unmodified-Since with its own 412. That
                # saves no meaningful bytes and could reject a request the origin accepted, so the
                # origin's response stands whenever these preconditions are present.
                return False
        if ifNoneMatch is None and ifModifiedSince is None:
            return False
        if "Signature" in responseHeaders or "Signature-Input" in responseHeaders:
            # Our addition (nginx predates RFC 9421): a signature can cover @status or Content-Type,
            # which the 304 changes. Matches the guards on compression and HTML rewriting.
            return False
        # Like nginx, every validator the client sent must match. RFC 9110 ignores If-Modified-Since
        # when If-None-Match is present; nginx is stricter. Trade-off: an origin with a matching ETag
        # but a changed Last-Modified sends the full body, which is always safe.
        if ifModifiedSince is not None and not HttpHeaderPolicy._IsUnmodifiedSince(ifModifiedSince, responseHeaders.getlist("Last-Modified")):
            return False
        if ifNoneMatch is not None and not HttpHeaderPolicy._EntityTagListMatches(ifNoneMatch, responseHeaders.getlist("ETag")):
            return False
        return True

    @staticmethod
    def _IsUnmodifiedSince(ifModifiedSince:str, lastModified:List[str]) -> bool:
        # nginx's default `if_modified_since exact`: only an identical time counts as unmodified.
        # Trade-off: a client date later than Last-Modified gets the full body. Such dates usually
        # come from another source than this response, so trusting them is riskier than resending.
        if len(lastModified) != 1:
            return False
        requested = HttpHeaderPolicy._ParseHttpDate(ifModifiedSince)
        return requested is not None and requested == HttpHeaderPolicy._ParseHttpDate(lastModified[0])

    @staticmethod
    def _ParseHttpDate(value:str) -> Optional[datetime]:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            # Python 3.7-3.9 raise TypeError for unparseable dates; 3.10+ raise ValueError.
            return None
        # HTTP dates are GMT, but the asctime form carries no zone; treat it as UTC as well.
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _EntityTagListMatches(ifNoneMatch:str, etags:List[str]) -> bool:
        # A port of nginx's ngx_http_test_if_match in weak mode: "*" matches any current
        # representation; otherwise each listed tag is compared with W/ removed from both sides.
        if ifNoneMatch == "*":
            return True
        if len(etags) != 1:
            # Repeated ETag fields are ambiguous, so never claim a match against them.
            return False
        etag = etags[0].strip()
        if len(etag) > 2 and etag.startswith("W/"):
            etag = etag[2:]
        end = len(ifNoneMatch)
        start = 0
        while start < end:
            if end - start > 2 and ifNoneMatch.startswith("W/", start):
                start += 2
            if len(etag) > end - start:
                return False
            if ifNoneMatch.startswith(etag, start):
                position = start + len(etag)
                while position < end and ifNoneMatch[position] in " \t":
                    position += 1
                if position == end or ifNoneMatch[position] == ",":
                    return True
            while start < end and ifNoneMatch[start] != ",":
                start += 1
            while start < end and ifNoneMatch[start] in " \t,":
                start += 1
        return False
