#!/usr/bin/env bash
#
# Install the memory system.
#
#   ./install.sh                 install the CLI, create an empty instance
#   ./install.sh --schedule      also create the nightly consolidation schedule
#
# Idempotent.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTANCE="${PI_MEMORY_DIR:-$HOME/.local/share/pi-memory}"
BIN_DIR="/usr/local/bin"
MAKE_SCHEDULE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --schedule) MAKE_SCHEDULE="yes"; shift ;;
    -h|--help)  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

echo "pi-memory → $REPO"

# --- 1. python ------------------------------------------------------------
command -v python3 >/dev/null || die "python3 is required"
python3 - <<'PY' || die "python3 lacks the sqlite3 module"
import sqlite3, sys
if sys.version_info < (3, 9):
    raise SystemExit("python3.9 or newer is required")
PY
ok "python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])') with sqlite3"

# --- 2. the instance directory -------------------------------------------
mkdir -p "$INSTANCE"
chmod 700 "$INSTANCE"
ok "instance at $INSTANCE"

# --- 3. the CLI -----------------------------------------------------------
# It has to be somewhere on the *minimal* PATH, not just an interactive one:
# scheduled jobs run under systemd with no login environment, and a missing CLI
# there means a silent 3am no-op. /usr/local/bin is on that PATH; ~/.pi/agent/bin
# is not.
TARGET="$BIN_DIR/memory"
if [ -w "$BIN_DIR" ]; then
  ln -sfn "$REPO/bin/memory" "$TARGET"
elif command -v sudo >/dev/null && sudo -n true 2>/dev/null; then
  sudo ln -sfn "$REPO/bin/memory" "$TARGET"
else
  warn "cannot write $BIN_DIR without a password; installing to ~/.local/bin instead"
  mkdir -p "$HOME/.local/bin"
  TARGET="$HOME/.local/bin/memory"
  ln -sfn "$REPO/bin/memory" "$TARGET"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) warn "add ~/.local/bin to PATH, and to the environment of any scheduler" ;;
  esac
fi
chmod +x "$REPO/bin/memory"
ok "CLI at $TARGET"

# --- 4. prove it resolves without a login environment ---------------------
MINIMAL="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
if env -i PATH="$MINIMAL" sh -c 'command -v memory >/dev/null'; then
  ok "resolves under the minimal systemd PATH"
else
  warn "does NOT resolve under $MINIMAL — scheduled jobs will not find it."
  warn "If you installed to a custom location, put its directory on that PATH."
fi

# --- 5. the nightly consolidation schedule -------------------------------
PROMPT='Run `memory dream` and follow the instructions it prints.'
if [ -n "$MAKE_SCHEDULE" ]; then
  command -v paseo >/dev/null || die "paseo is required for --schedule"
  paseo schedule create "$PROMPT" \
    --name dream --cron '0 3 * * *' --timezone "${TZ_NAME:-UTC}" \
    --provider pi --cwd "$REPO" 2>&1 | tail -3
  ok "nightly schedule created (timezone ${TZ_NAME:-UTC}; set TZ_NAME to change it)"
else
  say "to schedule the nightly consolidation, run:"
  echo
  printf '    paseo schedule create %s \\\n      --name dream --cron "0 3 * * *" --timezone Australia/Sydney \\\n      --provider pi --cwd %s\n\n' "'$PROMPT'" "$REPO"
fi

echo
ok "done. Try:  memory status"
