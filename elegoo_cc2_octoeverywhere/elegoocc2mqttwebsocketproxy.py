import logging

from octoeverywhere.mqttmux.relayproxy import MqttRelayWebSocketProxyProviderBuilder


# Elegoo CC2's mux is registered under the constant key "elegoo-cc2" (see
# ElegooCc2Client.__init__). Same name preservation pattern as the Bambu
# relay builder above so the host code import path is unchanged.
class MqttWebsocketProxyProviderBuilder(MqttRelayWebSocketProxyProviderBuilder):

    def __init__(self, logger: logging.Logger) -> None:
        super().__init__(logger, mux_key="elegoo-cc2")
