"""
backup.py — Boucle de sauvegarde.

Orchestre le backup de tous les environnements et projets configurés.
Chaque projet suit le même cycle :
  1. Sauvegarde compose + .env (filesystem ou API)
  2. Stop projet (API Arcane)
  3. Backup bind mounts (tar local ou SSH)
  4. Backup volumes Docker (API Arcane)
  5. Start projet (API Arcane + retry)
  6. Prune vieux snapshots (rétention)

Gestion des erreurs : un projet en échec ne bloque pas les suivants.
Un rapport détaillé est produit en fin de backup.
"""

import os
import re
import sys
import json
import shutil
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from models import Config, Environment, Project, BindMount, DockerVolume, AccessMode, Retention
from api import (
    ArcaneError, check_connection,
    get_project_compose, get_project_runtime,
    stop_project, start_project,
    create_volume_backup, download_volume_backup, delete_volume_backup,
    get_container_details, discover_host_paths
)
from prune import run_prune, save_metadata, RetentionPolicy

# ─── Logger ────────────────────────────────────────────────────────────────────

logger = logging.getLogger("arkbackup")


def _setup_logging(backup_root: str, env_names: Optional[list[str]] = None,
                   run_id: str = "") -> str:
    """
    Configure le logging : fichier + console.

    Retourne le chemin du fichier de log.
    Format (rétro-compatible, multi-env) : backup_<env1>_<env2>_<run_id>.log
    Format (un seul env) : <backup_root>/<env>/backup_<run_id>.log

    Le log d'un environnement est placé dans son répertoire pour rester
    associé aux snapshots qu'il documente (purge possible par run_id).
    """
    timestamp = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")

    safe_names = [
        re.sub(r"[^a-zA-Z0-9_-]", "_", n.lower())
        for n in (env_names or []) if n
    ]

    if len(safe_names) == 1:
        # Un seul environnement : log dans le répertoire de l'environnement
        log_dir = os.path.join(backup_root, safe_names[0])
        log_name = f"backup_{timestamp}.log"
    else:
        # Multi-env (ou aucun) : rétro-compatible à la racine
        log_dir = backup_root
        suffix = "_".join(safe_names)
        log_name = f"backup_{suffix}_{timestamp}.log" if suffix else f"backup_{timestamp}.log"

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, log_name)

    os.makedirs(backup_root, exist_ok=True)

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Handler fichier (tout)
    fh = logging.FileHandler(log_path)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    # Handler console (INFO et +)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)

    logger.setLevel(logging.DEBUG)
    logger.addHandler(fh)
    logger.addHandler(ch)

    return log_path


def _purge_orphan_logs(backup_root: str, current_log_path: str) -> None:
    """
    Supprime les logs de backup dont tous les snapshots associés ont été purgés.

    Un log est lié à ses snapshots par le run_id stocké dans metadata.json :
      - le nom du log est backup_<run_id>.log (dans <backup_root>/<env>/)
      - chaque snapshot du run contient metadata.json avec "run_id"

    Règle : si plus aucun snapshot (metadata.json) ne porte le run_id d'un log,
    le log est orphelin → supprimé. Le log du run en cours est toujours exclu.
    """
    if not os.path.isdir(backup_root):
        return

    pattern = re.compile(r"^backup_(\d{8}_\d{6})\.log$")

    for env_dir in sorted(os.listdir(backup_root)):
        env_path = os.path.join(backup_root, env_dir)
        if not os.path.isdir(env_path):
            continue

        # Collecter tous les run_ids présents dans les snapshots de cet env
        snapshot_run_ids = set()
        for root, dirs, files in os.walk(env_path):
            if "metadata.json" in files:
                meta_path = os.path.join(root, "metadata.json")
                try:
                    with open(meta_path) as f:
                        meta = json.load(f)
                    rid = meta.get("run_id")
                    if rid:
                        snapshot_run_ids.add(str(rid))
                except (json.JSONDecodeError, OSError):
                    pass

        # Purger les logs de cet env dont le run_id n'a plus aucun snapshot
        for log_name in os.listdir(env_path):
            if not log_name.startswith("backup_") or not log_name.endswith(".log"):
                continue
            log_full = os.path.join(env_path, log_name)
            if os.path.abspath(log_full) == os.path.abspath(current_log_path):
                continue  # jamais le log du run en cours

            m = pattern.match(log_name)
            if not m:
                # Ancien format (sans run_id exploitable) : ne pas toucher
                continue
            rid = m.group(1)
            if rid not in snapshot_run_ids:
                try:
                    os.remove(log_full)
                    logger.info(f"  🧹 Log orphelin supprimé : {log_full}")
                except OSError as e:
                    logger.warning(f"  ⚠️  Impossible de supprimer {log_full} : {e}")


# ─── Exclusions globales ─────────────────────────────────────────────────────────

def _is_excluded(path: str, exclusions: list[str]) -> bool:
    """
    Vérifie si un chemin correspond à un pattern d'exclusion globale.

    Un chemin est exclu s'il commence par l'un des patterns.
    Exemple: /var/lib/data/media/SERIES → exclu par /var/lib/data/media/
    """
    for pattern in exclusions:
        if pattern.endswith("/"):
            if path.startswith(pattern) or path == pattern.rstrip("/"):
                return True
        elif path == pattern or path.startswith(pattern + "/"):
            return True
    return False


# ─── Utilitaires ───────────────────────────────────────────────────────────────

def _timestamp() -> str:
    """Horodatage pour les dossiers de snapshot."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _env_backup_root(config: Config, env_name: str) -> str:
    """Retourne le répertoire de backup pour un environnement."""
    return os.path.join(config.backup_root, env_name)


def _project_backup_root(config: Config, env_name: str, project_name: str) -> str:
    """Retourne le répertoire de backup pour un projet."""
    return os.path.join(_env_backup_root(config, env_name), project_name)


def _snapshot_dir(config: Config, env_name: str, project_name: str) -> str:
    """Retourne le répertoire du snapshot en cours."""
    return os.path.join(_project_backup_root(config, env_name, project_name), _timestamp())


# ─── Progression ───────────────────────────────────────────────────────────────

class BackupReport:
    """
    Rapport de progression.

    Accumule les résultats (OK/ERROR/WARN) pour les afficher
    en fin de backup. Permet aussi de savoir combien de projets
    ont réussi/échoué.
    """

    def __init__(self):
        self.results = []  # list of dict: { env, project, status, message }

    def ok(self, env: str, project: str, message: str = "OK"):
        self.results.append({"env": env, "project": project, "status": "OK", "message": message})

    def warn(self, env: str, project: str, message: str):
        self.results.append({"env": env, "project": project, "status": "WARN", "message": message})

    def error(self, env: str, project: str, message: str):
        self.results.append({"env": env, "project": project, "status": "ERROR", "message": message})

    def print_summary(self):
        """Affiche le rapport final."""
        ok_count = sum(1 for r in self.results if r["status"] == "OK")
        warn_count = sum(1 for r in self.results if r["status"] == "WARN")
        err_count = sum(1 for r in self.results if r["status"] == "ERROR")

        print()
        print("╔══════════════════════════════════════════╗")
        print("║  Rapport de backup                       ║")
        print("╚══════════════════════════════════════════╝")
        print()

        if not self.results:
            print("  Aucun projet à sauvegarder.")
            return

        for r in self.results:
            icons = {"OK": "✅", "WARN": "⚠️", "ERROR": "❌"}
            print(f"  {icons.get(r['status'], '?')} {r['env']}/{r['project']}  — {r['message']}")

        print()
        print(f"  {ok_count} OK, {warn_count} avertissements, {err_count} erreurs")
        if err_count > 0:
            print(f"  Consulte le log pour les détails.")


# ─── Sauvegarde d'un projet ────────────────────────────────────────────────────

def _save_compose_files(config: Config, env: Environment, project: Project,
                        snap_dir: str) -> None:
    """
    Sauvegarde le compose.yaml et .env.

    Priorité :
      1. Filesystem (si le chemin est accessible)
      2. API Arcane (sinon)

    Si les deux échouent, on log un WARNING mais on continue —
    le backup des data peut être utile même sans le compose.
    """
    # Essai 1 : filesystem
    if env.access.mode == AccessMode.DIRECT and project.data_dir:
        compose_path = os.path.join(project.data_dir, project.compose_file_name)
        env_path = os.path.join(project.data_dir, ".env")
        found = False

        if os.path.isfile(compose_path):
            try:
                dest = os.path.join(snap_dir, project.compose_file_name)
                shutil.copy2(compose_path, dest)  # copy2 préserve metadata
                logger.info(f"  📄 Compose copié depuis {compose_path}")
                found = True
            except OSError as e:
                logger.warning(f"  ⚠️  Impossible de copier {compose_path} : {e}")

        if os.path.isfile(env_path):
            try:
                shutil.copy2(env_path, os.path.join(snap_dir, ".env"))
                logger.info(f"  📄 .env copié depuis {env_path}")
            except OSError as e:
                logger.warning(f"  ⚠️  Impossible de copier {env_path} : {e}")
        else:
            logger.info(f"  📄 Pas de fichier .env dans {project.data_dir}")

        if found:
            return

    # Essai 2 : API Arcane
    try:
        compose_data = get_project_compose(
            config.arcane_api_url, config.arcane_api_key,
            env.id, project.id
        )
        compose_content = compose_data.get("composeContent", "")
        env_content = compose_data.get("envContent", "")

        if compose_content:
            dest = os.path.join(snap_dir, compose_data.get("composeFileName", "compose.yaml"))
            with open(dest, "w") as f:
                f.write(compose_content)
            logger.info("  📄 Compose sauvegardé depuis l'API Arcane")

        if env_content:
            with open(os.path.join(snap_dir, ".env"), "w") as f:
                f.write(env_content)
            logger.info("  📄 .env sauvegardé depuis l'API Arcane")

        if not compose_content and not env_content:
            logger.warning("  ⚠️  Aucun compose ni .env trouvé (API et filesystem)")

    except ArcaneError as e:
        logger.warning(f"  ⚠️  Impossible de récupérer le compose via l'API : {e}")


def _backup_bind_mount_local(bind_mounts: list[BindMount], snap_dir: str,
                               exclusions: list[str] = None) -> None:
    """
    Sauvegarde les bind mounts sélectionnés (accès direct).

    Utilise tar avec --preserve-permissions et --xattrs pour
    conserver les permissions, owners, ACLs et extended attributes.

    Chaque bind mount est archivé dans un fichier nommé d'après
    son répertoire racine (ex: data_authentik.tar).
    """
    for bm in bind_mounts:
        if not bm.selected:
            logger.info(f"    ⏭️  {bm.path} — exclu par l'utilisateur")
            continue

        # Vérifier les exclusions globales
        if exclusions and _is_excluded(bm.path, exclusions):
            logger.info(f"    ⏭️  {bm.path} — exclu par la politique globale (media/downloads/iso)")
            continue

        if not (os.path.isfile(bm.path) or os.path.isdir(bm.path)):
            logger.warning(f"    ⚠️  {bm.path} — introuvable, ignoré")
            continue

        # Valider le chemin (entrée non fiable de l'API) — cohérent avec la version distante
        try:
            _sanitize_path(bm.path, "bind mount")
        except ValueError as e:
            logger.warning(f"    ⚠️  {e} — bind mount ignoré")
            continue

        # Nom du fichier : on prend le dernier segment du chemin
        dir_name = os.path.basename(bm.path.rstrip("/"))
        archive_name = f"data_{dir_name}.tar"
        archive_path = os.path.join(snap_dir, archive_name)

        logger.info(f"    📦 Archivage de {bm.path} → {archive_name}")

        # Estimer la taille avant de commencer
        try:
            du = subprocess.run(
                ["du", "-sb", bm.path],
                capture_output=True, text=True, timeout=10
            )
            size_bytes = int(du.stdout.split()[0])
            size_mb = size_bytes / 1024 / 1024
            if size_mb > 1000:
                logger.info(f"    📏 Taille estimée : {size_mb / 1024:.1f} Go")
            else:
                logger.info(f"    📏 Taille estimée : {size_mb:.0f} Mo")
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass

        # Tar avec préservation des permissions (pas de compression gzip)
        cmd = [
            "tar", "cpf", archive_path,
            "--xattrs",              # extended attributes
            "--sparse",              # détection des fichiers sparse
            "-C", os.path.dirname(bm.path.rstrip("/")),
            os.path.basename(bm.path.rstrip("/"))
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=3600  # 1h max
            )
            if result.returncode != 0:
                # Tar peut retourner 1 pour des warnings (ex: fichiers changés
                # pendant l'archivage) — c'est normal, on log en WARNING
                if result.returncode == 1:
                    logger.warning(f"    ⚠️  tar a retourné des warnings : {result.stderr[:500]}")
                else:
                    logger.error(f"    ❌ tar a échoué (code {result.returncode}) : {result.stderr[:500]}")
                    continue

            # Vérifier que l'archive a été créée
            if os.path.isfile(archive_path):
                size = os.path.getsize(archive_path)
                logger.info(f"    ✅ Archive créée : {size / 1024 / 1024:.1f} Mo")

        except subprocess.TimeoutExpired:
            logger.error(f"    ❌ tar a dépassé le timeout (1h) pour {bm.path}")
            # Nettoyer l'archive partielle si elle existe
            if os.path.isfile(archive_path):
                os.remove(archive_path)
        except OSError as e:
            logger.error(f"    ❌ Erreur système sur {bm.path} : {e}")


def _backup_bind_mount_remote(config: Config, env: Environment,
                               bind_mounts: list[BindMount], snap_dir: str) -> None:
    """
    Sauvegarde les bind mounts via SSH (environnement distant).

    Commande exécutée sur le LXC :
      tar czpf - /chemin/du/data

    La sortie de tar est redirigée vers un fichier local via SSH.
    Conserve les permissions grâce à l'option -p de tar.
    """
    access = env.access
    ssh_base = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -p {access.port}"

    if access.ssh_key_path:
        ssh_base += f" -i {access.ssh_key_path}"

    ssh_target = f"{access.username}@{access.hostname}"

    for bm in bind_mounts:
        if not bm.selected:
            logger.info(f"    ⏭️  {bm.path} — exclu par l'utilisateur")
            continue

        # Vérifier les exclusions globales
        if _is_excluded(bm.path, config.global_exclusions):
            logger.info(f"    ⏭️  {bm.path} — exclu par la politique globale (media/downloads/iso)")
            continue

        dir_name = os.path.basename(bm.path.rstrip("/"))
        archive_name = f"data_{dir_name}.tar"
        archive_path = os.path.join(snap_dir, archive_name)

        logger.info(f"    📦 Archivage distant de {bm.path} → {archive_name}")

        # Valider le chemin AVANT toute commande shell (entrée non fiable de l'API)
        try:
            _sanitize_path(bm.path, "bind mount")
        except ValueError as e:
            logger.warning(f"    ⚠️  {e} — bind mount ignoré")
            continue

        # Vérifier que le chemin existe sur le LXC
        q_path = _ssh_quote(bm.path)
        check_cmd = f"{ssh_base} {ssh_target} 'test -e {q_path} && echo OK || echo NOT_FOUND'"
        try:
            check = subprocess.run(
                check_cmd, shell=True, capture_output=True, text=True, timeout=15
            )
            if "NOT_FOUND" in check.stdout:
                logger.warning(f"    ⚠️  {bm.path} — introuvable sur {env.name}, ignoré")
                continue
        except subprocess.TimeoutExpired:
            logger.warning(f"    ⚠️  {env.name} — timeout vérification chemin, ignoré")
            continue

        # Tar à distance → fichier local
        parent_dir = os.path.dirname(bm.path.rstrip("/"))
        base_name = os.path.basename(bm.path.rstrip("/"))
        q_parent = _ssh_quote(parent_dir)
        q_base = _ssh_quote(base_name)
        tar_cmd = (
            f"{ssh_base} {ssh_target} "
            f"'tar cpf - --xattrs --sparse -C {q_parent} "
            f"{q_base}' "
            f"> {archive_path}"
        )

        try:
            result = subprocess.run(
                tar_cmd, shell=True, capture_output=True, text=True, timeout=3600
            )
            if result.returncode != 0:
                # SSH retourne le code de tar
                if result.returncode == 1:
                    logger.warning(f"    ⚠️  tar distant a retourné des warnings : {result.stderr[:500]}")
                else:
                    logger.error(f"    ❌ tar distant a échoué (code {result.returncode})")
                    logger.error(f"       stderr: {result.stderr[:500]}")
                    if os.path.isfile(archive_path):
                        os.remove(archive_path)
                    continue

            if os.path.isfile(archive_path):
                size = os.path.getsize(archive_path)
                logger.info(f"    ✅ Archive créée : {size / 1024 / 1024:.1f} Mo")

        except subprocess.TimeoutExpired:
            logger.error(f"    ❌ tar distant a dépassé le timeout (1h) pour {bm.path}")
            if os.path.isfile(archive_path):
                os.remove(archive_path)


def _backup_volumes(config: Config, env: Environment,
                    volumes: list[DockerVolume], snap_dir: str) -> None:
    """
    Sauvegarde les volumes Docker via l'API Arcane.

    Pour chaque volume sélectionné :
      1. Créer un backup via l'API
      2. Télécharger le backup
      3. Supprimer le backup distant (pour ne pas accumuler)
    """
    for vol in volumes:
        if not vol.selected:
            logger.info(f"    ⏭️  volume:{vol.name} — exclu par l'utilisateur")
            continue

        logger.info(f"    💾 Backup du volume Docker '{vol.name}'...")

        # Créer le backup
        try:
            backup_id = create_volume_backup(
                config.arcane_api_url, config.arcane_api_key,
                env.id, vol.name
            )
        except ArcaneError as e:
            logger.error(f"    ❌ Échec création backup pour '{vol.name}' : {e}")
            continue

        if not backup_id:
            logger.error(f"    ❌ API n'a pas retourné d'ID de backup pour '{vol.name}'")
            continue

        logger.info(f"    📥 Téléchargement du backup {backup_id}...")

        # Télécharger
        dest_path = os.path.join(snap_dir, f"volume_{vol.name}.tar.gz")
        try:
            success = download_volume_backup(
                config.arcane_api_url, config.arcane_api_key,
                env.id, backup_id, dest_path
            )
        except ArcaneError as e:
            logger.error(f"    ❌ Échec téléchargement backup '{vol.name}' : {e}")
            continue

        if not success:
            logger.error(f"    ❌ Téléchargement échoué pour '{vol.name}'")
            continue

        # Vérifier le fichier téléchargé
        if os.path.isfile(dest_path):
            size = os.path.getsize(dest_path)
            logger.info(f"    ✅ Volume '{vol.name}' téléchargé : {size / 1024:.1f} Ko")

        # Nettoyer le backup distant
        try:
            delete_volume_backup(
                config.arcane_api_url, config.arcane_api_key,
                env.id, backup_id
            )
            logger.info(f"    🧹 Backup distant {backup_id} supprimé")
        except ArcaneError as e:
            logger.warning(f"    ⚠️  Impossible de supprimer le backup distant {backup_id} : {e}")


# ─── Métadonnées du projet (version tracking) ──────────────────────────────────

def _save_project_metadata(snap_dir: str, config: Config, env: Environment,
                            project: Project, was_stopped: bool,
                            run_id: str = "") -> None:
    """
    Sauvegarde les métadonnées du projet dans metadata.json.

    Récupère les versions des images Docker depuis les infos du projet
    Arcane, ou depuis le fichier compose si disponible.
    """
    images = {}

    # Tentative : récupérer les images depuis l'API Arcane
    try:
        compose_data = get_project_compose(
            config.arcane_api_url, config.arcane_api_key,
            env.id, project.id
        )
        compose_content = compose_data.get("composeContent", "")
        if compose_content:
            import yaml
            parsed = yaml.safe_load(compose_content)
            if isinstance(parsed, dict):
                services = parsed.get("services", {})
                for svc_name, svc_config in services.items():
                    if isinstance(svc_config, dict) and "image" in svc_config:
                        images[svc_name] = svc_config["image"]
    except Exception:
        pass

    save_metadata(
        snap_dir=snap_dir,
        project_name=project.name,
        images=images,
        compose_file=project.compose_file_name,
        env_present=os.path.isfile(os.path.join(snap_dir, ".env")),
        run_id=run_id,
    )
    if images:
        logger.info(f"  🏷️  Versions sauvegardées : {', '.join(f'{k}={v}' for k, v in images.items())}")
    else:
        logger.info("  🏷️  Aucune version d'image détectée")


# ─── Estimation de taille (dry-run) ──────────────────────────────────────────────

def _estimate_backup_size(config: Config, tier: str = "all") -> None:
    """
    Estime la taille totale des données à sauvegarder (dry-run).

    Parcourt tous les bind mounts sélectionnés et non exclus,
    additionne leur taille via du -sb. Affiche un rapport par
    environnement et projet.
    """
    import subprocess

    total_bytes = 0
    logger.info("📊 Estimation de la taille des données à sauvegarder...")
    logger.info("")

    for env in config.environments:
        env_bytes = 0
        env_projects = 0

        for project in env.projects:
            if project.skip:
                continue

            # Filtrer par tier
            if tier == "daily" and project.retention.schedule != "daily":
                continue
            if tier == "weekly" and project.retention.schedule != "weekly":
                continue

            project_bytes = 0
            for bm in project.bind_mounts:
                if not bm.selected:
                    continue
                if _is_excluded(bm.path, config.global_exclusions):
                    continue

                if env.access.mode == AccessMode.DIRECT and (os.path.isfile(bm.path) or os.path.isdir(bm.path)):
                    try:
                        du = subprocess.run(
                            ["du", "-sb", bm.path],
                            capture_output=True, text=True, timeout=10
                        )
                        size = int(du.stdout.split()[0])
                        project_bytes += size
                    except (subprocess.TimeoutExpired, OSError, ValueError):
                        pass

            if project_bytes > 0:
                env_projects += 1
                env_bytes += project_bytes
                if project_bytes > 1024**3:
                    logger.info(f"  📦 {env.name}/{project.name} : {project_bytes / 1024**3:.1f} Go")
                elif project_bytes > 1024**2:
                    logger.info(f"  📦 {env.name}/{project.name} : {project_bytes / 1024**2:.0f} Mo")
                else:
                    logger.info(f"  📦 {env.name}/{project.name} : {project_bytes / 1024:.0f} Ko")

        if env_bytes > 0:
            total_bytes += env_bytes
            if env_bytes > 1024**3:
                logger.info(f"  🌐 {env.name} total : {env_bytes / 1024**3:.1f} Go ({env_projects} projets)")
            else:
                logger.info(f"  🌐 {env.name} total : {env_bytes / 1024**2:.0f} Mo ({env_projects} projets)")
            logger.info("")

    if total_bytes > 1024**3:
        logger.info(f"📊 Estimation totale : {total_bytes / 1024**3:.1f} Go")
    elif total_bytes > 1024**2:
        logger.info(f"📊 Estimation totale : {total_bytes / 1024**2:.0f} Mo")
    else:
        logger.info(f"📊 Estimation totale : {total_bytes / 1024:.0f} Ko")

    # Avertissement si > 100 Go
    if total_bytes > 100 * 1024**3:
        logger.warning("⚠️  ATTENTION : le backup dépasse 100 Go !")
        logger.warning("   Vérifie que les exclusions médias sont bien configurées.")
        logger.warning("   Utilise --status pour voir les exclusions globales.")


def _ssh_cmd(env: Environment, command: str) -> str:
    """Construit une commande SSH pour un environnement distant.

    Les champs de l'environnement (hostname, username, port, clé) sont
    validés avant usage — défense en profondeur en plus de _safe_ssh_env.
    """
    try:
        _safe_ssh_env(env)
    except ValueError as e:
        raise ValueError(f"SSH refusé : {e}")
    access = env.access
    ssh = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -p {access.port}"
    if access.ssh_key_path:
        ssh += f" -i {access.ssh_key_path}"
    return f"{ssh} {access.username}@{access.hostname} '{command}'"


# ─── Sanitization des entrées (sécurité) ───────────────────────────────────────

# Les identifiants et chemins utilisés dans les commandes shell (SSH, tar)
# proviennent de l'API Arcane — des entrées non fiables. On les valide par
# whitelist stricte : tout caractère hors de ces ensembles est rejeté, ce qui
# empêche l'injection de commandes via un projet ou un bind mount malveillant.

# Nom de projet / service : lettres, chiffres, point, tiret, underscore
# (convention Docker Compose). Pas de slash, pas d'espace, pas de métacaractère.
_RE_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Chemin de filesystem : lettres, chiffres, slash, point, tiret, underscore,
# espace (U+0020) et '=' (présents dans de vrais chemins, ex: "Application Support", immich).
# Les métacaractères shell ($ ; ` | & < > ( ) { } ' " \ nouvelle-ligne, tab, CR) sont rejetés.
_RE_VALID_PATH = re.compile(r"^[A-Za-z0-9/._\-\x20=]+$")

# Hostname / utilisateur SSH : lettres, chiffres, point, tiret, underscore.
_RE_VALID_HOST = re.compile(r"^[A-Za-z0-9._-]+$")


def _sanitize_name(name: str, what: str = "nom") -> str:
    """Valide un identifiant (projet, service). Retourne le nom si sûr,
    sinon lève ValueError — l'appelant doit ignorer/skipper l'élément."""
    if not name or not _RE_VALID_NAME.match(name):
        raise ValueError(f"{what} invalide (caractères non autorisés) : {name!r}")
    return name


def _sanitize_path(path: str, what: str = "chemin") -> str:
    """Valide un chemin de filesystem utilisé dans une commande shell.
    Retourne le chemin si sûr, sinon lève ValueError.

    Rejette aussi les segments '..' (path traversal) : un bind mount
    légitime ne contient jamais '..'.
    """
    if not path or not _RE_VALID_PATH.match(path):
        raise ValueError(f"{what} invalide (caractères non autorisés) : {path!r}")
    # Path traversal : rejeter tout segment '..'
    for seg in path.split("/"):
        if seg == "..":
            raise ValueError(f"{what} invalide (segment '..') : {path!r}")
    return path


def _sanitize_host(host: str, what: str = "hôte") -> str:
    """Valide un hostname ou un utilisateur SSH."""
    if not host or not _RE_VALID_HOST.match(host):
        raise ValueError(f"{what} invalide (caractères non autorisés) : {host!r}")
    return host


def _safe_ssh_env(env: Environment) -> None:
    """Valide les paramètres SSH d'un environnement AVANT toute commande.
    Lève ValueError si un champ est dangereux — le run est annulé pour cet env."""
    _sanitize_host(env.access.hostname, "hostname SSH")
    _sanitize_host(env.access.username, "utilisateur SSH")
    port = int(env.access.port)
    if not (1 <= port <= 65535):
        raise ValueError(f"port SSH invalide : {env.access.port}")
    if env.access.ssh_key_path:
        _sanitize_path(env.access.ssh_key_path, "chemin clé SSH")


def _ssh_quote(value: str) -> str:
    """Quote une valeur pour être interpolée dans une commande SSH.

    La commande distante est construite comme  ssh host 'cmd {value}'  —
    le shell LOCAL interprète les quotes simples, puis le shell DISTANT
    reçoit la commande. Pour que la valeur survive intacte aux DEUX
    couches, on remplace chaque ' par '\'' (la séquence standard qui
    ferme, quote, et rouvre la chaîne simple).

    À utiliser sur les chemins/noms validés avant interpolation.
    """
    return value.replace("'", "'\\''")


# ─── Backup d'un bind mount (local) ─────────────────────────────────────────────

# Cache des chemins hôte découverts, par environnement (une seule requête API par env)
_HOST_PATHS_CACHE: dict = {}


def _resolve_compose_dir(config: Config, env: Environment, project: Project) -> str:
    """
    Résout le répertoire hôte du compose d'un projet distant.

    Priorité :
      1. project.compose_dir si renseigné (chemin hôte explicite)
      2. Découverte depuis les bind mounts de l'agent arcane (cache par env)
      3. Fallback : répertoire relatif ./stacks/<nom> (avertissement)

    Les hôtes n'ont pas tous la même arborescence (ex: /data/stacks,
    /docker-projects/stacks, ...). Le chemin réel est exposé par le
    compose de l'agent (mount -> /app/data/projects).
    """
    if project.compose_dir:
        # compose_dir vient de la config — valider avant usage dans les commandes SSH
        try:
            return _sanitize_path(project.compose_dir, "compose_dir")
        except ValueError as e:
            logger.warning(f"  ⚠️  {e} — utilisation du fallback")
            project.compose_dir = ""  # forcer la découverte
    # fallthrough vers la découverte

    if env.id not in _HOST_PATHS_CACHE:
        _HOST_PATHS_CACHE[env.id] = discover_host_paths(
            config.arcane_api_url, config.arcane_api_key, env.id
        )

    projects_dir = _HOST_PATHS_CACHE[env.id].get("projects_dir")
    if projects_dir:
        # projects_dir vient de l'API (non fiable) — valider le chemin complet
        candidate = f"{projects_dir}/{project.name}"
        try:
            return _sanitize_path(candidate, "répertoire projet découvert")
        except ValueError as e:
            logger.warning(f"  ⚠️  {e} — utilisation du fallback")

    logger.warning(
        f"  ⚠️  Répertoire projet non détecté pour {env.name}/{project.name} — "
        f"utilisation du fallback relatif ./stacks/{project.name}"
    )
    # Valider le nom dans la fonction elle-même (défense en profondeur :
    # ne pas dépendre d'un appelant qui aurait oublié _sanitize_name)
    try:
        _sanitize_name(project.name, "nom de projet (fallback)")
    except ValueError as e:
        raise ValueError(f"{e}")
    return f"./stacks/{project.name}"


def backup_project(config: Config, env: Environment, project: Project,
                   snap_dir: str, report: BackupReport,
                   run_id: str = "") -> None:
    """
    Sauvegarde un projet complet.

    Cycle :
      1. Compose + .env
      2. Stop (API Arcane)
      3. Bind mounts (tar local ou SSH)
      4. Volumes Docker (API)
      5. Start (API Arcane + retry)

    Si le stop échoue, on log et on continue (le projet peut être déjà arrêté).
    Si le start échoue après 3 tentatives, c'est une erreur critique.
    """
    env_name = env.name
    proj_name = project.name

    # Sanitization : les noms viennent de l'API Arcane (entrées non fiables).
    # Un nom invalide pourrait injecter des commandes dans les appels SSH.
    try:
        _sanitize_name(proj_name, "nom de projet")
        _sanitize_name(project.compose_file_name, "nom du fichier compose")
        _sanitize_name(env_name, "nom d'environnement")
    except ValueError as e:
        logger.error(f"  ❌ {e} — projet ignoré")
        report.error(env_name, proj_name, f"Sanitization: {e}")
        return

    logger.info(f"")
    logger.info(f"━━━ [{env_name}] {proj_name} ━━━")

    # 1. Compose + .env
    logger.info("  📄 Étape 1/5 : Sauvegarde des fichiers compose")
    _save_compose_files(config, env, project, snap_dir)

    # Les agents Arcane ne doivent jamais être arrêtés (sinon l'API devient injoignable)
    is_agent = "arcane-agent" in proj_name.lower()

    if is_agent:
        logger.info("  ⚠️  Agent Arcane détecté — arrêt/démarrage via SSH")

    import time

    # 2. Vérifier l'état et arrêter si nécessaire
    logger.info("  ⏹️  Étape 2/5 : Arrêt du projet")

    # Vérifier l'état actuel du projet
    try:
        runtime = get_project_runtime(config.arcane_api_url, config.arcane_api_key,
                                       env.id, project.id)
        was_running = runtime.get("status") == "running"
        logger.info(f"  ℹ️  Statut: {runtime.get('status', '?')} ({runtime.get('runningCount', 0)}/{runtime.get('serviceCount', 0)} services)")
    except ArcaneError as e:
        logger.warning(f"  ⚠️  Impossible de vérifier l'état : {e}")
        was_running = False  # Prudent : supposer arrêté si on ne peut pas vérifier

    if was_running and not is_agent:
        # Arrêt via API Arcane (standard)
        try:
            stopped = stop_project(config.arcane_api_url, config.arcane_api_key,
                                    env.id, project.id)
            if stopped:
                logger.info("  ✅ Projet arrêté")
            else:
                logger.warning("  ⚠️  L'API n'a pas confirmé l'arrêt")
        except ArcaneError as e:
            logger.warning(f"  ⚠️  Échec de l'arrêt : {e}")
            logger.warning("  ⚠️  Backup quand même — données possiblement incohérentes")
        time.sleep(3)
    elif is_agent and was_running:
        # Arrêt via SSH (agent Arcane, pas d'API disponible)
        compose_path = _resolve_compose_dir(config, env, project)
        q_compose = _ssh_quote(compose_path)
        q_file = _ssh_quote(project.compose_file_name)
        ssh_cmd = _ssh_cmd(env, f"cd {q_compose} && docker compose -f {q_file} down")
        try:
            logger.info(f"  🔌 Arrêt via SSH: {compose_path}/{project.compose_file_name}")
            result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                logger.info("  ✅ Agent arrêté")
            else:
                logger.warning(f"  ⚠️  Échec arrêt SSH: {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            logger.warning("  ⚠️  Timeout arrêt SSH")
        time.sleep(3)
    else:
        logger.info("  ⏭️  Projet déjà arrêté — skip stop")

    # 3. Bind mounts
    logger.info("  💾 Étape 3/5 : Sauvegarde des bind mounts")
    if env.access.mode == AccessMode.DIRECT:
        _backup_bind_mount_local(project.bind_mounts, snap_dir, config.global_exclusions)
    else:
        _backup_bind_mount_remote(config, env, project.bind_mounts, snap_dir)

    # 4. Volumes Docker
    logger.info("  💿 Étape 4/5 : Sauvegarde des volumes Docker")
    _backup_volumes(config, env, project.volumes, snap_dir)

    # 5. Start
    if is_agent and was_running:
        # Démarrage via SSH (agent Arcane)
        compose_path = _resolve_compose_dir(config, env, project)
        q_compose = _ssh_quote(compose_path)
        q_file = _ssh_quote(project.compose_file_name)
        ssh_cmd = _ssh_cmd(env, f"cd {q_compose} && docker compose -f {q_file} up -d")
        logger.info(f"  ▶️  Démarrage agent via SSH: {compose_path}")
        try:
            result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                logger.info("  ✅ Agent redémarré")
            else:
                logger.warning(f"  ⚠️  Échec démarrage SSH: {result.stderr[:200]}")
                report.error(env_name, proj_name, f"Agent SSH start failed: {result.stderr[:100]}")
        except subprocess.TimeoutExpired:
            logger.error("  ❌ Timeout démarrage agent via SSH")
            report.error(env_name, proj_name, "Agent SSH start timeout")
    elif was_running and not is_agent:
        logger.info("  ▶️  Étape 5/5 : Démarrage du projet")
        delays = [5, 15, 30]
        api_started = False

        for attempt in range(3):
            try:
                api_started = start_project(config.arcane_api_url, config.arcane_api_key,
                                             env.id, project.id)
                if api_started:
                    break
            except ArcaneError as e:
                logger.warning(f"  ⚠️  Tentative {attempt + 1}/3 échouée : {e}")

            if attempt < 2:
                logger.info(f"  ⏳ Nouvelle tentative dans {delays[attempt]}s...")
                time.sleep(delays[attempt])

        # Vérifier que le projet a vraiment démarré
        if api_started:
            running_confirmed = False
            for check_attempt in range(6):  # 30s max (6 × 5s)
                time.sleep(5)
                try:
                    runtime = get_project_runtime(config.arcane_api_url, config.arcane_api_key,
                                                   env.id, project.id)
                    status = runtime.get("status")
                    running = runtime.get("runningCount", 0)
                    total = runtime.get("serviceCount", 0)
                    if status == "running" and running > 0:
                        logger.info(f"  ✅ Projet redémarré ({running}/{total} services)")
                        running_confirmed = True
                        break
                    else:
                        logger.info(f"  ⏳ Attente démarrage... ({running}/{total} services, status={status})")
                except ArcaneError:
                    pass

            if not running_confirmed:
                logger.warning(f"  ⚠️  Projet pas encore running après 30s — vérifie sur Arcane")
                report.warn(env_name, proj_name, "Start may be incomplete — check Arcane")
        else:
            logger.error("  ❌❌❌ ÉCHEC CRITIQUE : impossible de redémarrer le projet !")
            logger.error(f"  ❌❌❌ Redémarrage manuel nécessaire sur Arcane.")
            report.error(env_name, proj_name, "Start failed after 3 retries — manual restart needed")
    elif is_agent:
        logger.info("  ⏭️  Agent Arcane — pas de redémarrage nécessaire (déjà actif)")
        report.ok(env_name, proj_name)
        _save_project_metadata(snap_dir, config, env, project, was_running, run_id)
        project_dir = _project_backup_root(config, env_name, proj_name)
        run_prune(project_dir, project.retention.to_policy())
        return
    else:
        logger.info("  ⏭️  Projet était déjà arrêté — pas de redémarrage nécessaire")
        report.ok(env_name, proj_name)
        # Métadonnées et prune
        _save_project_metadata(snap_dir, config, env, project, was_running, run_id)
        project_dir = _project_backup_root(config, env_name, proj_name)
        run_prune(project_dir, project.retention.to_policy())
        return

    # 6. Métadonnées (version tracking) — pour les projets redémarrés
    _save_project_metadata(snap_dir, config, env, project, was_running, run_id)

    # 7. Prune GFS
    logger.info("  🧹 Nettoyage des vieux snapshots (GFS)")
    project_dir = _project_backup_root(config, env_name, proj_name)
    run_prune(project_dir, project.retention.to_policy())
    report.ok(env_name, proj_name)


# ─── Cas spécial : Arcane ──────────────────────────────────────────────────────

def backup_arcane(config: Config, snap_dir: str) -> None:
    """
    Backup hot de la base SQLite d'Arcane.

    Arcane stocke ses données dans un répertoire data_dir
    (ex: /var/lib/appdata/arcane/). La DB est une SQLite que
    l'on peut backuper à chaud sans arrêter le conteneur.

    Méthode : sqlite3 .backup (fiable, atomique, sans downtime).
    """
    logger.info("")
    logger.info("━━━ [local] arcane (hot backup SQLite) ━━━")

    # Répertoire des données Arcane : découverte depuis l'API (mount -> /app/data),
    # puis bind mounts de la config, sinon défaut conventionnel.
    data_dir = ""
    try:
        from api import discover_host_paths
        paths = discover_host_paths(config.arcane_api_url, config.arcane_api_key, "0")
        data_dir = paths.get("data_dir", "")
    except Exception:
        data_dir = ""
    if not data_dir:
        for env in config.environments:
            if env.access.mode == AccessMode.DIRECT:
                for proj in env.projects:
                    if "arcane" in proj.name.lower() and proj.bind_mounts:
                        data_dir = os.path.dirname(proj.bind_mounts[0].path)
                        break
                if data_dir:
                    break
    if not data_dir:
        data_dir = "/var/lib/arcane"  # défaut conventionnel
        logger.warning(f"  ⚠️  Répertoire Arcane non détecté, utilisation de {data_dir}")

    db_path = os.path.join(data_dir, "arcane.db")

    if not os.path.isfile(db_path):
        logger.warning(f"  ⚠️  Base de données introuvable : {db_path}")
        return

    # Backup via sqlite3 .backup (fiable, atomique)
    tmp_backup = "/tmp/arcane-hotbackup.db"
    try:
        result = subprocess.run(
            ["sqlite3", db_path, f".backup {tmp_backup}"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            logger.error(f"  ❌ Échec du backup SQLite : {result.stderr}")
            return
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.error(f"  ❌ Échec du backup SQLite : {e}")
        return

    # Archiver la DB temporaire
    archive_path = os.path.join(snap_dir, "arcane-db.tar.gz")
    try:
        result = subprocess.run(
            ["tar", "czpf", archive_path, "--xattrs",
             "-C", "/tmp", "arcane-hotbackup.db"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            logger.error(f"  ❌ Échec de l'archivage : {result.stderr}")
            return

        size = os.path.getsize(archive_path)
        logger.info(f"  ✅ Base Arcane sauvegardée : {size / 1024:.1f} Ko")
    except OSError as e:
        logger.error(f"  ❌ Erreur : {e}")
    finally:
        # Nettoyer le fichier temporaire
        if os.path.isfile(tmp_backup):
            os.remove(tmp_backup)

    # Backup des autres fichiers importants d'Arcane (si besoin)
    # composeFileName, projects directory, etc.
    compose_file = os.path.join(data_dir, "compose.yaml")
    if os.path.isfile(compose_file):
        try:
            shutil.copy2(compose_file, os.path.join(snap_dir, "arcane-compose.yaml"))
            logger.info("  ✅ Fichier compose d'Arcane sauvegardé")
        except OSError as e:
            logger.warning(f"  ⚠️  Impossible de copier le compose : {e}")


# ─── Boucle principale ─────────────────────────────────────────────────────────

def run_backup(config: Config, tier: str = "all", dry_run: bool = False) -> None:
    """
    Point d'entrée de la sauvegarde.

    Appelé par main.py --run.
    Parcourt tous les environnements et projets configurés,
    produit un rapport détaillé.

    Args:
        config: Configuration complète
        tier: "all" (tous les projets), "daily" (daily uniquement),
              "weekly" (weekly uniquement)
        dry_run: Si True, estime la taille sans rien sauvegarder
    """
    backup_root = config.backup_root
    # Identifiant unique de ce run — stocké dans metadata.json des snapshots
    # et dans le nom du log, pour permettre la purge des logs orphelins
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Nom des environnements qui seront sauvegardés (déjà filtrés par --env dans main.py)
    env_names = [e.name for e in config.environments]
    log_path = _setup_logging(backup_root, env_names, run_id)

    logger.info("╔══════════════════════════════════════════════════════╗")
    logger.info("║  Arcane Backup Companion (ABC) — Démarrage      ║")
    logger.info("╚══════════════════════════════════════════════════════╝")
    logger.info(f"  API Arcane : {config.arcane_api_url}")
    logger.info(f"  Destination : {backup_root}")
    logger.info(f"  Tier : {tier}")
    if dry_run:
        logger.info(f"  Mode : DRY-RUN (estimation uniquement)")
    logger.info(f"  Log : {log_path}")
    logger.info("")

    if dry_run:
        _estimate_backup_size(config, tier)
        return

    report = BackupReport()

    for env in config.environments:
        # Test de connexion pour les environnements distants
        if env.access.mode == AccessMode.SSH:
            logger.info(f"")
            logger.info(f"═══ 🌐 {env.name} (SSH: {env.access.hostname}) ═══")
            # Sanitization : valider les paramètres SSH avant toute commande
            try:
                _safe_ssh_env(env)
            except ValueError as e:
                logger.error(f"  ❌ {e} — environnement ignoré")
                for p in env.projects:
                    if not p.skip:
                        report.error(env.name, p.name, f"Sanitization: {e}")
                continue
            # Vérifier que le LXC est joignable
            ssh_base = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -p {env.access.port}"
            if env.access.ssh_key_path:
                ssh_base += f" -i {env.access.ssh_key_path}"
            ping_cmd = f"{ssh_base} {env.access.username}@{env.access.hostname} 'echo PONG'"
            try:
                ping = subprocess.run(ping_cmd, shell=True, capture_output=True, text=True, timeout=10)
                if "PONG" not in ping.stdout:
                    logger.error(f"  ❌ Hôte {env.name} injoignable (SSH). Environnement ignoré.")
                    logger.error(f"     stderr: {ping.stderr[:300]}")
                    for p in env.projects:
                        if not p.skip:
                            report.error(env.name, p.name, "Hôte injoignable")
                    continue
                logger.info(f"  ✅ Connexion SSH établie")
            except subprocess.TimeoutExpired:
                logger.error(f"  ❌ Hôte {env.name} — timeout SSH (10s). Environnement ignoré.")
                for p in env.projects:
                    if not p.skip:
                        report.error(env.name, p.name, "Timeout SSH")
                continue
        else:
            logger.info(f"")
            logger.info(f"═══ 🌐 {env.name} (accès direct) ═══")

        # Backup de chaque projet — les agents Arcane en dernier (doivent rester actifs)
        sorted_projects = sorted(env.projects, key=lambda p: "arcane-agent" in p.name.lower() if p.name else False)
        for project in sorted_projects:
            if project.skip:
                logger.info(f"  ⏭️  {project.name} — exclu par l'utilisateur")
                continue

            # Filtrer par tier de backup
            if tier == "daily" and project.retention.schedule != "daily":
                logger.info(f"  ⏭️  {project.name} — schedule={project.retention.schedule}, pas daily")
                continue
            if tier == "weekly" and project.retention.schedule != "weekly":
                logger.info(f"  ⏭️  {project.name} — schedule={project.retention.schedule}, pas weekly")
                continue

            # Créer le répertoire de snapshot
            snap_dir = _snapshot_dir(config, env.name, project.name)
            os.makedirs(snap_dir, exist_ok=True)

            try:
                if project.name.lower() == "arcane":
                    backup_arcane(config, snap_dir)
                    report.ok(env.name, project.name)
                else:
                    backup_project(config, env, project, snap_dir, report, run_id)
            except Exception as e:
                # Attraper toute exception non gérée pour ne pas bloquer
                # les autres projets
                import traceback
                logger.error(f"  ❌❌❌ Exception non gérée pour {project.name}")
                logger.error(f"      {traceback.format_exc()}")
                report.error(env.name, project.name, f"Exception: {e}")

                # Nettoyer le snapshot partiel
                if os.path.isdir(snap_dir):
                    try:
                        shutil.rmtree(snap_dir)
                        logger.info(f"  🧹 Snapshot partiel supprimé : {snap_dir}")
                    except OSError:
                        pass

    # Rapport final
    report.print_summary()
    logger.info("")
    logger.info("═══ Backup terminé ═══")
    logger.info(f"Log : {log_path}")

    # Purge des logs orphelins (snapshots associés purgés par la rétention GFS)
    try:
        _purge_orphan_logs(backup_root, log_path)
    except Exception as e:
        logger.warning(f"  ⚠️  Purge des logs orphelins échouée : {e}")

    # Notification Unraid (si activée)
    _send_notification(config, report)


def _send_notification(config: Config, report: BackupReport) -> None:
    """Envoie une notification via le système natif d'Unraid (notify)."""
    if not config.notifications_enabled:
        logger.info("  ℹ️  Notifications désactivées (config.notifications_enabled=false)")
        return

    ok_count = sum(1 for r in report.results if r["status"] == "OK")
    warn_count = sum(1 for r in report.results if r["status"] == "WARN")
    err_count = sum(1 for r in report.results if r["status"] == "ERROR")

    total = len(report.results)
    status = "✅" if err_count == 0 else "❌"
    subject = f"Arcane Backup Companion — {status} {ok_count}/{total} OK, {warn_count} warnings, {err_count} erreurs"

    # Résumé détaillé pour le body
    lines = []
    for r in report.results:
        icons = {"OK": "✅", "WARN": "⚠️", "ERROR": "❌"}
        lines.append(f"{icons.get(r['status'], '?')} {r['env']}/{r['project']}")
        if r["status"] != "OK":
            lines.append(f"   └─ {r['message']}")

    body = "\n".join(lines)

    # Envoyer via le script de notification Unraid
    notify_cmd = "/usr/local/emhttp/webGui/scripts/notify"
    try:
        result = subprocess.run(
            [notify_cmd, "-i", "normal", "-s", subject, "-d", body],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            logger.info("  📬 Notification envoyée")
            return
        else:
            logger.warning(f"  ⚠️  Échec notification : {result.stderr[:200]}")
    except FileNotFoundError:
        logger.info("  ℹ️  notify non trouvé")
    except subprocess.TimeoutExpired:
        logger.warning("  ⚠️  Timeout notification")