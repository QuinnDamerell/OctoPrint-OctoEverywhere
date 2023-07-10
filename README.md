[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/X8X7LBLK2)

# K1 Octoeverywhere

This repo edits the install script for Octoeverywhere to work with the limited MIPS architecture for the Creality K1. There are various services not available such as systemd, pushd, apt, etc. There are additional steps you will need to take as well.

## Requirements

This requires you to have root access to your Creality K1. This is done through an exploit using a shadow gcode file. For more information, please check out https://github.com/giveen/K1_Files/tree/ab81d83ca6421c8420a7a85e456059eb0e641bd3/exploit

## Caveats

Firmware updates will most likely completely overwrite these changes.

## Installation

1. Edit /usr/bin/virtualenv with `vi`. The first line should be changed from `#!/usr/bin/python` to `#!/usr/bin/python3`

2. Make a directory named `octoeverywhere-logs` in `/usr/data/`

3. Make a dummy systemd service (even though this board doesn't use it, just for the script to detect with minimal changes) in `/etc/systemd/system/moonraker.service` 

4. Add this line: `Environment=MOONRAKER_CONF=/usr/data/printer_data/config/moonraker.conf` to the contents of the created moonraker.service abov

5. Clone this repo into `/usr/data`

6. Rename the cloned folder to `octoeverywhere`

7. `cd` into `octoeverywhere` and run `./install.sh`

8. The script will hang on `Waiting for the plugin to produce a printer id...` - go ahead and respond `n` when it asks you if you want to keep waiting.

9. Copy the `startup_script.sh` from this repo into `/usr/data/`

10. Run the startup script `./startup_script.sh` (might need to make it executable `chmod 777 startup_script.sh`)

11. Click the link that the script echos to finish setup on Octoeverywhere's website

12. You're linked to Octoeverywhere! Last step is to make sure we start this service on startup. Copy `S99octoeverywhere` from this repo to `/etc/init.d/S99octoeverywhere`

13. Restart your printer

14. Profit! Enjoy :) 
