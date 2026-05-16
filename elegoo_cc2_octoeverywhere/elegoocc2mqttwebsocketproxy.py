import logging
from typing import Any, Callable, Dict, Optional

from octoeverywhere.mqttwebsocketproxy import (
    IMqttWebsocketProxyConnector,
    MqttConnectionContext,
    MqttWebsocketProxyProviderBuilder as CommonMqttWebsocketProxyProviderBuilder,
)

from .elegoocc2client import ElegooCc2Client


class ElegooCc2MqttWebsocketProxyConnector(IMqttWebsocketProxyConnector):

    def __init__(self, logger:logging.Logger) -> None:
        self.Logger = logger


    def GetConnectionContext(self, args:Optional[Dict[str, Any]], isClosed:Callable[[], bool]) -> Optional[MqttConnectionContext]:
        return ElegooCc2Client.Get().GetMqttProxyConnectionContext(args, isClosed)


class MqttWebsocketProxyProviderBuilder(CommonMqttWebsocketProxyProviderBuilder):

    def __init__(self, logger:logging.Logger):
        super().__init__(logger, ElegooCc2MqttWebsocketProxyConnector(logger))
