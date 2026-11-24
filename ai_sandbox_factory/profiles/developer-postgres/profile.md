# Developer + PostgreSQL

This workspace has the normal development tools and the PostgreSQL client.
The `database` profile container runs PostgreSQL 17 and is reachable at
`database.profile.internal:5432`. Its database and user are both `app`.
Read `/run/profile-services/connections.json` for this sandbox's password.

You have administrative SSH access to the actual database container:

```sh
ssh -F /run/profile-services/config database.profile.internal
```

With Pi's inner shell enabled, use the existing `bash_escalate` path for SSH,
`psql` and ping. In a workspace terminal or native agent, run these commands
directly. The database can also run SQL locally through SSH:

```sh
ssh -F /run/profile-services/config database.profile.internal "psql -U app -d app -c 'SELECT current_database();'"
```

Use this database for the user's project. Its data survives sandbox stops and
recreation. A copied sandbox starts with a separate database. Deleting this
sandbox and its private volumes removes its database data.
