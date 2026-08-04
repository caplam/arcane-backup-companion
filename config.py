"""
config.py — Setup interactif + chargement/sauvegarde de la configuration.

Le setup se déroule en plusieurs étapes :
  1. Connexion API Arcane
  2. Découverte des environnements
  3. Pour chaque environnement : découverte des projets
  4. Pour chaque projet : choix des bind mounts et volumes à sauvegarder
  5. Configuration de la rétention
  6. Sauvegarde dans config.yaml

Le fichier config.yaml est lu au début de chaque --run.
"""

import os
import sys
import yaml
from typing import Optional

from models import (
    Config, Environment, Project, BindMount, DockerVolume,
    HostAccess, AccessMode, Retention
)
from api import check_connection, list_environments, list_projects, get_project_compose


# ─── Utilitaires ───────────────────────────────────────────────────────────────

def _prompt(question: str, default: str = "") -> str:
    """Pose une question à l'utilisateur et retourne sa réponse."""
    if default:
        prompt = f"{question} [{default}] : "
    else:
        prompt = f"{question} : "
    try:
        value = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    if not value and default:
        return default
    return value


def _prompt_yes_no(question: str, default: bool = True) -> bool:
    """Question oui/non avec valeur par défaut."""
    choices = "Y/n" if default else "y/N"
    value = _prompt(f"{question} ({choices})", "y" if default else "n")
    return value.lower().startswith("y")


def _discover_bind_mounts_from_compose(compose_content: str, env_content: str = "") -> list[str]:
    """
    Extrait les chemins des bind mounts depuis le contenu YAML d'un compose.

    Utilise le .env (env_content) pour résoudre les variables d'environnement
    comme ${PWD}/data ou $HOME/config.

    Exclut automatiquement les chemins système (/etc/localtime, /proc, /sys...).
    """

    import re

    # Chemins système à exclure (bind mounts inutiles à sauvegarder)
    SYSTEM_PATHS = [
        "/",                          # racine : ne jamais sauvegarder tout le système
        "/etc/localtime", "/etc/timezone", "/etc/hostname",
        "/etc/hosts", "/etc/resolv.conf",
        "/proc", "/sys", "/sys/",
        "/var/run/docker.sock", "/run/docker.sock",
        "/dev", "/dev/",
        "/tmp", "/tmp/",
        # Média et gros volumes (re-créables, trop volumineux) — exemples,
        # l'utilisateur adapte à son environnement dans global_exclusions
        "/var/lib/docker",  # données Docker volumineuses (exemple par défaut)
    ]

    if not compose_content:
        return []

    # Parser le .env pour créer un dictionnaire de variables
    env_vars = {}
    if env_content:
        for line in env_content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env_vars[key.strip()] = val.strip()

    def _resolve_path(path: str) -> str:
        """Résout les variables d'environnement dans un chemin.

        Remplace ${VAR} et $VAR par leur valeur depuis le .env.
        Si une variable n'est pas définie, on la laisse telle quelle.
        """
        # Si le chemin ne contient pas de $, pas besoin de résoudre
        if "$" not in path:
            return path

        def _replace_var(match):
            var_name = match.group(1) or match.group(2)
            return env_vars.get(var_name, match.group(0))
        # ${VAR} ou $VAR
        pattern = r'\$\{([^}]+)\}|\$([a-zA-Z_][a-zA-Z0-9_]*)'
        return re.sub(pattern, _replace_var, path)

    def _is_system_path(path: str) -> bool:
        """Vrai si le chemin est un path système à exclure."""
        for sp in SYSTEM_PATHS:
            if path == sp or path.startswith(sp + "/"):
                return True
        return False


    try:
        data = yaml.safe_load(compose_content)
    except yaml.YAMLError:
        return []

    if not isinstance(data, dict):
        return []

    paths = []
    services = data.get("services", {})
    for svc_name, svc_config in services.items():
        if not isinstance(svc_config, dict):
            continue
        volumes = svc_config.get("volumes", [])
        if not isinstance(volumes, list):
            continue
        for vol in volumes:
            if isinstance(vol, str) and ":" in vol:
                source = vol.split(":")[0]
                # Exclure les volumes nommés (ex: redis-data:/data)
                # et les chemins avec variables (ex: ${PWD}/data)
                if not source.startswith("/") and not source.startswith("$"):
                    continue
                source = _resolve_path(source)
                if _is_system_path(source):
                    continue
                paths.append(source)
            elif isinstance(vol, dict):
                if vol.get("type") == "bind" and "source" in vol:
                    source = vol["source"]
                    if _is_system_path(source):
                        continue
                    paths.append(source)

    return sorted(set(p for p in paths if p))


# ─── Menu de configuration existante ───────────────────────────────────────────

def _setup_menu(config: Config) -> None:
    """
    Menu proposé quand une config existe déjà.

    Permet d'ajouter des environnements ou projets sans
    refaire le setup complet.
    """
    while True:
        print("Que veux-tu faire ?")
        print()
        print("  1) Ajouter un nouvel environnement")
        print("  2) Reconfigurer un environnement existant")
        print("  3) Ajouter un projet à un environnement")
        print("  4) Terminer")
        print()
        choice = input("Choix [4] : ").strip()

        if choice == "1":
            _setup_environments(config)
            save_config(config)
        elif choice == "2":
            print()
            _list_environments(config)
            idx = input("Numéro de l'environnement à reconfigurer : ").strip()
            if idx.isdigit() and int(idx) < len(config.environments):
                env = config.environments[int(idx)]
                config.environments.remove(env)
                # Vider les projets existants avant re-setup pour éviter les doublons
                env.projects = []
                _setup_projects(config, env)
                config.environments.append(env)
                save_config(config)
                print(f"  ✅ Environnement '{env.name}' mis à jour.")
        elif choice == "3":
            print()
            _list_environments(config)
            idx = input("Numéro de l'environnement : ").strip()
            if idx.isdigit() and int(idx) < len(config.environments):
                env = config.environments[int(idx)]
                _setup_project_picker(config, env)
                save_config(config)
        elif choice == "4" or choice == "":
            print()
            print("✅ Configuration terminée.")
            return
        else:
            print("  Choix invalide.")
        print()


def _list_environments(config: Config) -> None:
    """Affiche la liste des environnements configurés."""
    print("Environnements configurés :")
    for i, env in enumerate(config.environments):
        names = ", ".join(p.name for p in env.projects[:5])
        if len(env.projects) > 5:
            names += f"... ({len(env.projects)} total)"
        print(f"  [{i}] {env.name} ({names})")


def _setup_project_picker(config: Config, env: Environment) -> None:
    """
    Affiche tous les projets d'un environnement, marque ceux déjà configurés,
    et permet d'ajouter ou modifier un projet spécifique.
    """
    from api import list_projects, get_project_compose

    existing_names = {p.name for p in env.projects}
    projects_data = list_projects(config.arcane_api_url, config.arcane_api_key, env.id)

    if not projects_data:
        print("  ⚠️  Aucun projet trouvé dans cet environnement.")
        return

    # Déduplication (l'API peut retourner des doublons)
    seen = set()
    deduped = []
    for p in projects_data:
        name = p.get("name", "?")
        if name not in seen:
            seen.add(name)
            deduped.append(p)
    if len(deduped) < len(projects_data):
        print(f"  ({len(projects_data) - len(deduped)} doublon(s) ignoré(s))")
    projects_data = deduped

    print()
    print("  Projets disponibles :")
    for i, p in enumerate(projects_data):
        name = p.get("name", "?")
        if name in existing_names:
            p_obj = next(p2 for p2 in env.projects if p2.name == name)
            status = "✅" if not p_obj.skip else "⏭️"
            print(f"  [{i}] {status} {name} (déjà configuré)")
        else:
            print(f"  [{i}] ➕ {name} (nouveau)")

    print()
    idx = input("  Numéro du projet à configurer (Enter = annuler) : ").strip()
    if not idx.isdigit() or int(idx) >= len(projects_data):
        return

    p_data = projects_data[int(idx)]
    p_name = p_data.get("name", "?")

    # Si le projet existe déjà, le retirer pour le reconfigurer
    existing = [p for p in env.projects if p.name == p_name]
    if existing:
        if not _prompt_yes_no(f"  Reconfigurer '{p_name}' ?", True):
            return
        env.projects.remove(existing[0])
        print(f"  ♻️  {p_name} — reconfiguration")

    _configure_single_project(config, env, p_data)


def _configure_single_project(config: Config, env: Environment,
                               p_data: dict) -> None:
    """
    Configure un projet unique : bind mounts, volumes, rétention.
    Utilisé par le menu d'ajout/modification de projet.
    """
    from api import get_project_compose
    from models import Retention

    p_name = p_data.get("name", "?")
    p_id = p_data.get("id", "")
    p_status = p_data.get("status", "running")

    # Auto-skip stopped projects (section 7.4 du design)
    if p_status not in (None, "running"):
        print(f"  ─── {p_name} ({p_status}) ───")
        include = _prompt_yes_no(f"  Projet '{p_name}' n'est pas en cours d'exécution ({p_status}). Inclure quand même ?", False)
        if not include:
            print(f"  ⏭️  {p_name} — ignoré (status: {p_status})")
            return

    if p_name.lower() == "arcane":
        print(f"  ─── {p_name} — géré manuellement (hot backup SQLite) ───")
        include = _prompt_yes_no(f"  Inclure '{p_name}' ?", True)
        if include:
            env.projects.append(Project(id=p_id, name=p_name, data_dir="", retention=Retention()))
        return

    print(f"  ─── {p_name} ───")
    compose_data = get_project_compose(config.arcane_api_url, config.arcane_api_key, env.id, p_id)
    compose_content = compose_data.get("composeContent", "")
    env_content = compose_data.get("envContent", "")
    compose_file_name = compose_data.get("composeFileName", "compose.yaml")

    bind_paths = _discover_bind_mounts_from_compose(compose_content, env_content)

    print(f"    compose : {compose_file_name}")
    if bind_paths:
        print(f"    bind mounts détectés ({len(bind_paths)}) :")
        for bp in bind_paths:
            print(f"      • {bp}")

    bind_mounts = []
    for bp in bind_paths:
        keep = _prompt_yes_no(f"    Sauvegarder {bp} ?", True)
        bind_mounts.append(BindMount(path=bp, selected=keep))

    docker_volumes = []
    try:
        parsed = yaml.safe_load(compose_content)
        if isinstance(parsed, dict):
            vol_decls = parsed.get("volumes", {})
            if isinstance(vol_decls, dict):
                for vol_name in vol_decls:
                    keep = _prompt_yes_no(f"    Sauvegarder le volume Docker '{vol_name}' ?", True)
                    docker_volumes.append(DockerVolume(name=vol_name, selected=keep))
    except yaml.YAMLError:
        pass

    print()
    print("    Politique de rétention GFS :")
    print("      1) Default (7 daily, 4 weekly, 6 monthly)")
    print("      2) Weekly  (4 weekly, 6 monthly)")
    print("      3) Monthly (12 monthly)")
    print("      4) Unlimited")
    print("      5) Custom")
    choice = _prompt("    Choix", "1")
    if choice == "2":
        retention = Retention(policy="weekly", schedule="weekly", keep_daily=0, keep_weekly=4, keep_monthly=6)
    elif choice == "3":
        retention = Retention(policy="monthly", schedule="weekly", keep_daily=0, keep_weekly=0, keep_monthly=12)
    elif choice == "4":
        retention = Retention(policy="unlimited", schedule="daily", keep_daily=0, keep_weekly=0, keep_monthly=0)
    elif choice == "5":
        daily = int(_prompt("    Daily à garder", "7"))
        weekly = int(_prompt("    Weekly à garder", "4"))
        monthly = int(_prompt("    Monthly à garder", "6"))
        retention = Retention(policy="custom", schedule="daily", keep_daily=daily, keep_weekly=weekly, keep_monthly=monthly)
    else:
        retention = Retention(policy="default")

    skip = not _prompt_yes_no(f"    Activer la sauvegarde pour '{p_name}' ?", True)

    project = Project(
        id=p_id, name=p_name, compose_file_name=compose_file_name,
        compose_dir=compose_data.get("path", ""),
        data_dir="", bind_mounts=bind_mounts, volumes=docker_volumes,
        retention=retention, skip=skip,
    )
    env.projects.append(project)


# ─── Setup ─────────────────────────────────────────────────────────────────────

def run_setup() -> Config:
    """
    Lance le setup interactif complet.

    Sauvegarde la configuration incrémentalement : si tu quittes
    en cours de route, les valeurs déjà saisies (API, environnements)
    seront conservées et rechargées au prochain lancement.
    """
    print("╔══════════════════════════════════════════╗")
    print("║  Arcane Backup Companion (ABC) — Configuration    ║")
    print("╚══════════════════════════════════════════╝")
    print()

    # Charger la config existante si elle existe
    existing = load_config()
    if existing:
        config = existing
        print(f"  ℹ️  Configuration existante trouvée ({len(existing.environments)} environnement(s)).")
        print()
        _setup_menu(config)
        return config
    else:
        config = Config()

    # Étape 1 : API Arcane
    _setup_api(config)

    # Étape 1b : Répertoire de backup
    _setup_backup_root(config)

    # Étape 1c : Notifications
    _setup_notifications(config)

    # Étape 2 : Découverte des environnements
    _setup_environments(config)

    # Étape 3 : Sauvegarde
    save_config(config)
    print()
    print(f"✅ Configuration sauvegardée dans {_config_path()}")

    return config


def _setup_api(config: Config) -> None:
    """Configure l'URL et la clé API Arcane."""
    print("─── Connexion API Arcane ───")
    print()

    url = _prompt("URL de l'API Arcane", config.arcane_api_url)
    key = _prompt("Clé API Arcane")

    config.arcane_api_url = url.rstrip("/")
    config.arcane_api_key = key

    # Test de connexion
    print("  Test de connexion...", end=" ")
    if check_connection(url, key):
        print("✅ OK")
    else:
        print("❌ ÉCHEC")
        retry = _prompt_yes_no("Réessayer ?", True)
        if retry:
            _setup_api(config)
        else:
            print("Configuration annulée.")
            sys.exit(1)
    print()
    # Sauvegarde immédiate après validation de l'API
    save_config(config)
    print("  💾 API sauvegardée.")


def _setup_backup_root(config: Config) -> None:
    """Demande le répertoire de destination des backups."""
    print("─── Répertoire de backup ───")
    print()
    default = config.backup_root or "./backups"
    path = _prompt("Destination des sauvegardes", default)
    config.backup_root = path
    print()


def _setup_notifications(config: Config) -> None:
    """Configure les notifications (système natif Unraid uniquement)."""
    print("─── Notifications ───")
    print()
    print("  Seules les notifications du système natif Unraid sont prises en charge")
    print("  (script /usr/local/emhttp/webGui/scripts/notify).")
    print()
    enable = _prompt_yes_no("  Activer les notifications ?", config.notifications_enabled)
    config.notifications_enabled = enable
    if not enable:
        print("  ℹ️  Les notifications seront désactivées.")
    print()


def _setup_environments(config: Config) -> None:
    """Découvre et configure les environnements Arcane."""
    print("─── Découverte des environnements ───")
    print()

    envs_data = list_environments(config.arcane_api_url, config.arcane_api_key)

    if not envs_data:
        print("❌ Aucun environnement trouvé. Vérifie que des agents Arcane sont connectés.")
        sys.exit(1)

    print(f"  {len(envs_data)} environnement(s) trouvé(s) :")
    for i, env in enumerate(envs_data):
        name = env.get("name", "?")
        status = env.get("status", "?")
        print(f"    [{i}] {name} ({status})")

    print()

    # Sélection des environnements à configurer
    existing_names = {e.name for e in config.environments}

    # Si on ajoute depuis le menu, laisser choisir
    if existing_names:
        print("  Environnements disponibles :")
        for i, env_data in enumerate(envs_data):
            name = env_data.get("name", "?")
            status = env_data.get("status", "?")
            if name in existing_names:
                print(f"    [{i}] ✅ {name} (déjà configuré)")
            else:
                print(f"    [{i}] ➕ {name} ({status})")
        print()
        idx = input("  Numéro de l'environnement à configurer (Enter = annuler) : ").strip()
        if not idx.isdigit() or int(idx) >= len(envs_data):
            return
        envs_data = [envs_data[int(idx)]]

    for env_data in envs_data:
        name = env_data.get("name", "?")
        env_id = env_data.get("id", "")
        status = env_data.get("status", "?")

        if status != "online":
            print(f"  ⏭️  {name} ({status}) — ignoré (hors ligne)")
            continue

        # Ignorer les environnements déjà configurés
        if name in existing_names:
            print(f"  ✅ {name} — déjà configuré")
            continue

        # Environnement 0 = local, toujours inclus (accès direct au filesystem)
        if env_id == "0":
            print(f"\n  ✅ {name} — environnement local (accès direct)")
            access = HostAccess(mode=AccessMode.DIRECT)
        else:
            # Environnements distants = SSH, proposés
            print(f"\n  🌐 {name} — environnement distant")
            include = _prompt_yes_no(f"  Inclure '{name}' ?", True)
            if not include:
                continue

            ssh_host = _prompt(f"  Adresse SSH", name)
            ssh_port = int(_prompt("  Port SSH", "22"))
            ssh_user = _prompt("  Utilisateur SSH", "root")
            ssh_key = _prompt("  Chemin de la clé SSH", "/root/.ssh/id_ed25519")
            access = HostAccess(
                mode=AccessMode.SSH,
                hostname=ssh_host,
                port=ssh_port,
                username=ssh_user,
                ssh_key_path=ssh_key,
            )

        env = Environment(
            id=env_id,
            name=name,
            access=access,
        )

        # Découverte des projets
        _setup_projects(config, env)

        config.environments.append(env)
        print()
        # Sauvegarde après chaque environnement configuré
        save_config(config)
        print(f"  💾 Environnement '{env.name}' sauvegardé.")


def _setup_projects(config: Config, env: Environment) -> None:
    """Découvre et configure les projets d'un environnement."""
    projects_data = list_projects(config.arcane_api_url, config.arcane_api_key, env.id)

    if not projects_data:
        print("  ⚠️  Aucun projet trouvé dans cet environnement.")
        return

    print(f"\n  {len(projects_data)} projet(s) trouvé(s) :")

    # Déduplication : certains projets peuvent apparaître plusieurs fois
    total_raw = len(projects_data)
    seen = set()
    deduped = []
    for p in projects_data:
        name = p.get("name", "?")
        if name not in seen:
            seen.add(name)
            deduped.append(p)
    projects_data = deduped
    if len(projects_data) < total_raw:
        print(f"  ({total_raw - len(projects_data)} doublon(s) ignoré(s))")

    for p_data in projects_data:
        p_name = p_data.get("name", "?")
        p_id = p_data.get("id", "")
        p_status = p_data.get("status", "running")

        # Auto-skip stopped projects (section 7.4 du design)
        if p_status not in (None, "running"):
            print(f"\n    ─── {p_name} ({p_status}) ───")
            include = _prompt_yes_no(f"    Projet '{p_name}' n'est pas en cours d'exécution ({p_status}). Inclure quand même ?", False)
            if not include:
                print(f"    ⏭️  {p_name} — ignoré (status: {p_status})")
                continue

        # Exclure les projets "arcane" (gérés manuellement)
        if p_name.lower() == "arcane":
            print(f"\n    ─── {p_name} — géré manuellement (hot backup SQLite) ───")
            include = _prompt_yes_no(f"    Inclure '{p_name}' ?", True)
            if not include:
                continue
            project = Project(
                id=p_id,
                name=p_name,
                data_dir="",
                retention=Retention(),
            )
            env.projects.append(project)
            continue

        print(f"\n    ─── {p_name} ───")

        # Récupérer le compose
        compose_data = get_project_compose(config.arcane_api_url, config.arcane_api_key, env.id, p_id)
        compose_content = compose_data.get("composeContent", "")
        env_content = compose_data.get("envContent", "")
        compose_file_name = compose_data.get("composeFileName", "compose.yaml")

        # Détecter les bind mounts (en résolvant les variables depuis le .env)
        bind_paths = _discover_bind_mounts_from_compose(compose_content, env_content)

        print(f"      compose : {compose_file_name}")
        if bind_paths:
            print(f"      bind mounts détectés ({len(bind_paths)}) :")
            for bp in bind_paths:
                print(f"        • {bp}")
        else:
            print(f"      Aucun bind mount détecté.")

        # Demander pour chaque bind mount
        bind_mounts = []
        for bp in bind_paths:
            keep = _prompt_yes_no(f"      Sauvegarder {bp} ?", True)
            bind_mounts.append(BindMount(path=bp, selected=keep))

        # Détecter les volumes Docker (depuis le compose)
        docker_volumes = []
        try:
            parsed = yaml.safe_load(compose_content)
            if isinstance(parsed, dict):
                # Volumes nommés déclarés en top-level
                vol_decls = parsed.get("volumes", {})
                if isinstance(vol_decls, dict):
                    for vol_name in vol_decls:
                        keep = _prompt_yes_no(f"      Sauvegarder le volume Docker '{vol_name}' ?", True)
                        docker_volumes.append(DockerVolume(name=vol_name, selected=keep))
        except yaml.YAMLError:
            pass

        # Rétention spécifique
        print()
        print("      Politique de rétention GFS :")
        print("        1) Default (7 daily, 4 weekly, 6 monthly)")
        print("        2) Weekly  (4 weekly, 6 monthly, pas de daily)")
        print("        3) Monthly (12 monthly, pas de daily/weekly)")
        print("        4) Unlimited (tout garder)")
        print("        5) Custom")
        choice = _prompt("      Choix", "1")
        if choice == "2":
            retention = Retention(policy="weekly", schedule="weekly", keep_daily=0, keep_weekly=4, keep_monthly=6)
        elif choice == "3":
            retention = Retention(policy="monthly", schedule="weekly", keep_daily=0, keep_weekly=0, keep_monthly=12)
        elif choice == "4":
            retention = Retention(policy="unlimited", schedule="daily", keep_daily=0, keep_weekly=0, keep_monthly=0)
        elif choice == "5":
            daily = int(_prompt("      Daily à garder", "7"))
            weekly = int(_prompt("      Weekly à garder", "4"))
            monthly = int(_prompt("      Monthly à garder", "6"))
            retention = Retention(policy="custom", schedule="daily", keep_daily=daily, keep_weekly=weekly, keep_monthly=monthly)
        else:
            retention = Retention(policy="default")

        skip = not _prompt_yes_no(f"      Activer la sauvegarde pour '{p_name}' ?", True)

        project = Project(
            id=p_id,
            name=p_name,
            compose_file_name=compose_file_name,
            data_dir="",
            bind_mounts=bind_mounts,
            volumes=docker_volumes,
            retention=retention,
            skip=skip,
        )
        env.projects.append(project)


# ─── Chargement / Sauvegarde ───────────────────────────────────────────────────

def _config_dir() -> str:
    """Retourne le répertoire de configuration."""
    return os.path.dirname(os.path.abspath(__file__))


def _config_path() -> str:
    """Retourne le chemin complet du fichier config.yaml."""
    return os.path.join(_config_dir(), "config.yaml")


def save_config(config: Config) -> None:
    """
    Sauvegarde la configuration en YAML.

    Utilise un custom representer pour les enums (AccessMode).
    """
    from dataclasses import asdict

    # Custom representer pour les enums → leur valeur .value
    def _enum_representer(dumper, data):
        return dumper.represent_str(data.value)

    yaml.add_representer(AccessMode, _enum_representer)

    data = asdict(config)
    path = _config_path()
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    # Permissions restrictives (contient la clé API)
    os.chmod(path, 0o600)


def load_config() -> Config:
    """
    Charge la configuration depuis config.yaml.

    Retourne un objet Config, ou None si le fichier n'existe pas.
    """
    path = _config_path()
    if not os.path.exists(path):
        return None

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"❌ Erreur de lecture du fichier config.yaml : {e}")
        print(f"   Le fichier a été généré par une ancienne version.")
        print(f"   Supprime-le et relance --setup :")
        print(f"     rm {path}")
        print(f"     python3 main.py --setup")
        sys.exit(1)

    if not data:
        return None

    return _dict_to_config(data)


def _dict_to_config(data: dict) -> Config:
    """Convertit un dict YAML en objet Config (et ses sous-objets)."""
    envs = []
    for e_data in data.get("environments", []):
        access_data = e_data.get("access", {})
        access = HostAccess(
            mode=AccessMode(access_data.get("mode", "direct")),
            hostname=access_data.get("hostname", ""),
            port=access_data.get("port", 22),
            username=access_data.get("username", "root"),
            ssh_key_path=access_data.get("ssh_key_path", ""),
        )

        projects = []
        for p_data in e_data.get("projects", []):
            bms = [BindMount(**bm) for bm in p_data.get("bind_mounts", [])]
            vols = [DockerVolume(**v) for v in p_data.get("volumes", [])]
            ret = p_data.get("retention", {})
            retention = Retention(
                policy=ret.get("policy", "default"),
                schedule=ret.get("schedule", "daily"),
                keep_daily=ret.get("keep_daily", 7),
                keep_weekly=ret.get("keep_weekly", 4),
                keep_monthly=ret.get("keep_monthly", 6),
            )
            projects.append(Project(
                id=p_data.get("id", ""),
                name=p_data.get("name", ""),
                compose_file_name=p_data.get("compose_file_name", "compose.yaml"),
                data_dir=p_data.get("data_dir", ""),
                bind_mounts=bms,
                volumes=vols,
                retention=retention,
                skip=p_data.get("skip", False),
            ))

        envs.append(Environment(
            id=e_data.get("id", ""),
            name=e_data.get("name", ""),
            access=access,
            projects=projects,
            global_retention=Retention(**e_data.get("global_retention", {})),
        ))

    return Config(
        arcane_api_url=data.get("arcane_api_url", ""),
        arcane_api_key=data.get("arcane_api_key", ""),
        backup_root=data.get("backup_root", ""),
        environments=envs,
        global_exclusions=data.get("global_exclusions", []),
    )