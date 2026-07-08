# Deploying MatrixOS to a Raspberry Pi

An Ansible playbook that syncs this repository to the Pi's home directory,
installs dependencies with [uv](https://docs.astral.sh/uv/), and runs MatrixOS
as a systemd service (auto-start on boot, restart on failure).

## Prerequisites

- Ansible on your workstation, plus the `ansible.posix` collection:
  ```bash
  ansible-galaxy collection install ansible.posix
  ```
- SSH access to the Pi and passwordless `sudo` there (the playbook uses
  `become` to install the systemd unit).
- `rsync` installed on both the workstation and the Pi.

## Usage

```bash
cd deploy/ansible
cp inventory.ini.example inventory.ini
# edit inventory.ini: set ansible_host / ansible_user (and matrix_os_port)

ansible-playbook -i inventory.ini playbook.yml
```

Dry run first if you like:

```bash
ansible-playbook -i inventory.ini playbook.yml --check --diff
```

## What it does

1. Rsyncs the repo to `~/matrix-os` on the Pi (excludes `.git`, `.venv`,
   caches, and `apps.json` so the Pi keeps its own live app configuration).
2. Installs `uv` if missing, then runs `uv sync` (installs the bundled
   `rgbmatrix` wheel for the Pi's `linux_armv7l`).
3. Installs `/etc/systemd/system/matrix.service` and enables + starts it.

The service runs as `root` because the RGB matrix needs GPIO access;
`DisplayConfig.drop_privileges` lowers privileges after the matrix initializes.

## After deploy

```bash
systemctl status matrix      # on the Pi
journalctl -u matrix -f      # follow logs
```

The web UI is available at `http://<pi-host>:<matrix_os_port>` (default 8000):
Display, **Status**, **Apps**, and Logs pages.

Re-running the playbook re-syncs and restarts the service.
