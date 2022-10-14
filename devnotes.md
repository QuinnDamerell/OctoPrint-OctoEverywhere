## Setup The Pre-Commit Hooks
- `pip install pre-commit`
- `pre-commit install`

## For PY 3
- Use `python3 -m venv py3venv` to create an environment in the current dir
- Use `source py3venv/bin/activate` to activate
- Pip install deps from the setup.py file
- Pip install octoprint (to make F5 debugging easier)
- In VS, open the "select python interpreter" dialog and pick the environment.


## For PY 2
- Use `virtualenv py2venv` to create an env
- Use `source py2venv/bin/activate` to activate
- Pip install deps from the setup.py file
- Pip install octoprint (to make F5 debugging easier)
- In VS, open the "select python interpreter" dialog and pick the environment.


## Install Other Branches:
    - https://github.com/QuinnDamerell/OctoPrint-OctoEverywhere/archive/pip.zip

## Before checking in:
- Run in py2 env
- Run in py3 env
- Make sure http works, ws works (printer console), webcam works (stream and snapshot)

## OctoPi Useful Commands
- tail -f ./.octoprint/logs/octoprint.log
- source ./oprint/bin/activate

## For notes about shared dependencies with OctoPrint, see setup.py