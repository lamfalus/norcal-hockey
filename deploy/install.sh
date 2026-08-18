#!/usr/bin/env bash
# Install the nightly collector on a Raspberry Pi.
#
#   ./deploy/install.sh              install for the current user
#   ./deploy/install.sh --user pi    install for another user
#
# Idempotent: safe to re-run after pulling changes.

set -euo pipefail

USER_NAME="${USER:-$(id -un)}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) USER_NAME="$2"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn:\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- checks
say "Checking Python"
if ! command -v python3 >/dev/null; then
  echo "python3 not found. Install it with: sudo apt install python3" >&2
  exit 1
fi
python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    sys.exit(f"Python 3.9+ required, found {sys.version.split()[0]}")
print(f"  Python {sys.version.split()[0]} OK (no third-party packages needed)")
PY

say "Verifying the collector runs"
( cd "$REPO_DIR" && python3 -m norcalstats.cli --help >/dev/null )
echo "  norcalstats CLI OK"

# ---------------------------------------------------------------- config
CONFIG="$REPO_DIR/norcalstats.json"
if [[ ! -f "$CONFIG" ]]; then
  say "Writing default config to $CONFIG"
  cat > "$CONFIG" <<JSON
{
  "data_dir": "$REPO_DIR/data",
  "export_dir": "$REPO_DIR",
  "delay": 1.5,
  "publish": false,
  "git_branch": "main",
  "log_level": "INFO",
  "log_file": "$REPO_DIR/data/norcalstats.log"
}
JSON
  echo "  Edit it to enable publishing once you have git auth set up."
else
  echo "  Config already exists; leaving it alone."
fi

mkdir -p "$REPO_DIR/data"

# ------------------------------------------------------------- systemd
if ! command -v systemctl >/dev/null; then
  warn "systemd not found; skipping timer installation."
  warn "Run the update manually, or add a cron entry:"
  warn "  30 3 * * * cd $REPO_DIR && python3 -m norcalstats.cli update"
  exit 0
fi

say "Installing systemd units (requires sudo)"
sudo cp "$REPO_DIR/deploy/norcalstats.service" /etc/systemd/system/norcalstats@.service
sudo cp "$REPO_DIR/deploy/norcalstats.timer"   /etc/systemd/system/norcalstats@.timer
sudo systemctl daemon-reload
sudo systemctl enable --now "norcalstats@${USER_NAME}.timer"

say "Done"
echo
echo "  Status:      systemctl status norcalstats@${USER_NAME}.timer"
echo "  Next run:    systemctl list-timers norcalstats@${USER_NAME}.timer"
echo "  Run now:     sudo systemctl start norcalstats@${USER_NAME}.service"
echo "  Logs:        journalctl -u norcalstats@${USER_NAME}.service -f"
echo
echo "Before the season starts, seed the database with the historical backfill:"
echo "  cd $REPO_DIR && python3 -m norcalstats.cli backfill --from-season 27"
