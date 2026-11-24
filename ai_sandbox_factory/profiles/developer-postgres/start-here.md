Ask your coding agent to create a small table in the project's PostgreSQL database and query it.

The coding-agent container includes `psql`. The database runs in a separate profile container
at `database.profile.internal:5432`, with database `app` and user `app`.
The agent can read the generated password in
`/run/profile-services/connections.json` and administer the database container
over SSH.

In a sandbox terminal, try:

```sh
ssh -F /run/profile-services/config database.profile.internal "psql -U app -d app -c 'SELECT version();'"
```

With Pi's inner sandbox enabled, the agent uses `bash_escalate` for this direct
network command. It follows the sandbox's current approval mode. If approvals
are off, enable approvals or run the command in a sandbox terminal.

- Stopping and starting the sandbox keeps its database data.
- Rebuilding the coding-agent container keeps the database container and its data.
- **Create from existing…** creates another sandbox using this sandbox's
  configuration, with a new, empty PostgreSQL database. Existing database data
  is not copied.
- **Delete sandbox** permanently deletes its database data. Export the database
  data before deleting the sandbox.

Export the database data before moving to a different PostgreSQL major version.
