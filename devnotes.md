For PY 3
- Use `python3 -m venv py3venv` to create an envroment in the current dir
- Use `source py3venv/bin/activate` to activate
- Pip install needed things
- In VS, open the "select python interpreter" dialog and pick the environment.


For PY 2
- Use `virtualenv py2venv` to create an env
- Use `source py2venv/bin/activate` to activate
- Pip install things
- In VS, open the "select python interpreter" dialog and pick the environment.

Requiremnets from PIP
- websocket_client
- requests
- jsonpickle

Before checking in:
- Run in py2 env
- Run in py3 env
- Make sure http works, ws works (printer console), webcam works (stream and snapshot)