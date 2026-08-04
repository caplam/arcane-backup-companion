"""
prune.py — Politique de rétention GFS (Grandfather-Father-Son).

Logique purement algorithmique : les fonctions prennent des données en entrée,
retournent des décisions en sortie, sans écrire sur le disque (sauf run_prune()).

Fonctions principales :
  - gfs_prune(snapshots, policy)  → décide quels snapshots garder/supprimer
  - save_metadata(snap_dir, info) → écrit metadata.json dans un snapshot
  - load_metadata(snap_dir)       → lit metadata.json
  - run_prune(project_dir, policy) → applique la politique sur le disque
"""

import os
import json
import shutil
import logging
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("arkbackup.prune")


# ─── Politique de rétention ────────────────────────────────────────────────────

@dataclass
class RetentionPolicy:
    """
    Politique de rétention GFS pour un projet.

    GFS = Grandfather-Father-Son :
      - Son (daily)   : snapshots quotidiens, garde les N plus récents
      - Father (weekly): snapshots hebdomadaires, 1 par semaine
      - Grandfather (monthly): snapshots mensuels, 1 par mois

    Mettre à 0 pour désactiver un niveau.
    Exemple : keep_daily=7, keep_weekly=4, keep_monthly=6
    → garde 7 daily, 4 weekly, 6 monthly = 17 snapshots max
    """
    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 6

    def __post_init__(self):
        """Validation simple."""
        for name, val in [("keep_daily", self.keep_daily),
                          ("keep_weekly", self.keep_weekly),
                          ("keep_monthly", self.keep_monthly)]:
            if val < 0:
                raise ValueError(f"{name} ne peut pas être négatif ({val})")


# Politiques prédéfinies
POLICY_DEFAULT = RetentionPolicy(keep_daily=7, keep_weekly=4, keep_monthly=6)
POLICY_WEEKLY = RetentionPolicy(keep_daily=0, keep_weekly=4, keep_monthly=6)
POLICY_MONTHLY = RetentionPolicy(keep_daily=0, keep_weekly=0, keep_monthly=12)
POLICY_UNLIMITED = RetentionPolicy(keep_daily=0, keep_weekly=0, keep_monthly=0)


# ─── Informations d'un snapshot ────────────────────────────────────────────────

@dataclass
class SnapshotInfo:
    """
    Représente un snapshot de backup.

    Le timestamp est extrait du nom du dossier (YYYYMMDD_HHMMSS).
    Les images sont lues depuis metadata.json s'il existe.
    """
    path: str
    timestamp: datetime
    images: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def is_monday(self) -> bool:
        """Vrai si le snapshot est un lundi (pour le weekly)."""
        return self.timestamp.weekday() == 0

    def is_first_of_month(self) -> bool:
        """Vrai si le snapshot est le 1er du mois (pour le monthly)."""
        return self.timestamp.day == 1


# ─── Logique GFS pure ──────────────────────────────────────────────────────────

def gfs_prune(snapshots: list[SnapshotInfo],
              policy: RetentionPolicy) -> tuple[list[SnapshotInfo], list[SnapshotInfo]]:
    """
    Applique la politique GFS sur une liste de snapshots.

    Retourne (a_garder, a_supprimer) — deux listes de SnapshotInfo.

    Algorithme :
      1. Trier les snapshots par date (plus récent en premier)
      2. Marquer TOUS les snapshots comme "à supprimer"
      3. Garder les keep_daily plus récents
      4. Parmi les restants, garder 1 par semaine (les keep_weekly dernières)
      5. Parmi les restants, garder 1 par mois (les keep_monthly derniers)
      6. Tout ce qui n'est pas marqué "gardé" → supprimé

    Si policy = (0, 0, 0) → rétention illimitée, tout garder.
    """
    # Rétention illimitée
    if policy.keep_daily == 0 and policy.keep_weekly == 0 and policy.keep_monthly == 0:
        return snapshots[:], []

    # Trier du plus récent au plus ancien
    sorted_snaps = sorted(snapshots, key=lambda s: s.timestamp, reverse=True)

    # Ensemble des snapshots à garder (par index)
    to_keep: set[int] = set()

    # Étape 1 : garder les keep_daily plus récents
    if policy.keep_daily > 0:
        for i in range(min(policy.keep_daily, len(sorted_snaps))):
            to_keep.add(i)

    # Étape 2 : garder 1 par semaine (parmi les restants)
    if policy.keep_weekly > 0:
        weekly_found = 0
        seen_weeks = set()
        for i in range(len(sorted_snaps)):
            if i in to_keep:
                continue
            if weekly_found >= policy.keep_weekly:
                break
            snap = sorted_snaps[i]
            # Semaine ISO (année, numéro de semaine)
            week_key = snap.timestamp.isocalendar()[:2]
            if week_key not in seen_weeks:
                seen_weeks.add(week_key)
                to_keep.add(i)
                weekly_found += 1

    # Étape 3 : garder 1 par mois (parmi les restants)
    if policy.keep_monthly > 0:
        monthly_found = 0
        seen_months = set()
        for i in range(len(sorted_snaps)):
            if i in to_keep:
                continue
            if monthly_found >= policy.keep_monthly:
                break
            snap = sorted_snaps[i]
            month_key = (snap.timestamp.year, snap.timestamp.month)
            if month_key not in seen_months:
                seen_months.add(month_key)
                to_keep.add(i)
                monthly_found += 1

    # Construire les listes
    to_keep_list = [sorted_snaps[i] for i in sorted(to_keep)]
    to_delete_list = [sorted_snaps[i] for i in range(len(sorted_snaps)) if i not in to_keep]

    return to_keep_list, to_delete_list


# ─── Métadonnées ───────────────────────────────────────────────────────────────

def save_metadata(snap_dir: str, project_name: str, images: dict,
                  compose_file: str = "", env_present: bool = False,
                  run_id: str = "") -> None:
    """
    Écrit metadata.json dans un snapshot.

    Appelé une fois le backup d'un projet terminé.
    Contient les versions des images Docker pour le tracking.

    run_id : identifiant unique du run de backup (timestamp de début).
    Stocké pour permettre la purge des logs orphelins : un log est
    supprimé quand plus aucun snapshot ne porte son run_id.
    """
    metadata = {
        "project": project_name,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "images": images,
        "compose_file": compose_file,
        "env_present": env_present,
    }
    if run_id:
        metadata["run_id"] = run_id
    path = os.path.join(snap_dir, "metadata.json")
    with open(path, "w") as f:
        json.dump(metadata, f, indent=2)


def load_metadata(snap_dir: str) -> Optional[dict]:
    """Lit metadata.json s'il existe."""
    path = os.path.join(snap_dir, "metadata.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ─── Découverte des snapshots sur le disque ────────────────────────────────────

def discover_snapshots(project_dir: str) -> list[SnapshotInfo]:
    """
    Liste tous les snapshots d'un projet, triés par date.

    Un snapshot est un dossier au format YYYYMMDD_HHMMSS.
    Ignore les dossiers qui ne correspondent pas à ce format.
    """
    snapshots = []
    base = Path(project_dir)
    if not base.exists():
        return snapshots

    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        try:
            ts = datetime.strptime(d.name, "%Y%m%d_%H%M%S")
        except ValueError:
            continue

        meta = load_metadata(str(d))
        info = SnapshotInfo(
            path=str(d),
            timestamp=ts,
            images=meta.get("images", {}) if meta else {},
            metadata=meta if meta else {},
        )
        snapshots.append(info)

    return snapshots


# ─── Application sur le disque ─────────────────────────────────────────────────

def run_prune(project_dir: str, policy: RetentionPolicy) -> None:
    """
    Applique la politique de rétention sur le disque pour un projet.

    Découvre les snapshots, applique gfs_prune(), supprime les
    dossiers marqués "à supprimer". Log chaque action.
    """
    if not os.path.isdir(project_dir):
        return

    # Rétention illimitée
    if policy.keep_daily == 0 and policy.keep_weekly == 0 and policy.keep_monthly == 0:
        logger.info(f"  🧹 Rétention illimitée — aucun snapshot supprimé")
        return

    snapshots = discover_snapshots(project_dir)

    if not snapshots:
        return

    to_keep, to_delete = gfs_prune(snapshots, policy)

    if not to_delete:
        logger.info(f"  🧹 {len(snapshots)} snapshot(s) — aucun à supprimer")
        return

    for snap in to_delete:
        try:
            shutil.rmtree(snap.path)
            logger.info(f"  🧹 Snapshot supprimé : {snap.name}")
        except OSError as e:
            logger.warning(f"  ⚠️  Impossible de supprimer {snap.name} : {e}")

    kept = len(to_keep)
    deleted = len(to_delete)
    if deleted > 0:
        logger.info(f"  🧹 {kept} gardé(s), {deleted} supprimé(s)")