# Publishing memgres

Two artifacts ship on a version tag `vX.Y.Z`:

- **Container image** → `ghcr.io/mozgsml/memgres` (built & pushed by `ci.yml`).
- **PyPI package** → `memgres` (built & published by `release.yml`).

## One-time PyPI setup (Trusted Publishing, no tokens)

1. Create a PyPI account: https://pypi.org/account/register/
2. Add a **pending publisher** (works before the project exists) at
   https://pypi.org/manage/account/publishing/ with:
   - **PyPI Project Name:** `memgres`
   - **Owner:** `mozgsml`
   - **Repository name:** `memgres`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi`
3. On GitHub, create an environment named `pypi`
   (repo → Settings → Environments → New environment → `pypi`).

That's it — no API token is stored anywhere; PyPI trusts the GitHub workflow via OIDC.

## Cut a release

```bash
# 1. Bump the version in the ONLY place it's defined: memgres/_version.py
#    (pyproject.toml reads it dynamically via [tool.setuptools.dynamic];
#     nothing else carries the number — don't edit pyproject.toml).
# 2. Move CHANGELOG's [Unreleased] block under a new "## [X.Y.Z] — <date>" heading.
# 3. Tag with the SAME version and push:
git tag vX.Y.Z
git push origin vX.Y.Z
```

The tag triggers:
- `ci.yml` → tests, then builds and pushes the image (`:X.Y.Z`, `:X.Y`, `:latest`).
- `release.yml` → builds sdist+wheel and publishes to PyPI via Trusted Publishing.

After it lands: `pip install memgres` and `docker pull ghcr.io/mozgsml/memgres:X.Y.Z`.

## Alternative: API token instead of Trusted Publishing

If you'd rather not use OIDC, create a PyPI API token and store it as the repo
secret `PYPI_API_TOKEN`, then replace the publish step in `release.yml` with:

```yaml
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          password: ${{ secrets.PYPI_API_TOKEN }}
```

(Drop the `environment:` and `id-token: write` lines in that case.)

## Making the container image public

GHCR packages are private on first push. Make it public once at
`https://github.com/users/mozgsml/packages/container/memgres/settings`
→ *Change visibility* → **Public**, and *Connect repository* → `memgres`.
Then `docker pull ghcr.io/mozgsml/memgres:latest` works with no login.
```
