The current environment is a development container. The project is mounted at /workspace.
This base profile supplies Node.js, Git, system Python (python3), and common
command-line tools. Factory adds the selected coding agent. Pi sandboxes include Pi.
The profile does not create an application Python virtual environment.
A .venv visible inside /workspace may have been created on the host with different
absolute paths or a different Python version. Do not assume it is usable here.
Use HTTP_PROXY/HTTPS_PROXY when present. With Restricted networking selected, only
approved external destinations are reachable. Unrestricted networking uses ordinary
container networking.
