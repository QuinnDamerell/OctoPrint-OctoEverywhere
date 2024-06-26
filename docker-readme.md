# Bambu Connect Docker Support

OctoEverywhere's docker image only works for [Bambu Connect](https://octoeverywhere.com/bambu?source=github_docker_readme) for Bambu Lab 3D printers. If you are using OctoPrint or Klipper, [follow our getting started guide](https://octoeverywhere.com/getstarted?source=github_docker_readme) to install the OctoEverywhere plugin.

Official Docker Image: https://hub.docker.com/r/octoeverywhere/octoeverywhere


## Bambu Cloud Vs Lan Only Connection Modes

Bambu Labs made a change where 3rd-party addons can't connect to the printer directly over your LAN network if the printer is connected to Bambu Cloud.

You have two options to setup OctoEverywhere:

1) Connect OctoEverywhere via the Bambu Cloud
2) Put your printer in Lan Only Mode and connect locally


### Connect Via Bambu Cloud

If you want to continue using Bambu Cloud, you can set up OctoEverywhere to access your printer via Bambu Cloud. To do so, you simply need to provide the docker image with your Bambu Cloud account email address and password.

**Rest assured, your Bambu Cloud email address and password are stored locally, encrypted on disk, and are never sent to the OctoEverywhere service.**

Additionally, due to restrictions from Bambu Labs, no 3rd-party services can support accounts with two-factor authentication enabled. Two-factor authentication must be disabled on your account to use OctoEverywhere; use a strong password instead.

If your Bambu Cloud account is setup to login with Google, Apple, or another 3rd party login service, you need to set an account password:
- Login to the Bambu Handy mobile app using the 3rd party provider.
- Tap the person icon at the bottom right.
- Tap Account Security > Change Password.

This will allow you to set a password. You can then use your email address and password with Bambu Connect.

### Use LAN Only Mode

If you don't mind disabling the Bambu Cloud, you can enable "LAN only mode" on your Bambu Lab 3D printer. With Bambu Cloud disabled, you WILL still be able to use Bambu Studio and Bambu Handy while on the same network as your 3D printer. OctoEverywhere can then directly connect to your printer over you local network, there's no need to supply a Bambu Cloud email or password.

## Required Setup Environment Vars

To use the Bambu Connect plugin, you need to get the following information.

- Your printer's Serial Number - https://octoeverywhere.com/s/bambu-sn
- Your printer's IP Address - (use the printer's display)
- If you're connecting with Bambu Cloud:
    - Your Bambu Cloud account email address
    - Your Bambu Cloud account password
    - Note: Your Bambu Cloud email address and password are stored locally, encrypted on disk, and never sent to the OctoEverywhere service.
    - Learn more here - https://octoeverywhere.com/s/bambu-setup
- If you're connecting in LAN Only Mode:
    - Your printer's Access Code - https://octoeverywhere.com/s/access-code

These three values must be set at environment vars when you first run the container. Once the container is ran, you don't need to include them, unless you want to update the values.

- SERIAL_NUMBER=(serial number)
- PRINTER_IP=(ip address)
- If connecting via Bambu Cloud:
    - BAMBU_CLOUD_ACCOUNT_EMAIL=(email)
    - BAMBU_CLOUD_ACCOUNT_PASSWORD=(password)
    - Optional - BAMBU_CLOUD_REGION=china - Use if your Bambu account is in the China region.
- If connecting via LAN Only Mode:
    - ACCESS_CODE=(code)
    - LAN_ONLY_MODE=TRUE


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

`docker run --name bambu-connect -e SERIAL_NUMBER=<serial number> -e PRINTER_IP=<ip address> -e BAMBU_CLOUD_ACCOUNT_EMAIL="<email>" -e BAMBU_CLOUD_ACCOUNT_PASSWORD="<password>" -v /your/local/data/folder:/data -d octoeverywhere/octoeverywhere`

Follow the "Linking Your Bambu Connect Plugin" to link the plugin to your account.

## Building The Image Locally

You can build the docker image locally if you prefer; use the following command.

`docker build -t octoeverywhere-local .`