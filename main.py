"""
main.py — Point d'entrée du script Arcane Backup Companion (ABC).

Usage :
  python3 main.py --setup    # Configuration interactive (première fois)
  python3 main.py --run      # Lancement d'un backup
  python3 main.py --status   # Affiche la configuration actuelle
  python3 main.py --help     # Aide

Premier lancement :
  Si --run est appelé sans config.yaml existant, le script
  lance automatiquement --setup.
"""

import sys
import os
from datetime import datetime

# Ajouter le répertoire courant au path (pour l'import des modules)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import run_setup, load_config, save_config
from api import check_connection
from backup import run_backup
from restore import run_restore
from models import Config


def cmd_setup():
    """Lance le setup interactif."""
    config = run_setup()
    print(f"  📁 Backup root : {config.backup_root}")
    for env in config.environments:
        print(f"  🌐 {env.name} ({len(env.projects)} projet(s))")
        for p in env.projects:
            bms = len([bm for bm in p.bind_mounts if bm.selected])
            vols = len([v for v in p.volumes if v.selected])
            status = "⏭️" if p.skip else "✅"
            print(f"    {status} {p.name} ({bms} bind mounts, {vols} volumes)")
    print()
    print("Pour lancer un backup : python3 main.py --run")


def cmd_status():
    """Affiche la configuration actuelle."""
    config = load_config()
    if not config:
        print("❌ Aucune configuration trouvée.")
        print("   Lance d'abord : python3 main.py --setup")
        sys.exit(1)

    print("╔══════════════════════════════════════════╗")
    print("║  Arcane Backup Companion (ABC) — Status            ║")
    print("╚══════════════════════════════════════════╝")
    print()
    print(f"  API Arcane : {config.arcane_api_url}")
    print(f"  Backup root : {config.backup_root}")
    print(f"  Connexion API : ", end="")
    if check_connection(config.arcane_api_url, config.arcane_api_key):
        print("✅ OK")
    else:
        print("❌ ÉCHEC")
    print()
    print(f"  Exclusions globales :")
    for excl in config.global_exclusions:
        print(f"    ⏭️  {excl}")
    print()

    for env in config.environments:
        print(f"  🌐 {env.name} ({env.access.mode.value})")
        for p in env.projects:
            if p.skip:
                print(f"    ⏭️  {p.name}")
            else:
                print(f"    ✅ {p.name}")
                for bm in p.bind_mounts:
                    icon = "✅" if bm.selected else "⏭️"
                    print(f"      {icon} {bm.path}")
                for v in p.volumes:
                    icon = "✅" if v.selected else "⏭️"
                    print(f"      {icon} volume:{v.name}")
                print(f"      📅 {p.retention.schedule} / {p.retention.policy} ({p.retention.keep_daily}d+{p.retention.keep_weekly}w+{p.retention.keep_monthly}m)")
        print()


def cmd_run():
    """Lance la sauvegarde complète."""
    config = load_config()
    if not config:
        print("❌ Aucune configuration trouvée. Lancement du setup...")
        cmd_setup()
        config = load_config()
        if not config:
            print("❌ Configuration échouée.")
            sys.exit(1)

    # Vérifier la connexion API avant de commencer
    print(f"🔍 Vérification API Arcane...", end=" ")
    if not check_connection(config.arcane_api_url, config.arcane_api_key):
        print("❌ API injoignable. Backup annulé.")
        sys.exit(1)
    print("✅ OK")

    # Déterminer le tier de backup et les environnements cibles
    tier = "all"
    dry_run = False
    target_envs = []  # vide = tous
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--tier" and i + 1 < len(args):
            tier = args[i + 1].lower()
            i += 2
        elif args[i] == "--env" and i + 1 < len(args):
            target_envs.append(args[i + 1].lower())
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            i += 1

    if tier not in ("all", "daily", "weekly"):
        print(f"❌ Tier inconnu : {tier}. Utilise 'daily', 'weekly' ou 'all'.")
        sys.exit(1)

    # Filtrer les environnements si --env est spécifié
    if target_envs:
        available = [e.name for e in config.environments]
        config.environments = [e for e in config.environments if e.name.lower() in target_envs]
        if not config.environments:
            print(f"❌ Aucun environnement trouvé parmi : {', '.join(target_envs)}")
            print(f"   Disponibles : {', '.join(available)}")
            sys.exit(1)

    print(f"📦 Démarrage du backup — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📁 Destination : {config.backup_root}")
    if tier != "all":
        print(f"🎯 Tier : {tier}")
    print()

    run_backup(config, tier, dry_run)


def cmd_restore():
    """Lance la restauration interactive."""
    config = load_config()
    if not config:
        print("❌ Aucune configuration trouvée.")
        print("   Lance d'abord : python3 main.py --setup")
        sys.exit(1)

    # Configurer le logging (sinon les messages restore sont silencieux)
    from backup import _setup_logging
    _setup_logging(config.backup_root, [e.name for e in config.environments])

    # Support: --restore env/projet --to /chemin
    target = ""
    target_dir = ""

    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--to" and i + 1 < len(args):
            target_dir = args[i + 1]
            i += 2
        elif not target:
            target = args[i]
            i += 1
        else:
            i += 1

    parts = target.split("/")

    env_name = parts[0] if len(parts) >= 1 and parts[0] else ""
    project_name = parts[1] if len(parts) >= 2 else ""
    snapshot_name = parts[2] if len(parts) >= 3 else ""

    run_restore(config, env_name, project_name, snapshot_name, target_dir)


def cmd_help():
    """Affiche l'aide."""
    print("Usage: python3 main.py [OPTION]")
    print()
    print("  --setup                    Configuration interactive")
    print("  --run [--tier all|daily|weekly] [--dry-run] [--env <name>]")
    print("                             Lancement d'un backup")
    print("                                --tier all   : tous les projets (défaut)")
    print("                                --tier daily : projets quotidiens uniquement")
    print("                                --tier weekly : projets hebdomadaires uniquement")
    print("                                --dry-run    : estime la taille sans backup")
    print("                                --env <name> : backup d'un environnement spécifique")
    print("                                              (ex: --env <nom_hote>)")
    print("                                              (ex: --env <h1> --env <h2>)")
    print("  --restore [env/projet]     Restauration interactive ou directe")
    print("  --status                   Affiche la configuration")
    print("  --help                     Affiche cette aide")
    print()
    print("Exemples :")
    print("  python3 main.py --setup                   # Première configuration")
    print("  python3 main.py --run                     # Backup de tous les projets")
    print("  python3 main.py --run --env <name>          # Backup d'un environnement uniquement")
    print("  python3 main.py --run --env <name1> --env <name2>  # Multi-environnements")
    print("  python3 main.py --run --tier daily        # Backup quotidien")
    print("  python3 main.py --run --tier all --env <name1> --env <name2>")
    print("  python3 main.py --status           # Voir la config")


def main():
    if len(sys.argv) < 2:
        # Sans argument : si config existe → run, sinon → setup
        config = load_config()
        if config:
            cmd_run()
        else:
            cmd_setup()
        return

    command = sys.argv[1]

    if command == "--setup":
        cmd_setup()
    elif command == "--run":
        cmd_run()
    elif command == "--restore":
        cmd_restore()
    elif command == "--status":
        cmd_status()
    elif command in ("--help", "-h"):
        cmd_help()
    else:
        print(f"❌ Option inconnue : {command}")
        print("   Utilise --help pour voir les options disponibles.")
        sys.exit(1)


if __name__ == "__main__":
    main()