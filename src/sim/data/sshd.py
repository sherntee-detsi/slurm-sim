import base64
import hashlib
import json
import os
import pwd
import signal
import socket
import stat
import subprocess
import sys
import tempfile


def private_directory(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"SSH directory must be owned by the user and private: {path}")


def host_key(directory):
    private_directory(directory)
    key = os.path.join(directory, "host_ed25519")
    if not os.path.exists(key):
        with tempfile.TemporaryDirectory(prefix=".host-key-", dir=directory) as tmp:
            generated = os.path.join(tmp, "key")
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", generated],
                check=True,
            )
            try:
                os.link(generated, key)
            except FileExistsError:
                pass
    info = os.lstat(key)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"SSH host key must be owned by the user and private: {key}")
    public = subprocess.check_output(["ssh-keygen", "-y", "-f", key], text=True).strip()
    key_type, key_data = public.split()[:2]
    digest = hashlib.sha256(base64.b64decode(key_data, validate=True)).digest()
    fingerprint = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
    alias = "sim-" + base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return key, f"{key_type} {key_data}", fingerprint, alias


def quote_config(value):
    if "\n" in value or "\r" in value:
        raise ValueError("SSH configuration paths must not contain newlines")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def serve(job_id, ready_file, runtime_dir, key_dir, authorized_keys, join_mode=""):
    user = pwd.getpwuid(os.getuid()).pw_name
    if not os.path.isfile(authorized_keys):
        raise RuntimeError(f"SSH authorized keys file not found: {authorized_keys}")
    private_directory(runtime_dir)
    key, public_key, fingerprint, alias = host_key(key_dir)
    config = os.path.join(runtime_dir, "sshd_config")
    with open(config, "w") as f:
        f.write(
            f"HostKey {quote_config(key)}\n"
            f"AuthorizedKeysFile {quote_config(authorized_keys)}\n"
            f"AllowUsers {user}\n"
            "UsePAM no\n"
            "PasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\n"
            "PubkeyAuthentication yes\n"
            "PermitRootLogin no\n"
            "AllowTcpForwarding yes\n"
            "X11Forwarding no\n"
            "StrictModes yes\n"
        )
    subprocess.run(["/usr/sbin/sshd", "-t", "-f", config], check=True)
    children = []
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(32)
        listener.settimeout(1)
        metadata = {
            "job_id": job_id,
            "port": listener.getsockname()[1],
            "fingerprint": fingerprint,
            "host_key_alias": alias,
            "host_public_key": public_key,
            "userns": join_mode == "userns",
        }
        fd, temporary = tempfile.mkstemp(prefix=".ssh-ready-", dir=os.path.dirname(ready_file))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(metadata, f)
            os.replace(temporary, ready_file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        try:
            while not stopping:
                children = [child for child in children if child.poll() is None]
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                with client:
                    children.append(subprocess.Popen(
                        ["/usr/sbin/sshd", "-i", "-e", "-f", config],
                        stdin=client,
                        stdout=client,
                        start_new_session=True,
                    ))
        finally:
            for child in children:
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            for child in children:
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
            try:
                os.unlink(ready_file)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    try:
        serve(*sys.argv[1:])
    except Exception as e:
        print(f"Container SSH failed: {e}", file=sys.stderr)
        sys.exit(1)
