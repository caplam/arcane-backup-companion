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

**Recommandé : environnement virtuel (venv)** — isole les dépendances, survit aux
reboots (essentiel sur Unraid où le python système est en tmpfs et perd ses
packages à chaque reboot), et évite de polluer le python système.

```bash
git clone <repo-url> arcane-backup-companion
cd arcane-backup-companion

# Créer le venv dans le projet + installer les dépendances
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# Vérifier
./venv/bin/python -c "import yaml; print('yaml OK', yaml.__version__)"
```

Toutes les commandes utilisent ensuite `./venv/bin/python` (pas `python3` système).

### Cas particulier Unraid (production)

Sur Unraid, le projet doit être déployé sur un **partage persistant** (ex: sur le
pool array/cache, pas sur une clé USB) et exécuté via **User Scripts**.
⚠️ **NE PAS** installer les dépendances dans le python système (`pip3 install pyyaml`)
— `/usr/` est un tmpfs, tout est purgé au reboot. Utiliser le venv sur le partage persistant :

```bash
cd <partage-persistant>/arcane-backup-companion
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

**User Scripts Unraid** (1 par environnement) — la commande DOIT pointer vers le venv :

```bash
<partage-persistant>/arcane-backup-companion/venv/bin/python <partage-persistant>/arcane-backup-companion/main.py --run
```

> **Symptôme si on utilise `python3 main.py --run`** : après un reboot,
> `ModuleNotFoundError: No module named 'yaml'` (pyyaml purgé du tmpfs).
> Correctif : pointer le user script vers `venv/bin/python` (voir ci-dessus).


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
# Sauvegarder tous les projets (tous les tiers par défaut)
python3 main.py --run

# Sauvegarder un environnement spécifique (tous les tiers)
python3 main.py --run --env <nom>

# Sauvegarder plusieurs environnements
python3 main.py --run --env <h1> --env <h2>

# Tiers
python3 main.py --run --tier all       # tous les projets (défaut)
python3 main.py --run --tier daily     # projets quotidiens uniquement
python3 main.py --run --tier weekly    # projets hebdomadaires uniquement

# Estimation sans sauvegarder
python3 main.py --run --dry-run

# Voir la configuration actuelle
python3 main.py --status
```

## Tiers de sauvegarde et planification

Il y a **trois concepts distincts** qui interagissent. Les comprendre évite les surprises lors de la configuration des cron.

### 1. `schedule` du projet (configuré par projet)

Chaque projet a une politique de rétention définie pendant `--setup` :

```yaml
retention:
  schedule: daily   # daily | weekly
  daily: 7          # garde 7 snapshots quotidiens
  weekly: 4         # garde 4 snapshots hebdomadaires
  monthly: 6        # garde 6 snapshots mensuels
```

Le champ `schedule` contrôle uniquement **à quel tier le projet appartient** pour le filtrage — il ne planifie **rien** par lui-même. ABC ne se lance jamais tout seul : un cron (ou User Script) doit le déclencher.

### 2. `--tier` (filtre de sauvegarde par run)

Le flag `--tier` filtre quels projets sont sauvegardés pendant un run :

| Tier | Projets sauvegardés |
|---|---|
| `all` (**défaut**) | Tous les projets, quel que soit leur `schedule` |
| `daily` | Uniquement les projets avec `schedule: daily` |
| `weekly` | Uniquement les projets avec `schedule: weekly` |

⚠️ **Danger** : si tu lances un cron quotidien avec `--tier daily`, les projets avec `schedule: weekly` ne seront **jamais** sauvegardés. Avec le tier par défaut `all`, tous les projets sont sauvegardés à chaque run.

### 3. Rétention GFS (prune, exécutée après chaque projet)

La rétention GFS **ne décide jamais quand** sauvegarder — elle **nettoie après chaque run**, en gardant au maximum `daily` + `weekly` + `monthly` snapshots par projet et en supprimant le surplus.

**Conséquence** : la fréquence des snapshots = la fréquence du cron. Si un projet est sauvegardé quotidiennement mais a `schedule: weekly`, un snapshot est créé chaque jour, puis le prune GFS supprime tout sauf un snapshot par semaine (et un par mois). C'est fonctionnellement correct mais légèrement gaspilleur (les sauvegardes quotidiennes d'un projet weekly sont jetées).

### Configurations de cron recommandées

| Configuration | Avantages | Inconvénients |
|---|---|---|
| **1 cron quotidien par env, sans `--tier` (défaut `all`)** | Simple, tout est couvert, le GFS régule la rétention | Les projets weekly sont sauvegardés chaque jour puis purgés (gaspillage) |
| **2 crons par env** : daily `--tier daily` + weekly `--tier weekly` | Économe, les projets weekly ne tournent que quand prévu | Plus de scripts à maintenir |

Pour un homelab, **un cron quotidien par environnement** (défaut `all`) est généralement le meilleur compromis.

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
