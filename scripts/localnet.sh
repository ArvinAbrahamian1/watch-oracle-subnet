#!/usr/bin/env bash
# Run a local subtensor chain (pow-faucet enabled) in Docker for subnet development.
#
# Usage: scripts/localnet.sh [start|stop|reset]
#   start  (default) install Docker if missing, start the chain in the background,
#          wait until it produces blocks, print the ws endpoint
#   stop   stop the container, keep chain state
#   reset  delete the container and its chain state (next start is a fresh genesis)
#
# Env:
#   LOCALNET_IMAGE  default ghcr.io/raofoundation/subtensor-localnet:devnet
#   FAST_BLOCKS     True (0.25s blocks, default) or False (12s mainnet-like blocks)
#   BIND_ADDR       host address for the RPC ports, default 127.0.0.1 (the node
#                   runs with unsafe RPC, so only use 0.0.0.0 behind a firewall)
#   WAIT_SECONDS    how long to wait for block production, default 300
set -euo pipefail

IMAGE="${LOCALNET_IMAGE:-ghcr.io/raofoundation/subtensor-localnet:devnet}"
FAST_BLOCKS="${FAST_BLOCKS:-True}"
BIND_ADDR="${BIND_ADDR:-127.0.0.1}"
WAIT_SECONDS="${WAIT_SECONDS:-300}"
CONTAINER=subtensor-localnet
VOLUME=subtensor-localnet-data
RPC_PORT=9944

log() { printf '[localnet] %s\n' "$*" >&2; }
die() { printf '[localnet] error: %s\n' "$*" >&2; exit 1; }

[[ "$FAST_BLOCKS" == True || "$FAST_BLOCKS" == False ]] || die "FAST_BLOCKS must be True or False"
SUDO=""
[[ $EUID -eq 0 ]] || SUDO=sudo

install_docker() {
  # Docker Engine from Docker's apt repo, per docs.docker.com/engine/install/ubuntu
  # shellcheck source=/dev/null
  [[ -r /etc/os-release ]] && . /etc/os-release
  [[ "${ID:-}" == ubuntu ]] || die "Docker is missing and auto-install only supports Ubuntu"
  log "installing Docker Engine"
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq ca-certificates curl
  $SUDO install -m 0755 -d /etc/apt/keyrings
  $SUDO curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  $SUDO chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io
  $SUDO systemctl enable --now docker
}

command -v docker >/dev/null 2>&1 || install_docker
command -v curl >/dev/null 2>&1 || { $SUDO apt-get update -qq && $SUDO apt-get install -y -qq curl; }
# Use sudo for docker until the user is in the docker group (needs a re-login).
DOCKER=(docker)
docker info >/dev/null 2>&1 || DOCKER=(${SUDO:+"$SUDO"} docker)
"${DOCKER[@]}" info >/dev/null 2>&1 || die "cannot reach the Docker daemon"

container_state() { "${DOCKER[@]}" inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing; }

# Host the RPC is reachable on from this machine.
rpc_host="$BIND_ADDR"
[[ "$rpc_host" == 0.0.0.0 ]] && rpc_host=127.0.0.1

rpc() {
  curl -fsS -m 3 -H 'Content-Type: application/json' \
    -d "{\"id\":1,\"jsonrpc\":\"2.0\",\"method\":\"$1\",\"params\":[]}" \
    "http://$rpc_host:$RPC_PORT"
}

best_block() {
  local hex
  hex=$(rpc chain_getHeader 2>/dev/null | grep -o '"number":"0x[0-9a-fA-F]*"' | grep -o '0x[0-9a-fA-F]*') || return 1
  echo $((hex))
}

start() {
  case "$(container_state)" in
    running) log "container $CONTAINER already running" ;;
    missing)
      log "pulling $IMAGE"
      "${DOCKER[@]}" pull -q "$IMAGE" >/dev/null
      log "starting $CONTAINER (fast blocks: $FAST_BLOCKS)"
      # The named volume over /tmp holds node state; --no-purge keeps it across restarts.
      "${DOCKER[@]}" run -d --name "$CONTAINER" --restart unless-stopped \
        -p "$BIND_ADDR:9944:9944" -p "$BIND_ADDR:9945:9945" \
        -v "$VOLUME:/tmp" \
        "$IMAGE" "$FAST_BLOCKS" --no-purge >/dev/null
      ;;
    *)
      log "restarting existing container $CONTAINER"
      "${DOCKER[@]}" start "$CONTAINER" >/dev/null
      ;;
  esac

  local running_mode
  running_mode=$("${DOCKER[@]}" inspect -f '{{index .Args 0}}' "$CONTAINER")
  [[ "$running_mode" == "$FAST_BLOCKS" ]] \
    || die "existing chain runs with fast blocks $running_mode, not $FAST_BLOCKS; run '$0 reset' first"

  log "waiting for block production (up to ${WAIT_SECONDS}s)"
  local deadline=$((SECONDS + WAIT_SECONDS)) block
  until block=$(best_block) && ((block > 1)); do
    if [[ "$(container_state)" != running ]]; then
      "${DOCKER[@]}" logs --tail 50 "$CONTAINER" >&2 || true
      die "container exited; logs above"
    fi
    ((SECONDS < deadline)) || die "no blocks after ${WAIT_SECONDS}s; check: ${DOCKER[*]} logs $CONTAINER"
    sleep 2
  done

  log "chain is producing blocks (best block #$block)"
  echo "ws://$rpc_host:$RPC_PORT"
}

case "${1:-start}" in
  start) start ;;
  stop) "${DOCKER[@]}" stop "$CONTAINER" >/dev/null && log "stopped (state kept)" ;;
  reset)
    "${DOCKER[@]}" rm -f "$CONTAINER" >/dev/null 2>&1 || true
    "${DOCKER[@]}" volume rm "$VOLUME" >/dev/null 2>&1 || true
    log "removed container and chain state"
    ;;
  *) die "usage: $0 [start|stop|reset]" ;;
esac
