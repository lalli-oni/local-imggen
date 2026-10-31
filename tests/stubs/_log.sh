# shellcheck shell=bash
# sourced by every stub: append "name args..." to $STUB_LOG (one call per line)
[ -z "${STUB_LOG:-}" ] || printf '%s\n' "$(basename "$0") $*" >> "$STUB_LOG"
