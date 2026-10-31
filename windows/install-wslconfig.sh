#!/usr/bin/env bash
# Write C:\Users\<you>\.wslconfig so Windows keeps 2 cores (on hosts with more than 4) and gets cached RAM back.
# Unlike comfy/limits.sh this is always on once applied, for all of WSL.
# Never overwrites an existing .wslconfig: prints both so you can merge by hand.
# Applying needs `wsl --shutdown` in PowerShell (stops ComfyUI too, so do it between runs).
set -euo pipefail
win_home=$(wslpath "$(powershell.exe -NoProfile -Command '$env:USERPROFILE' | tr -d '\r')")
WSLCFG="$win_home/.wslconfig"
read -r RAM_GB CPUS < <(powershell.exe -NoProfile -Command \
  '"{0} {1}" -f [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB), $env:NUMBER_OF_PROCESSORS' | tr -d '\r')
[[ "${RAM_GB:-}" =~ ^[0-9]+$ && "${CPUS:-}" =~ ^[0-9]+$ ]] \
  || { echo "!! Could not read RAM/CPU count from Windows (got '${RAM_GB:-}' '${CPUS:-}'). Is WSL interop on?"; exit 1; }
echo "Host: ${RAM_GB} GB RAM, ${CPUS} logical CPUs"

proposed=$(cat <<CFG
[wsl2]
# WSL's default (50% of RAM), made explicit.
memory=$(( RAM_GB / 2 ))GB
# Leave 2 logical cores to Windows (only on hosts with more than 4).
processors=$(( CPUS > 4 ? CPUS - 2 : CPUS ))

[experimental]
# Hand cached RAM back to Windows instead of holding it.
autoMemoryReclaim=gradual
CFG
)

if [ -f "$WSLCFG" ]; then
  echo "!! $WSLCFG already exists, not touching it. Current:"; cat "$WSLCFG"
  echo; echo "Proposed:"; echo "$proposed"
  exit 0
fi
echo "$proposed" > "$WSLCFG"
echo "==> Wrote $WSLCFG:"; cat "$WSLCFG"
echo
echo "Apply: run 'wsl --shutdown' in PowerShell, then open a WSL terminal again."
echo "Undo:  delete $WSLCFG and run 'wsl --shutdown'."
