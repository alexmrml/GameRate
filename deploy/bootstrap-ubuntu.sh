#!/usr/bin/env bash
set -Eeuo pipefail

readonly PUBLIC_KEY_FILE="${1:-}"
readonly DEPLOY_USER="${2:-gamerate-deploy}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "run this script as root" >&2
  exit 2
fi
if [[ ! -f "${PUBLIC_KEY_FILE}" ]]; then
  echo "usage: $0 /path/to/deploy-key.pub [deploy-user]" >&2
  exit 2
fi
if [[ ! "${DEPLOY_USER}" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
  echo "invalid deployment username" >&2
  exit 2
fi

source /etc/os-release
if [[ "${ID}" != "ubuntu" || "${VERSION_ID}" != "24.04" ]]; then
  echo "this bootstrap supports Ubuntu 24.04 only" >&2
  exit 2
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl dbus-user-session gnupg uidmap

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

zrok_key="$(mktemp)"
trap 'rm -f "${zrok_key}"' EXIT
curl -fsSL https://get.openziti.io/tun/package-repos.gpg -o "${zrok_key}"
gpg --dearmor --yes --output /usr/share/keyrings/openziti.gpg "${zrok_key}"
chmod a+r /usr/share/keyrings/openziti.gpg
cat >/etc/apt/sources.list.d/openziti-release.list <<'EOF'
deb [signed-by=/usr/share/keyrings/openziti.gpg] https://packages.openziti.org/zitipax-openziti-deb-stable debian main
EOF

apt-get update
apt-get install -y \
  docker-ce docker-ce-cli docker-buildx-plugin docker-compose-plugin docker-ce-rootless-extras \
  zrok2-agent

# The deployment account uses its own unprivileged Docker daemon. The root daemon is not used.
systemctl disable --now docker.service docker.socket || true
systemctl mask docker.service docker.socket
if [[ -S /var/run/docker.sock ]]; then
  rm -f /var/run/docker.sock
fi

if ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${DEPLOY_USER}"
fi
readonly DEPLOY_HOME="$(getent passwd "${DEPLOY_USER}" | cut -d: -f6)"
readonly DEPLOY_UID="$(id -u "${DEPLOY_USER}")"

install -d -m 0700 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "${DEPLOY_HOME}/.ssh"
{
  printf 'restrict '
  cat "${PUBLIC_KEY_FILE}"
} >"${DEPLOY_HOME}/.ssh/authorized_keys"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_HOME}/.ssh/authorized_keys"
chmod 0600 "${DEPLOY_HOME}/.ssh/authorized_keys"

loginctl enable-linger "${DEPLOY_USER}"
systemctl start "user@${DEPLOY_UID}.service"

as_deploy() {
  runuser -u "${DEPLOY_USER}" -- env \
    HOME="${DEPLOY_HOME}" \
    USER="${DEPLOY_USER}" \
    XDG_RUNTIME_DIR="/run/user/${DEPLOY_UID}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${DEPLOY_UID}/bus" \
    "$@"
}

if ! as_deploy docker context inspect rootless >/dev/null 2>&1; then
  as_deploy dockerd-rootless-setuptool.sh install
fi
as_deploy systemctl --user enable --now docker.service
as_deploy docker context use rootless

install -d -m 0700 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" \
  "${DEPLOY_HOME}/gamerate/deploy"

cat <<EOF

Bootstrap complete.

Deployment user: ${DEPLOY_USER}
Application dir: ${DEPLOY_HOME}/gamerate/deploy

Rootless Docker is running. zrok2-agent is installed but is intentionally not enabled yet;
enable it after supplying the account token as ${DEPLOY_USER}.
EOF
