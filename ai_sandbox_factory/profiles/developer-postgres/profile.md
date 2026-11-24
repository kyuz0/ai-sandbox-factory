# Developer + PostgreSQL

This sandbox's coding-agent container has the normal development tools and the
PostgreSQL client. The `database` profile service runs PostgreSQL 17 in its own
container at
`database.profile.internal:5432`. Its database and user are both `app`.
Read `/run/profile-services/connections.json` for this sandbox's password.

You have administrative SSH access to the actual database container:

```sh
ssh -F /run/profile-services/config database.profile.internal
```

With Pi's inner sandbox enabled, use `bash_escalate` for SSH, `psql` and ping.
The configured approval mode applies. If approvals are off, ask the user to
enable approvals or run the command in a sandbox terminal. In Codex, Claude Code,
Antigravity or a sandbox terminal, connect directly to the profile host. Follow
any shell approval required by the selected coding agent.
To run `psql` inside the database container, use SSH:

```sh
ssh -F /run/profile-services/config database.profile.internal "psql -U app -d app -c 'SELECT current_database();'"
```

Use this database for the user's project.

- Stopping and starting this sandbox keeps its database data.
- Rebuilding the coding-agent container keeps this database container and its data.
- **Create from existing…** in Factory creates another sandbox using this sandbox's
  configuration. The new sandbox gets a new, empty PostgreSQL database. This
  database's data is not copied.
- **Delete sandbox** permanently deletes this sandbox's database data.
