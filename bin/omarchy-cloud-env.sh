#!/bin/bash
#
# Environments: where rclone runs and where a mount lives.
#
# A service is connected in exactly one environment -- this machine, or a
# container that keeps its own rclone configuration and its own mount
# namespace (Distrobox, Docker, Podman). The host-side systemd unit still
# supervises every mount; for a container it runs rclone *inside* the container
# and watches for the mountpoint there. Credentials never leave the environment
# they were created in.
#
# Sourced, not executed. Everything here is a thin, argv-safe wrapper around
# the container tools: no command is ever rebuilt as a shell string.

# ---------------------------------------------------------------- kinds

ENV_KINDS="host distrobox docker podman"

env_valid_kind() {
  case "$1" in
    host|distrobox|docker|podman) return 0 ;;
    *) return 1 ;;
  esac
}

# Container names become part of unit instance names and are passed to the
# container tools verbatim, so keep them boring.
env_valid_name() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
}

# "Host", "Distrobox sfl", "Docker nextcloud-dev"
env_label() {
  local kind="$1" name="${2:-}"
  case "$kind" in
    host|"") echo "Host" ;;
    distrobox) echo "Distrobox $name" ;;
    docker)    echo "Docker $name" ;;
    podman)    echo "Podman $name" ;;
    *)         echo "$kind $name" ;;
  esac
}

env_tool_available() {
  case "$1" in
    host) return 0 ;;
    distrobox|docker|podman) command -v "$1" >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------- exec

# env_exec KIND NAME USER COMMAND [ARGS...]
#
# Runs COMMAND inside the environment with stdin, stdout and stderr passed
# through, so interactive rclone flows (2FA prompts, OAuth) work unchanged.
# USER only applies to docker and podman; Distrobox already runs as you.
env_exec() {
  local kind="$1" name="$2" user="$3"
  shift 3
  case "$kind" in
    host|"")
      "$@"
      ;;
    distrobox)
      distrobox enter --name "$name" -- "$@"
      ;;
    docker|podman)
      local -a opts=(exec -i)
      # A terminal is only useful when there is one; passing -t without one
      # makes docker refuse to start the command.
      [[ -t 0 && -t 1 ]] && opts+=(-t)
      [[ -n "$user" ]] && opts+=(-u "$user")
      "$kind" "${opts[@]}" "$name" "$@"
      ;;
    *)
      echo "omarchy-cloud: unknown environment kind: $kind" >&2
      return 1
      ;;
  esac
}

# ---------------------------------------------------------------- state

env_running() {
  local kind="$1" name="$2"
  case "$kind" in
    host|"") return 0 ;;
    distrobox)
      env_tool_available distrobox || return 1
      distrobox list --no-color 2>/dev/null | awk -F'|' -v want="$name" '
        NR > 1 {
          gsub(/^ +| +$/, "", $2); gsub(/^ +| +$/, "", $3)
          if ($2 == want) { exit (tolower($3) ~ /^(up|running)/) ? 0 : 1 }
        }
        END { if (NR <= 1) exit 1 }
      '
      ;;
    docker|podman)
      env_tool_available "$kind" || return 1
      [[ "$("$kind" inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" == "true" ]]
      ;;
    *) return 1 ;;
  esac
}

# Bring a stopped container up. Distrobox starts on enter; docker and podman
# need an explicit start. A container that does not exist cannot be started
# and the caller gets a non-zero status.
env_start() {
  local kind="$1" name="$2"
  case "$kind" in
    host|"") return 0 ;;
    distrobox)
      env_tool_available distrobox || return 1
      distrobox enter --name "$name" -- true >/dev/null 2>&1
      ;;
    docker|podman)
      env_tool_available "$kind" || return 1
      env_running "$kind" "$name" && return 0
      "$kind" start "$name" >/dev/null 2>&1
      ;;
    *) return 1 ;;
  esac
}

env_exists() {
  local kind="$1" name="$2"
  case "$kind" in
    host|"") return 0 ;;
    distrobox)
      env_tool_available distrobox || return 1
      distrobox list --no-color 2>/dev/null | awk -F'|' -v want="$name" '
        NR > 1 { gsub(/^ +| +$/, "", $2); if ($2 == want) { found = 1; exit } }
        END { exit found ? 0 : 1 }
      '
      ;;
    docker|podman)
      env_tool_available "$kind" || return 1
      "$kind" inspect -f '{{.Name}}' "$name" >/dev/null 2>&1
      ;;
    *) return 1 ;;
  esac
}

# Every environment this machine can see, one per line:
#   kind<TAB>name<TAB>running
# Distrobox containers are also visible to docker/podman; they are listed
# once, under distrobox, because that is the tool that knows how to enter
# them as the right user with the right home.
env_list() {
  printf 'host\thost\ttrue\n'

  # Callers run with pipefail; a tool that errors out simply lists nothing.
  if env_tool_available distrobox; then
    { distrobox list --no-color 2>/dev/null || true; } | awk -F'|' '
      NR > 1 {
        gsub(/^ +| +$/, "", $2); gsub(/^ +| +$/, "", $3)
        if ($2 == "") next
        printf("distrobox\t%s\t%s\n", $2, (tolower($3) ~ /^(up|running)/) ? "true" : "false")
      }
    '
  fi

  local tool
  for tool in docker podman; do
    env_tool_available "$tool" || continue
    { "$tool" ps -a --format '{{.Names}}\t{{.State}}\t{{.Label "manager"}}' 2>/dev/null || true; } |
      awk -F'\t' -v kind="$tool" '
        $1 != "" && $3 != "distrobox" {
          printf("%s\t%s\t%s\n", kind, $1, ($2 == "running") ? "true" : "false")
        }
      '
  done
  return 0
}

# ---------------------------------------------------------------- paths

# The home directory inside the environment, which is where "~" in the mount
# folder setting points for that environment.
env_home() {
  local kind="$1" name="$2" user="$3"
  case "$kind" in
    host|"") printf '%s\n' "$HOME" ;;
    *) env_exec "$kind" "$name" "$user" sh -c 'printf "%s\n" "$HOME"' 2>/dev/null ;;
  esac
}

# Is DIR a mountpoint inside the environment? Reads the environment's own
# mount table so it does not depend on the mountpoint binary being installed
# in a minimal container image.
env_mounted() {
  local kind="$1" name="$2" user="$3" dir="$4"
  [[ -n "$dir" ]] || return 1
  env_exec "$kind" "$name" "$user" sh -c \
    'awk -v d="$1" '"'"'$5 == d { found = 1 } END { exit found ? 0 : 1 }'"'"' /proc/self/mountinfo' \
    sh "$dir" 2>/dev/null
}

env_unmount() {
  local kind="$1" name="$2" user="$3" dir="$4"
  [[ -n "$dir" ]] || return 1
  env_exec "$kind" "$name" "$user" fusermount3 -uz "$dir" 2>/dev/null ||
    env_exec "$kind" "$name" "$user" fusermount -uz "$dir" 2>/dev/null ||
    env_exec "$kind" "$name" "$user" umount -l "$dir" 2>/dev/null
}

env_has_rclone() {
  local kind="$1" name="$2" user="$3"
  env_exec "$kind" "$name" "$user" sh -c 'command -v rclone >/dev/null 2>&1' 2>/dev/null
}

# ---------------------------------------------------------------- rclone

# After this, every plain `rclone ...` call in the sourcing script runs inside
# the environment. The interactive scripts are written against rclone's
# command line, not against an environment abstraction, and this keeps them
# that way: sign-in prompts, OAuth and the 2FA state machine all work the same
# whether the remote lives on the host or in a container.
env_bind_rclone() {
  local kind="$1" name="$2" user="${3:-}"
  case "$kind" in
    host|"") unset -f rclone 2>/dev/null; return 0 ;;
  esac
  eval "rclone() { env_exec $(printf '%q %q %q' "$kind" "$name" "$user") rclone \"\$@\"; }"
}

# ---------------------------------------------------------------- records

# Per-service ownership record: label and extra flags as before, plus where
# the service lives. Missing keys mean "on this machine, under the mount
# folder", which is exactly what every record written before environments
# existed means.
#
# Sets: RECORD_LABEL RECORD_EXTRA_FLAGS RECORD_REMOTE RECORD_ENV_KIND
#       RECORD_ENV_NAME RECORD_ENV_USER RECORD_MOUNT_DIR
load_remote_record() {
  local file="$1" id="$2"
  local label="$id" extra_flags="" remote="$id" env_kind="host" env_name="" env_user="" mount_dir=""
  # shellcheck source=/dev/null
  [[ -f "$file" ]] && source "$file"
  RECORD_LABEL="$label"
  RECORD_EXTRA_FLAGS="$extra_flags"
  RECORD_REMOTE="${remote:-$id}"
  RECORD_ENV_KIND="${env_kind:-host}"
  RECORD_ENV_NAME="${env_name:-}"
  RECORD_ENV_USER="${env_user:-}"
  RECORD_MOUNT_DIR="${mount_dir:-}"
  env_valid_kind "$RECORD_ENV_KIND" || RECORD_ENV_KIND="host"
  if [[ "$RECORD_ENV_KIND" == host ]]; then
    RECORD_ENV_NAME=""
    RECORD_ENV_USER=""
  fi
  # Callers run under set -e; a record is never a failure.
  return 0
}

# write_remote_record FILE ID WRITER  (reads the RECORD_* variables)
write_remote_record() {
  local file="$1" id="$2" writer="${3:-omarchy-cloud}"
  local dir tmp
  dir="$(dirname "$file")"
  mkdir -p "$dir" || return 1
  tmp="$(mktemp "$dir/.${id}.XXXXXX")" || return 1
  {
    printf '# Written by %s for the %q service.\n' "$writer" "$id"
    echo "# extra_flags is appended to the rclone mount command line."
    printf 'label=%q\n' "$RECORD_LABEL"
    printf 'extra_flags=%q\n' "$RECORD_EXTRA_FLAGS"
    if [[ "$RECORD_REMOTE" != "$id" ]]; then
      printf 'remote=%q\n' "$RECORD_REMOTE"
    fi
    if [[ "${RECORD_ENV_KIND:-host}" != host ]]; then
      echo "# Where rclone runs and the folder is mounted."
      printf 'env_kind=%q\n' "$RECORD_ENV_KIND"
      printf 'env_name=%q\n' "$RECORD_ENV_NAME"
      [[ -n "$RECORD_ENV_USER" ]] && printf 'env_user=%q\n' "$RECORD_ENV_USER"
    fi
    if [[ -n "$RECORD_MOUNT_DIR" ]]; then
      echo "# Absolute mount folder inside that environment, instead of <mount folder>/<name>."
      printf 'mount_dir=%q\n' "$RECORD_MOUNT_DIR"
    fi
  } >"$tmp" || { rm -f "$tmp"; return 1; }
  chmod 600 "$tmp" || { rm -f "$tmp"; return 1; }
  mv "$tmp" "$file"
}

# Where the service is (or would be) mounted, inside its environment.
# MOUNT_ROOT_RAW is the unexpanded setting ("~/Cloud"); a leading ~ means the
# environment's own home, not the host's.
resolve_mount_dir() {
  local id="$1" root_raw="$2"
  if [[ -n "${RECORD_MOUNT_DIR:-}" ]]; then
    printf '%s\n' "$RECORD_MOUNT_DIR"
    return 0
  fi
  local root="$root_raw" home
  if [[ "$root" == "~" || "$root" == "~/"* ]]; then
    home="$(env_home "$RECORD_ENV_KIND" "$RECORD_ENV_NAME" "$RECORD_ENV_USER")" || home=""
    [[ -n "$home" ]] || return 1
    root="${home}${root#\~}"
  fi
  printf '%s/%s\n' "${root%/}" "$id"
}
