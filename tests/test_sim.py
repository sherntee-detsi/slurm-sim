import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from sim import cli, core


class SimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"SIM_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        core.write_json(core.profile_path("default"), core.DEFAULTS)

    def test_submit_status_stop_and_script(self):
        commands = []

        def fake(args, *, input=None):
            commands.append((args, input))
            if args[0] == "sbatch":
                return "42"
            if args[0] == "squeue":
                return "42|RUNNING|node01|00:02:00|08:00:00|None|compute"
            if args[0] == "scontrol":
                return "node01"
            if args[0] == "sstat":
                return "2048K|00:01:00"
            return ""

        with mock.patch.object(core, "command", side_effect=fake):
            record = core.start("default")
            _, info = core.current()
            self.assertEqual(info["hostname"], "node01")
            self.assertEqual(info["used"], {"memory_bytes": 2097152, "cpu_percent": 6.2})
            with self.assertRaisesRegex(RuntimeError, "already active"):
                core.start("default")
            core.stop()
        self.assertIsNone(core.state())
        self.assertEqual(commands[-1][0], ["scancel", "42"])
        batch_args, batch_script = commands[0]
        self.assertIn("--nodes=1", batch_args)
        self.assertIn("--cpus-per-task=8", batch_args)
        self.assertIn("--output=" + str(core.directory("logs") / "%j.out"), batch_args)
        self.assertEqual(record["cluster"], "")
        subprocess.run(["bash", "-n"], input=batch_script, text=True, check=True)

    def test_profile_validation_and_shell_quoting(self):
        for name in ("../escape", "/tmp/file", ""):
            with self.assertRaises(ValueError):
                core.profile_path(name)
        with self.assertRaises(ValueError):
            core.validate({"container_ssh": True})
        with self.assertRaises(ValueError):
            core.validate({"memory": "1G\n#SBATCH --exclusive"})
        with self.assertRaises(ValueError):
            core.validate({"flags": "'unterminated"})
        config = core.validate({"image": "$(touch /tmp/never)", "flags": "--bind 'a b:c d'"})
        batch = core.script(config, core.directory("instances"))
        self.assertIn("IMAGE='$(touch /tmp/never)'", batch)
        self.assertIn("FLAGS=(--bind 'a b:c d')", batch)
        subprocess.run(["bash", "-n"], input=batch, text=True, check=True)

    def test_submit_error_and_unknown_outcome(self):
        with mock.patch.object(core, "command", side_effect=RuntimeError("sbatch failed: invalid account")):
            with self.assertRaises(RuntimeError):
                core.start("default")
        self.assertIsNone(core.state())
        with mock.patch.object(core, "command", side_effect=RuntimeError("sbatch timed out; its outcome may be unknown.")):
            with self.assertRaises(RuntimeError):
                core.start("default")
        self.assertEqual(core.state()["job_id"], "")
        with self.assertRaisesRegex(RuntimeError, "unknown outcome"):
            core.stop()

    def test_scheduler_error_never_clears_state(self):
        with mock.patch.object(core, "command", return_value="42"):
            core.start("default")
        with mock.patch.object(core, "command", side_effect=RuntimeError("controller unavailable")):
            with self.assertRaises(RuntimeError):
                core.stop()
            with self.assertRaises(RuntimeError):
                core.start("default")
        self.assertEqual(core.state()["job_id"], "42")

    def test_federated_jobs_use_cluster_for_queue_and_cancel(self):
        calls = []

        def fake(args, *, input=None):
            calls.append(args)
            if args[0] == "sbatch":
                return "42;cluster"
            if args[0] == "squeue":
                return "42|RUNNING|node01|00:02:00|08:00:00|None|compute"
            if args[0] == "scontrol":
                return "node01"
            return ""

        with mock.patch.object(core, "command", side_effect=fake):
            core.start("default")
            _, info = core.current()
            self.assertIsNone(info["used"]["cpu_percent"])
            core.stop()
        self.assertIn(["squeue", "--clusters", "cluster", "--noheader", "--jobs", "42",
                       "--format=%i|%T|%N|%M|%l|%R|%P"], calls)
        self.assertIn(["scontrol", "show", "hostnames", "node01"], calls)
        self.assertIn(["scancel", "--clusters", "cluster", "42"], calls)
        self.assertFalse(any(args[0] == "sstat" for args in calls))

    def test_ssh_metadata_and_config(self):
        with mock.patch.object(core, "command", return_value="42"):
            record = core.start("default")
        directory = Path(record["ready_dir"])
        (directory / "sim-42").write_text("42\n")
        metadata = {"job_id": "42", "port": 12345, "fingerprint": "SHA256:" + "a" * 43,
                    "host_key_alias": "sim-" + "b" * 43}
        (directory / "sim-42.ssh.json").write_text(json.dumps(metadata))
        info = {"state": "RUNNING", "hostname": "node01"}
        with mock.patch("sim.cli.getpass.getuser", return_value="testuser"):
            config = cli.ssh_config(record, info)
        self.assertIn("ProxyCommand ssh -W %h:%p testuser@node01", config)
        self.assertIn("Port 12345", config)
        metadata["job_id"] = "43"
        (directory / "sim-42.ssh.json").write_text(json.dumps(metadata))
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            cli.ssh_config(record, info)

    def test_missing_accounting_and_api(self):
        self.assertEqual(core.seconds("2-01:02:03"), 176523)
        with mock.patch.object(core, "command", side_effect=RuntimeError("sstat unavailable")):
            self.assertEqual(core.usage({"job_id": "1"}, "00:02:00"),
                             {"memory_bytes": None, "cpu_percent": None})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = cli.main(["api", "list-profiles"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(out.getvalue()), {"output": ["default"]})
        out = io.StringIO()
        with mock.patch.object(core, "command", side_effect=RuntimeError("scheduler offline")):
            with contextlib.redirect_stdout(out):
                result = cli.main(["api", "start"])
        self.assertEqual(result, 1)
        self.assertIn("scheduler offline", json.loads(out.getvalue())["error"])

    def test_cli_with_fake_slurm_commands(self):
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        fake = """#!/bin/sh
case "${0##*/}" in
    sbatch) cat >/dev/null; echo 937 ;;
    squeue) echo '937|PENDING|(null)|0:00|8:00:00|Resources|compute' ;;
    scancel) exit 0 ;;
    *) exit 1 ;;
esac
"""
        for name in ("sbatch", "squeue", "scancel"):
            executable = bin_dir / name
            executable.write_text(fake)
            executable.chmod(0o700)
        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        command = [os.sys.executable, "-m", "sim.cli"]
        start = subprocess.run(command + ["start"], env=env, text=True, capture_output=True, check=True)
        self.assertIn("Submitted job 937", start.stdout)
        self.assertIn("PENDING", start.stdout)
        info = subprocess.run(command + ["api", "info"], env=env, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(info.stdout)["output"][0]["job_id"], "937")
        subprocess.run(command + ["stop"], env=env, text=True, capture_output=True, check=True)
        self.assertIsNone(core.state())

    def test_batch_container_lifecycle(self):
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        log = Path(self.tmp.name) / "apptainer.log"
        executable = bin_dir / "apptainer"
        executable.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_LOG"\n')
        executable.chmod(0o700)
        ready = core.directory("instances")
        batch = core.script(core.validate({"image": "example.sif"}), ready)
        env = os.environ.copy()
        env.update(PATH=str(bin_dir) + os.pathsep + env["PATH"], SLURM_JOB_ID="123", TEST_LOG=str(log))
        proc = subprocess.Popen(["bash"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, env=env)
        try:
            proc.stdin.write(batch)
            proc.stdin.close()
            marker = ready / "sim-123"
            deadline = time.monotonic() + 5
            while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "container did not become ready")
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            proc.stderr.close()
        self.assertFalse(marker.exists())
        self.assertIn("instance stop sim-123", log.read_text())


if __name__ == "__main__":
    unittest.main()
