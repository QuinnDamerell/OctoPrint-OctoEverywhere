# Bambu Connect Docker Support

OctoEverywhere's docker image only works with [Bambu Connect](https://octoeverywhere.com/bambu?source=github_docker_readme) for Bambu Lab 3D printers. If you are using OctoPrint or Klipper, [follow our getting started guide to install the OctoEverywhere plugin.](https://octoeverywhere.com/getstarted?source=github_docker_readme)

Official Docker Image: https://hub.docker.com/r/octoeverywhere/octoeverywhere


## Bambu Cloud Vs Lan Only Connection Modes

Bambu Lab made a firmware change in July 2024 where 3rd-party addons can't connect to the printer directly over your LAN network if the printer is connected to Bambu Cloud.

Thus, you can pick either of these install methods:

1) Connect OctoEverywhere to your 3D printer through Bambu Cloud.
2) Put your 3D printer in "LAN Only Mode" and connect OctoEverywhere locally to the 3D printer.

Note if you put your printer in "LAN Only Mode" you **can** still use Bambu Studio and Bambu Handy when on the same network as the 3D printer.

### Connect Via Bambu Cloud

For OctoEverywhere to connect to your 3D printer through Bambu Cloud, you just need to supply your Bambu Cloud account info to the local plugin.

**Rest assured, your Bambu Cloud email address and password are stored locally, encrypted on disk, and are never sent to the OctoEverywhere service.**

If you use Facebook, Google, or Apple to login to Bambu Cloud, [follow this guide to set a password on your account.](https://intercom.help/octoeverywhere/en/articles/9529936-bambu-cloud-with-bambu-connect)


### Connect Via 'LAN Only Mode'

If you don't mind disabling the Bambu Cloud, you can enable "LAN only mode" on your Bambu Lab 3D printer.

In "LAN only mode" OctoEverywhere can directly connect to your 3D printer on you local network, there's no need to supply your Bambu Cloud email or password. With Bambu Cloud disabled, you WILL still be able to use Bambu Studio and Bambu Handy while on the same network as your 3D printer.

## Required Setup Information

To use the Bambu Connect plugin, you need to get the following information.

- Your printer's Serial Number - https://octoeverywhere.com/s/bambu-sn
- Your printer's IP Address - Use the printer's display or https://octoeverywhere.com/s/bambu-ip
- If you're connecting with Bambu Cloud...
    - Your Bambu Cloud account email address
    - Your Bambu Cloud account password
    - **Note:** Your Bambu Cloud email address and password are stored locally, encrypted on disk, and never sent to the OctoEverywhere service.
    - Learn more here: https://octoeverywhere.com/s/bambu-setup
- Or if you're connecting in LAN Only Mode...
    - Your printer's Access Code - https://octoeverywhere.com/s/access-code

## Linking Your Bambu Connect Plugin

Once the docker container is running, you need to view the logs to find the linking URL.

Docker Compose:
`docker compose logs | grep https://octoeverywhere.com/getstarted`

Docker:
`docker logs bambu-connect | grep https://octoeverywhere.com/getstarted`

# Running The Docker Image

## Using Docker Compose

Using docker compose is the easiest way to run OctoEverywhere's Bambu Connect using docker.

- Install [Docker and Docker Compose](https://docs.docker.com/compose/install/linux/)
- Clone this repo
- Edit the `./docker-compose.yml` file to enter your environment information..
- Run `docker compose up -d`
- Follow the "Linking Your Bambu Connect Plugin" to link the plugin to your account.

## Using Docker

These three values must be set at environment vars when you first run the container. Once the container is ran, you don't need to include them, unless you want to update the values.

- SERIAL_NUMBER=(serial number)
- PRINTER_IP=(ip address)
- If connecting via Bambu Cloud...
    - BAMBU_CLOUD_ACCOUNT_EMAIL=(email)
    - BAMBU_CLOUD_ACCOUNT_PASSWORD=(password)
    - Optional - BAMBU_CLOUD_REGION=china - Use if your Bambu account is in the China region.
- If connecting via LAN Only Mode...
    - ACCESS_CODE=(code)
    - LAN_ONLY_MODE=TRUE

Pull the docker container locally:

`docker pull octoeverywhere/octoeverywhere`

Run the docker container passing the required information:

`docker run --name bambu-connect -e SERIAL_NUMBER=<serial number> -e PRINTER_IP=<ip address> -e BAMBU_CLOUD_ACCOUNT_EMAIL="<email>" -e BAMBU_CLOUD_ACCOUNT_PASSWORD="<password>" -v ./data:/data -d octoeverywhere/octoeverywhere`

Follow the "Linking Your Bambu Connect Plugin" to link the plugin to your account.

## Building The Image Locally

You can build the docker image locally if you prefer; use the following command.

`docker build -t octoeverywhere-local .`