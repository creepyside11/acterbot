#!/usr/bin/env bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
umask 077
incoming=$1
base=/opt/acter-aster
project=acter-aster

stage() { printf 'ACTER_STAGE=%s\n' "$1"; }
compose() { docker compose -p "$project" --profile https "$@"; }

install_docker() {
    if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
        docker info >/dev/null 2>&1 || systemctl enable --now docker
        docker info >/dev/null
        return
    fi

    . /etc/os-release
    case "${ID:-}" in debian|ubuntu) ;; *) echo "Unsupported distribution"; return 1 ;; esac
    release=${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}
    [[ "$release" =~ ^[a-z0-9]+$ ]]
    apt-get -o DPkg::Lock::Timeout=60 update
    apt-get -o DPkg::Lock::Timeout=60 install -y ca-certificates curl
    if ! grep -hsq 'download.docker.com/linux/' /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; then
        install -m 0755 -d /etc/apt/keyrings
        curl --fail --silent --show-error --location --proto '=https' --max-time 45 \
            "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/acter-docker.asc
        chmod 0644 /etc/apt/keyrings/acter-docker.asc
        cat > /etc/apt/sources.list.d/acter-docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $release
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/acter-docker.asc
EOF
        apt-get -o DPkg::Lock::Timeout=60 update
    fi
    if command -v docker >/dev/null; then
        apt-get -o DPkg::Lock::Timeout=60 install -y docker-compose-plugin
    else
        apt-get -o DPkg::Lock::Timeout=60 install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    fi
    systemctl enable --now docker
    docker info >/dev/null
}

stage preflight
[[ $(id -u) == 0 ]]
install -d -m 0700 "$base" "$base/releases"
exec 8>"$base/install.lock"
flock -n 8

stage docker
install_docker

stage files
release_dir=$(mktemp -d "$base/releases/release.XXXXXXXX")
previous=$(readlink -f "$base/current" 2>/dev/null || true)
[[ -f "$previous/compose.yaml" ]] || previous=

tar -xzf "$incoming/node.tar.gz" -C "$release_dir" --no-same-owner --no-same-permissions
if [[ -n "$previous" ]]; then
    settings='^(TELEGRAM_BOT_TOKEN|TELEGRAM_OWNER_ID|TELEGRAM_POLL|GATEWAY_KEY|PUBLIC_URL|PUBLIC_HOST|TLS_HOST)='
    awk -v settings="$settings" '$0 !~ settings' "$previous/.env" > "$release_dir/.env"
    grep -E "$settings" "$incoming/node.env" >> "$release_dir/.env"
else
    install -m 0600 "$incoming/node.env" "$release_dir/.env"
fi
chmod 0600 "$release_dir/.env"
stage build
if (cd "$release_dir" && compose config --quiet && compose up -d --build --wait --wait-timeout 180); then
    pointer="$base/.$(basename "$release_dir")"
    ln -s "$release_dir" "$pointer"
    mv -Tf "$pointer" "$base/current"
else
    if [[ -n "$previous" ]]; then
        stage rollback
        (cd "$previous" && compose up -d --build --wait --wait-timeout 120) || true
    fi
    exit 1
fi
stage ready
