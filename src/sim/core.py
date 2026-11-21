import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from importlib.resources import files


DEFAULTS = {
    "partition": "", "account": "", "cpus": "8", "memory": "32G",
    "time": "8:00:00", "gpus": "", "image": "", "flags": "",
    "container_ssh": False, "apptainer_module": "",
}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
JOB_ID = re.compile(r"[0-9]+\Z")
TOKEN = re.compile(r"[A-Za-z0-9_.:/+-]+\Z")
TIMEOUT = 15


def home():
    path = Path(os.environ.get("SIM_HOME", str(Path.home() / ".sim"))).expanduser().absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = path.lstat()
    if not path.is_dir() or path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise RuntimeError(f"SIM_HOME must be a private directory owned by you: {path}")
    return path


def directory(name):
    path = home() / name
    path.mkdir(mode=0o700, exist_ok=True)
    stat = path.lstat()
    if not path.is_dir() or path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise RuntimeError(f"sim directory must be private and owned by you: {path}")
    return path


@contextlib.contextmanager
def locked():
    with (home() / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_json(path, value):
    fd, temp = tempfile.mkstemp(prefix=".sim-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def profile_path(name):
    if not NAME.fullmatch(name) or name in (".", ".."):
        raise ValueError("Profile names must contain only letters, numbers, '.', '_' or '-'.")
    return directory("profiles") / (name + ".json")


def validate(config):
    if not isinstance(config, dict):
        raise ValueError("Profile must be a JSON object.")
    unknown = config.keys() - DEFAULTS.keys()
    if unknown:
        raise ValueError("Unknown profile fields: " + ", ".join(sorted(unknown)))
    result = DEFAULTS | config
    for key in DEFAULTS:
        if key == "container_ssh":
            if not isinstance(result[key], bool):
                raise ValueError("container_ssh must be true or false.")
        elif not isinstance(result[key], str) or "\n" in result[key] or "\r" in result[key]:
            raise ValueError(f"{key} must be a single-line string.")
        else:
            result[key] = result[key].strip()
    for key in ("partition", "account", "memory", "time", "gpus", "apptainer_module"):
        if result[key] and not TOKEN.fullmatch(result[key]):
            raise ValueError(f"Invalid {key}.")
    if not result["cpus"].isdigit() or int(result["cpus"]) < 1:
        raise ValueError("cpus must be a positive integer.")
    if not result["memory"] or not result["time"]:
        raise ValueError("memory and time are required.")
    if result["container_ssh"] and not result["image"]:
        raise ValueError("Container SSH requires an image.")
    try:
        shlex.split(result["flags"])
    except ValueError as exc:
        raise ValueError(f"Invalid container flags: {exc}") from exc
    return result


def load_profile(name):
    path = profile_path(name)
    try:
        return validate(json.loads(path.read_text()))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Profile {name!r} not found; run 'sim profile {name}'.") from exc


def list_profiles():
    return sorted(p.stem for p in directory("profiles").glob("*.json"))


def command(args, *, input=None):
    try:
        proc = subprocess.run(args, input=input, text=True, capture_output=True, timeout=TIMEOUT)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{args[0]} was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{args[0]} timed out; its outcome may be unknown.") from exc
    if proc.returncode:
        raise RuntimeError(f"{args[0]} failed: {proc.stderr.strip() or proc.stdout.strip() or proc.returncode}")
    return proc.stdout.strip()


def partitions():
    output = command(["sinfo", "--noheader", "--format=%P"])
    return sorted({line.strip().removesuffix("*") for line in output.splitlines() if line.strip()})


def state():
    try:
        return json.loads((home() / "current.json").read_text())
    except FileNotFoundError:
        return None


def cluster_args(record):
    return ["--clusters", record["cluster"]] if record.get("cluster") else []


def seconds(value):
    try:
        days, sep, clock = value.partition("-")
        if not sep:
            clock, days = days, "0"
        parts = [int(part) for part in clock.split(":")]
        if len(parts) not in (2, 3):
            return None
        total = 0
        for part in parts:
            total = total * 60 + part
        return int(days) * 86400 + total
    except ValueError:
        return None


def usage(record, elapsed):
    if record.get("cluster"):
        return {"memory_bytes": None, "cpu_percent": None}
    try:
        output = command(["sstat", "--noheader", "--parsable2",
                          "--jobs", record["job_id"] + ".batch", "--format=MaxRSS,AveCPU"])
        memory, cpu = output.splitlines()[0].split("|")[:2]
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]?)", memory.strip(), re.IGNORECASE)
        used_memory = int(float(match[1]) * 1024 ** (" KMGT".index(match[2].upper()) if match[2] else 0)) if match else None
        cpu_time, wall = seconds(cpu.strip()), seconds(elapsed)
        cpu_percent = round(100 * cpu_time / (wall * int(record["config"]["cpus"])), 1) if cpu_time is not None and wall else None
        return {"memory_bytes": used_memory, "cpu_percent": cpu_percent}
    except (RuntimeError, ValueError, IndexError):
        return {"memory_bytes": None, "cpu_percent": None}


def status(record):
    if not record.get("job_id"):
        raise RuntimeError("A submission has an unknown outcome; inspect current.json and Slurm before removing it.")
    job_id = record["job_id"]
    output = command(["squeue", *cluster_args(record), "--noheader", "--jobs", job_id,
                      "--format=%i|%T|%N|%M|%l|%R|%P"])
    if not output:
        return None
    lines = output.splitlines()
    if len(lines) != 1:
        raise RuntimeError("Unexpected squeue response for workbench job.")
    parts = lines[0].split("|", 6)
    if len(parts) != 7 or parts[0].strip() != job_id:
        raise RuntimeError("Unexpected squeue response for workbench job.")
    _, job_state, nodes, elapsed, limit, reason, partition = (p.strip() for p in parts)
    hostname = ""
    if job_state in ("RUNNING", "COMPLETING") and nodes and nodes != "(null)":
        hosts = command(["scontrol", "show", "hostnames", nodes]).splitlines()
        if len(hosts) != 1:
            raise RuntimeError("Expected exactly one compute node.")
        hostname = hosts[0].strip()
    return {
        "job_id": job_id, "cluster": record.get("cluster", ""), "profile": record["profile"],
        "state": job_state, "hostname": hostname, "partition": partition,
        "elapsed": elapsed, "time_limit": limit, "reason": reason,
        "requested": record["config"], "directory": record["directory"],
        "used": usage(record, elapsed) if job_state == "RUNNING"
                else {"memory_bytes": None, "cpu_percent": None},
    }


def current():
    record = state()
    return (record, status(record)) if record else (None, None)


def script(config, ready_dir):
    source = (files("sim") / "data" / "sshd.py").read_text() if config["container_ssh"] else ""
    values = {
        "IMAGE": config["image"], "APPTAINER_MODULE": config["apptainer_module"],
        "READY_DIR": str(ready_dir), "KEY_DIR": str(directory("ssh")),
        "SSH_ENABLED": "1" if config["container_ssh"] else "0",
    }
    lines = ["#!/bin/bash"]
    lines.extend(f"{key}={shlex.quote(value)}" for key, value in values.items())
    lines.append("FLAGS=(" + shlex.join(shlex.split(config["flags"])) + ")")
    lines.append("SSH_SOURCE=" + shlex.quote(source))
    lines.append((files("sim") / "data" / "idle.sh").read_text())
    return "\n".join(lines)


def start(name):
    config = load_profile(name)
    with locked():
        old = state()
        if old and status(old):
            raise RuntimeError("A sim workbench is already active.")
        ready = Path(tempfile.mkdtemp(prefix="job-", dir=directory("instances")))
        record = {"profile": name, "config": config, "directory": os.getcwd(),
                  "ready_dir": str(ready), "job_id": ""}
        batch = script(config, ready)
        args = ["sbatch", "--parsable", "--nodes=1", "--ntasks=1",
                f"--cpus-per-task={config['cpus']}", f"--mem={config['memory']}",
                f"--time={config['time']}", f"--job-name=sim-{name}",
                f"--chdir={record['directory']}", f"--output={directory('logs')}/%j.out"]
        for field, option in (("partition", "partition"), ("account", "account"), ("gpus", "gpus")):
            if config[field]:
                args.append(f"--{option}={config[field]}")
        write_json(home() / "current.json", record)
        try:
            output = command(args, input=batch)
        except RuntimeError as exc:
            if "timed out" not in str(exc):
                (home() / "current.json").unlink(missing_ok=True)
                ready.rmdir()
            raise
        job_id, separator, cluster = output.partition(";")
        if not JOB_ID.fullmatch(job_id) or (separator and not NAME.fullmatch(cluster)):
            raise RuntimeError(f"Unexpected sbatch response {output!r}; submission outcome is unknown.")
        record.update(job_id=job_id, cluster=cluster)
        write_json(home() / "current.json", record)
        return record


def stop():
    with locked():
        record, info = current()
        if record and info:
            command(["scancel", *cluster_args(record), record["job_id"]])
        if record:
            (home() / "current.json").unlink(missing_ok=True)
        return record


def container_info(record):
    job_id = record["job_id"]
    ready = Path(record["ready_dir"])
    instance = f"sim-{job_id}"
    try:
        if (ready / instance).read_text().strip() != job_id:
            return None, None
    except FileNotFoundError:
        return None, None
    try:
        ssh = json.loads((ready / (instance + ".ssh.json")).read_text())
        if (not isinstance(ssh, dict) or ssh.get("job_id") != job_id
                or type(ssh.get("port")) is not int or not 1 <= ssh["port"] <= 65535
                or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", ssh.get("fingerprint", ""))
                or not re.fullmatch(r"sim-[A-Za-z0-9_-]{43}", ssh.get("host_key_alias", ""))):
            ssh = None
    except (OSError, ValueError, TypeError):
        ssh = None
    return instance, ssh
