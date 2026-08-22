# Production deployment

The production path is:

```text
browser --HTTPS--> hosted zrok frontend --OpenZiti--> zrok2-agent on VPS
        --HTTP 127.0.0.1:8000--> rootless Docker port --> FastAPI
                                                     |--> PostgreSQL container
                                                     `--> worker container
```

No application or PostgreSQL port binds to a public VPS interface. FastAPI receives trusted
proxy headers only on this loopback-only production endpoint. The browser sees HTTPS, so the
session cookie is `Secure`; the application already sets `HttpOnly` and `SameSite=Lax`.

## 1. Create the deployment key

Run on the administrator workstation. Do not give this key a passphrase because GitHub Actions
must use it unattended.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/gamerate_deploy -C gamerate-github-actions
```

The private key goes only to the `production` GitHub Environment secret
`PROD_SSH_PRIVATE_KEY`. The public `.pub` file is installed on the VPS by the bootstrap.
After the public key is installed and the private key has been uploaded to GitHub, securely
remove the local private-key copy; it is not copied to the VPS or stored in the repository.

## 2. Bootstrap a clean Ubuntu 24.04 VPS

Copy the bootstrap and the public key through the VPS administrator account, then run it as root:

```bash
scp deploy/bootstrap-ubuntu.sh ~/.ssh/gamerate_deploy.pub ubuntu@VPS_HOST:/tmp/
ssh ubuntu@VPS_HOST \
  'sudo bash /tmp/bootstrap-ubuntu.sh /tmp/gamerate_deploy.pub gamerate-deploy'
```

The script installs only Docker Engine/Compose tooling and zrok on the host. Docker itself runs
rootless under `gamerate-deploy`; this user has no sudo permission and is not in the root-equivalent
`docker` group. PostgreSQL and all Python dependencies remain in containers.

The authorized deployment key is marked `restrict`, which disables PTY, forwarding and agent/X11
forwarding while keeping the SSH commands and SFTP used by the workflow available.

## 3. Install the production environment

Copy the deployment directory, generate a URI-safe database password, and edit the environment:

```bash
scp -r deploy gamerate-deploy@VPS_HOST:gamerate/
ssh gamerate-deploy@VPS_HOST \
  'cp ~/gamerate/deploy/.env.production.example ~/gamerate/deploy/.env && chmod 600 ~/gamerate/deploy/.env'
openssl rand -hex 32
ssh gamerate-deploy@VPS_HOST
```

Put the generated value in `POSTGRES_PASSWORD`, add `GEMINI_API_KEY` and
`GOOGLE_CLOUD_API_KEY`, and keep `WEB_BIND_ADDRESS=127.0.0.1`. The production Compose file forces
`COOKIE_SECURE=true` regardless of the environment file.

## 4. Enable the hosted zrok service

Create an account at `myzrok.io` and copy its account token. On the VPS, as
`gamerate-deploy`, run:

```bash
zrok2 enable YOUR_ACCOUNT_TOKEN
systemctl --user enable --now zrok2-agent.service
zrok2 create name -n public gamerate
zrok2 share public http://127.0.0.1:8000 -n public:gamerate --headless
zrok2 agent status
zrok2 list names
```

If `gamerate` is unavailable, choose another name and use the URL printed by `zrok2 list names`.
The reserved name is restored by the agent after logout or reboot. No inbound firewall rule is
needed for zrok: the agent establishes the connection outbound.

## 5. Configure the GitHub production environment

Create a GitHub Environment named `production`, allow deployments only from `main`, and add:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `PROD_SSH_PRIVATE_KEY` | contents of `~/.ssh/gamerate_deploy` |
| Secret | `PROD_HOST` | VPS IP address or SSH hostname |
| Variable | `PROD_SSH_PORT` | usually `22` |
| Variable | `PROD_SSH_USER` | `gamerate-deploy` |
| Variable | `PROD_PUBLIC_URL` | zrok HTTPS URL, without a trailing slash |
| Secret | `PROD_KNOWN_HOSTS` | verified OpenSSH host-key line for the VPS |

Obtain the host-key fingerprint from the VPS provider console:

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Then run `ssh-keyscan -p 22 VPS_HOST` on the workstation and accept its output for
`PROD_KNOWN_HOSTS` only after its fingerprint matches the console. This avoids disabling SSH host
verification in CI.

The workflow uses the short-lived repository `GITHUB_TOKEN` to pull the private GHCR image on the
VPS, then immediately logs out. No long-lived GHCR token is stored on the server.

## 6. First deployment and user

After these files reach `main`, the normal CI must succeed. The `Production` workflow then builds
an amd64/arm64 image, publishes it to GHCR, deploys its immutable digest and checks `/health`
through zrok.

Create the first application user after the deployment:

```bash
ssh gamerate-deploy@VPS_HOST \
  'cd ~/gamerate/deploy && APP_IMAGE=$(sed -n "s/^APP_IMAGE=//p" .image.env) docker compose --env-file .env --env-file .image.env -f compose.production.yml exec web gamerate create-user admin'
```

The command prompts for the password without echoing it.

Run the external authenticated smoke test from the workstation:

```bash
bash deploy/smoke-test.sh https://YOUR_NAME.share.zrok.io admin
```

It checks the unauthenticated redirect, `Secure`/`HttpOnly`/`SameSite=Lax` cookie attributes,
login redirect, rejected and accepted CSRF tokens, and an authenticated SSE event. Its only valid
mutation is logout; it does not start a crawl or change settings.

## Operations

```bash
# Application status and logs
ssh gamerate-deploy@VPS_HOST \
  'cd ~/gamerate/deploy && docker compose --env-file .env --env-file .image.env -f compose.production.yml ps'
ssh gamerate-deploy@VPS_HOST \
  'cd ~/gamerate/deploy && docker compose --env-file .env --env-file .image.env -f compose.production.yml logs --tail=200 web worker'

# zrok status and logs
ssh gamerate-deploy@VPS_HOST 'zrok2 agent status'
ssh gamerate-deploy@VPS_HOST 'journalctl --user -u zrok2-agent.service -n 200 --no-pager'

# Database backup (keep the resulting file off-server)
ssh gamerate-deploy@VPS_HOST \
  'cd ~/gamerate/deploy && docker compose --env-file .env --env-file .image.env -f compose.production.yml exec -T db pg_dump -U gamerate -d gamerate -Fc' \
  > gamerate.dump
```

At the VPS/provider firewall, allow the chosen SSH port only. Do not allow TCP 8000 or PostgreSQL
5432. The Compose file also enforces the important part locally: port 8000 binds to loopback and
the database has no published port at all.
