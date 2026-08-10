#!/usr/bin/env bash
##############################################################################
# ONE-SHOT DEPENDENCY INSTALLER for the tk-fe-daq monitoring stack.
#
#   ./install.sh          install whatever is missing, then verify
#   ./install.sh --check  verify only, change nothing (safe on a shared box)
#   ./install.sh --pull   also download the Prometheus/Grafana images NOW, so a
#                         later ./on.sh works with no route to Docker Hub
#   ./install.sh --pull --check   just report which images are already local
#
# Installs: docker engine, the docker compose plugin, curl, jq, gettext
# (envsubst), openssh-client. Everything else the stack needs lives inside the
# two container images.
#
# BEHIND A PROXY (typical at CERN / on a beam-line subnet) export these FIRST,
# both for this script and for the docker daemon:
#   export https_proxy=http://proxy.example.cern.ch:3128 http_proxy=$https_proxy
#   sudo mkdir -p /etc/systemd/system/docker.service.d
#   printf '[Service]\nEnvironment="HTTPS_PROXY=%s"\n' "$https_proxy" \
#       | sudo tee /etc/systemd/system/docker.service.d/proxy.conf
#   sudo systemctl daemon-reload && sudo systemctl restart docker
##############################################################################
set -uo pipefail
cd "$(dirname "$0")"

CHECK_ONLY=0
DO_PULL=0
for a in "$@"; do
    case "$a" in
        --check) CHECK_ONLY=1 ;;
        --pull)  DO_PULL=1 ;;
        -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "unknown option: $a (try --help)" >&2; exit 2 ;;
    esac
done

bold=$(tput bold 2>/dev/null || true); red=$(tput setaf 1 2>/dev/null || true)
grn=$(tput setaf 2 2>/dev/null || true); ylw=$(tput setaf 3 2>/dev/null || true)
rst=$(tput sgr0 2>/dev/null || true)
step() { echo; echo "${bold}== $* ==${rst}"; }
ok()   { echo "  ${grn}OK${rst}    $*"; }
warn() { echo "  ${ylw}WARN${rst}  $*"; }
bad()  { echo "  ${red}FAIL${rst}  $*"; FAILED=1; }
FAILED=0

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null && SUDO="sudo" || {
        echo "${red}Not root and no sudo. Re-run as root, or ask an admin to run this once.${rst}" >&2; exit 1; }
fi

##############################################################################
step "1/5  Which machine is this?"
##############################################################################
. /etc/os-release 2>/dev/null || true
echo "  distro: ${PRETTY_NAME:-unknown}   kernel: $(uname -r)   arch: $(uname -m)"

PKG=""
for p in apt-get dnf yum pacman zypper; do command -v "$p" >/dev/null && { PKG="$p"; break; }; done
[ -n "$PKG" ] && echo "  package manager: $PKG" || warn "no known package manager found - you will have to install by hand"

# distro name -> package name, per manager
pkgs_for() {
    case "$PKG" in
        apt-get) echo "curl jq gettext-base openssh-client ca-certificates" ;;
        dnf|yum) echo "curl jq gettext openssh-clients ca-certificates" ;;
        pacman)  echo "curl jq gettext openssh ca-certificates" ;;
        zypper)  echo "curl jq gettext-runtime openssh-clients ca-certificates" ;;
    esac
}
pkg_install() {
    case "$PKG" in
        apt-get) $SUDO apt-get update -qq && $SUDO apt-get install -y "$@" ;;
        dnf)     $SUDO dnf install -y "$@" ;;
        yum)     $SUDO yum install -y "$@" ;;
        pacman)  $SUDO pacman -Sy --noconfirm --needed "$@" ;;
        zypper)  $SUDO zypper --non-interactive install "$@" ;;
        *)       return 1 ;;
    esac
}

##############################################################################
step "2/5  Small tools (curl, jq, envsubst, ssh)"
##############################################################################
# curl   - check.sh talks to the exporter and to the Prometheus API
# jq     - check.sh parses the Prometheus target list
# envsubst - render.sh bakes .env into prometheus.yml
# ssh/ssh-keygen - only needed when USE_TUNNEL=yes
missing=()
for c in curl jq envsubst ssh-keygen; do command -v "$c" >/dev/null || missing+=("$c"); done
if [ ${#missing[@]} -eq 0 ]; then
    ok "curl, jq, envsubst, ssh-keygen all present"
elif [ "$CHECK_ONLY" -eq 1 ]; then
    bad "missing: ${missing[*]}  (re-run without --check to install)"
else
    echo "  missing: ${missing[*]} -> installing"
    if pkg_install $(pkgs_for); then ok "installed"; else bad "install failed - install these by hand: ${missing[*]}"; fi
fi

##############################################################################
step "3/5  Docker engine + compose plugin"
##############################################################################
install_docker_from_distro() {
    case "$PKG" in
        apt-get) pkg_install docker.io docker-compose-v2 || pkg_install docker.io ;;
        dnf|yum) pkg_install docker docker-compose-plugin || pkg_install docker ;;
        pacman)  pkg_install docker docker-compose ;;
        zypper)  pkg_install docker docker-compose ;;
        *) return 1 ;;
    esac
}
install_docker_upstream() {
    # Needs a route to download.docker.com. Used only when the distro packages
    # are absent or too old to ship `docker compose` (v2).
    echo "  falling back to the official Docker install script (needs download.docker.com)"
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh || return 1
    $SUDO sh /tmp/get-docker.sh || return 1
    pkg_install docker-compose-plugin || true
}

if command -v docker >/dev/null; then
    ok "docker present: $(docker --version 2>/dev/null)"
elif [ "$CHECK_ONLY" -eq 1 ]; then
    bad "docker is NOT installed (re-run without --check to install)"
else
    install_docker_from_distro || install_docker_upstream \
        || bad "could not install docker - see https://docs.docker.com/engine/install/"
    command -v docker >/dev/null && ok "docker installed: $(docker --version 2>/dev/null)"
fi

if command -v docker >/dev/null; then
    # `docker compose` (v2, a plugin) - NOT the old `docker-compose` python script.
    if docker compose version >/dev/null 2>&1; then
        ok "compose plugin present: $(docker compose version --short 2>/dev/null)"
    elif [ "$CHECK_ONLY" -eq 1 ]; then
        bad "'docker compose' (v2 plugin) missing"
    else
        pkg_install docker-compose-plugin || pkg_install docker-compose-v2 || true
        if docker compose version >/dev/null 2>&1; then
            ok "compose plugin installed"
        else
            # Last resort: drop the static plugin binary in place ourselves.
            arch="$(uname -m)"; case "$arch" in aarch64|arm64) arch=aarch64 ;; *) arch=x86_64 ;; esac
            $SUDO mkdir -p /usr/local/lib/docker/cli-plugins
            if $SUDO curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
                    "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${arch}" \
               && $SUDO chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
               && docker compose version >/dev/null 2>&1; then
                ok "compose plugin installed from GitHub release"
            else
                bad "no 'docker compose' available - install the compose v2 plugin by hand"
            fi
        fi
    fi
fi

##############################################################################
step "4/5  Docker daemon running, and usable without sudo"
##############################################################################
if command -v docker >/dev/null; then
    if [ "$CHECK_ONLY" -eq 0 ] && command -v systemctl >/dev/null; then
        $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
    fi
    if docker info >/dev/null 2>&1; then
        ok "daemon is up and this user can talk to it"
    elif $SUDO docker info >/dev/null 2>&1; then
        # Works with sudo but not as this user -> group membership, not a daemon problem.
        if [ "$CHECK_ONLY" -eq 1 ]; then
            warn "daemon up, but '$USER' is not in the 'docker' group (would need sudo)"
        else
            $SUDO usermod -aG docker "${USER:-$(id -un)}" 2>/dev/null \
                && warn "added '$USER' to the 'docker' group - ${bold}LOG OUT AND BACK IN${rst} (or run: newgrp docker) then re-run ./install.sh --check" \
                || warn "could not add '$USER' to the docker group; run the stack with sudo"
        fi
    else
        bad "docker daemon is not running - try: $SUDO systemctl start docker  (then: journalctl -u docker -n 50)"
    fi
fi

##############################################################################
step "5/5  Container images"
##############################################################################
[ -f .env ] && set -a && . ./.env && set +a
PROM_IMG="${PROMETHEUS_IMAGE:-prom/prometheus:latest}"
GRAF_IMG="${GRAFANA_IMAGE:-grafana/grafana:latest}"
have_img() { docker image inspect "$1" >/dev/null 2>&1; }

if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
    warn "skipped - docker not usable yet"
elif [ "$DO_PULL" -eq 1 ] && [ "$CHECK_ONLY" -eq 0 ]; then
    for img in "$PROM_IMG" "$GRAF_IMG"; do
        echo "  pulling $img ..."
        docker pull "$img" >/dev/null 2>&1 && ok "$img cached locally" \
            || bad "pull failed for $img - no route to the registry? set https_proxy (see the header of this file) and retry"
    done
else
    for img in "$PROM_IMG" "$GRAF_IMG"; do
        have_img "$img" && ok "$img already local" \
            || warn "$img not local yet - ./on.sh will pull it (needs the registry). Pre-download now with: ./install.sh --pull"
    done
fi

##############################################################################
echo
if [ "$FAILED" -eq 0 ]; then
    echo "${grn}${bold}Dependencies OK.${rst}"
    echo
    echo "Next:"
    echo "  1.  cp .env.example .env      # if you have not already"
    echo "  2.  edit .env                 # NETWORK BLOCK 1: where the DAQ exposer lives"
    echo "  3.  ./check.sh                # is that address actually reachable from here?"
    echo "  4.  ./render.sh && ./on.sh"
else
    echo "${red}${bold}Some dependencies are missing - see the FAIL lines above.${rst}"
    exit 1
fi
