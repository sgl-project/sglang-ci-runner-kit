#!/usr/bin/env bash
# Vendor-side loop: fetch a JIT config, run exactly one job, repeat.
# Run it as a systemd service, one instance per runner slot on the host.
#
# RUNNER_MODE=container (mode A): the job itself runs on this host, so each
#   runner lives in a fresh `docker run --rm` container. Use this when PR code
#   executes here. The runner process runs inside the container and the key
#   stays on the host: the loop refuses DOCKER_ARGS that would expose it
#   (a mount of the key or a directory above it, --privileged, the docker socket).
# RUNNER_MODE=host (mode B): the runner is a thin client that hands the job to
#   an external scheduler (e.g. Jenkins) and never checks out PR code. Each round
#   starts from a clean copy of the runner install, run as RUNNER_USER, which
#   must not be the user running this loop and must not be able to read the key.
#   The loop user needs passwordless `sudo -u $RUNNER_USER` (root has it).
#
# After a failed request, or a job that ends within MIN_JOB_SECONDS, the loop
# backs off exponentially with full jitter (up to BACKOFF_MAX_SECONDS), so a
# crash-looping host doesn't eat the broker's shared request budget.
#
# Required env:
#   BROKER_URL      the broker's base URL from sglang, e.g. https://<broker>
#   VENDOR          the vendor's short name
#   KID             this host's key id, e.g. <vendor>-host01-2026-11
#   KEY_PATH        this host's private key (.key from `jit_client.py keygen`)
#   RUNNER_LABEL    e.g. <vendor>-1card
# Mode A:  RUNNER_IMAGE, plus DOCKER_ARGS for devices and read-only mounts
# Mode B:  RUNNER_DIR, a pristine actions/runner install readable by RUNNER_USER;
#          RUNNER_USER
set -euo pipefail

: "${BROKER_URL:?}" "${VENDOR:?}" "${KID:?}" "${KEY_PATH:?}" "${RUNNER_LABEL:?}"
RUNNER_MODE=${RUNNER_MODE:-container}
# Split on whitespace only; no glob expansion. Quoting inside DOCKER_ARGS is not
# supported, so paths with spaces can't be mounted.
read -r -a docker_args <<<"${DOCKER_ARGS:-}"
RUNNER_HOST=${RUNNER_HOST:-$(hostname -s)}
CLIENT=${CLIENT:-$(dirname "$0")/jit_client.py}
MIN_JOB_SECONDS=${MIN_JOB_SECONDS:-30}
BACKOFF_BASE_SECONDS=${BACKOFF_BASE_SECONDS:-5}
BACKOFF_MAX_SECONDS=${BACKOFF_MAX_SECONDS:-600}

die() { echo "$*" >&2; exit 1; }

# True if the path $1 is $2 or a directory above it.
covers() {
  local src=${1%/}
  [[ -z $src || $2 == "$src" || $2 == "$src"/* ]]
}

# Mode A: the container must not be able to reach the key. This catches
# configuration mistakes; it is not a sandbox for hostile DOCKER_ARGS.
check_docker_args() {
  local key arg src next=
  key=$(realpath "$KEY_PATH")
  for arg in ${docker_args[@]+"${docker_args[@]}"}; do
    src=
    if [[ -n $next ]]; then
      [[ $next == mount ]] && src=$(sed -nE 's/(^|.*,)(source|src)=([^,]*).*/\3/p' <<<"$arg") || src=${arg%%:*}
      next=
    else
      case $arg in
        -v|--volume) next=volume ;;
        --mount) next=mount ;;
        -v*|--volume=*) arg=${arg#--volume=}; arg=${arg#-v}; src=${arg%%:*} ;;
        --mount=*) src=$(sed -nE 's/(^|.*,)(source|src)=([^,]*).*/\3/p' <<<"${arg#--mount=}") ;;
        --privileged|--privileged=true) die "DOCKER_ARGS: --privileged would expose the host and its key" ;;
        *docker.sock*) die "DOCKER_ARGS: the docker socket would expose the host and its key" ;;
      esac
    fi
    [[ $src == *docker.sock* ]] && die "DOCKER_ARGS: the docker socket would expose the host and its key"
    if [[ $src == /* ]] && covers "$(realpath -m "$src")" "$key"; then
      die "DOCKER_ARGS mounts $src, which contains the key $KEY_PATH"
    fi
  done
}

# Mode B: the runner must run as a different user that cannot read the key.
check_runner_user() {
  : "${RUNNER_USER:?mode B needs RUNNER_USER, a user other than $(id -un)}"
  [[ $RUNNER_USER != "$(id -un)" ]] || die "RUNNER_USER must not be the user running this loop ($(id -un))"
  sudo -n -u "$RUNNER_USER" true || die "$(id -un) needs passwordless sudo -u $RUNNER_USER"
  if sudo -n -u "$RUNNER_USER" test -r "$KEY_PATH"; then
    die "RUNNER_USER=$RUNNER_USER can read $KEY_PATH; make the key readable by $(id -un) only"
  fi
}

case "$RUNNER_MODE" in
  container) : "${RUNNER_IMAGE:?}"; check_docker_args ;;
  host) : "${RUNNER_DIR:?}"; check_runner_user ;;
  *) die "unknown RUNNER_MODE=$RUNNER_MODE" ;;
esac

failures=0
backoff() {
  local cap=$((BACKOFF_BASE_SECONDS << (failures < 10 ? failures : 10)))
  ((cap > BACKOFF_MAX_SECONDS)) && cap=$BACKOFF_MAX_SECONDS
  failures=$((failures + 1))
  local delay=$((RANDOM % (cap + 1)))
  echo "$1; retrying in ${delay}s (failure $failures)" >&2
  sleep "$delay"
}

while true; do
  if ! cfg=$(python3 "$CLIENT" request --url "$BROKER_URL" --vendor "$VENDOR" \
      --kid "$KID" --key "$KEY_PATH" --name "$RUNNER_HOST" --label "$RUNNER_LABEL"); then
    backoff "could not get a JIT config"
    continue
  fi

  started=$SECONDS
  case "$RUNNER_MODE" in
    container)
      # No --privileged, no docker socket; shared caches mounted read-only.
      docker run --rm ${docker_args[@]+"${docker_args[@]}"} "$RUNNER_IMAGE" ./run.sh --jitconfig "$cfg" || true
      ;;
    host)
      work=$(sudo -n -u "$RUNNER_USER" mktemp -d)
      sudo -n -u "$RUNNER_USER" cp -a "$RUNNER_DIR/." "$work/"
      # shellcheck disable=SC2016
      sudo -n -u "$RUNNER_USER" bash -c 'cd "$1" && exec ./run.sh --jitconfig "$2"' _ "$work" "$cfg" || true
      sudo -n -u "$RUNNER_USER" rm -rf "$work"
      ;;
  esac

  if ((SECONDS - started < MIN_JOB_SECONDS)); then
    backoff "runner exited after $((SECONDS - started))s"
  else
    failures=0
  fi
done
