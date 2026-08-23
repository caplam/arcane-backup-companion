"""
api.py — Appels à l'API Arcane.

Toutes les fonctions sont des wrappers autour de l'API REST.
Un seul fichier à modifier si l'API Arcane change.

Endpoints utilisés :
  GET  /api/environments                    → liste des hôtes
  GET  /api/environments/{id}/projects      → projets d'un hôte
  GET  /api/environments/{id}/projects/{pid}/compose → compose + .env
  POST /api/environments/{id}/projects/{pid}/down   → stop
  POST /api/environments/{id}/projects/{pid}/up     → start
  GET  /api/environments/{id}/containers/{cid}      → infos conteneur
  POST /api/environments/{id}/volumes/{name}/backups → créer un volume backup
  GET  /api/environments/{id}/volumes/backups/{bid}/download → télécharger
  DELETE /api/environments/{id}/volumes/backups/{bid} → nettoyer

Erreurs : toutes les fonctions lèvent ArcaneError en cas d'échec.
"""

import json
import subprocess
import sys
from typing import Optional
from models import Environment, Project, BindMount, DockerVolume, HostAccess, AccessMode


class ArcaneError(Exception):
    """Erreur de communication avec l'API Arcane."""
    pass


def _curl(method: str, url: str, api_key: str, data: Optional[str] = None,
          output_file: Optional[str] = None,
          raw_response: bool = False,
          timeout: int = 30,
          progress_timeout: bool = False) -> Optional[dict]:
    """
    Appel curl bas niveau.

    Si raw_response=True, retourne le texte brut sans parser le JSON.
    Utile pour les endpoints start/stop qui ne retournent pas de JSON.

    timeout: garde-fou absolu en secondes (défaut 30). Un appel bloqué sans
    aucune réponse lève une ArcaneError après ce délai.

    progress_timeout: si True, le contrôle du temps repose sur la PROGRESSION
    du transfert (curl --speed-limit/--speed-time) plutôt que sur une durée
    fixe. Le transfert s'arrête seulement s'il est en panne (vitesse < 1 Ko/s
    pendant 30s) — un gros volume qui transfère continue tant que nécessaire.
    À utiliser pour les téléchargements de backups de volume Docker.
    """
    cmd = ["curl", "-skf", "-X", method,
           "-H", f"x-api-key: {api_key}"]

    if data:
        cmd.extend(["-H", "Content-Type: application/json",
                    "-d", data])

    if output_file:
        cmd.extend(["-o", output_file])

    if progress_timeout:
        # Abandonne seulement si le transfert est à l'arrêt, pas sur une durée.
        cmd.extend(["--speed-limit", "1024", "--speed-time", "30"])

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ArcaneError(f"Timeout sur {url}")

    if result.returncode != 0:
        raise ArcaneError(f"curl {method} {url} a échoué : {result.stderr}")

    if output_file:
        return {"status": "downloaded"}

    if raw_response:
        return {"status": "ok", "body": result.stdout}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ArcaneError(f"Réponse invalide de {url} : {e}")


def check_connection(api_url: str, api_key: str) -> bool:
    """
    Vérifie que l'API Arcane est joignable.

    Appelle /environments (appel léger, < 1s).
    Retourne True si la connexion est OK, False sinon.
    Utile au début de --run pour éviter une cascade d'erreurs.
    """
    try:
        resp = _curl("GET", f"{api_url}/environments", api_key)
        return "data" in resp
    except ArcaneError:
        return False


def list_environments(api_url: str, api_key: str) -> list[dict]:
    """
    Retourne la liste des environnements (hôtes) connectés à Arcane.

    Format : [{ "id": "0", "name": "hote_local", "status": "online" }, ...]
    """
    resp = _curl("GET", f"{api_url}/environments", api_key)
    return resp.get("data", [])


def list_projects(api_url: str, api_key: str, env_id: str, limit: int = 50) -> list[dict]:
    """
    Liste tous les projets d'un environnement.

    Pagination : on commence avec ?limit=50 et on boucle si nécessaire.
    """
    projects = []
    start = 0

    while True:
        resp = _curl("GET", f"{api_url}/environments/{env_id}/projects?start={start}&limit={limit}", api_key)
        batch = resp.get("data", [])
        projects.extend(batch)

        if len(batch) < limit:
            break
        start += limit

    return projects


def get_project_compose(api_url: str, api_key: str, env_id: str, project_id: str) -> dict:
    """
    Récupère le compose et le .env d'un projet.

    Retourne : { "composeContent": "...", "envContent": "...",
                 "composeFileName": "compose.yaml", "services": [...] }
    """
    resp = _curl("GET", f"{api_url}/environments/{env_id}/projects/{project_id}/compose", api_key)
    return resp.get("data", {})


def get_project_runtime(api_url: str, api_key: str, env_id: str, project_id: str) -> dict:
    """Retourne l'état d'exécution d'un projet (running/stopped, services)."""
    resp = _curl("GET", f"{api_url}/environments/{env_id}/projects/{project_id}/runtime", api_key)
    return resp.get("data", {})


def stop_project(api_url: str, api_key: str, env_id: str, project_id: str) -> bool:
    """Arrête un projet (tous ses conteneurs). Retourne True si OK."""
    try:
        _curl("POST", f"{api_url}/environments/{env_id}/projects/{project_id}/down",
              api_key, raw_response=True)
        return True
    except ArcaneError:
        return False


def start_project(api_url: str, api_key: str, env_id: str, project_id: str) -> bool:
    """Démarre un projet. Retourne True si OK."""
    try:
        _curl("POST", f"{api_url}/environments/{env_id}/projects/{project_id}/up",
              api_key, data="{}", raw_response=True)
        return True
    except ArcaneError:
        return False


def create_volume_backup(api_url: str, api_key: str, env_id: str, volume_name: str) -> Optional[str]:
    """
    Crée un backup d'un volume Docker via l'API Arcane.

    Retourne l'ID du backup à télécharger, ou None si échec.
    """
    # La création d'un backup déclenche l'archivage du volume côté agent
    # Arcane (opération synchrone, peut dépasser 30s pour les gros volumes).
    # Timeout long en garde-fou ; pas de notion de progression ici.
    resp = _curl("POST", f"{api_url}/environments/{env_id}/volumes/{volume_name}/backups",
                 api_key, timeout=300)
    return resp.get("data", {}).get("id")


def download_volume_backup(api_url: str, api_key: str, env_id: str,
                           backup_id: str, dest_path: str) -> bool:
    """Télécharge un backup de volume vers un fichier local."""
    try:
        # Le téléchargement transfère l'archive complète du volume (peut être
        # plusieurs centaines de Mo voire Go). On contrôle par la PROGRESSION
        # (curl --speed-limit/--speed-time) : tant que ça transfère, on attend.
        # Le timeout=3600 n'est qu'un garde-fou absolu contre un serveur muet.
        _curl("GET", f"{api_url}/environments/{env_id}/volumes/backups/{backup_id}/download",
              api_key, output_file=dest_path, timeout=3600, progress_timeout=True)
        return True
    except ArcaneError:
        return False


def delete_volume_backup(api_url: str, api_key: str, env_id: str, backup_id: str) -> bool:
    """Supprime un backup de volume sur le serveur distant."""
    try:
        _curl("DELETE", f"{api_url}/environments/{env_id}/volumes/backups/{backup_id}", api_key)
        return True
    except ArcaneError:
        return False


def get_container_details(api_url: str, api_key: str, env_id: str, container_id: str) -> dict:
    """Détails d'un conteneur (mounts, volumes, labels)."""
    resp = _curl("GET", f"{api_url}/environments/{env_id}/containers/{container_id}", api_key)
    return resp.get("data", {})


def discover_host_paths(api_url: str, api_key: str, env_id: str) -> dict:
    """
    Découvre les répertoires hôte d'un environnement distant depuis les
    bind mounts du projet agent arcane.

    Le compose de l'agent monte (par convention) :
      <host_projects_dir> -> /app/data/projects   (répertoire des stacks)
      <host_data_dir>     -> /app/data            (répertoire des données)

    Retourne {"projects_dir": "/data/stacks", "data_dir": "/data/appdata"}.
    Retourne {} si l'agent n'est pas trouvé ou que les mounts sont absents —
    l'appelant doit alors retomber sur son fallback historique.
    """
    import yaml

    try:
        projects = list_projects(api_url, api_key, env_id)
    except ArcaneError:
        return {}

    # Trouver le projet agent arcane (nom contenant "arcane")
    agent_pid = None
    for p in projects:
        name = p.get("name", "")
        if "arcane" in name.lower():
            agent_pid = p.get("id")
            break
    if not agent_pid:
        return {}

    try:
        compose_data = get_project_compose(api_url, api_key, env_id, agent_pid)
    except ArcaneError:
        return {}

    compose_content = compose_data.get("composeContent", "")
    if not compose_content:
        return {}

    paths = {}
    try:
        data = yaml.safe_load(compose_content) or {}
    except yaml.YAMLError:
        return {}

    for svc in (data.get("services") or {}).values():
        for vol in (svc.get("volumes") or []):
            if isinstance(vol, dict):
                source, target = vol.get("source"), vol.get("target")
            elif isinstance(vol, str) and ":" in vol:
                parts = vol.split(":")
                source, target = parts[0], parts[1]
            else:
                continue
            if target == "/app/data/projects" and source:
                paths["projects_dir"] = source.rstrip("/")
            elif target == "/app/data" and source:
                paths["data_dir"] = source.rstrip("/")
    return paths