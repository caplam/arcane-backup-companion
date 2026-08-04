# Arcane Backup Companion (ABC)

Sauvegarde unifiée et automatique des projets Docker gérés par [Arcane](https://github.com/arcane) — multi-hôtes.

**[English](README.md)** · Français

Arcane Backup Companion (ABC) découvre automatiquement les environnements et projets via l'API Arcane, sauvegarde leurs données (compose, .env, bind mounts, volumes Docker) avec une rétention GFS, et permet une restauration simple ou une extraction vers un autre hôte.

## Fonctionnalités

- **Multi-hôtes** : sauvegarde les projets Docker répartis sur plusieurs serveurs (accès local direct ou SSH)
- **Découverte automatique** : environnements et projets listés depuis l'API Arcane
- **Données essentielles** : compose, .env, bind mounts, volumes Docker nommés
- **Rétention GFS** : Grandfather-Father-Son (quotidien / hebdomadaire / mensuel)
- **Restauration** : sur place (avec arrêt/redémarrage) ou extraction vers un répertoire (migration)
- **Hot backup SQLite** : sauvegarde de la base Arcane à chaud, sans downtime
- **Logs par hôte** : un log par environnement, purgé automatiquement quand les snapshots associés disparaissent
- **Tiers de sauvegarde** : `daily` / `weekly` / `all` selon la politique de rétention de chaque projet

## Prérequis

- Python 3.10+
- Un serveur [Arcane](https://github.com/arcane) avec une clé API
- Accès aux hôtes Docker : filesystem local (accès direct) ou SSH (clé publique)

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

Le setup interactif :
1. Demande l'URL de l'API Arcane et la clé API
2. Découvre les environnements (hôtes) et projets
3. Pour chaque projet : choisit les bind mounts et volumes à sauvegarder
4. Configure la rétention GFS
5. Génère `config.yaml` (ignoré par git)

## Utilisation

```bash
# Sauvegarder tous les projets (tier daily par défaut)
python3 main.py --run

# Sauvegarder un environnement spécifique
python3 main.py --run --env <nom>

# Sauvegarder plusieurs environnements
python3 main.py --run --env <h1> --env <h2>

# Tiers
python3 main.py --run --tier daily     # projets quotidiens (défaut)
python3 main.py --run --tier weekly    # projets hebdomadaires
python3 main.py --run --tier all       # tous les projets

# Estimation sans sauvegarder
python3 main.py --run --dry-run

# Voir la configuration actuelle
python3 main.py --status
```

## Restauration

```bash
# Menu interactif
python3 main.py --restore

# Restaurer le snapshot le plus récent d'un projet
python3 main.py --restore <env>/<projet>

# Restaurer un snapshot spécifique
python3 main.py --restore <env>/<projet>/<snapshot>

# Extraction vers un répertoire (sans toucher au projet — migration)
python3 main.py --restore <env>/<projet> --to /tmp/test
```

La restauration sur place :
1. Sauvegarde l'état actuel (`pre_restore_*`)
2. Arrête le projet (API Arcane)
3. Restaure compose, .env, bind mounts, volumes
4. Redémarre le projet

Les permissions et extended attributes des bind mounts sont préservés (`tar --xattrs`).

## Architecture

```
main.py       ← Point d'entrée (--setup, --run, --status, --restore)
config.py     ← Setup interactif + chargement YAML
backup.py     ← Boucle de sauvegarde (tar, SSH, API)
api.py        ← Wrapper API Arcane (curl)
restore.py    ← Restauration sur place / extraction
prune.py      ← Rétention GFS + métadonnées
models.py     ← Classes de données (Config, Project, BindMount, ...)
```

## Configuration avancée

### Exclusions globales

Dans `config.yaml`, `global_exclusions` liste les chemins de bind mounts à ne jamais sauvegarder (médias, téléchargements, gros volumes re-créables) :

```yaml
global_exclusions:
  - "/var/lib/data/media/"
  - "/var/lib/data/downloads/"
```

### Découverte des arborescences hôtes

Les hôtes n'ont pas tous la même arborescence (`/data/stacks`, `/docker-projects/stacks`, ...). ABC découvre dynamiquement le répertoire des projets de chaque hôte depuis les bind mounts de l'agent Arcane — rien n'est codé en dur.

### Rétention GFS

Chaque projet a une politique de rétention :

```yaml
retention:
  schedule: daily   # daily | weekly
  daily: 7          # garde 7 snapshots quotidiens
  weekly: 4         # garde 4 snapshots hebdomadaires
  monthly: 3        # garde 3 snapshots mensuels
```

### Notifications

Les notifications utilisent **uniquement le système natif d'Unraid** (le script `notify` dans `/usr/local/emhttp/webGui/scripts/notify`). Elles sont envoyées après chaque run de backup quand au moins un projet a été traité.

- Active/désactive-les pendant le setup, ou via la clé `notifications_enabled` dans `config.yaml` :

```yaml
notifications_enabled: true    # ou false
```

- Sur les hôtes non-Unraid, garde cette valeur à `false` (le script `notify` n'y existe pas).

## Sécurité

- La clé API Arcane vit dans `config.yaml` (ignoré par git)
- Les snapshots peuvent contenir des données sensibles — protège le `backup_root`
- Les permissions des fichiers restaurés sont préservées

## Notifications

Les notifications utilisent le **système de notification natif d'Unraid** (`/usr/local/emhttp/webGui/scripts/notify`) — Apprise ou les webhooks ne sont pas pris en charge.

Tu peux activer ou désactiver les notifications pendant `--setup` (ou via `notifications_enabled` dans `config.yaml`). Sur les hôtes non-Unraid, laisse-les désactivées.

## Licence

MIT
