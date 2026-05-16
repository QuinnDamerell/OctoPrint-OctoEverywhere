import logging
import time
from typing import Any, Callable, Dict, Optional

from octoeverywhere.mqttwebsocketproxy import (
    IMqttWebsocketProxyConnector,
    MqttConnectionContext,
    MqttWebsocketProxyProviderBuilder as CommonMqttWebsocketProxyProviderBuilder,
)

from .bambuclient import BambuClient, ConnectionContext


class BambuMqttWebsocketProxyConnector(IMqttWebsocketProxyConnector):

    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger


    def GetConnectionContext(self, args:Optional[Dict[str, Any]], isClosed:Callable[[], bool]) -> Optional[MqttConnectionContext]:
        # Wait for a connection context. After the first successful connection, it will be set and then will always exist.
        connectionContext:Optional[ConnectionContext] = None
        attempt = 0
        while isClosed() is False:
            attempt += 1
            if attempt > 10:
                raise Exception("Bambu MQTT proxy timed out waiting for a connection context.")
            connectionContext = BambuClient.Get().GetCurrentConnectionContext()
            if connectionContext is not None:
                break
            time.sleep(1.5 * attempt)

        if connectionContext is None:
            return None

        # Check if there are any arg overrides.
        userName = connectionContext.UserName
        accessToken = connectionContext.AccessToken
        if args is not None:
            userNameOverride = args.get("username", None)
            accessCodeOverride = args.get("access_code", None)
            if userNameOverride is not None or accessCodeOverride is not None:
                self.Logger.info("Bambu MQTT proxy is using an user name or access code override. User: %s, Access Code: %s", userNameOverride, accessCodeOverride)
            userName = userNameOverride if userNameOverride is not None else userName
            accessToken = accessCodeOverride if accessCodeOverride is not None else accessToken

        # We use TLS for both Bambu Cloud and local connections. Local printers use a self-signed certificate.
        return MqttConnectionContext(
            connectionContext.IpOrHostname,
            connectionContext.Port,
            userName,
            accessToken,
            useTls=True,
            allowInvalidCert=not connectionContext.IsCloud,
            keepAliveSec=5
        )


# Keep this Bambu-local name so existing host code can import it unchanged.
class MqttWebsocketProxyProviderBuilder(CommonMqttWebsocketProxyProviderBuilder):

    def __init__(self, logger:logging.Logger):
        super().__init__(logger, BambuMqttWebsocketProxyConnector(logger))
