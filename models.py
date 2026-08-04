"""
models.py — Classes de données utilisées par tous les modules.

Chaque classe représente une entité du monde réel :
  - Environment : un hôte Docker (ex: serveur_local, hote1, ...)
  - Project     : un projet Docker Compose (authentik, sonarr, etc.)
  - BindMount   : un répertoire de données à sauvegarder
  - DockerVolume : un volume Docker nommé à sauvegarder
  - Config      : la configuration complète (sérialisée en YAML)
"""

from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum


class AccessMode(Enum):
    """Comment accéder aux bind mounts de cet environnement."""
    DIRECT = "direct"   # Accès local (filesystem accessible directement)
    SSH = "ssh"         # Accès distant via SSH (LXCs)


@dataclass
class HostAccess:
    """Paramètres de connexion pour un environnement distant."""
    mode: AccessMode
    hostname: str = ""         # ex: 192.168.1.50
    port: int = 22
    username: str = "root"
    ssh_key_path: str = ""     # ex: /root/.ssh/id_ed25519


@dataclass
class BindMount:
    """
    Un bind mount à sauvegarder.

    Le data_dir est le répertoire qui contient ce bind mount.
    path est le chemin précis du bind mount dans le filesystem.
    selected=True si l'utilisateur a choisi de le sauvegarder.
    """
    path: str                  # ex: /var/lib/appdata/application/data
    container_path: str = ""   # ex: /var/lib/postgresql/data (info)
    selected: bool = True


@dataclass
class DockerVolume:
    """Un volume Docker nommé à sauvegarder."""
    name: str                  # ex: authentik-redis-data
    selected: bool = True


@dataclass
class Retention:
    """Politique de rétention GFS pour un projet."""
    policy: str = "default"  # "default", "weekly", "monthly", "unlimited"
    schedule: str = "daily"  # "daily", "weekly"
    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 6

    def to_policy(self):
        from prune import RetentionPolicy
        return RetentionPolicy(
            keep_daily=self.keep_daily,
            keep_weekly=self.keep_weekly,
            keep_monthly=self.keep_monthly,
        )


@dataclass
class Project:
    """
    Un projet Docker (Compose) découvert via l'API Arcane.

    L'upload de la config se fait pendant le setup. Le script
    demande pour chaque projet quels bind mounts et volumes
    sauvegarder, et avec quelle rétention.
    """
    id: str                    # UUID Arcane
    name: str                  # ex: authentik
    compose_file_name: str = "compose.yaml"  # détecté par l'API
    compose_dir: str = ""      # répertoire du compose sur l'hôte (pour SSH)
    data_dir: str = ""         # répertoire racine du projet (filesystem)
    bind_mounts: list[BindMount] = field(default_factory=list)
    volumes: list[DockerVolume] = field(default_factory=list)
    retention: Retention = field(default_factory=Retention)
    skip: bool = False         # True si l'utilisateur veut exclure ce projet


@dataclass
class Environment:
    """
    Un environnement Arcane = un hôte Docker.

    Pour l'hôte local, l'accès est DIRECT (les bind mounts sont
    accessibles depuis le filesystem). Pour les LXCs / hôtes
    distants, l'accès est SSH.
    """
    id: str                    # UUID Arcane (0 pour l'hôte local)
    name: str                  # ex: serveur_local, hote1, ...
    access: HostAccess = field(default_factory=lambda: HostAccess(AccessMode.DIRECT))
    projects: list[Project] = field(default_factory=list)
    global_retention: Retention = field(default_factory=Retention)


@dataclass
class Config:
    """
    Configuration complète, sérialisée en YAML.

    C'est ce que --setup génère et --run utilise.
    """
    arcane_api_url: str = ""  # ex: https://arcane.example.com/api
    arcane_api_key: str = ""
    backup_root: str = ""  # ex: /path/to/backups — requis (sinon défaut relatif ./backups)
    notifications_enabled: bool = True  # notifications Unraid (si hôte Unraid)
    environments: list[Environment] = field(default_factory=list)
    global_exclusions: list[str] = field(default_factory=list)