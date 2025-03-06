# OctoEverywhere Companion Docker Image

OctoEverywhere's Companion Docker image works with: ✅

- [OctoEverywhere Bambu Connect](https://octoeverywhere.com/bambu?source=github_readme_docker) - OctoEverywhere for Bambu Lab 3D printers.
- [OctoEverywhere Elegoo Connect](https://octoeverywhere.com/elegoo-centauri?source=github_readme_docker) - OctoEverywhere for Elegoo Centauri & Centauri Carbon 3D printers.
- [OctoEverywhere Klipper Companion](https://octoeverywhere.com/?source=github_readme_docker) - OctoEverywhere for Klipper 3D printers.

OctoEverywhere's Companion Docker image **does not work with:** ⛔

- [OctoEverywhere For OctoPrint](https://octoeverywhere.com/?source=github_readme_docker) - Install the OctoEverywhere plugin directly in OctoPrint.
- [OctoEverywhere For Klipper](https://octoeverywhere.com/?source=github_readme_docker) - If you can install OctoEverywhere on the same device running Klipper, it's the recommended setup. Otherwise use the OctoEverywhere Klipper Companion.


🤔 Confused? [Follow our step-by-step guide](https://octoeverywhere.com/getstarted?source=github_readme_docker_guide) to find the right version for your 3D printer!

Official Docker Image: https://hub.docker.com/r/octoeverywhere/octoeverywhere

Official Docker Compose: [GitHub Repo File](https://github.com/QuinnDamerell/OctoPrint-OctoEverywhere/blob/master/docker-compose.yml)

## Required Image Setup Information

There are three modes of the OctoEverywhere docker image depending on what 3D printer you're trying to use.

- `COMPANION_MODE=bambu` - Bambu Connect for Bambu Lab 3D printer.
- `COMPANION_MODE=elegoo` - Elegoo Connect for the Elegoo Centauri & Centauri Carbon.
- `COMPANION_MODE=klipper` - For Klipper / Moonraker based 3D printers.

Different companion modes need different printer information.

### Bambu Connect

To use Bambu Connect, you need to get the following information.

- Your printer's Access Code - https://octoeverywhere.com/s/access-code
- Your printer's Serial Number - https://octoeverywhere.com/s/bambu-sn
- Your printer's IP Address - https://octoeverywhere.com/s/bambu-ip

These three values must be set as environment vars when you first run the container. Once the container is run, you don't need to include them again, unless you want to update the values.

- ACCESS_CODE=(code)
- SERIAL_NUMBER=(serial number)
- PRINTER_IP=(ip address)

### Elegoo Connect

To use Elegoo Connect, you need to get the following information.

- Your Elegoo printer's IP address. - https://octoeverywhere.com/s/elegoo-ip

The IP address must be set as an environment var when you first run the container. Once the container is run, you don't need to include them again, unless you want to update the values.

- PRINTER_IP=(ip address)

### Klipper Companion

To use Elegoo Connect, you need to get the following information.

- Your printer's IP address.
- (optional) Moonraker's server port. Defaults to 7125.
- (optional) Your web frontend's server port. Defaults to 80.

These three values must be set as environment vars when you first run the container. Once the container is run, you don't need to include them again, unless you want to update the values.

- PRINTER_IP=(ip address)
- MOONRAKER_PORT=(port)
- WEBSERVER_PORT=(port)

## Required Persistent Storage

You must map the `/data` folder in your docker container to a directory on your computer so the plugin can write data that will remain between runs. Failure to do this will require relinking the plugin when the container is destroyed or updated.

## Linking Your OctoEverywhere Companion

Once the docker container is running, you need to look at the logs to find the linking URL.

Docker Compose:
`docker compose logs | grep https://octoeverywhere.com/getstarted`

Docker:
`docker logs octoeverywhere | grep https://octoeverywhere.com/getstarted`

# Running The Docker Image

## Using Docker Compose

Using docker compose is the easiest way to run the OctoEverywhere Companion is using docker image.

- Install [Docker and Docker Compose](https://docs.docker.com/compose/install/linux/)
- Clone this repo
- Edit the `./docker-compose.yml` file to enter your environment information.
- Run `docker compose up -d`
- Follow the "Linking Your OctoEverywhere Companion" to link the plugin with your account.

## Using Docker

Docker compose is a fancy wrapper to run docker containers. You can also run docker containers manually.

Use a command like this example, but update the required vars.

`docker run --name octoeverywhere -e COMPANION_MODE=<mode> -e PRINTER_IP=<ip address> (add other required env vars) -v /your/local/path:/data -d octoeverywhere/octoeverywhere`

Follow the "Linking Your OctoEverywhere Companion" to link the plugin with your account.

## Building The Image Locally

You can build the docker image locally if you prefer, use the following command.

`docker build -t octoeverywhere .`