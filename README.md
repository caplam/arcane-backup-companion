# Arcane Backup Companion (ABC)

Unified, automatic backup for Docker projects managed by [Arcane](https://github.com/arcane) — multi-host.

**[Français](README.fr.md)** · English

Arcane Backup Companion (ABC) discovers environments and projects automatically through the Arcane API, backs up their data (compose files, .env, bind mounts, named Docker volumes) with GFS retention, and supports restore in place or extraction to another host.

## Features

- **Multi-host** : backs up Docker projects spread across multiple servers (local direct access or SSH)
- **Auto-discovery** : environments and projects listed from the Arcane API
- **Essential data only** : compose files, .env, bind mounts, named Docker volumes
- **GFS retention** : Grandfather-Father-Son (daily / weekly / monthly)
- **Restore** : in place (with stop/start) or extract to a directory (migration)
- **Hot SQLite backup** : consistent backup of the Arcane database without downtime
- **Per-host logs** : one log per environment, automatically purged when the associated snapshots are gone
- **Backup tiers** : `daily` / `weekly` / `all` based on each project's retention policy

## Requirements

- Python 3.10+
- An [Arcane](https://github.com/arcane) server with an API key
- Access to the Docker hosts : local filesystem (direct access) or SSH (public key)

## Installation

**Recommended: virtual environment (venv)** — isolates dependencies, survives
reboots (essential on Unraid where the system Python lives in tmpfs and loses
installed packages on every reboot), and keeps the system Python clean.

```bash
git clone <repo-url> arcane-backup-companion
cd arcane-backup-companion

# Create a venv inside the project + install dependencies
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# Verify
./venv/bin/python -c "import yaml; print('yaml OK', yaml.__version__)"
```

Use `./venv/bin/python` for all commands (not the system `python3`).

### Unraid specifics (production)

On Unraid the project should be deployed on a **persistent share** (e.g. on the
array/cache pool, not on a USB flash device) and run via **User Scripts**.
⚠️ **Do NOT** install dependencies into the system Python (`pip3 install pyyaml`)
— `/usr/` lives in tmpfs, everything is wiped on reboot. Use a venv on the persistent share:

```bash
cd <persistent-share>/arcane-backup-companion
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

**Unraid User Scripts** (one per environment) — the command MUST point to the venv:

```bash
<persistent-share>/arcane-backup-companion/venv/bin/python <persistent-share>/arcane-backup-companion/main.py --run
```

> **Symptom if using `python3 main.py --run`**: after a reboot,
> `ModuleNotFoundError: No module named 'yaml'` (pyyaml wiped from tmpfs).
> Fix: point the user script to `venv/bin/python` (see above).


## Configuration

```bash
./venv/bin/python main.py --setup
```

The interactive setup :
1. Asks for the Arcane API URL and API key
2. Discovers environments (hosts) and projects
3. For each project : selects the bind mounts and volumes to back up
4. Configures GFS retention
5. Generates `config.yaml` (ignored by git)

## Usage

> All commands use `./venv/bin/python` (see Installation).
> Replace with `python3` only if you chose a different execution method.

```bash
# Back up all projects (all tiers by default)
./venv/bin/python main.py --run

# Back up a specific environment (all tiers)
./venv/bin/python main.py --run --env <name>

# Back up multiple environments
./venv/bin/python main.py --run --env <h1> --env <h2>

# Tiers
./venv/bin/python main.py --run --tier all       # every project (default)
./venv/bin/python main.py --run --tier daily     # daily projects only
./venv/bin/python main.py --run --tier weekly    # weekly projects only

# Estimate without backing up
./venv/bin/python main.py --run --dry-run

# Show current configuration
./venv/bin/python main.py --status
```

## Backup tiers and scheduling

There are **three distinct concepts** that interact. Understanding them avoids surprises when setting up cron jobs.

### 1. Project `schedule` (configured per project)

Each project has a retention schedule set during `--setup` :

```yaml
retention:
  schedule: daily   # daily | weekly
  daily: 7          # keep 7 daily snapshots
  weekly: 4         # keep 4 weekly snapshots
  monthly: 6        # keep 6 monthly snapshots
```

The `schedule` field only controls **which tier the project belongs to** for filtering — it does **not** schedule anything by itself. ABC never runs by itself : a cron job (or User Script) must launch it.

### 2. `--tier` (backup filter per run)

The `--tier` flag filters which projects are backed up during a run :

| Tier | Projects backed up |
|---|---|
| `all` (**default**) | Every project, regardless of `schedule` |
| `daily` | Only projects with `schedule: daily` |
| `weekly` | Only projects with `schedule: weekly` |

⚠️ **Danger** : if you run a daily cron with `--tier daily`, projects with `schedule: weekly` are **never** backed up. With the default tier `all`, every project is backed up on every run.

### 3. GFS retention (prune, runs after each project)

GFS retention **never decides when** to back up — it only **cleans up after each run**, keeping at most `daily` + `weekly` + `monthly` snapshots per project and deleting the surplus.

**Consequence** : snapshot frequency = cron frequency. If a project is backed up daily but has `schedule: weekly`, a snapshot is created every day, then the GFS prune deletes all but one snapshot per week (and one per month). This is functionally correct but slightly wasteful (the daily backups of a weekly project are discarded).

### Recommended cron setups

| Setup | Pros | Cons |
|---|---|---|
| **1 daily cron per env, no `--tier` (default `all`)** | Simple, everything is covered, GFS regulates retention | Weekly projects are backed up daily then pruned (wasteful) |
| **2 crons per env** : daily `--tier daily` + weekly `--tier weekly` | Economical, weekly projects only run when intended | More scripts to maintain |

For a homelab, **one daily cron per environment** (default `all`) is usually the best trade-off.

## Restore

```bash
# Interactive menu
./venv/bin/python main.py --restore

# Restore the most recent snapshot of a project
./venv/bin/python main.py --restore <env>/<project>

# Restore a specific snapshot
./venv/bin/python main.py --restore <env>/<project>/<snapshot>

# Extract to a directory (without touching the project — migration)
./venv/bin/python main.py --restore <env>/<project> --to /tmp/test
```

Restore in place :
1. Backs up the current state (`pre_restore_*`)
2. Stops the project (Arcane API)
3. Restores compose, .env, bind mounts, volumes
4. Starts the project again

Bind mount permissions and extended attributes are preserved (`tar --xattrs`).

## Architecture

```
main.py       ← Entry point (--setup, --run, --status, --restore)
config.py     ← Interactive setup + YAML loading
backup.py     ← Backup loop (tar, SSH, API)
api.py        ← Arcane API wrapper (curl)
restore.py    ← Restore in place / extraction
prune.py      ← GFS retention + metadata
models.py     ← Data classes (Config, Project, BindMount, ...)
```

## Advanced configuration

### Global exclusions

In `config.yaml`, `global_exclusions` lists bind mount paths that should never be backed up (media, downloads, large regenerable volumes) :

```yaml
global_exclusions:
  - "/var/lib/data/media/"
  - "/var/lib/data/downloads/"
```

### Host layout discovery

Hosts do not all share the same directory layout (`/data/stacks`, `/docker-projects/stacks`, ...). ABC discovers each host's project directory dynamically from the Arcane agent bind mounts — nothing is hard-coded.

### GFS retention

Each project has its own retention policy :

```yaml
retention:
  schedule: daily   # daily | weekly
  daily: 7          # keep 7 daily snapshots
  weekly: 4         # keep 4 weekly snapshots
  monthly: 3        # keep 3 monthly snapshots
```

### Notifications

Notifications use **Unraid's native notification system only** (the `notify` script at `/usr/local/emhttp/webGui/scripts/notify`). They are sent after each backup run when at least one project was processed.

- Enable/disable them during setup, or with the `notifications_enabled` key in `config.yaml`:

```yaml
notifications_enabled: true    # or false
```

- On non-Unraid hosts, keep this set to `false` (the `notify` script does not exist there).

## Security

- The Arcane API key lives in `config.yaml` (ignored by git)
- Snapshots may contain sensitive data — protect the `backup_root` directory
- File permissions are preserved on restore

## Notifications

Notifications use the **native Unraid notification system** (`/usr/local/emhttp/webGui/scripts/notify`) — Apprise or other webhook services are not supported.

You can enable or disable notifications during `--setup` (or by setting `notifications_enabled` in `config.yaml`). On non-Unraid hosts, leave it disabled.

## License

MIT
