#!/usr/bin/env bash
# cloudflared for `gen tunnel` (#71): one pinned, checksum-verified binary in ~/.local/bin. No sudo,
# no apt repo, no service, no Cloudflare account (gen tunnel uses a quick tunnel). Never run automatically.
#
#   ./tunnel-cloudflared.sh install [-y]   download VERSION, check SHA256, install (asks y/N first)
#   ./tunnel-cloudflared.sh status         what is installed
#   ./tunnel-cloudflared.sh remove         delete it again
#
# To move to a newer release: take its version and the cloudflared-linux-amd64 SHA256 from
# https://github.com/cloudflare/cloudflared/releases (pick one at least two weeks old), edit both below.
set -euo pipefail

VERSION=2026.10.7
SHA256=1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872
URL="${CLOUDFLARED_BASE_URL:-https://github.com/cloudflare/cloudflared/releases/download}/$VERSION/cloudflared-linux-amd64"
BIN="${CLOUDFLARED_BIN:-$HOME/.local/bin/cloudflared}"

usage() { sed -n '2,/^set -/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

cmd=${1:-}
yes=0
[ "${2:-}" = "-y" ] && yes=1

case "$cmd" in
  status)
    if [ -x "$BIN" ]; then
      echo "installed: $BIN ($("$BIN" --version 2>/dev/null | head -1))"
      echo "sha256:    $(sha256sum "$BIN" | cut -d' ' -f1)"
      [ "$(sha256sum "$BIN" | cut -d' ' -f1)" = "$SHA256" ] && echo "matches the pinned $VERSION" \
        || echo "!! not the pinned $VERSION (re-run: $0 install)"
    else
      echo "not installed ($BIN)"
    fi
    ;;
  remove)
    rm -f "$BIN" "$BIN.part"
    echo "removed $BIN"
    ;;
  install)
    if [ -x "$BIN" ] && [ "$(sha256sum "$BIN" | cut -d' ' -f1)" = "$SHA256" ]; then
      echo "cloudflared $VERSION is already installed: $BIN"
      exit 0
    fi
    echo "Downloads cloudflared $VERSION (~40 MB) from GitHub to $BIN"
    echo "and checks it against SHA256 $SHA256."
    if [ $yes -eq 0 ]; then
      # No terminal means no.
      if { exec 3</dev/tty; } 2>/dev/null; then
        printf 'Download now? [y/N] '
        read -r ans <&3 || ans=
      else
        ans=
      fi
      case "$ans" in y|Y|yes) ;; *) echo "Nothing downloaded."; exit 1 ;; esac
    fi
    mkdir -p "$(dirname "$BIN")"
    curl -fL --proto '=https' --tlsv1.2 -o "$BIN.part" "$URL"
    got=$(sha256sum "$BIN.part" | cut -d' ' -f1)
    if [ "$got" != "$SHA256" ]; then
      rm -f "$BIN.part"
      echo "!! checksum mismatch: got $got, expected $SHA256. Nothing installed." >&2
      exit 1
    fi
    chmod 755 "$BIN.part"
    mv "$BIN.part" "$BIN"
    echo "installed: $BIN ($("$BIN" --version 2>/dev/null | head -1))"
    case ":$PATH:" in *":$(dirname "$BIN"):"*) ;; *) echo "(gen tunnel finds it there even though it is not on PATH)";; esac
    ;;
  -h|--help|"") usage ;;
  *) usage >&2; exit 2 ;;
esac
