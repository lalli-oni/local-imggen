#!/usr/bin/env bash
# The Windows scheduled task that runs `gen daily` once a day (#3).
#
#   windows/daily-task.sh on [--at HH:MM]   create or update the task (default 06:00)
#   windows/daily-task.sh off               remove it
#   windows/daily-task.sh status            is it there, last/next run, last lines of daily.log
#   windows/daily-task.sh run               start it now (to check it end to end)
#
# The task belongs to your Windows user and runs only while you are logged on (setting the wallpaper
# needs your desktop session). It starts WSL if it isn't running (`wsl.exe -d <distro> -e ...`), and
# with "start when available" a run missed while the PC was off or asleep happens at the next chance.
# conhost --headless keeps a console window from popping up. No admin rights, no systemd, no sudo.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TASK=imagegen-daily
VENV=${IMAGEGEN_VENV:-$HOME/venvs/imagegen}
DISTRO=${WSL_DISTRO_NAME:-Ubuntu-24.04}

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; }
ps() { powershell.exe -NoProfile -NonInteractive -Command "$1" | tr -d '\r'; }

cmd=${1:-}
[ $# -gt 0 ] && shift
at=06:00
while [ $# -gt 0 ]; do
  case "$1" in
    --at) at=${2:-}; shift 2 || { usage; exit 2; } ;;
    *) usage; exit 2 ;;
  esac
done

case "$cmd" in
  on)
    [[ $at =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "!! --at wants HH:MM (24h), got '$at'"; exit 2; }
    [ -x "$VENV/bin/python" ] || { echo "!! $VENV/bin/python not found: run ./setup.sh first"; exit 1; }
    for p in "$HERE" "$VENV"; do
      [[ $p =~ ^[A-Za-z0-9_./-]+$ ]] || { echo "!! '$p' has characters the task command can't carry"; exit 1; }
    done
    args="--headless wsl.exe -d $DISTRO --cd $HERE -e $VENV/bin/python $HERE/imagegen.py daily"
    ps "\$a = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument '$args'; \
\$t = New-ScheduledTaskTrigger -Daily -At '$at'; \
\$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1); \
Register-ScheduledTask -TaskName '$TASK' -Action \$a -Trigger \$t -Settings \$s -Description 'imagegen: gen daily (new wallpaper)' -Force | Out-Null"
    echo "==> Task '$TASK' runs daily at $at:"
    echo "    conhost.exe $args"
    echo "Try it now: $0 run   (then $0 status)"
    echo "Undo:       $0 off"
    ;;
  off)
    ps "if (Get-ScheduledTask -TaskName '$TASK' -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName '$TASK' -Confirm:\$false; 'removed' } else { 'absent' }" \
      | grep -q removed && echo "==> Task '$TASK' removed" || echo "Task '$TASK' was not there"
    ;;
  status)
    out=$(ps "\$t = Get-ScheduledTask -TaskName '$TASK' -ErrorAction SilentlyContinue; if (\$t) { \$i = \$t | Get-ScheduledTaskInfo; 'state: ' + \$t.State; 'last run: ' + \$i.LastRunTime + ' (result ' + \$i.LastTaskResult + ')'; 'next run: ' + \$i.NextRunTime } else { 'absent' }")
    if [ "$out" = absent ] || [ -z "$out" ]; then
      echo "Task '$TASK': not set up (turn it on with: $0 on)"
    else
      echo "Task '$TASK':"; printf '%s\n' "$out" | sed 's/^/  /'
    fi
    log=$("$VENV/bin/python" -c "import sys; sys.path.insert(0, '$HERE'); import daily; print(daily.log_file())" 2>/dev/null \
          || python3 -c "import sys; sys.path.insert(0, '$HERE'); import daily; print(daily.log_file())" 2>/dev/null || true)
    if [ -n "$log" ] && [ -f "$log" ]; then
      echo "Last runs ($log):"; tail -n 5 "$log" | sed 's/^/  /'
    else
      echo "No daily runs logged yet."
    fi
    ;;
  run)
    ps "Start-ScheduledTask -TaskName '$TASK'"
    echo "==> Started '$TASK'. It takes a few minutes; check with: $0 status"
    ;;
  *) usage; exit 2 ;;
esac
