import argparse
import getpass
import json
import math
import os
import re
import select
import shlex
import sys

from sim import __version__, core


def edit_profile(name):
    path = core.profile_path(name)
    config = core.load_profile(name) if path.exists() else core.DEFAULTS.copy()
    if not sys.stdin.isatty():
        raise RuntimeError("The profile editor needs a terminal; edit the JSON profile directly instead.")
    try:
        available = core.partitions()
        if available:
            print("Partitions: " + ", ".join(available))
    except RuntimeError as exc:
        print(f"Partition discovery unavailable: {exc}", file=sys.stderr)
    print(f"Profile {name} (press Enter to keep a value; '-' clears optional fields)")
    labels = {
        "partition": "Partition (empty uses Slurm default)", "account": "Account",
        "cpus": "CPUs", "memory": "Memory", "time": "Walltime",
        "gpus": "GPUs (optional Slurm --gpus value)", "image": "Apptainer image",
        "flags": "Apptainer flags", "container_ssh": "Enable container SSH (yes/no)",
        "apptainer_module": "Apptainer module (optional)",
    }
    for key, label in labels.items():
        old = config[key]
        display = ("yes" if old else "no") if key == "container_ssh" else old
        value = input(f"{label} [{display}]: ").strip()
        if not value:
            continue
        if key == "container_ssh":
            if value.lower() not in ("yes", "y", "no", "n"):
                raise ValueError("Container SSH must be yes or no.")
            config[key] = value.lower() in ("yes", "y")
        else:
            config[key] = "" if value == "-" else value
    config = core.validate(config)
    core.write_json(path, config)
    print(f"Saved {path}")


def ssh_config(record, info):
    if not info or info["state"] != "RUNNING":
        raise RuntimeError("Workbench is not running.")
    _, ssh = core.container_info(record)
    if not ssh:
        raise RuntimeError("Container SSH is not ready or is not enabled.")
    user, host = getpass.getuser(), info["hostname"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", user) or not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        raise RuntimeError("Invalid user or compute hostname for SSH configuration.")
    return (f"# Verify the container host key: {ssh['fingerprint']}\n"
            "Host sim\n    HostName 127.0.0.1\n"
            f"    Port {ssh['port']}\n    User {user}\n"
            f"    HostKeyAlias {ssh['host_key_alias']}\n"
            f"    ProxyCommand ssh -W %h:%p {user}@{host}")


def display(record, info):
    if not info:
        return "Workbench is no longer in the Slurm queue."
    lines = [f"Profile: {info['profile']}    Job ID: {info['job_id']}",
             f"State: {info['state']}    Partition: {info['partition']}",
             f"Elapsed: {info['elapsed']}    Limit: {info['time_limit']}"]
    if info["state"] == "RUNNING" and info["hostname"]:
        user = getpass.getuser()
        host = info["hostname"]
        target = shlex.quote(f"{user}@{host}")
        cluster = f" --clusters={shlex.quote(info['cluster'])}" if info["cluster"] else ""
        lines += [f"Node: {host}",
                  f"Slurm shell: srun{cluster} --jobid={info['job_id']} --overlap --pty bash -l",
                  f"SSH: ssh -X {target}",
                  f"Tunnel: ssh -L 8080:127.0.0.1:8080 {target}",
                  f"VS Code: code --remote {shlex.quote('ssh-remote+' + host)} {shlex.quote(info['directory'])}"]
        instance, ssh = core.container_info(record)
        if instance:
            module = record["config"]["apptainer_module"]
            prefix = f"module load {shlex.quote(module)} && " if module else ""
            join = "--userns " if ssh and ssh.get("userns") else ""
            remote = prefix + f"apptainer shell {join}{shlex.quote('instance://' + instance)}"
            lines.append(f"Container shell: ssh -t {target} {shlex.quote(remote)}")
        if ssh:
            lines.append("Container VS Code SSH: sim ssh-config")
    elif info["reason"]:
        lines.append(f"Reason: {info['reason']}")
    used = info["used"]
    cpu = f"{used['cpu_percent']}%" if used["cpu_percent"] is not None else "unavailable"
    memory = f"{used['memory_bytes'] / 1024**3:.2f} GiB" if used["memory_bytes"] is not None else "unavailable"
    lines.append(f"Batch-step average CPU: {cpu}    Max RSS: {memory}")
    return "\n".join(lines)


def monitor(record):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(display(record, core.status(record)))
        return
    try:
        while True:
            info = core.status(record)
            print("\033[2J\033[H" + display(record, info), flush=True)
            if not info:
                return
            print("\nPress Enter or Ctrl+C to leave the job running.", flush=True)
            if select.select([sys.stdin], [], [], 10)[0]:
                sys.stdin.readline()
                print(f"Job {record['job_id']} left running.")
                return
    except KeyboardInterrupt:
        print(f"\nJob {record['job_id']} left running.")


def parser():
    p = argparse.ArgumentParser(prog="sim", description="Slurm interactive macros")
    p.add_argument("--version", action="version", version=__version__)
    subs = p.add_subparsers(dest="action")
    for name in ("profile", "start"):
        sub = subs.add_parser(name, help=f"{'Edit' if name == 'profile' else 'Start'} a profile")
        sub.add_argument("profile", nargs="?", default="default")
    for name in ("monitor", "list", "end", "stop", "ssh-config"):
        subs.add_parser(name)
    api = subs.add_parser("api", help="Machine-readable JSON API")
    api.add_argument("--timeout", type=float, default=15)
    api_sub = api.add_subparsers(dest="api_action", required=True)
    for name in ("info", "end", "list-profiles", "version"):
        api_sub.add_parser(name)
    api_start = api_sub.add_parser("start")
    api_start.add_argument("profile", nargs="?", default="default")
    return p


def api_action(args):
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("Timeout must be positive.")
    core.TIMEOUT = args.timeout
    if args.api_action == "version":
        return __version__
    if args.api_action == "list-profiles":
        return core.list_profiles()
    if args.api_action == "end":
        core.stop()
        return []
    if args.api_action == "start":
        record = core.start(args.profile)
        info = core.status(record)
        return [info or {"job_id": record["job_id"], "profile": record["profile"], "state": "UNKNOWN"}]
    record, info = core.current()
    if not info:
        return []
    instance, ssh = core.container_info(record) if info["state"] == "RUNNING" else (None, None)
    info["container_instance"] = instance
    info["container_ssh_ready"] = ssh is not None
    return [info]


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.action == "api":
            print(json.dumps({"output": api_action(args)}))
        elif args.action == "profile":
            edit_profile(args.profile)
        elif args.action == "start":
            record = core.start(args.profile)
            print(f"Submitted job {record['job_id']}.", flush=True)
            monitor(record)
        elif args.action == "monitor":
            record, info = core.current()
            if not info:
                raise RuntimeError("No active workbench.")
            monitor(record)
        elif args.action in ("end", "stop"):
            record = core.stop()
            print(f"Stopped job {record['job_id']}." if record else "No active workbench.")
        elif args.action == "list":
            _, info = core.current()
            if info:
                print(f"{info['job_id']}\t{info['profile']}\t{info['state']}")
        elif args.action == "ssh-config":
            record, info = core.current()
            if not record:
                raise RuntimeError("No active workbench.")
            print(ssh_config(record, info))
        else:
            p.print_help()
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        if args.action == "api":
            print(json.dumps({"error": str(exc), "output": []}))
        else:
            print(f"sim: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
