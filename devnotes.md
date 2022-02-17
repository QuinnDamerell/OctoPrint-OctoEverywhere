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
    - https://github.com/QuinnDamerell/OctoPrint-OctoEverywhere/archive/advance-ws.zip

## Before checking in:
- Run in py2 env
- Run in py3 env
- Make sure http works, ws works (printer console), webcam works (stream and snapshot)