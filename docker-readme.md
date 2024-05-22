# Bambu Connect Docker Support

OctoEverywhere's docker image only works for [Bambu Connect](https://octoeverywhere.com/bambu?source=github_docker_readme) for Bambu Lab 3D printers. If you are using OctoPrint or Klipper, [follow our getting started guide](https://octoeverywhere.com/getstarted?source=github_docker_readme) to install the OctoEverywhere plugin.

Official Docker Image: https://hub.docker.com/r/octoeverywhere/octoeverywhere

## Required Setup Environment Vars

To use the Bambu Connect plugin, you need to get the following information.

- Your printer's Access Code - https://octoeverywhere.com/s/access-code
- Your printer's Serial Number - https://octoeverywhere.com/s/bambu-sn
- Your printer's IP Address - (use the printer's display)

These three values must be set at environment vars when you first run the container. Once the container is run, you don't need to include them again, unless you want to update the values.

- ACCESS_CODE=(code)
- SERIAL_NUMBER=(serial number)
- PRINTER_IP=(ip address)

## Required Persistent Storage

You must map the `/data` folder in your docker container to a directory on your computer so the plugin can write data that will remain between runs. Failure to do this will require relinking the plugin when the container is destroyed or updated.

## Linking Your Bambu Connect Plugin

Once the docker container is running, you need to look at the logs to find the linking URL.

Docker Compose:
`docker compose logs | grep https://octoeverywhere.com/getstarted`

Docker:
`docker logs bambu-connect | grep https://octoeverywhere.com/getstarted`

# Running The Docker Image

## Using Docker Compose

Using docker compose is the easiest way to run OctoEverywhere's Bambu Connect using docker.

- Install [Docker and Docker Compose](https://docs.docker.com/compose/install/linux/)
- Clone this repo
- Edit the `./docker-compose.yml` file to enter your environment vars
- Run `docker compose up -d`
- Follow the "Linking Your Bambu Connect Plugin" to link the plugin to your account.

## Using Docker

Docker compose is a fancy wrapper to run docker containers. You can also run docker containers manually.

Use a command like this example, but update the required vars.

`docker pull octoeverywhere/octoeverywhere`
`docker run --name bambu-connect -e ACCESS_CODE=<code> -e SERIAL_NUMBER=<serial number> -e PRINTER_IP=<ip address> -v /your/local/path:/data -d octoeverywhere/octoeverywhere`

Follow the "Linking Your Bambu Connect Plugin" to link the plugin to your account.

## Building The Image Locally

You can build the docker image locally if you prefer, use the following command.

`docker build -t octoeverywhere .`