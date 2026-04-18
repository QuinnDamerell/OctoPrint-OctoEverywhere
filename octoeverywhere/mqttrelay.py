import asyncio
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from octoeverywhere.sentry import Sentry


# Global registry for connecting MqttRelay instances to their amqtt broker plugins.
# The plugin is loaded by import path string, so it discovers its relay via this registry.
_relay_lock = threading.Lock()
_relay_registry: Dict[int, "MqttRelay"] = {}
# Credentials registry: relay_id -> (username, password). Used by the auth plugin.
_relay_credentials: Dict[int, tuple] = {}
# Upstream connection state: relay_id -> bool. When False, new LAN connections are rejected.
_relay_upstream_connected: Dict[int, bool] = {}
_next_relay_id = 0


class IMqttRelayUpstream(ABC):
    """Interface that an upstream MQTT client must implement for the relay to forward messages.

    Any printer-specific MQTT client (Bambu, Elegoo, etc.) should implement this interface
    so the relay can publish messages from LAN clients to the upstream connection.
    """

    @abstractmethod
    def RelayPublish(self, topic: str, payload: bytes) -> bool:
        """Forward a message published by a LAN client to the upstream MQTT connection.

        Args:
            topic: The MQTT topic the LAN client published to.
            payload: The raw message payload.

        Returns:
            True if the publish was successful, False otherwise.
        """


class MqttRelay:
    """Runs an embedded MQTT 3.1.1 broker (via amqtt) that LAN clients can connect to.

    All messages published by LAN clients are intercepted and forwarded to the upstream
    MQTT connection via the IMqttRelayUpstream interface. Messages received by the upstream
    connection should be fed back via OnUpstreamMessage(), which broadcasts them to all
    connected LAN clients.

    This is abstract and works with any upstream MQTT client - Bambu, Elegoo, or any future
    printer that communicates over MQTT.
    """

    def __init__(self, logger: logging.Logger, upstream: IMqttRelayUpstream,
                 bindAddress: str = "0.0.0.0", port: int = 1883,
                 authUsername: Optional[str] = None, authPassword: Optional[str] = None) -> None:
        global _next_relay_id
        self.Logger = logger
        self.Upstream = upstream
        self.BindAddress = bindAddress
        self.Port = port
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._broker: Any = None  # amqtt.broker.Broker, typed as Any to avoid hard import
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._shutdownEvent: Optional[asyncio.Event] = None

        # Register in global registry so the broker plugin can find us
        with _relay_lock:
            self._relayId = _next_relay_id
            _next_relay_id += 1
            _relay_registry[self._relayId] = self
            # Store credentials so the auth plugin can validate connecting clients.
            if authUsername is not None and authPassword is not None:
                _relay_credentials[self._relayId] = (authUsername, authPassword)
            # Start with upstream disconnected - connections are rejected until SetUpstreamConnected(True).
            _relay_upstream_connected[self._relayId] = False


    def Start(self) -> None:
        """Start the MQTT relay broker in a background thread."""
        self._thread = threading.Thread(target=self._RunBrokerThread, daemon=True, name="MqttRelay")
        self._thread.start()
        if not self._started.wait(timeout=30):
            self.Logger.error("MqttRelay: Broker failed to start within 30 seconds.")


    def Stop(self) -> None:
        """Signal the broker to shut down and clean up."""
        loop = self._loop
        shutdownEvent = self._shutdownEvent
        if loop is not None and shutdownEvent is not None:
            loop.call_soon_threadsafe(shutdownEvent.set)
        with _relay_lock:
            _relay_registry.pop(self._relayId, None)
            _relay_credentials.pop(self._relayId, None)
            _relay_upstream_connected.pop(self._relayId, None)


    def SetUpstreamConnected(self, connected: bool) -> None:
        """Update whether the upstream MQTT connection is alive.

        When set to False, all existing LAN clients are disconnected and new
        connections will be rejected immediately (auth fails).
        When set to True, LAN clients are allowed to connect again.
        """
        with _relay_lock:
            _relay_upstream_connected[self._relayId] = connected
        if not connected:
            self.DisconnectAllClients()


    def DisconnectAllClients(self) -> None:
        """Disconnect all currently connected LAN clients."""
        broker = self._broker
        loop = self._loop
        if loop is None or broker is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._DisconnectAllClientsAsync(), loop)
        except Exception as e:
            self.Logger.error("MqttRelay: Error disconnecting LAN clients: %s", e)


    async def _DisconnectAllClientsAsync(self) -> None:
        """Disconnect all LAN client sessions from the broker."""
        broker = self._broker
        if broker is None:
            return
        # Collect client IDs from the broker's sessions dict.
        clientIds = list(broker.sessions.keys())
        if not clientIds:
            return
        self.Logger.info("MqttRelay: Disconnecting %d LAN client(s) due to upstream connection loss.", len(clientIds))
        for clientId in clientIds:
            try:
                sessionTuple = broker.sessions.get(clientId)
                if sessionTuple is not None:
                    _, handler = sessionTuple
                    if handler is not None:
                        await handler.handle_connection_closed()
                        await handler.stop()
            except Exception as e:
                self.Logger.debug("MqttRelay: Error disconnecting client %s: %s", clientId, e)


    def OnUpstreamMessage(self, topic: str, payload: bytes) -> None:
        """Called by the upstream client when a message arrives from the remote MQTT connection.

        This broadcasts the message to all LAN clients connected to the local broker.
        """
        broker = self._broker
        loop = self._loop
        if loop is not None and broker is not None:
            asyncio.run_coroutine_threadsafe(
                broker.internal_message_broadcast(topic, payload),
                loop
            )


    def _OnBrokerMessageReceived(self, topic: str, data: bytes) -> None:
        """Called by the broker plugin when a LAN client publishes a message.

        Forwards it to the upstream MQTT connection.
        """
        try:
            self.Upstream.RelayPublish(topic, data)
        except Exception as e:
            self.Logger.error("MqttRelay: Error relaying message to upstream on topic '%s': %s", topic, e)


    def _RunBrokerThread(self) -> None:
        """Entry point for the broker background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._StartBrokerAsync())
        except Exception as e:
            Sentry.OnException("MqttRelay: Broker run failed.", e)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass


    async def _StartBrokerAsync(self) -> None:
        """Create, configure, and run the amqtt broker until shutdown is signaled."""
        # Lazy import so systems without amqtt don't fail on module load
        from amqtt.broker import Broker  # type: ignore[import-untyped]

        self._shutdownEvent = asyncio.Event()

        config = {
            "listeners": {
                "default": {
                    "type": "tcp",
                    "bind": f"{self.BindAddress}:{self.Port}"
                }
            },
            # Load our custom plugins by class import path.
            # MqttRelayBrokerPlugin intercepts messages from LAN clients and relays them upstream.
            # MqttRelayAuthPlugin enforces the same credentials the upstream connection uses.
            "plugins": {
                "octoeverywhere.mqttrelay.MqttRelayBrokerPlugin": {
                    "relay_id": self._relayId
                },
                "octoeverywhere.mqttrelay.MqttRelayAuthPlugin": {
                    "relay_id": self._relayId
                }
            }
        }

        self._broker = Broker(config)
        try:
            await self._broker.start()
        except Exception as e:
            self.Logger.error("MqttRelay: Failed to start broker on %s:%s - %s", self.BindAddress, self.Port, e)
            self._started.set()
            return

        self.Logger.info("MqttRelay: MQTT broker started on %s:%s", self.BindAddress, self.Port)
        self._started.set()

        # Block until shutdown is requested
        await self._shutdownEvent.wait()

        self.Logger.info("MqttRelay: Shutting down broker...")
        try:
            await self._broker.shutdown()
        except Exception as e:
            self.Logger.error("MqttRelay: Error during broker shutdown: %s", e)
        self.Logger.info("MqttRelay: Broker stopped.")


#
# amqtt broker plugins.
# These are loaded by amqtt's PluginManager via import path string.
# They require amqtt to be installed; on systems without amqtt they are simply not defined.
#
try:
    from dataclasses import dataclass

    from amqtt.plugins.base import BaseAuthPlugin, BasePlugin  # type: ignore[import-untyped]

    class MqttRelayBrokerPlugin(BasePlugin):  # type: ignore[type-arg]
        """amqtt broker plugin that intercepts messages published by LAN clients
        and relays them to the upstream MQTT connection via the MqttRelay."""

        @dataclass
        class Config:
            relay_id: int = -1

        def __init__(self, context: Any) -> None:
            super().__init__(context)
            self._relay: Optional[MqttRelay] = None
            with _relay_lock:
                self._relay = _relay_registry.get(self.config.relay_id)

        async def on_broker_message_received(self, client_id: Any = None, message: Any = None) -> None:
            """Fired when any LAN client publishes a message to the broker."""
            if self._relay is not None and message is not None and message.topic and message.data:
                self._relay._OnBrokerMessageReceived(message.topic, message.data)


    class MqttRelayAuthPlugin(BaseAuthPlugin):  # type: ignore[type-arg]
        """amqtt auth plugin that enforces the same MQTT credentials the upstream connection uses.

        LAN clients must connect with the same username/password as the upstream
        (e.g. bblp / LAN access code for Bambu printers).
        """

        @dataclass
        class Config:
            relay_id: int = -1

        async def authenticate(self, *, session: Any = None) -> bool:
            if session is None:
                return False
            # Reject connections when the upstream MQTT connection is down.
            with _relay_lock:
                upstreamConnected = _relay_upstream_connected.get(self.config.relay_id, False)
            if not upstreamConnected:
                return False
            with _relay_lock:
                creds = _relay_credentials.get(self.config.relay_id)
            # If no credentials were configured, allow all connections (open broker).
            if creds is None:
                return True
            expectedUser, expectedPass = creds
            sessionUser = getattr(session, "username", None)
            sessionPass = getattr(session, "password", None)
            return sessionUser == expectedUser and sessionPass == expectedPass

except ImportError:
    # amqtt is not installed - plugin classes won't be defined.
    # This is fine; MqttRelay.Start() will also fail at import time with a clear error.
    pass
