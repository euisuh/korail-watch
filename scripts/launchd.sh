#!/bin/sh
set -eu

label=com.euisuh.korail-watch
agents="$HOME/Library/LaunchAgents"
plist="$agents/$label.plist"

case "${1:-}" in
  install)
    config=${2:-"$PWD/trip.toml"}
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
    mkdir -p "$agents" "$HOME/Library/Logs"
    python3 - "$plist" "$caffeinate" "$watcher" "$config" "$config_dir" <<'PY'
import plistlib
import sys

path, caffeinate, watcher, config, working_directory = sys.argv[1:]
payload = {
    "Label": "com.euisuh.korail-watch",
    "ProgramArguments": [caffeinate, "-i", watcher, "watch", "--arm", "--config", config],
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
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$plist"
    echo "Installed and started $label"
    ;;
  uninstall)
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
    rm -f "$plist"
    echo "Uninstalled $label"
    ;;
  *)
    echo "usage: $0 install [/absolute/path/to/trip.toml] | uninstall" >&2
    exit 2
    ;;
esac
