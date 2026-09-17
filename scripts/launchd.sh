#!/bin/sh
set -eu

label=com.euisuh.korail-watch
agents="$HOME/Library/LaunchAgents"
plist="$agents/$label.plist"

usage() {
  echo "usage: $0 install [/absolute/path/to/trip.toml] [--continuous] | pause | resume | uninstall" >&2
  exit 2
}

case "${1:-}" in
  install) [ "$#" -le 3 ] || usage ;;
  pause|resume|uninstall) [ "$#" -eq 1 ] || usage ;;
  *) usage ;;
esac

uid=$(id -u)
case "$uid" in
  ""|*[!0-9]*) echo "Could not determine the current user ID" >&2; exit 1 ;;
esac
domain="gui/$uid"
target="$domain/$label"
inspection_error=$(mktemp "${TMPDIR:-/tmp}/korail-watch-launchctl.XXXXXX")
trap 'rm -f "$inspection_error"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

job_loaded() {
  if launchctl print "$target" >/dev/null 2>"$inspection_error"; then
    return 0
  else
    status=$?
  fi
  if [ "$status" -eq 113 ] &&
    grep -Fq "Could not find service \"$label\" in domain for user gui: $uid" "$inspection_error"
  then
    return 1
  fi
  cat "$inspection_error" >&2
  exit "$status"
}

unload_if_loaded() {
  if job_loaded; then
    launchctl bootout "$target"
  else
    return 0
  fi
  if job_loaded; then
    echo "Service is still loaded after bootout: $target" >&2
    return 1
  else
    return 0
  fi
}

case "$1" in
  install)
    config=${2:-"$PWD/trip.toml"}
    mode=${3:-}
    case "$mode" in
      ""|--continuous) ;;
      *) echo "optional third argument must be --continuous" >&2; exit 2 ;;
    esac
    config_dir=$(cd "$(dirname "$config")" && pwd)
    config="$config_dir/$(basename "$config")"
    watcher=$(command -v korail-watch)
    caffeinate=$(command -v caffeinate)
    check_output=$("$watcher" check --config "$config")
    printf '%s\n' "$check_output"
    result=$(printf '%s\n' "$check_output" | tail -n 1)
    case "$result" in
      available|not-found) ;;
      *) echo "Read-only check did not complete safely: $result" >&2; exit 1 ;;
    esac
    launchctl disable "$target"
    unload_if_loaded
    mkdir -p "$agents" "$HOME/Library/Logs"
    python3 - "$plist" "$caffeinate" "$watcher" "$config" "$config_dir" "$mode" <<'PY'
import plistlib
import sys

path, caffeinate, watcher, config, working_directory, mode = sys.argv[1:]
arguments = [caffeinate, "-i", watcher, "watch", "--arm", "--config", config]
if mode:
    arguments.append(mode)
payload = {
    "Label": "com.euisuh.korail-watch",
    "ProgramArguments": arguments,
    "WorkingDirectory": working_directory,
    "RunAtLoad": True,
    "KeepAlive": False,
    "StandardOutPath": str(__import__("pathlib").Path.home() / "Library/Logs/korail-watch.log"),
    "StandardErrorPath": str(__import__("pathlib").Path.home() / "Library/Logs/korail-watch.log"),
}
with open(path, "wb") as handle:
    plistlib.dump(payload, handle)
PY
    chmod 600 "$plist"
    launchctl enable "$target"
    launchctl bootstrap "$domain" "$plist"
    echo "Installed $label; startup requested. Check logs and status."
    ;;
  pause)
    launchctl disable "$target"
    unload_if_loaded
    echo "Paused $label; saved installation remains disabled."
    ;;
  resume)
    [ -f "$plist" ] || {
      echo "No saved installation found: $plist" >&2
      exit 1
    }
    launchctl enable "$target"
    if job_loaded; then
      launchctl kickstart "$target"
    else
      launchctl bootstrap "$domain" "$plist"
    fi
    echo "Resume requested for $label; startup health is not verified. Check logs and status."
    ;;
  uninstall)
    launchctl disable "$target"
    unload_if_loaded
    rm -f "$plist"
    echo "Uninstalled $label"
    ;;
esac
