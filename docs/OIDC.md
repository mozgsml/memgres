# Signing in with OpenID Connect

The web panel ([docs/WEB.md](WEB.md)) lets people sign in through any standard
OpenID Connect provider — Zitadel, Keycloak, Authentik, Entra ID, Google, Okta,
Auth0 — and as many of them at once as you like.

**Signing in is not being let in.** A provider says who someone is. memgres
decides whether they get an account, and every provider is configured on its
own: an employee directory can be trusted to create accounts, Google cannot.

> GitHub sign-in is OAuth 2.0 without OpenID Connect and is not supported.

## The provider file

`MEMGRES_OIDC_CONFIG` points at a TOML file. One table per provider; the table
name is the provider's id and appears in its redirect URI.

```toml
[providers.corp]
label = "Company SSO"
hint = "Employees — your account opens right away"
issuer = "https://id.example.com"
client_id = "memgres"
client_secret_file = "/run/secrets/corp-oidc"
allowed_email_domains = ["example.com"]
on_email_match = "link"
on_no_match = "create"

[[providers.corp.require]]
claim = "urn:zitadel:iam:org:project:roles"
has = "memgres"

[providers.google]
label = "Google"
issuer = "https://accounts.google.com"
client_id = "1234-abc.apps.googleusercontent.com"
client_secret_file = "/run/secrets/google-oidc"
# managed defaults: an email match waits for approval, anyone else is refused
```

The file is read at startup. Anything wrong with it — an unknown key, a missing
`client_id`, an unreadable secret file — stops the server with a message naming
the problem, rather than leaving a sign-in door that half works.

| Key | Default | |
|---|---|---|
| `issuer` | required | exactly as the provider publishes it, trailing slash included. Must be `https://` |
| `client_id` | required | |
| `client_secret_file` | — | a file holding the client secret. Omit for a public client: the flow uses PKCE either way |
| `token_auth` | `client_secret_basic` with a secret, `none` without | or `client_secret_post` |
| `label`, `hint` | the id, empty | what the sign-in button says |
| `scopes` | `["openid", "email", "profile"]` | must include `openid` |
| `require` | none | conditions on the claims; **all** must hold. See below |
| `allowed_email_domains` | any | the person's **verified** email must be at one of these |
| `on_email_match` | `pending` (managed) / `link` (open) | a verified email equals an account's email: `link`, `pending` or `deny` |
| `on_no_match` | `deny` (managed) / `create` (open) | nobody matches: `create`, `pending` or `deny` |
| `prompt` | `select_account` | what to ask the provider before it answers: `select_account` (show its account chooser), `login` (re-authenticate every time), or `""` (ask nothing) |
| `sync_on_login` | `false` | see [Taking access back](#taking-access-back) |
| `allow_http` | `false` | allow an `http://` issuer — for a local test provider only |

Register this redirect URI at the provider:

```
{MEMGRES_PUBLIC_URL}/ui/auth/<provider id>/callback
```

### `require`

Each condition names a claim and what it must contain:

```toml
[[providers.corp.require]]
claim = "groups"                    # a key, or a list of keys for a nested claim
has = "memgres-users"               # list contains it / object has it as a key / string equals it

[[providers.corp.require]]
claim = ["org", "id"]
equals = "123456"                   # exactly this value
```

Claims come from the verified `id_token`, filled in from the userinfo endpoint
(some providers put roles or groups only there). Where both carry a claim, the
signed `id_token` wins. memgres checks the conditions itself even if the provider
can also refuse: the rule does not depend on a switch at the provider.

Typical rules:

- **Zitadel**: project role — `claim = "urn:zitadel:iam:org:project:roles"`, `has = "memgres"`; organization — `claim = "urn:zitadel:iam:user:resourceowner:id"`, `equals = "<org id>"`.
- **Keycloak / Authentik**: a group — `claim = "groups"`, `has = "memgres-users"` (add a groups mapper to the client).
- **Entra ID**: an app role — `claim = "roles"`, `has = "memgres.user"`.
- **Require MFA** where the provider reports it: `claim = "amr"`, `has = "mfa"`.

## Which account the provider hands back

A browser usually holds more than one session at the identity provider — a
service admin used once for setup, a colleague who borrowed the laptop. Asked
nothing, the provider picks one of them and does not say which, so the person
can arrive as an account that is not theirs. In a managed deployment that
account is unlinked, so it queues for an administrator — and if the person
queuing *is* the administrator, they have locked themselves out with no way to
choose differently. Switching account in the provider's own interface does not
help: the other session stays valid, and it is what the next sign-in returns.

So memgres sends `prompt=select_account` on both signing in and linking, and
the provider shows its chooser. Set `prompt = ""` for a provider that always
shows one anyway, or `prompt = "login"` to force re-authentication.

If you are already stuck this way: sign in through `/signin/admin` with an
administrator's token, or open the provider in a private window, where it has
no session to reuse.

## Who gets in

In order:

1. **The provider's rules.** `require` and `allowed_email_domains` must pass.
   If they do not, sign-in is refused and **nothing is created** — no account,
   no request.
2. **A linked sign-in.** If this (issuer, subject) is already linked to an
   account, that account signs in, unless it is disabled. The email plays no
   part here: people change addresses.
3. **A verified email that matches an account** → `on_email_match`:
   - `link` — link and sign in;
   - `pending` — ask an administrator, suggesting that account;
   - `deny` — refuse.

   Two exceptions turn `link` into `pending` whatever the setting: the matching
   account is an **administrator** (having a mailbox with the admin's address at
   some provider must not make anyone the admin), or it **already has a sign-in
   from this provider** (a second account at the same provider is not the same
   person by default).
4. **Nobody matches** → `on_no_match`:
   - `create` — a new account with the role `user` and **no spaces**. Access to
     memory is still something a person grants;
   - `pending` — ask an administrator;
   - `deny` — refuse.

An email counts only when the provider marks it verified (`email_verified`).

**Invitations come after this, not instead of it.** When a space administrator
invites an address that has no account ([docs/WEB.md](WEB.md#members-of-a-space)),
the invitation is applied only once a sign-in through these steps has let the
person in, and only if the provider marks that address verified. It never
creates an account, never turns `deny` into `pending`, and never links anything.

Trusting a provider's `email_verified` is trusting whoever can set it. At an
employee directory (Zitadel, Keycloak, Entra ID) an administrator can usually
mark an address verified by hand; that is fine when those administrators are
yours, and a reason to keep an open provider on `pending` or `deny`.

### Waiting for approval

A `pending` sign-in creates a request, not an account. The person sees that
they are waiting; administrators see it under **Admin** with a count in the
sidebar, and decide:

- **Link** to the suggested account, or to **another account** found by name or
  email (only a superadmin may link to an administrator account);
- **Create account** — a plain user with no spaces;
- **Reject** — the same person cannot request again for 30 days.

If someone has invited the person's address into a space, the request says so —
which space, and who invited them. Approving does not apply the invitation by
itself: it applies when they next sign in.

Signing in again while waiting does not create a second request. A request
needs an email the provider marks verified — without one, sign-in is simply
refused. At most 100 requests per provider and 500 in total can be open at once;
past that, new ones are refused. Decided requests are removed after 90 days.

## Linking more than one provider

One account can have any number of sign-in methods. A signed-in person links
another in **Account → Sign-in methods**: they are sent to the provider and
back, and the method is added to *this* account whatever its email says. They
can unlink any method but the last. Unlinking ends the sessions opened with it.

Starting a link is a CSRF-checked request, not a link any page could follow, and
the session that started it must still be valid when the provider sends the
person back: signing out, a revoked token or a disabled account in those minutes
stops the link.

A sign-in already linked to a different account is never moved.

## Taking access back

Removing someone's role at the provider stops their **next** sign-in, because
the rules are checked every time. What it cannot do on its own is reach tokens
they already issued, which keep working until they expire (panel tokens always
expire, in at most a year).

With `sync_on_login = true`, a person who signs in and no longer meets the
provider's rules also has their sessions ended and **all their tokens revoked**
— except the server's own bootstrap admin token (label `bootstrap`). That still needs them to try
signing in. To cut someone off at once, disable the account (REST
`POST /admin/users/{id}/disabled` or MCP `memory_admin_set_disabled`): every
token and session stops on the next request.

## How the flow is protected

- Authorization Code flow with PKCE (`S256`), a random `state` and `nonce`.
- `state` is single use, expires in 10 minutes, and is bound to the browser that
  started the sign-in by a separate cookie — a callback URL opened in another
  browser completes nothing (this is what stops someone signing you in as them).
- The `id_token` is verified against the provider's published keys: asymmetric
  signatures only (never `none`, never a shared-secret HMAC), `iss` exactly,
  `aud` containing the client id (and `azp` when there are several audiences),
  `exp`/`iat` with a minute of clock skew, and the `nonce`.
- The discovery document must name the configured issuer; userinfo must be for
  the same subject.
- Discovery and keys are cached for an hour; a token naming an unknown key
  triggers one refetch, at most once a minute.
- One client address may start at most 30 sign-ins per 10 minutes, and at most
  10 000 can be in progress at once.
- OIDC requires secure cookies except on `localhost`: without `__Host-` cookies a
  sibling subdomain could plant its own flow cookie.
