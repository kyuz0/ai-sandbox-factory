Ask your coding agent to connect to both database hosts and explain their roles.

The primary database is `app` on `database.profile.internal`, user `app`.
The reporting database is `reports` on `reporting.profile.internal`, user `reporter`.
Both listen on port 5432 inside this sandbox's private network. They have separate
data and generated passwords in `/run/profile-services/connections.json`.

In a sandbox terminal, check each host:

```sh
ssh -F /run/profile-services/config database.profile.internal "psql -U app -d app -c 'SELECT current_database();'"
ssh -F /run/profile-services/config reporting.profile.internal "psql -U reporter -d reports -c 'SELECT current_database();'"
```

Connect directly from the coding-agent container using the saved connection
settings and password:

```sh
python3 - <<'PYTHON'
import json, os, subprocess
with open('/run/profile-services/connections.json') as source:
    connection = json.load(source)['database']
subprocess.run(['psql', '-h', connection['host'], '-p', str(connection['port']),
                '-U', connection['user'], '-d', connection['database'],
                '-c', 'SELECT current_database();'],
               env={**os.environ, 'PGPASSWORD': connection['password']}, check=True)
PYTHON
```

Pi's inner sandbox requires `bash_escalate` for these direct network commands.
The current approval mode applies. If approvals are off, enable approvals or use
a sandbox terminal.

Stop/start and coding-agent container rebuilds keep the databases. Create from
existing creates new databases without copying their data. Delete sandbox deletes
both databases' data. Export any needed database data before deletion.
