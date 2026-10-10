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

        tlsPort = os.environ.get("MQTT_RELAY_TLS_PORT", None)
        # A blank value (e.g. "MQTT_RELAY_TLS_PORT=" left in a compose file) is treated as unset.
        if tlsPort is not None and len(tlsPort.strip()) > 0:
            tlsPort = int(tlsPort.strip())
            logger.info(f"Setting MQTT Relay TLS Port: {tlsPort}")
            config.SetInt(Config.SectionMqtt, Config.MqttLocalBrokerTlsPort, tlsPort)

        certPath = os.environ.get("MQTT_RELAY_TLS_CERT_PATH", None)
        if certPath is not None:
            config.SetStr(Config.SectionMqtt, Config.MqttLocalBrokerTlsCertPath, certPath)
        keyPath = os.environ.get("MQTT_RELAY_TLS_KEY_PATH", None)
        if keyPath is not None:
            config.SetStr(Config.SectionMqtt, Config.MqttLocalBrokerTlsKeyPath, keyPath)

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
