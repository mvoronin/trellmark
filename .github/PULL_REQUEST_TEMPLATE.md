## What this changes

<!-- What behaviour differs after this merges, and why. -->

## How it was verified

<!--
Which of `just check`, `just test`, targeted tests, or manual steps you ran,
and what the result was. "CI is green" on its own is not a verification note.
-->

---

### ⚠️ Before you push: no sensitive data

Pull requests, diffs, and commit history are public and permanent — force-push
does not reliably erase what was already fetched or indexed. Check that this
branch adds **no**:

- [ ] Exports or dumps — JSON exports, `just backup` output, `pg_dump` files,
      fixtures built from real data, or any database contents.
- [ ] Credentials — `.env` files, `TRELLMARK_*` secrets, database or
      administrator passwords, password hashes, API tokens, or session cookies.
- [ ] Private hostnames or addresses — real `TRELLMARK_PUBLIC_ORIGIN` values,
      internal domains, or internal IPs in code, tests, fixtures, or comments.
- [ ] Real personal content — actual links, notes, or group names. Test data
      should be invented and obviously fake.

Secret scanning with push protection is enabled on this repository, but it only
catches recognised credential formats. It will not catch an export, an internal
hostname, or your own bookmarks.

**If a secret did get committed, do not just amend it away.** Treat it as
compromised, rotate it, and say so on the PR — or report it privately via
[a security advisory](https://github.com/mvoronin/trellmark/security/advisories/new)
if disclosing it publicly would itself be a problem.

### Security

- [ ] This change is not a fix for an undisclosed vulnerability. Security fixes
      are coordinated through
      [a private advisory](https://github.com/mvoronin/trellmark/security/advisories/new)
      first — see [SECURITY.md](https://github.com/mvoronin/trellmark/blob/main/SECURITY.md).
