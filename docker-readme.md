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

The MQTT relay will require the same auth credentials as the 3D printer by default. If you want to use a static username and password, set `MQTT_RELAY_REQUIRE_UPSTREAM_AUTH` to false and set a static username and password.

- `MQTT_RELAY_ENABLED=true`
- `MQTT_RELAY_PORT=1883`
- `MQTT_RELAY_REQUIRE_UPSTREAM_AUTH=true`
- `MQTT_RELAY_USERNAME=<str>`
- `MQTT_RELAY_PASSWORD=<str>`



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
