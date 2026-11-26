# Developer with two database hosts

The main agent sandbox includes `psql`. Use `database.profile.internal:5432`
for the project's primary database (`app`, user `app`). Use
`reporting.profile.internal:5432` for reporting and experiments (`reports`,
user `reporter`). These are independent PostgreSQL servers with separate data.

Read `/run/profile-services/connections.json` for this sandbox's generated
passwords and connection settings. Do not assume both servers use the same
password. The main agent sandbox has administrative SSH access to both:

```sh
ssh -F /run/profile-services/config database.profile.internal "psql -U app -d app -c 'SELECT current_database();'"
ssh -F /run/profile-services/config reporting.profile.internal "psql -U reporter -d reports -c 'SELECT current_database();'"
```

With Pi's inner sandbox enabled, use `bash_escalate` for direct SSH, database
connections and ping. If approvals are off, ask the user to enable approvals or
run the command in a sandbox terminal. Other coding agents use their normal
shell tools and approval mechanism. Internal connections do not use the external
SSH connector. External requests still follow Factory's proxy rules.

Stopping, starting or rebuilding the main agent sandbox keeps both databases.
Create from existing initializes new databases; it does not copy this data.
Delete sandbox permanently deletes both databases' data. Export needed data first.
