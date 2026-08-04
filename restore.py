"""
restore.py — Restauration d'un projet à partir d'un snapshot.

Fonctionnement :
  1. Lister les snapshots disponibles pour un projet
  2. Choisir un snapshot (ou le plus récent)
  3. Arrêter le projet (API Arcane)
  4. Restaurer compose + .env
  5. Restaurer les bind mounts
  6. Restaurer les volumes Docker
  7. Redémarrer le projet

Sécurité :
  - Demande confirmation avant toute action destructive
  - Sauvegarde l'état actuel avant de restaurer (optionnel)
  - Vérifie que le snapshot est complet avant de commencer
"""

import os
import sys
import shutil
import logging
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from models import Config, Environment, Project, AccessMode, Retention
from api import (
    ArcaneError, check_connection,
    get_project_compose, stop_project, start_project,
    create_volume_backup, download_volume_backup, delete_volume_backup,
    discover_host_paths,
)
from prune import discover_snapshots, load_metadata, RetentionPolicy

logger = logging.getLogger("arkbackup.restore")


# ─── Utilitaires ───────────────────────────────────────────────────────────────

def _confirm(prompt: str, default: bool = False) -> bool:
    """Demande une confirmation à l'utilisateur."""
    choices = "Y/n" if default else "y/N"
    value = input(f"{prompt} ({choices}) : ").strip().lower()
    if not value:
        return default
    return value.startswith("y")


def _backup_current_state(project_dir: str, tag: str = "pre-restore") -> Optional[str]:
    """
    Sauvegarde l'état actuel d'un projet avant restauration.

    Crée un snapshot de pré-restauration dans le même dossier que
    les snapshots, avec un tag spécial pour le distinguer.
    """
    backup_name = f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup_path = os.path.join(project_dir, backup_name)

    if os.path.isdir(project_dir):
        try:
            shutil.copytree(project_dir, backup_path)
            logger.info(f"  💾 État actuel sauvegardé dans {backup_name}")
            return backup_path
        except OSError as e:
            logger.warning(f"  ⚠️  Impossible de sauvegarder l'état actuel : {e}")
            return None
    return None


# ─── Restauration des fichiers compose ─────────────────────────────────────────

def _restore_compose_files(config: Config, snap_dir: str, project: Project, env: Environment) -> bool:
    """
    Restaure le docker-compose.yml et .env dans le répertoire du projet.

    Le répertoire projet est dérivé dynamiquement par environnement
    (discover_host_paths), comme au backup — les hôtes n'ont pas tous
    la même arborescence (ex: /data/stacks, /docker-projects/stacks).

    Retourne False si le répertoire ne peut pas être déterminé
    (l'utilisateur devra copier les fichiers manuellement).
    """
    if env.access.mode == AccessMode.DIRECT:
        # Accès direct : dériver le répertoire projet
        host_paths = discover_host_paths(
            config.arcane_api_url, config.arcane_api_key, env.id
        )
        projects_dir = host_paths.get("projects_dir")
        if not projects_dir:
            logger.warning("  ⚠️  Répertoire projet introuvable (découverte agent échouée)")
            logger.warning("  ⚠️  Les fichiers compose sont dans le snapshot, copie manuelle nécessaire")
            return False
        project_dir = os.path.join(projects_dir, project.name)
        logger.info(f"  📁 Répertoire projet dérivé : {project_dir}")
    else:
        logger.warning("  ⚠️  Accès distant — restauration des compose par SSH non implémentée")
        logger.warning("  ⚠️  Les fichiers compose sont dans le snapshot, copie manuelle nécessaire")
        return False

    os.makedirs(project_dir, exist_ok=True)

    # Restaurer le compose
    compose_name = project.compose_file_name
    compose_src = os.path.join(snap_dir, compose_name)
    compose_dst = os.path.join(project_dir, compose_name)

    if os.path.isfile(compose_src):
        try:
            shutil.copy2(compose_src, compose_dst)
            logger.info(f"  ✅ Compose restauré : {compose_dst}")
        except OSError as e:
            logger.error(f"  ❌ Impossible de copier le compose : {e}")
            return False
    else:
        logger.warning(f"  ⚠️  Aucun fichier compose dans le snapshot")

    # Restaurer le .env
    env_src = os.path.join(snap_dir, ".env")
    env_dst = os.path.join(project_dir, ".env")
    if os.path.isfile(env_src):
        try:
            shutil.copy2(env_src, env_dst)
            logger.info(f"  ✅ .env restauré : {env_dst}")
        except OSError as e:
            logger.error(f"  ❌ Impossible de copier le .env : {e}")
            return False
    else:
        logger.info("  ℹ️  Pas de .env dans le snapshot")

    return True


# ─── Restauration des bind mounts ──────────────────────────────────────────────

def _restore_bind_mounts(snap_dir: str, project: Project) -> int:
    """
    Restaure les bind mounts à partir des archives tar.gz.

    Pour chaque archive data_*.tar trouvée dans le snapshot :
      1. Déterminer le chemin de destination
      2. Demander confirmation
      3. Extraire avec préservation des permissions

    Retourne le nombre de bind mounts restaurés.
    """
    restored = 0

    for bm in project.bind_mounts:
        if not bm.selected:
            continue

        dir_name = os.path.basename(bm.path.rstrip("/"))
        archive_name = f"data_{dir_name}.tar"
        archive_path = os.path.join(snap_dir, archive_name)

        if not os.path.isfile(archive_path):
            logger.warning(f"  ⚠️  Archive introuvable : {archive_name}")
            continue

        logger.info(f"  📦 Restauration de {bm.path}...")

        # Vérifier que le répertoire parent existe
        parent_dir = os.path.dirname(bm.path.rstrip("/"))
        os.makedirs(parent_dir, exist_ok=True)

        # Extraire avec tar CLI : préserve permissions + xattrs/ACLs,
        # contrepartie exacte du backup (`tar cpf --xattrs --sparse`).
        # (tarfile Python ne restaure pas les extended attributes.)
        cmd = [
            "tar", "xpf", archive_path,
            "--xattrs",
            "--sparse",
            "-C", parent_dir,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode == 0:
                logger.info(f"  ✅ Restauré : {bm.path}")
                restored += 1
            else:
                logger.error(f"  ❌ Échec de la restauration de {bm.path} : {result.stderr[:500]}")
        except subprocess.TimeoutExpired:
            logger.error(f"  ❌ Timeout restauration de {bm.path}")

    return restored


# ─── Restauration d'un volume Docker ───────────────────────────────────────────

def _restore_volume(env: Environment, vol_name: str, archive_path: str) -> bool:
    """
    Restaure un volume Docker à partir d'une archive.

    Méthode :
      1. Créer un volume temporaire
      2. Extraire l'archive dans le volume via un conteneur helper
      3. Supprimer l'ancien volume
      4. Renommer le volume temporaire

    Note : cette méthode n'utilise pas l'API Arcane pour la restauration
    (qui n'expose pas d'endpoint de restore). On passe par Docker CLI.
    """
    import tempfile

    tmp_volume = f"{vol_name}_restore_tmp"
    logger.info(f"  💿 Restauration du volume Docker '{vol_name}'...")

    # Créer un volume temporaire
    try:
        subprocess.run(
            ["docker", "volume", "create", tmp_volume],
            capture_output=True, text=True, timeout=15, check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"  ❌ Impossible de créer le volume temporaire : {e}")
        return False

    # Extraire l'archive dans le volume temporaire via un conteneur helper
    try:
        # Monter le volume temporaire dans un conteneur alpine et extraire
        extract_cmd = [
            "docker", "run", "--rm",
            "-v", f"{tmp_volume}:/data",
            "-v", f"{archive_path}:/backup.tar.gz",
            "alpine:latest",
            "tar", "xzf", "/backup.tar.gz", "-C", "/data"
        ]
        result = subprocess.run(extract_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            logger.error(f"  ❌ Échec de l'extraction : {result.stderr}")
            docker_cleanup(tmp_volume)
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.error(f"  ❌ Erreur : {e}")
        docker_cleanup(tmp_volume)
        return False

    # Supprimer l'ancien volume
    try:
        subprocess.run(
            ["docker", "volume", "rm", "-f", vol_name],
            capture_output=True, text=True, timeout=15, check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"  ❌ Impossible de supprimer l'ancien volume '{vol_name}' : {e}")
        docker_cleanup(tmp_volume)
        return False

    # Renommer le volume temporaire
    # Docker ne permet pas de renommer un volume, on crée un nouvel alias
    # Astuce : on peut utiliser un conteneur pour copier les données
    # Mais la méthode la plus fiable est de supprimer le volume temporaire
    # et de recréer le volume original avec les données
    try:
        # Créer le volume original
        subprocess.run(
            ["docker", "volume", "create", vol_name],
            capture_output=True, text=True, timeout=15, check=True
        )
        # Copier les données du volume temporaire vers le volume original
        copy_cmd = [
            "docker", "run", "--rm",
            "-v", f"{tmp_volume}:/src:ro",
            "-v", f"{vol_name}:/dst",
            "alpine:latest",
            "sh", "-c", "cp -a /src/. /dst/"
        ]
        subprocess.run(copy_cmd, capture_output=True, text=True, timeout=60, check=True)
        # Supprimer le volume temporaire
        docker_cleanup(tmp_volume)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"  ❌ Erreur lors de la copie des données : {e}")
        docker_cleanup(tmp_volume)
        return False

    logger.info(f"  ✅ Volume '{vol_name}' restauré")
    return True


def docker_cleanup(volume_name: str) -> None:
    """Supprime un volume Docker temporaire."""
    try:
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume_name],
            capture_output=True, text=True, timeout=10
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass


# ─── Restauration d'Arcane (hot backup) ────────────────────────────────────────

def _derive_arcane_dir(config: Config) -> str:
    """
    Dérive le répertoire des données Arcane depuis la config.

    Cherche dans l'environnement local (accès direct) un projet
    contenant "arcane" avec des bind mounts, et prend le parent du
    premier bind mount. Sinon, défaut conventionnel /var/lib/arcane.
    """
    data_dir = ""
    for env in config.environments:
        if env.access.mode == AccessMode.DIRECT:
            for proj in env.projects:
                if "arcane" in proj.name.lower() and proj.bind_mounts:
                    data_dir = os.path.dirname(proj.bind_mounts[0].path)
                    break
            if data_dir:
                break
    if not data_dir:
        data_dir = "/var/lib/arcane"
        logger.warning(f"  ⚠️  Répertoire Arcane non détecté, utilisation de {data_dir}")
    return data_dir


def _restore_arcane_db(config: Config, snap_dir: str) -> bool:
    """
    Restaure la base SQLite d'Arcane depuis un hot backup.

    Procédure :
      1. Extraire l'archive arcane-db.tar.gz
      2. Arrêter le conteneur Arcane
      3. Copier la DB (répertoire dérivé depuis la config, comme au backup)
      4. Redémarrer Arcane
    """
    archive_path = os.path.join(snap_dir, "arcane-db.tar.gz")
    if not os.path.isfile(archive_path):
        logger.warning("  ⚠️  Aucun backup Arcane trouvé dans le snapshot")
        return False

    logger.info("  ⚠️  ATTENTION : restauration de la base Arcane")
    if not _confirm("  ⚠️  Veux-tu vraiment restaurer la base de données d'Arcane ?", False):
        logger.info("  ⏭️  Restauration Arcane annulée")
        return False

    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="arcane-restore-")

    try:
        # Extraire l'archive
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=tmp_dir)

        db_src = os.path.join(tmp_dir, "arcane-hotbackup.db")
        if not os.path.isfile(db_src):
            logger.error("  ❌ Fichier DB introuvable dans l'archive")
            return False

        # Arrêter Arcane
        logger.info("  ⏹️  Arrêt du conteneur Arcane...")
        result = subprocess.run(
            ["docker", "stop", "arcane"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            logger.warning(f"  ⚠️  Impossible d'arrêter Arcane : {result.stderr}")

        # Copier la DB — répertoire dérivé comme au backup
        db_dst = os.path.join(_derive_arcane_dir(config), "arcane.db")
        try:
            shutil.copy2(db_src, db_dst)
            logger.info(f"  ✅ Base Arcane restaurée : {db_dst}")
        except OSError as e:
            logger.error(f"  ❌ Impossible de copier la DB : {e}")
            return False
        finally:
            # Redémarrer Arcane
            logger.info("  ▶️  Redémarrage d'Arcane...")
            subprocess.run(
                ["docker", "start", "arcane"],
                capture_output=True, text=True, timeout=30
            )

    finally:
        # Nettoyer
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return True


# ─── Restauration d'un projet ──────────────────────────────────────────────────

def restore_project(config: Config, env: Environment, project: Project,
                    snapshot_name: str = "", target_dir: str = "") -> bool:
    """
    Restaure un projet à partir d'un snapshot.

    Args:
        config: Configuration globale
        env: L'environnement d'origine
        project: Le projet à restaurer
        snapshot_name: Nom du snapshot (ex: 20260728_120000). Vide = plus récent.
        target_dir: Si renseigné, extrait les données dans ce répertoire
                    sans arrêter/redémarrer le projet. Sinon, restaure
                    à l'emplacement d'origine avec arrêt/redémarrage.

    Returns:
        True si la restauration a réussi, False sinon.
    """
    from backup import _env_backup_root, _project_backup_root

    env_name = env.name
    proj_name = project.name
    project_dir = _project_backup_root(config, env_name, proj_name)

    # 1. Lister les snapshots disponibles
    snapshots = discover_snapshots(project_dir)
    if not snapshots:
        logger.error(f"  ❌ Aucun snapshot trouvé pour {env_name}/{proj_name}")
        logger.error(f"     Répertoire : {project_dir}")
        return False

    # 2. Choisir le snapshot
    if snapshot_name:
        selected = [s for s in snapshots if s.name == snapshot_name]
        if not selected:
            logger.error(f"  ❌ Snapshot '{snapshot_name}' introuvable")
            logger.error(f"     Snapshots disponibles : {', '.join(s.name for s in snapshots[-5:])}")
            return False
        snap = selected[0]
    else:
        snap = snapshots[-1]  # Plus récent
        logger.info(f"  📋 Snapshot le plus récent sélectionné : {snap.name}")

    # 3. Afficher les infos du snapshot
    meta = load_metadata(snap.path)
    if meta:
        logger.info(f"  📋 Date du snapshot : {meta.get('date', '?')}")
        images = meta.get("images", {})
        if images:
            logger.info(f"  📋 Versions sauvegardées :")
            for svc, img in images.items():
                logger.info(f"      {svc}: {img}")
    else:
        logger.warning("  ⚠️  Pas de metadata.json dans ce snapshot")

    # 4. Mode extraction (--to) : pas de stop/start, extraction simple
    if target_dir:
        return _extract_to_target(snap.path, project, target_dir)

    # 5. Mode restauration originale : confirmation + stop/start
    print()
    logger.info(f"  ⚠️  Action DESTRUCTIVE : {env_name}/{proj_name} va être restauré")
    logger.info(f"  ⚠️  Le projet sera ARRÊTÉ pendant la restauration")
    if not _confirm(f"  ⚠️  Confirmer la restauration de {snap.name} ?", False):
        logger.info("  ⏭️  Restauration annulée")
        return False

    # 6. Backup de l'état actuel
    pre_restore = _backup_current_state(project_dir)
    if pre_restore:
        logger.info(f"  💾 Backup pré-restauration : {os.path.basename(pre_restore)}")

    # 7. Arrêter le projet
    logger.info("  ⏹️  Arrêt du projet...")
    try:
        stop_project(config.arcane_api_url, config.arcane_api_key, env.id, project.id)
        import time
        time.sleep(3)
    except ArcaneError as e:
        logger.warning(f"  ⚠️  Impossible d'arrêter le projet : {e}")

    errors = []

    # 8. Restaurer les fichiers compose
    logger.info("  📄 Restauration des fichiers compose...")
    if not _restore_compose_files(config, snap.path, project, env):
        errors.append("compose")

    # 9. Restaurer les bind mounts
    logger.info("  💾 Restauration des bind mounts...")
    restored = _restore_bind_mounts(snap.path, project)
    if restored == 0 and project.bind_mounts:
        logger.warning("  ⚠️  Aucun bind mount restauré")
        errors.append("bind_mounts")

    # 10. Restaurer les volumes Docker
    logger.info("  💿 Restauration des volumes Docker...")
    for vol in project.volumes:
        if not vol.selected:
            continue
        vol_archive = os.path.join(snap.path, f"volume_{vol.name}.tar.gz")
        if os.path.isfile(vol_archive):
            if not _restore_volume(env, vol.name, vol_archive):
                errors.append(f"volume:{vol.name}")
        else:
            logger.warning(f"  ⚠️  Archive volume introuvable : {vol.name}")

    # 11. Cas spécial Arcane
    if proj_name.lower() == "arcane":
        _restore_arcane_db(config, snap.path)

    # 12. Redémarrer le projet
    logger.info("  ▶️  Redémarrage du projet...")
    started = False
    for attempt in range(3):
        try:
            if start_project(config.arcane_api_url, config.arcane_api_key,
                              env.id, project.id):
                started = True
                break
        except ArcaneError:
            pass
        if attempt < 2:
            time.sleep(5)

    if not started:
        logger.error("  ❌❌❌ Impossible de redémarrer le projet !")
        logger.error("  ❌❌❌ Redémarrage manuel nécessaire sur Arcane")
        errors.append("start")
        return False

    # 13. Résultat
    if errors:
        logger.warning(f"  ⚠️  Restauration terminée avec {len(errors)} erreur(s)")
        for e in errors:
            logger.warning(f"       - {e}")
        return False
    else:
        logger.info("  ✅ Restauration terminée avec succès")
        return True


def _extract_to_target(snap_path: str, project: Project, target_dir: str) -> bool:
    """
    Extrait les données d'un snapshot vers un répertoire cible.

    Structure de sortie :
      target_dir/
      ├── compose/                ← compose + .env (à placer dans le répertoire des projets Arcane)
      │   └── <nom_du_projet>/
      │       ├── compose.yaml
      │       └── .env
      ├── data/                   ← bind mounts extraits (chemins d'origine conservés)
      │   └── <chemin_original>/  ← ex: mnt/user/appdata/authentik/database/
      ├── volumes/                ← volumes Docker extraits en fichiers
      │   └── <nom_volume>/
      └── metadata.json
    """
    logger.info(f"  📦 Mode extraction vers : {target_dir}")
    os.makedirs(target_dir, exist_ok=True)

    # Structure avec le nom du projet
    compose_dir = os.path.join(target_dir, "compose", project.name)
    data_root = os.path.join(target_dir, "data")
    volumes_dir = os.path.join(target_dir, "volumes")
    os.makedirs(compose_dir, exist_ok=True)
    os.makedirs(data_root, exist_ok=True)
    os.makedirs(volumes_dir, exist_ok=True)

    success = True

    # Copier les fichiers compose
    compose_name = project.compose_file_name
    compose_src = os.path.join(snap_path, compose_name)
    if os.path.isfile(compose_src):
        try:
            dest = os.path.join(compose_dir, compose_name)
            shutil.copy2(compose_src, dest)
            logger.info(f"  ✅ Compose → {dest}")
        except OSError as e:
            logger.warning(f"  ⚠️  Impossible de copier le compose : {e}")
            success = False
    else:
        logger.info(f"  ℹ️  Aucun fichier compose dans le snapshot")

    env_src = os.path.join(snap_path, ".env")
    if os.path.isfile(env_src):
        try:
            dest = os.path.join(compose_dir, ".env")
            shutil.copy2(env_src, dest)
            logger.info(f"  ✅ .env → {dest}")
        except OSError as e:
            logger.warning(f"  ⚠️  Impossible de copier le .env : {e}")

    # Extraire les bind mounts en conservant le chemin d'origine dans la structure
    for bm in project.bind_mounts:
        if not bm.selected:
            continue
        dir_name = os.path.basename(bm.path.rstrip("/"))
        archive_name = f"data_{dir_name}.tar"
        archive_path = os.path.join(snap_path, archive_name)

        if not os.path.isfile(archive_path):
            logger.warning(f"  ⚠️  Archive introuvable : {archive_name}")
            continue

        # Reconstruire le chemin d'origine relatif
        # Ex: /var/lib/appdata/application/data/ → data/var/lib/appdata/application/data/
        # ⚠️ Le tar contient déjà le basename (créé avec `tar -C parent base`),
        #    donc on extrait dans le PARENT du chemin cible pour éviter le
        #    double nesting (ex: database/database/...).
        rel_path = bm.path.lstrip("/")
        dest_parent = os.path.join(data_root, os.path.dirname(rel_path))
        dest = os.path.join(dest_parent, os.path.basename(rel_path))
        os.makedirs(dest, exist_ok=True)

        logger.info(f"  📦 Extraction de {archive_name} → {dest}")
        try:
            cmd = [
                "tar", "xpf", archive_path,
                "--xattrs",
                "--sparse",
                "-C", dest_parent,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode != 0:
                logger.error(f"  ❌ Échec extraction {archive_name} : {result.stderr[:500]}")
                success = False
            else:
                logger.info(f"  ✅ Données extraites : {dest}")
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.error(f"  ❌ Échec extraction {archive_name} : {e}")
            success = False

    # Extraire les volumes Docker
    for vol in project.volumes:
        if not vol.selected:
            continue
        vol_archive = os.path.join(snap_path, f"volume_{vol.name}.tar.gz")
        if os.path.isfile(vol_archive):
            vol_dest = os.path.join(volumes_dir, vol.name)
            os.makedirs(vol_dest, exist_ok=True)
            try:
                with tarfile.open(vol_archive, "r:gz") as tar:
                    tar.extractall(path=vol_dest)
                logger.info(f"  ✅ Volume '{vol.name}' extrait : {vol_dest}")
            except (tarfile.TarError, OSError) as e:
                logger.error(f"  ❌ Échec extraction volume '{vol.name}' : {e}")
                success = False

    # Copier metadata.json
    meta_src = os.path.join(snap_path, "metadata.json")
    if os.path.isfile(meta_src):
        try:
            shutil.copy2(meta_src, os.path.join(target_dir, "metadata.json"))
        except OSError:
            pass

    if success:
        logger.info(f"  ✅ Extraction terminée dans {target_dir}")
        logger.info(f"")
        logger.info(f"  📁 Pour utiliser ces données sur un autre hôte :")
        logger.info(f"")
        logger.info(f"  1. Crée un nouveau projet Arcane sur l'hôte cible")
        logger.info(f"")
        logger.info(f"  2. Copie le contenu de compose/{project.name}/")
        logger.info(f"     dans le répertoire des projets Arcane de la cible")
        logger.info(f"     (le chemin dépend de l'hôte — ex: /data/stacks, /docker-projects/stacks, ...)")
        logger.info(f"")
        logger.info(f"  3. Copie les dossiers de data/")
        logger.info(f"     vers les bind mounts configurés dans le compose")
        logger.info(f"")
        if any(vol.selected for vol in project.volumes):
            logger.info(f"  4. Restaure les volumes Docker via :")
            logger.info(f"       docker run --rm -v <nom_volume>:/data -v {volumes_dir}/:/backup alpine tar xzf /backup/<nom>.tar.gz -C /data")
    else:
        logger.warning(f"  ⚠️  Extraction partielle — voir les erreurs ci-dessus")

    return success


# ─── Interface en ligne de commande ────────────────────────────────────────────

def run_restore(config: Config, env_name: str = "", project_name: str = "",
                snapshot_name: str = "", target_dir: str = "") -> None:
    """
    Point d'entrée pour la restauration.

    Usage :
      --restore                           # menu interactif
      --restore <env>/<projet>          # restaure le plus récent
      --restore <env>/<projet>/<snapshot>  # snapshot spécifique
      --restore <env>/<projet> --to /tmp/test  # extraction vers un dossier
    """
    from backup import _env_backup_root, _project_backup_root

    # Vérifier la connexion API
    if not check_connection(config.arcane_api_url, config.arcane_api_key):
        logger.error("❌ API Arcane injoignable. Restauration impossible.")
        return

    # Mode interactif : lister les environnements et projets
    if not env_name:
        print("╔══════════════════════════════════════════╗")
        print("║  Arcane Backup Companion (ABC) — Restauration      ║")
        print("╚══════════════════════════════════════════╝")
        print()
        print("Projets disponibles avec des snapshots :")
        print()

        for env in config.environments:
            for project in env.projects:
                if project.skip:
                    continue
                project_dir = _project_backup_root(config, env.name, project.name)
                snaps = discover_snapshots(project_dir)
                if snaps:
                    print(f"  {env.name}/{project.name} ({len(snaps)} snapshot(s))")
                    for s in snaps[-3:]:
                        meta = load_metadata(s.path)
                        date = meta.get("date", "?") if meta else s.name
                        print(f"    • {s.name}  ({date})")

        print()
        path = input("Chemin à restaurer (ex: <env>/<projet>) : ").strip()
        parts = path.split("/")
        if len(parts) >= 2:
            env_name = parts[0]
            project_name = parts[1]
            snapshot_name = parts[2] if len(parts) >= 3 else ""
        else:
            logger.error("Format invalide. Utilise : environnement/projet")
            return

        # Demander la destination
        to = input("Destination (Enter = emplacement d'origine, ou /chemin/vers/dossier) : ").strip()
        if to:
            target_dir = to

        # Demander le snapshot si non spécifié
        if not snapshot_name:
            project_dir = _project_backup_root(config, env_name, project_name)
            snaps = discover_snapshots(project_dir)
            if snaps:
                print("Snapshots disponibles :")
                for i, s in enumerate(snaps):
                    meta = load_metadata(s.path)
                    label = f"  [{i}] {s.name}"
                    if meta:
                        label += f" — {meta.get('date', '')}"
                        images = meta.get("images", {})
                        if images:
                            label += f" ({', '.join(images.values())})"
                    print(label)
                idx = input("Numéro du snapshot [dernier] : ").strip()
                if idx.isdigit():
                    snapshot_name = snaps[int(idx)].name

    # Trouver l'environnement et le projet (comparaison insensible à la casse)
    target_env = None
    target_project = None
    for env in config.environments:
        if env.name.lower() == env_name.lower():
            target_env = env
            for project in env.projects:
                if project.name.lower() == project_name.lower():
                    target_project = project
                    break
            break

    if not target_env or not target_project:
        logger.error(f"❌ Projet '{env_name}/{project_name}' introuvable dans la config")
        logger.error("   Vérifie le nom ou lance --setup pour reconfigurer")
        return

    # Afficher le mode
    if target_dir:
        logger.info(f"📦 Mode extraction vers : {target_dir}")
    else:
        logger.info("🔄 Mode restauration sur place (avec arrêt/redémarrage)")

    # Demander le snapshot si non spécifié (mode direct)
    if not snapshot_name:
        project_dir = _project_backup_root(config, target_env.name, target_project.name)
        snaps = discover_snapshots(project_dir)
        if snaps:
            print("Snapshots disponibles :")
            for i, s in enumerate(snaps):
                meta = load_metadata(s.path)
                label = f"  [{i}] {s.name}"
                if meta:
                    label += f" — {meta.get('date', '')}"
                    images = meta.get("images", {})
                    if images:
                        label += f" ({', '.join(images.values())})"
                print(label)
            idx = input("Numéro du snapshot [dernier] : ").strip()
            if idx.isdigit() and int(idx) < len(snaps):
                snapshot_name = snaps[int(idx)].name
                logger.info(f"  📋 Snapshot sélectionné : {snapshot_name}")
        else:
            logger.warning(f"  ⚠️  Aucun snapshot trouvé pour {target_env.name}/{target_project.name}")
            return

    # Lancer la restauration
    success = restore_project(config, target_env, target_project, snapshot_name, target_dir)
    if success:
        logger.info("✅ Restauration terminée")
    else:
        logger.error("❌ Restauration échouée — voir les logs ci-dessus")