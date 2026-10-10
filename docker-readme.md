# OctoEverywhere Companion Docker Image

OctoEverywhere's Companion Docker image works with: ✅

- [OctoEverywhere Bambu Connect](https://octoeverywhere.com/bambu?source=github_readme_docker) - OctoEverywhere for Bambu Lab 3D printers.
- [OctoEverywhere Elegoo Connect](https://octoeverywhere.com/elegoo-centauri-carbon?source=github_readme_docker) - OctoEverywhere for Elegoo Centauri Carbon (original) and Elegoo Centauri Carbon 2 3D printers.
- [OctoEverywhere Klipper Companion](https://octoeverywhere.com/?source=github_readme_docker) - OctoEverywhere for Klipper 3D printers.
- [OctoEverywhere PrusaLink](https://octoeverywhere.com/prusalink?source=github_readme_docker) - OctoEverywhere for Prusa 3D printers running PrusaLink or Prusa Connect.

OctoEverywhere's Companion Docker image **does not work with:** ⛔

- [OctoEverywhere For OctoPrint](https://octoeverywhere.com/?source=github_readme_docker) - Install the OctoEverywhere plugin directly in OctoPrint.
- [OctoEverywhere For Klipper](https://octoeverywhere.com/?source=github_readme_docker) - If you can install OctoEverywhere on the same device running Klipper, it's the recommended setup. Otherwise, use the OctoEverywhere Klipper Companion.


🤔 Confused? [Follow our step-by-step guide](https://octoeverywhere.com/getstarted?source=github_readme_docker_guide) to find the right version for your 3D printer!

Official Docker Image: https://hub.docker.com/r/octoeverywhere/octoeverywhere

Official Docker Compose: [GitHub Repo File](https://github.com/QuinnDamerell/OctoPrint-OctoEverywhere/blob/master/docker-compose.yml)

## Required Image Setup Information

There are five modes of the OctoEverywhere Docker image, depending on what 3D printer you're trying to use.

- `COMPANION_MODE=bambu`      - Bambu Connect for Bambu Lab 3D printer.
- `COMPANION_MODE=klipper`    - For Klipper / Moonraker based 3D printers.
- `COMPANION_MODE=prusalink`  - Prusa Link for Prusa 3D printers running Prusa Link.
- `COMPANION_MODE=elegoo`     - Elegoo Connect for the Elegoo Centauri & Centauri Carbon (original).
- `COMPANION_MODE=elegoo_cc2` - Elegoo Connect for the Elegoo Centauri Carbon 2.

Different companion modes need different printer information.

### Bambu Connect

To use Bambu Connect, you need to get the following information.

- Your printer's Access Code - https://octoeverywhere.com/s/access-code
- Your printer's Serial Number - https://octoeverywhere.com/s/bambu-sn
- Your printer's IP Address - https://octoeverywhere.com/s/bambu-ip

These three values must be set as environment vars when you first run the container. Once the container is run, you don't need to include them again, unless you want to update the values.

- `ACCESS_CODE=(code)`
- `SERIAL_NUMBER=(serial number)`
- `PRINTER_IP=(ip address)`

### Elegoo Connect for the Elegoo Centauri Carbon (original)

To use Elegoo Connect, you need to get the following information.

- Your Elegoo printer's IP address. - https://octoeverywhere.com/s/elegoo-ip

The IP address must be set as an environment var when you first run the container. Once the container is run, you don't need to include them again, unless you want to update the values.

- `PRINTER_IP=(ip address)`

### Elegoo Connect for the Elegoo Centauri Carbon 2

To use Elegoo Connect for the CC2, you need to get the following information.

- Your Elegoo CC2 printer's IP address. - https://octoeverywhere.com/s/cc2-ip
- Your Elegoo CC2 printer's access code - https://octoeverywhere.com/s/cc2-access-code

**Note if you disabled the access code on your CC2, use the default value of `123456`.**

These two values must be set as environment vars when you first run the container. Once the container is run, you don't need to include them again, unless you want to update the values.

- `PRINTER_IP=(ip address)`
- `ACCESS_CODE=(access code)`

### Klipper Companion

To use the Klipper Companion, you need to get the following information.

- Your printer's IP address.
- (optional) Moonraker's server port. Defaults to 7125.
- (optional) Moonraker API key. Defaults to None. If your Moonraker server requires auth, you can generate an API key in Mainsail or Fluidd.
- (optional) Your web frontend's server port. Defaults to 80.

These three values must be set as environment vars when you first run the container. Once the container is run, you don't need to include them again, unless you want to update the values.

- `PRINTER_IP=(ip address)`
- `MOONRAKER_PORT=(port)`
- `MOONRAKER_API_KEY=(apiKey)`
- `WEBSERVER_PORT=(port)`

### Prusa Link Connect

To use Prusa Link Connect, you need to get the following information.

- Your Prusa Link IP address.
- Your Prusa Link username and password, or your Prusa Link API key.

For help finding your Prusa Link API key, see https://octoeverywhere.com/s/prusa-link-api-key

These values must be set as environment vars when you first run the container. Once the container is run, you don't need to include them again, unless you want to update the values.

- `PRINTER_IP=(ip address)`
- `USERNAME=(username)`
- `PASSWORD=(password)`
- `API_KEY=(api key)`

## MQTT Relay Mux Broker Server

For Bambu Lab and Elegoo 3D printers, the OctoEverywhere plugin can also host a MQTT broker server that will combine the connections of all clients to a single MQTT connection to the printer. This is very useful for 3D printers like the Bambu A1, Bambu P1, and Elegoo CC2 that have a maximum number of clients that can connect directly to the 3D printer.

Point your app at the Docker host's address. Port `1883` automatically accepts plain MQTT or MQTT over TLS, while port `8883` accepts TLS only. The plugin detects the protocol for each connection, automatically creates a unique self-signed certificate, and checks for renewal at startup. Apps that verify certificates may require a setting to accept self-signed certificates or a custom certificate they trust. Disabling SSL verification in an app does not disable TLS.

The MQTT relay requires the credentials used by OctoEverywhere's upstream printer connection by default. For a local Bambu connection, these are `bblp` and the printer's access code; for Elegoo CC2, `elegoo` and the access code. A Bambu cloud connection uses different cloud credentials. To give local apps a separate login, set `MQTT_RELAY_REQUIRE_UPSTREAM_AUTH=false` and set both `MQTT_RELAY_USERNAME` and `MQTT_RELAY_PASSWORD`. TLS does not change the authentication settings.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MQTT_RELAY_ENABLED` | `true` | Enable the local MQTT relay. |
| `MQTT_RELAY_PORT` | `1883` | Port accepting plain MQTT and TLS. |
| `MQTT_RELAY_TLS_PORT` | `8883` | Additional TLS-only port. Set to `0` to disable this listener; TLS on `MQTT_RELAY_PORT` remains available. |
| `MQTT_RELAY_TLS_CERT_PATH` | Empty | Optional PEM certificate chain path inside the container, or a combined PEM containing the certificate and private key. |
| `MQTT_RELAY_TLS_KEY_PATH` | Empty | Separate matching, unencrypted PEM private key path. Omit for a combined certificate/key PEM. Requires `MQTT_RELAY_TLS_CERT_PATH`. |
| `MQTT_RELAY_REQUIRE_UPSTREAM_AUTH` | `true` | Require the upstream printer connection's credentials. |
| `MQTT_RELAY_USERNAME` | Empty | Separate relay username when upstream authentication is disabled. |
| `MQTT_RELAY_PASSWORD` | Empty | Separate relay password when upstream authentication is disabled. |

Publish the ports your apps use. With `docker run`, add `-p 1883:1883 -p 8883:8883`. With Docker Compose, merge these entries into your existing service:

```yaml
ports:
  - "1883:1883"
  - "8883:8883"
```

Existing containers that only publish `1883` must be recreated with the new mapping before apps can reach `8883`. For Compose, edit the file and run `docker compose up -d`; preserve the existing `/data` volume. Updating the image alone does not add a published port. If you set `MQTT_RELAY_TLS_PORT=0`, also remove its port mapping to free the host port.

For a second printer, use unique host ports such as `1884:1883` and `8884:8883`, then point its apps at host port `1884` or `8884`. Apps that require `8883` and cannot change it need a separate host IP address or computer per relay.

### Automatic TLS certificates

When neither custom TLS path is configured, the broker creates a unique self-signed certificate on its first startup. It saves the certificate and private key in `/data/mqtt-tls/server.pem` and exports only the public certificate to `/data/mqtt-tls/server.crt`. Keep `/data` mounted and preserve its contents when recreating the container; subsequent starts reuse the saved certificate.

The generated certificate is valid for 365 days. At each startup, the plugin checks it and renews it when fewer than 30 days remain, preserving the existing private key when it is usable. The check happens at startup, not continuously while the container runs. Clients that import or pin the certificate may need the renewed public certificate imported again.

**Only copy `server.crt` to clients. Never share `server.pem`, which contains the private key.** The public file is a self-signed server certificate, not a CA certificate; whether an app can trust it directly depends on that app.

### Use your own TLS certificate

1. Place a PEM certificate chain and its matching unencrypted private key in a directory on the Docker host. The certificate must cover the hostname or IP address entered in your apps, and its issuer must be trusted by those apps.
2. Mount that directory read-only and set the paths **inside the container**. Merge this example into your existing Compose service, preserving its printer settings and `/data` volume:

   ```yaml
   services:
     octoeverywhere-connect:
       environment:
         - MQTT_RELAY_TLS_CERT_PATH=/certs/fullchain.pem
         - MQTT_RELAY_TLS_KEY_PATH=/certs/privkey.pem
       volumes:
         - ./data:/data
         - ./certs:/certs:ro
   ```

   Make the files readable by the container's plugin user while keeping the private key private. Alternatively, set only `MQTT_RELAY_TLS_CERT_PATH` to a combined PEM containing the certificate chain and its unencrypted private key, leaving `MQTT_RELAY_TLS_KEY_PATH` unset. Do not share a combined PEM with clients. A key path without a certificate path is invalid. Environment variables are saved into the config; to clear an old separate key path, set `MQTT_RELAY_TLS_KEY_PATH=` when switching to a combined PEM.
3. Run `docker compose up -d` to apply the settings, then check `docker compose logs octoeverywhere-connect` for TLS startup errors. After later certificate replacements, restart the service so it reads the new files:

   ```bash
   docker compose restart octoeverywhere-connect
   ```

OctoEverywhere never overwrites or renews custom certificates. Manage their renewal yourself. If custom files cannot be loaded, the broker logs `TLS setup failed`, keeps plain MQTT available on its primary port, and rejects TLS connections. It does not generate a replacement for your custom certificate. To return to automatic certificates, clear both saved TLS paths (for Docker, explicitly set both environment variables to empty and recreate the container).

### Export and trust the generated certificate

1. With automatic certificates enabled, start the broker once, then export its **public certificate**. From the directory containing your Compose file, use the service name from that file:

   ```bash
   docker compose cp octoeverywhere-connect:/data/mqtt-tls/server.crt ./octoeverywhere-mqtt.crt
   ```

   For a container started with `docker run`, find its actual name with `docker ps`, then use `docker cp CONTAINER_NAME:/data/mqtt-tls/server.crt ./octoeverywhere-mqtt.crt`. A Compose service name and its generated container name are not necessarily the same. Copy the exported file to the machine running your MQTT app if it is elsewhere. When custom certificates are configured, distribute the configured public certificate or its issuing CA instead; the automatic `server.crt` file is not updated to represent custom credentials.

2. Confirm the file came from the intended plugin instance. Inspect its SHA-256 fingerprint and validity dates, then find Subject Alternative Name in the full certificate output (the hostnames/IP addresses it covers):

   ```bash
   openssl x509 -in octoeverywhere-mqtt.crt -noout -fingerprint -sha256 -dates
   openssl x509 -in octoeverywhere-mqtt.crt -noout -text
   ```

3. Prefer your app's trusted-server-certificate or custom-CA field when it supports importing a self-signed server certificate. Keep TLS and certificate verification enabled. Apps that require a CA certificate instead need your own certificate issued by a CA they trust. For an app that uses the operating system's trust store, follow the [Windows, macOS, and Linux trust instructions](https://help.octoeverywhere.com/help-docs/plugin/bambu-lab-mqtt-relay-server#trust-the-generated-certificate).

   Import on the **actual client machine or client container**, not just on the OctoEverywhere host. Some apps use a private certificate bundle or pin the Bambu manufacturer's certificate; adding a certificate to the OS store does not guarantee those apps will accept it.

4. Connect using an address covered by the certificate. The generated certificate normally includes the container's hostname and loopback addresses, plus an explicit bind address if configured when it was generated. The default `0.0.0.0` bind does not add the Docker host's LAN IP. Trusting the certificate does not fix an address mismatch; use a custom certificate for the address your app actually needs.

To check trust and hostname matching independently of MQTT login, run the following from the client with OpenSSL installed. Replace `BROKER_HOSTNAME` with a reachable hostname included in the certificate, and adjust the port if you changed its mapping:

```bash
openssl s_client -connect BROKER_HOSTNAME:8883 -servername BROKER_HOSTNAME -CAfile ./octoeverywhere-mqtt.crt -verify_hostname BROKER_HOSTNAME -verify_return_error
```

For an IP address, use `-verify_ip BROKER_IP` instead of `-verify_hostname BROKER_HOSTNAME`, set `-connect BROKER_IP:8883`, and omit `-servername`. Look for `Verify return code: 0 (ok)` or `Verification: OK`; press Ctrl+C after the handshake. This checks TLS only, not MQTT credentials or printer updates. A hostname/IP mismatch requires a suitable certificate even if its fingerprint and trust are correct.

After automatic certificate renewal, clients that imported or pinned the old certificate may need the new `server.crt`. For a managed setup, configure a custom certificate from your CA and trust that CA on the clients so routine server certificate renewal does not require importing a new self-signed certificate each time.

### TLS troubleshooting

If the plugin logs `TLS setup failed`, it rejects TLS connections and logs the failure while keeping plain MQTT available on the primary port. TLS connections never fall back to plaintext. Check the logged reason, certificate paths, and file permissions, then restart the plugin after fixing the problem.

Automatic certificate management uses the OpenSSL command-line tool, which is included in the official Docker image. If it is missing, update and recreate the container with the current image, or configure a valid custom certificate and key; custom certificates do not require the OpenSSL command-line tool. On standalone installations, `install.sh` attempts to install OpenSSL but only warns if that optional step fails, allowing plugin installation to continue. Install the OS package `openssl` (`openssl-util` on OpenWrt) manually if needed, then restart the plugin.

If the plugin cannot bind the extra TLS listener, the primary port remains available. A Docker host port conflict prevents the container itself from starting; free the occupied port or change the published mapping.

The relay carries MQTT messages, not webcam streams or FTP/FTPS file transfers. Apps may need separate direct printer connections for those features. See the [MQTT relay setup and troubleshooting guide](https://octoeverywhere.com/s/mqtt-relay).

## Required Persistent Storage

You must map the `/data` folder in your Docker container to a directory on your computer so the plugin can write data that will remain between runs. Failure to do this will require relinking the plugin when the container is destroyed or updated.

## Linking Your OctoEverywhere Companion

Once the Docker container is running, you need to look at the logs to find the linking URL.

Docker Compose:
`docker compose logs | grep https://octoeverywhere.com/getstarted`

Docker:
`docker logs octoeverywhere | grep https://octoeverywhere.com/getstarted`

# Running The Docker Image

## Using Docker Compose

Using docker compose is the easiest way to run the OctoEverywhere Companion is using Docker image.

- Install [Docker and Docker Compose](https://docs.docker.com/compose/install/linux/)
- Clone this repo
- Edit the `./docker-compose.yml` file to enter your environment information.
- Run `docker compose up -d`
- Follow the "Linking Your OctoEverywhere Companion" to link the plugin with your account.

## Using Docker

Docker Compose is a fancy wrapper to run Docker containers. You can also run Docker containers manually.

Use a command like this example, but update the required vars.

`docker run --name octoeverywhere -e COMPANION_MODE=<mode> -e PRINTER_IP=<ip address> (add other required env vars) -v /your/local/path:/data -d octoeverywhere/octoeverywhere`

Follow the "Linking Your OctoEverywhere Companion" to link the plugin with your account.

## Building The Image Locally

You can build the Docker image locally if you prefer, use the following command.

`docker build -t octoeverywhere .`
