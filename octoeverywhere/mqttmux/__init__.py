# OctoEverywhere MQTT multiplexer package.
#
# Provides a single shared upstream MQTT connection to a printer broker that
# multiple downstream "virtual clients" share simultaneously:
#   * LocalPluginClient  - in-process Pythonic API for the vendor plugin code
#   * WebSocketRelayClient - standards-compliant MQTT-over-WS relay for remote apps
#   * TcpBrokerClient    - local LAN MQTT broker accepting standard MQTT TCP clients
#
# The mux deduplicates subscriptions, remaps QoS 1/2 packet IDs, caches retained
# messages, and routes inbound PUBLISH messages to the right downstream clients.
