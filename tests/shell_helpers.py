"""Run shell scripts the way a user would, with or without a terminal.

limits.sh and update.sh ask y/N on /dev/tty (and treat "no terminal" as no). run_script() always
starts the script in a new session, so it can never reach the terminal pytest runs in (a test
must not stop and wait for you). With answer="y", it gets a pseudo-terminal of its own as its
controlling terminal, and the answer is typed at every [y/N] prompt.
"""
from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import subprocess
import termios
import time
from dataclasses import dataclass

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class Result:
    code: int
    out: str  # stdout + stderr, colours stripped, \r\n -> \n


def run_script(cmd: list[str], env: dict, cwd=None, answer: str | None = None, timeout: float = 30) -> Result:
    if answer is None:
        p = subprocess.run(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=timeout, start_new_session=True)
        return Result(p.returncode, ANSI.sub("", p.stdout))

    master, slave = pty.openpty()

    def take_tty():  # runs in the child after setsid(): make the pty its controlling terminal
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    p = subprocess.Popen(cmd, env=env, cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
                         start_new_session=True, preexec_fn=take_tty, close_fds=True)
    os.close(slave)
    buf, answered, deadline = b"", 0, time.time() + timeout
    try:
        while True:
            if time.time() > deadline:
                p.kill()
                raise TimeoutError(f"{cmd} still running after {timeout}s; output so far:\n"
                                   + buf.decode(errors="replace"))
            r, _, _ = select.select([master], [], [], 0.1)
            if not r:
                if p.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:  # EIO: every process closed the terminal
                break
            if not chunk:
                break
            buf += chunk
            while buf.count(b"[y/N]") > answered:  # answer every prompt (install.sh -> limits.sh asks twice)
                os.write(master, answer.encode() + b"\n")
                answered += 1
    finally:
        os.close(master)
        p.wait()
    out = ANSI.sub("", buf.decode(errors="replace")).replace("\r\n", "\n")
    return Result(p.returncode, out)
