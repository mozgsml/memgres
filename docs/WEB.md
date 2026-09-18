# The web panel

A browser view of memory for people: sign in, look through the spaces you can
reach as a graph or a tree, search, open a record and see what links to it and
who wrote it — issue tokens for your own AI clients, and run your spaces'
members, without asking an administrator. Administrators get the sign-in queue
and a directory of people.

The panel **reads memory only**. Writing stays with agents (MCP) and the REST
API. What it does change is who may reach memory: the members of a space, and —
for administrators — the accounts themselves.

It is served by `memgres-server` from the same image as everything else. Fonts,
the graph library (d3) and the Markdown renderer (marked, DOMPurify) ship inside
the package with their licences: the panel needs no internet access at runtime.

## Turning it on

```bash
pip install "memgres[server,web]"        # the image already has both
```

```yaml
services:
  web:
    image: ghcr.io/mozgsml/memgres:X.Y.Z
    command: memgres-server
    environment:
      MEMGRES_DATABASE_URL: postgresql://memgres:…@db:5432/memgres
      MEMGRES_KEY_MODE: managed
      MEMGRES_WEB_ENABLED: "true"
      MEMGRES_PUBLIC_URL: https://memory.example.com
      MEMGRES_MCP_PUBLIC_URL: https://memory.example.com/mcp
      MEMGRES_OIDC_CONFIG: /etc/memgres/providers.toml       # optional, see docs/OIDC.md
      MEMGRES_ADMIN_TOKEN_FILE: /run/secrets/memgres-admin   # the first way in
    volumes:
      - ./providers.toml:/etc/memgres/providers.toml:ro
    ports: ["8080:8080"]
```

The panel is at `/`. Put it behind a TLS-terminating reverse proxy; see
[Behind a proxy](#behind-a-proxy).

The panel needs `open` or `managed` mode. `single` has no accounts, so there is
nobody to sign in as, and the server refuses to start with the panel enabled.

| Variable | Default | |
|---|---|---|
| `MEMGRES_WEB_ENABLED` | `false` | mount the panel and its `/ui/api` on the REST server |
| `MEMGRES_PUBLIC_URL` | **required** | the address people open, e.g. `https://memory.example.com`. It is the only origin state-changing calls are accepted from, and the base of the OIDC redirect URI — never taken from the request's `Host` header |
| `MEMGRES_MCP_PUBLIC_URL` | — | the MCP endpoint shown in the config snippets of the token dialog. The panel cannot work it out: MCP is usually another container |
| `MEMGRES_OIDC_CONFIG` | — | path of the sign-in provider file ([docs/OIDC.md](OIDC.md)) |
| `MEMGRES_WEB_SESSION_HOURS` | `12` | a session ends this long after sign-in |
| `MEMGRES_WEB_COOKIE_SECURE` | `true` | send the session cookie over HTTPS only. Turning it off is for a plain-HTTP test: anyone who can see the traffic can copy the cookie and act as the person. With OIDC configured it may be turned off only when `MEMGRES_PUBLIC_URL` is `localhost`/`127.0.0.1` |
| `MEMGRES_FORWARDED_ALLOW_IPS` | uvicorn's default (`127.0.0.1`) | addresses of the reverse proxy whose `X-Forwarded-For` is trusted. Without it, behind a proxy every visitor looks like the proxy — and shares one sign-in throttle |

## The first sign-in

A fresh server has no sign-in providers anyone has linked, so `/signin` sends
everyone to **`/signin/admin`**. That page takes a token:

- the server's `MEMGRES_ADMIN_TOKEN` (or the one in `MEMGRES_ADMIN_TOKEN_FILE`), or
- the personal `mgk_` token of an account whose role is `superadmin` or `user_manager` —
  and only a **full** one: `admin` permission, not pinned to a space. A read-only or
  space-pinned token minted for an agent is refused even when its account is an
  administrator. A session opened with the whole account's authority could mint
  an unrestricted token or link a sign-in method, and outlive the revocation of
  the token that opened it.

Sign in there, open **Account → Sign-in methods**, and link your provider
account. From then on `/signin` shows the providers, and `/signin/admin` stays
available for the day a provider is down. Nothing in the panel links to it:
administrators open it by its address.

A plain user's token is refused at `/signin/admin` with the same answer as a
token that does not exist.

## What people can do

- **Memory.** Every space they can reach, in the sidebar, busiest first. A space
  opens as a graph (records as hexagons, sized by how many links they have) or
  as a tree. Select a record to fade everything unrelated; switch to the local
  view to see only its neighbours up to three steps away, with the path back to
  the root. Search uses the server's recall, lexical or semantic as configured;
  its results open in their own column beside the record, so opening one keeps
  the list, and the column closes with ×.
  A record's body is rendered as Markdown — headings, lists, tables, code,
  links, and `[[path]]` links that open the record they name. Nothing in a body
  runs: raw HTML is shown as text, only `http(s)`/`mailto` links are kept, images
  are not fetched. A record shows its history: who changed it and when, each name
  opening that person's page. The record panel is as wide as you drag its edge
  (double-click the edge to reset); the width is remembered in the browser.
- **Tokens** (Account → Tokens). A token for each device or client, with:
  - access `read` or `write` — never `admin`: an admin-ceiling token can mint
    more tokens, so a leaked one could not be contained by revoking it;
  - an expiry of 30, 90, 180 or 365 days — always;
  - optionally one space, from those the person reaches;
  - at most 50 live tokens per account.

  The secret is shown once, with ready configs for Claude Code, Cursor and
  OpenCode. A token never reaches further than its account.
- **Sign-in methods** (Account). Link another provider, or unlink one — not the
  last.
- **Language** (Account → Profile, and on the sign-in page). Otherwise the
  browser's language, otherwise English.
- **Their own activity** (Account → Profile): what they and their agents wrote,
  day by day for 26 weeks, by space and by kind. Only writes — reading is not
  tracked per person. A record that is erased takes its history with it.

### Members of a space

The administrators of a space — its owner, an `admin` member, a superadmin —
open **Members & access** from the space's panel. There they:

- **add someone by email** with `read`, `write` or `admin`. An account that
  already has that email (its profile email, or a provider's verified one) is
  added at once, never with less access than they already have. Anyone else gets
  an **invitation**, valid 30 days: the space opens for them the first time they
  sign in through a provider that marks this address verified. The form answers
  the same either way — but the member list then shows who was added, so a space
  administrator can learn that an address has an account here. That is why adding
  is limited to 60 people an hour per person, and why a colleague's page shows no
  role.

  An invitation opens the space, **not the server**. Whether that sign-in is let
  in is still up to the provider's rules and the administrators; while it waits
  in the queue, administrators see who invited the person and where. An
  invitation is used once, and lapses if its sender no longer administers the
  space (or is switched off) — the list marks such invitations. It never lowers
  access someone already has; neither does approving an old request to join.
  Invitations are not emailed yet: tell the person yourself.
- change a member's permission, or remove them; anyone may **leave** a space
  they are in, except its owner;
- **decide requests to join**, granting the permission they choose — which may
  be less than was asked;
- **hand the space over** to one of its members (owner or superadmin only),
  staying on as an `admin` or not;
- copy **the space's link**.

### A superadmin and other people's spaces

A superadmin's role reads every space (as `space="*"` does over MCP), but the
sidebar still lists only the spaces they own or were added to. Under the list,
**All spaces…** opens every space on the server with its owner, members and
records, searchable by name or owner. Opening one — or following a link to one —
puts it in a separate group, **Opened as superadmin**, drawn with a dashed
outline and marked in the space's title: it is not theirs, and nothing on the
server records that they looked. The group lasts until they sign out — signing
out clears it from the browser too — or until they remove a space with ×.
Nothing becomes a membership. A user manager has no such list: that role
administers accounts, not what is inside spaces.

There is no list of spaces a person cannot open. Someone who follows a space's
link without access sees *You can't open this space* — the same for a space that
does not exist — and can **ask for access** there. The space's administrators see
the request on Members & access, with a count next to the space in the sidebar.

### People

A person's name — in a record's history, among a space's members — opens their
page. What it shows depends on who is looking:

- **someone you share a space with**: name, department, position, the spaces you
  share, and what they wrote in those spaces only. Not their email, role, tokens
  or other spaces;
- **a service administrator**: everything — email, spaces, sign-in methods,
  tokens, open sessions, all their writes — plus the switches below;
- **anyone else**: not found, the same as an id that does not exist. Plain users
  have no directory of people.

Administrators (`user_manager`, `superadmin`) also see **Admin**:

- **Waiting to sign in** — people a provider vouched for whom no rule let in.
  Link them to the suggested account, to **another account** found by search,
  create an account, or reject. See [docs/OIDC.md](OIDC.md#waiting-for-approval).
- **People** — everyone with an account, searchable by name, email or
  department, with their last sign-in and last write. From a person's page:
  edit name, email, department and position; switch the account off or on; end
  their panel sessions; revoke a token; remove a sign-in method (the last one
  too — that is how a wrong link is undone); and, for a superadmin, set the role.

The control plane's rules apply unchanged: a `user_manager` cannot act on — or
see the tokens and sign-in methods of — an administrator's account, only a
superadmin sets roles, and the last active superadmin cannot be demoted or
switched off.

## Security model

- **The panel API is separate.** Everything the browser calls is under
  `/ui/api` and authenticates with the session cookie only. The REST API keeps
  accepting bearer tokens only, so a signed-in browser does not make it
  callable by some other page.
- **Sessions live in the database.** The cookie (`__Host-memgres_session`,
  `HttpOnly`, `Secure`, `SameSite=Strict`) holds a random id; the table holds
  its SHA-256. Every request checks the session again: an account that has been
  disabled, a token session whose token was revoked or whose account lost its
  admin role, a root session after the env token changed — each ends on the
  next request, not at the next sign-in. (When the env token was seeded as the
  bootstrap account's token, a session opened with it follows that token: revoke
  the token to end it, rotating the variable alone does not.)
- **Changes prove where they come from:** an `Origin` equal to
  `MEMGRES_PUBLIC_URL` and the session's CSRF token in `X-Memgres-CSRF`.
- **Memory is read-only by construction.** The session acts on memory with a
  `read` ceiling, so even a bug that routed a write through the panel would be
  refused by the store.
- **Membership and accounts go through the control plane.** Members, requests,
  roles, switching accounts off — every change calls the same service layer as
  REST and MCP (`memgres.admin`), as the account itself. A session counts as a
  full credential there because it can only be opened by one: a provider sign-in,
  or an unscoped admin-ceiling token.
- **Headers.** Panel pages carry a Content-Security-Policy with no inline or
  third-party scripts, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`;
  API answers are `Cache-Control: no-store`.
- **Guessing is slowed.** Ten failed token sign-ins from one address in 15
  minutes block further attempts from it for a while, and one address may start
  at most 30 provider sign-ins per 10 minutes. This is per process and per
  client address — set `MEMGRES_FORWARDED_ALLOW_IPS` behind a proxy, and put a
  rate limit in front of the server as well.
- **Browsing is not usage.** Records opened and searches run in the panel do not
  count toward the usage statistics agents see (`recalled`, `gets`).

## Behind a proxy

- Terminate TLS at the proxy and forward to port 8080.
- Set `MEMGRES_PUBLIC_URL` to the external address (the server will not start
  the panel without it).
- Set `MEMGRES_FORWARDED_ALLOW_IPS` to the proxy's address so the real client
  address is used for throttling.
- Rate-limit `/ui/api/session/token` and `/ui/auth/` at the proxy.
- Serve the panel on its own host name (or at least not next to other
  applications on the same origin): cookies and CSP are per origin.
- MCP is a different process (`memgres-mcp`, port 8765). To serve both on one
  host name, route `/mcp` to it and everything else to the panel — and set
  `MEMGRES_MCP_PUBLIC_URL` to that `/mcp` address so the token dialog shows it.

## Languages

UI strings live in `memgres/web/static/locales/<code>.json`. `en.json` is the
reference; a missing key falls back to English. To add a language, copy
`en.json` to `<code>.json` and translate the values. The picker lists every file
it finds. Plurals are objects keyed by `Intl.PluralRules` categories:

```json
"mem.records": { "one": "{n} record", "other": "{n} records" }
```

Placeholders in braces must stay as they are. Record contents, paths and tags
are data and are never translated.
