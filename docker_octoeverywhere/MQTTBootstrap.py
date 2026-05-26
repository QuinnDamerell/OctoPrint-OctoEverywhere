import os
import logging

from linux_host.config import Config

class MQTTBootstrap:

    @staticmethod
    def Bootstrap(logger:logging.Logger, config:Config) -> None:
        # All of these settings are optional.
        enabled = os.environ.get("MQTT_RELAY_ENABLED", None)
        if enabled is not None:
            enabled = enabled.lower() in ['true', '1', 'yes']
            logger.info(f"Setting MQTT Relay Enabled: {enabled}")
            config.SetBool(Config.SectionMqtt, Config.MqttLocalBrokerEnabled, enabled)

        port = os.environ.get("MQTT_RELAY_PORT", None)
        if port is not None:
            port = int(port)
            logger.info(f"Setting MQTT Relay Port: {port}")
            config.SetInt(Config.SectionMqtt, Config.MqttLocalBrokerPort, port)

        requireUpstreamAuth = os.environ.get("MQTT_RELAY_REQUIRE_UPSTREAM_AUTH", None)
        if requireUpstreamAuth is not None:
            requireUpstreamAuth = requireUpstreamAuth.lower() in ['true', '1', 'yes']
            logger.info(f"Setting MQTT Relay Require Upstream Auth: {requireUpstreamAuth}")
            config.SetBool(Config.SectionMqtt, Config.MqttLocalBrokerRequireUpstreamAuth, requireUpstreamAuth)

        username = os.environ.get("MQTT_RELAY_USERNAME", None)
        if username is not None:
            logger.info(f"Setting MQTT Relay Username: {username}")
            config.SetStr(Config.SectionMqtt, Config.MqttLocalBrokerUsername, username)

        password = os.environ.get("MQTT_RELAY_PASSWORD", None)
        if password is not None:
            logger.info("Setting MQTT Relay Password")
            config.SetStr(Config.SectionMqtt, Config.MqttLocalBrokerPassword, password)
