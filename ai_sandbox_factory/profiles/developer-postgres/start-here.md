Ask your coding agent to create a small table in the project's PostgreSQL database and query it.

The workspace includes `psql`. The database runs in a separate profile container
at `database.profile.internal:5432`, with database `app` and user `app`.
The agent can read the generated password in
`/run/profile-services/connections.json` and administer the database container
over SSH.

In the workspace terminal, try:

```sh
ssh -F /run/profile-services/config database.profile.internal "psql -U app -d app -c 'SELECT version();'"
```

With Pi's inner shell enabled, the agent uses `bash_escalate` for this direct
network command. It follows the sandbox's current approval mode.

Data survives stop/start and recreation. Creating from this sandbox copies its
setup but starts with a fresh database. Back up database data before deleting
its private volumes or changing PostgreSQL's major version.
