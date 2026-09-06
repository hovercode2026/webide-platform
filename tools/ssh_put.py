"""Upload a local file/dir to the VM via SFTP.
Usage: python ssh_put.py <local_path> <remote_path>
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


def sftp_mkdirs(sftp, remote_dir):
    parts = remote_dir.strip("/").split("/")
    cur = ""
    for p in parts:
        cur += "/" + p
        try:
            sftp.stat(cur)
            continue
        except OSError:
            pass
        try:
            sftp.mkdir(cur)
        except OSError:
            if _exists(sftp, cur):
                continue
            raise


def _exists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except OSError:
        return False


def put_dir(sftp, local, remote):
    sftp_mkdirs(sftp, remote)
    for root, dirs, files in os.walk(local):
        rel = os.path.relpath(root, local).replace("\\", "/")
        rdir = remote if rel == "." else remote + "/" + rel
        sftp_mkdirs(sftp, rdir)
        for f in files:
            lp = os.path.join(root, f)
            if os.path.isfile(lp):
                sftp.put(lp, rdir + "/" + f)
                print("put %s -> %s/%s" % (lp, rdir, f))


def main():
    local, remote = sys.argv[1], sys.argv[2]
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, 22, USER, PASS, timeout=15, banner_timeout=15, auth_timeout=15)
    sftp = cli.open_sftp()
    if os.path.isdir(local):
        put_dir(sftp, local, remote)
    else:
        rdir = os.path.dirname(remote.replace("\\", "/"))
        if rdir:
            sftp_mkdirs(sftp, rdir)
        sftp.put(local, remote)
        print("put %s -> %s" % (local, remote))
    sftp.close()
    cli.close()


if __name__ == "__main__":
    main()
