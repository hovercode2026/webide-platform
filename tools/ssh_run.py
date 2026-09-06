"""Run a command on the test VM via SSH (password auth).
Usage: python ssh_run.py "command" [timeout_sec]
Credentials come from tools/.env.local.py (git-ignored) or env: SSH_HOST, SSH_USER, SSH_PASS.
"""
import os
import sys
import warnings

warnings.filterwarnings("ignore")
import paramiko

_here = os.path.dirname(os.path.abspath(__file__))
_env = {}
_local = os.path.join(_here, ".env.local.py")
if os.path.exists(_local):
    with open(_local, encoding="utf-8") as f:
        exec(f.read(), _env)  # local-only file, never committed

HOST = os.environ.get("SSH_HOST") or _env.get("SSH_HOST", "")
USER = os.environ.get("SSH_USER") or _env.get("SSH_USER", "")
PASS = os.environ.get("SSH_PASS") or _env.get("SSH_PASS", "")


def main():
    cmd = sys.argv[1]
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, 22, USER, PASS, timeout=15, banner_timeout=15, auth_timeout=15)
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout, get_pty=False)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    sys.stdout.write(out)
    if err.strip():
        sys.stdout.write("\n[STDERR]\n" + err)
    sys.stdout.write("\n[EXIT %d]\n" % code)
    cli.close()
    sys.exit(0 if code == 0 else 1)


if __name__ == "__main__":
    main()
