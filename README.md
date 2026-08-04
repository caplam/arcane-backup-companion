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

```bash
git clone <repo-url> arcane-backup-companion
cd arcane-backup-companion
pip install -r requirements.txt
```

## Configuration

```bash
python3 main.py --setup
```

The interactive setup :
1. Asks for the Arcane API URL and API key
2. Discovers environments (hosts) and projects
3. For each project : selects the bind mounts and volumes to back up
4. Configures GFS retention
5. Generates `config.yaml` (ignored by git)

## Usage

```bash
# Back up all projects (daily tier by default)
python3 main.py --run

# Back up a single environment
python3 main.py --run --env <name>

# Back up several environments
python3 main.py --run --env <h1> --env <h2>

# Tiers
python3 main.py --run --tier daily     # daily projects (default)
python3 main.py --run --tier weekly    # weekly projects
python3 main.py --run --tier all       # every project

# Estimate size without backing up
python3 main.py --run --dry-run

# Show the current configuration
python3 main.py --status
```

## Restore

```bash
# Interactive menu
python3 main.py --restore

# Restore the most recent snapshot of a project
python3 main.py --restore <env>/<project>

# Restore a specific snapshot
python3 main.py --restore <env>/<project>/<snapshot>

# Extract to a directory (does not touch the project — migration)
python3 main.py --restore <env>/<project> --to /tmp/test
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
