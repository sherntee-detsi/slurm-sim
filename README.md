# sim — Slurm interactive macros

`sim` is a Slurm counterpart to PBS Workbench. It submits a single-node,
single-task allocation that stays alive for interactive work, tracks it
separately from PBS, and provides a monitor, profiles, a JSON API, and optional
Apptainer/VS Code SSH.

## Requirements and installation

Python 3.10+, Slurm client commands (`sbatch`, `squeue`, `scontrol`, `scancel`,
and optionally `sinfo` and `sstat`), and a shared writable home on login and
compute nodes are required. Install from this directory with `pipx install .`
or `uv tool install .`. Alternatively, run `PYTHONPATH=src python -m sim.cli`.

The default state directory is `~/.sim`, separate from PBS Workbench and the
source checkout.
Set `SIM_HOME` to an absolute, private, shared directory to change it. The
directory must be owned by you and inaccessible to other users. Job logs are in
`$SIM_HOME/logs` (or `~/.sim/logs`).

## Use

1. Run `sim profile` (or `sim profile NAME`) to edit a profile. Enter the
   partition and account required by your site; leaving them empty uses Slurm's
   defaults. CPU, memory, walltime, GPU request, Apptainer image, flags and
   module are configurable. Press Enter to retain a value or `-` to clear it.
   Profiles are JSON in `~/.sim/profiles/` and can also be edited directly.
2. Run `sim start` (or `sim start NAME`). The monitor shows the job state and,
   once running, commands for a Slurm shell, SSH, a tunnel, VS Code and the
   container shell. The Slurm shell uses `srun --overlap`, which requires site
   and Slurm-version support.
   Enter or Ctrl+C leaves the allocation running; `sim monitor` reopens it.
3. Run `sim end` or `sim stop` to cancel it. `sim list` shows the active job.

Only one sim allocation is tracked at a time. CPU and memory figures are
best-effort `sstat` batch-step average CPU and maximum RSS, not necessarily
whole-job usage; they are unavailable when site accounting does not report
them. No site-specific service-unit estimate is made. The compute node must be
reachable over SSH for the connection commands to work. Follow your site's
rules about SSH sessions and verify that interactive processes remain in the
allocation's Slurm cgroup.

The profile's `flags` are parsed using shell-style quoting, not shell expansion.
An Apptainer module is loaded only if specified; otherwise `apptainer` must
already be available on the compute node. The image must be accessible there.
GPU syntax is passed to Slurm's `--gpus`; site-specific resource settings may
require an alternative configuration not currently exposed by sim.

## VS Code inside Apptainer

Enable container SSH in a profile with an image, then start the job and wait
for it to become ready. `sim ssh-config` prints a `Host sim` block for your
**local** SSH configuration. First configure local SSH access to the compute
node through your cluster login node (typically using `ProxyJump`), then copy
the generated block before any general `Host *` rule. In VS Code use Remote-SSH
to connect to `sim`. Verify the displayed host-key fingerprint on first
connection, and regenerate the block after starting another job.

The container uses your cluster account's `~/.ssh/authorized_keys`; install
your public key using your site's approved procedure and ensure SSH permissions
are correct. Never copy a private key to the cluster. The included
`containers/vscode.def` can be built with a site-supported Apptainer build
method. A custom image needs Python 3 at `/usr/bin/python3`, `/usr/sbin/sshd`,
`ssh-keygen`, `/run/sshd`, Bash, tar, curl or wget, compatible system libraries,
and the user's account and login shell. `$HOME` is bound into the instance.
If `SIM_HOME` is outside `$HOME`, it must also be bind-mounted into the image.

The unprivileged listener binds to compute-node loopback, but other users on
the node can reach it. It uses public-key authentication and **`UsePAM no`**:
it does not enforce host PAM/MFA or session policies. Confirm it is allowed by
your administrators, that SSH forwarding is enabled, and that container SSH
sessions remain in the Slurm cgroup. Container and registry failures are
reported in the job log.

## JSON API

`sim api info`, `sim api start [PROFILE]`, `sim api end`,
`sim api list-profiles`, and `sim api version` emit `{"output": ...}`.
Errors return `{"error": "...", "output": []}` and a nonzero exit status.
Use `sim api --timeout SECONDS ...` to adjust the scheduler command timeout.
API start does not open the monitor. Usage fields may be `null`.

If `sbatch` times out or returns an unexpected response, the submission may
have succeeded. sim keeps `current.json` marked as unresolved and refuses to
submit or cancel another job. Inspect your Slurm queue and the file under
`SIM_HOME`, resolve any submitted job, and only then remove the unresolved
`current.json` manually. Scheduler errors never cause sim to forget a tracked
job.

This implementation has not been validated against every Slurm site or a
live container-enabled cluster; partitions, accounting, SSH and cgroup
policies vary by site.
