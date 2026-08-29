# Security policy

Trellmark is a private, single-user knowledge organizer that you self-host. A
vulnerability here usually means someone can reach another person's links,
notes, or session — so reports are welcome and taken seriously.

## Supported versions

Trellmark has no tagged releases yet. Only the current `main` branch is
supported, and fixes land there. There is no backporting to older commits, and
deliberately no migration path from any previous application.

If you run a deployment built from an older commit, please confirm the issue
still reproduces on `main` before reporting, where you reasonably can.

## Reporting a vulnerability

**Report privately through GitHub, not in a public issue:**

[**Open a private security advisory →**](https://github.com/mvoronin/trellmark/security/advisories/new)

That form is private between you and the maintainer. You can also reach it from
the repository's **Security** tab → **Report a vulnerability**.

Please do not open a public issue, discussion, or pull request for a suspected
vulnerability until a fix is available.

### What helps

- The commit SHA you tested against.
- What an attacker gains — read another user's data, bypass authentication,
  escalate to the host, and so on.
- Minimal reproduction steps, ideally against a scratch instance.
- Whether the issue needs an authenticated session, or works unauthenticated.
- Your assessment of severity, and any constraints that limit it.

### What not to send

Trellmark advisories are private, but they are still a channel to a person, and
attachments persist. Do not send real user data. See
[Reporting without sensitive data](#reporting-without-sensitive-data) below —
it applies here as much as it does to public issues.

### What to expect

Trellmark is maintained by one person as a side project, so there is no
response-time guarantee. Realistically:

- An acknowledgement that the report was read and understood.
- An assessment of whether it is in scope and what the severity looks like.
- A fix on `main`, and credit in the advisory if you would like it.

If a report goes unanswered for a few weeks, a nudge on the advisory thread is
welcome rather than rude.

## Scope

**In scope** — this repository: the FastAPI service, the JSON API, the
TypeScript SPA, authentication and session handling, the storage and migration
layer, JSON import/export, and the deployment contract under `deploy/`.

Findings that are particularly interesting:

- Authentication or session bypass, or session fixation.
- Reaching content across the safe/private visibility boundary.
- Anything that lets the app be reached other than through the intended
  loopback bind — for example `/internal/*` becoming externally reachable, or
  proxy headers being honoured from a non-loopback source.
- SQL injection, SSRF via the site-icon or page-title fetchers, or stored XSS
  in link and group content.

**Out of scope:**

- The separate infrastructure repository, and the shared Caddy instance it
  owns — including TLS configuration, certificates, domains, and routes.
- Deployments that widen the documented loopback bind, expose `/internal/*` at
  the edge, or otherwise depart from the contract in the README.
- Missing hardening headers with no demonstrated impact, and results from
  automated scanners pasted without a working reproduction.
- Vulnerabilities in dependencies that are already tracked by Dependabot,
  unless Trellmark's own use of the dependency makes the impact worse or
  reachable in a way the upstream advisory does not describe.

Reports about a self-hosted instance's own misconfiguration are out of scope
here, but a pull request clarifying the README is very welcome.

## Reporting without sensitive data

Trellmark holds exactly the kind of data that should never end up in a bug
report. **Never attach or paste:**

- **Exports and dumps** — JSON exports, `just backup` output, `pg_dump` files,
  or any database contents.
- **Credentials** — `.env` files, `TRELLMARK_*` secrets, database passwords,
  administrator passwords, password hashes, or session cookies.
- **Private hostnames and addresses** — your `TRELLMARK_PUBLIC_ORIGIN`, internal
  domains, or internal IP addresses.
- **Real content** — actual links, notes, group names, or anything else that
  identifies you or what you save.

Redact logs, tracebacks, and HTTP captures before pasting them, and reproduce
against a scratch instance with invented data wherever you can. A report with
five made-up bookmarks is more useful than one with a real export, because it
tells the maintainer precisely which fields matter.

If you genuinely cannot demonstrate an issue without sensitive data, say so in
the advisory and describe the shape of the data instead. Do not attach it and
hope it stays private.

## Hardening this repository already has

Secret scanning and push protection are enabled, so a committed credential
should be caught on push. That is a backstop, not permission to be careless: if
you believe a secret has been committed here, treat it as compromised, rotate
it, and report it privately through the advisory form above.
