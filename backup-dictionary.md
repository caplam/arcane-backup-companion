# Arcane Backup Companion (ABC) — Dictionnaire d'optimisation des sauvegardes

> **Objectif** : Déterminer pour chaque application quelles données sont essentielles à sauvegarder, lesquelles peuvent être exclues, et si l'app a déjà un mécanisme de backup intégré.
> 
> Utilisé pour configurer les bind mounts dans ABC et optimiser vitesse/volume.

---

## Apps média

### Plex
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Backup via export/import de la base (Plex dance) |
| Répertoires essentiels | `/config/Library/Application Support/Plex Media Server/` (Preferences.xml, plug-ins, base de données) |
| À exclure | `Cache/` (8 Go, recréé), `Metadata/` (30 Go, recréé), `Media/` (prévisualisations, 101 Go) |
| Taille estimée | Config : ~500 Mo — Cache/Metadata/Media : ~140 Go (excluables) |
| Notes | La base contient les bibliothèques, le statut de visionnage, les listes. Cache et metadata se reconstruisent automatiquement |

### Jellyfin
| Champ | Valeur |
|---|---|
| Backup intégré | Oui. Backup intégré dans l'interface admin (config + metadata) |
| Répertoires essentiels | `/config/data/` (base, users, watchstate), `/config/` (fichiers xml, encoding, logging) |
| À exclure | `/config/cache/` (recréé), `/config/data/transcoding-temp/` (temporaire) |
| Taille estimée | Config : 100-500 Mo selon metadata |
| Notes | Le backup intégré exporte users, watchstate, collections. Metadata peut être re-scanné depuis les fichiers mais prend du temps |

### Jellystat
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Simple base Postgres + config |
| Répertoires essentiels | `/app/backend/data/` (base SQLite ou config) |
| À exclure | Rien |
| Taille estimée | < 100 Mo |
| Notes | Application de statistiques, données peu volumineuses. Base Postgres si configuré |

### Jellyplex
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Proxy/sync entre Jellyfin et Plex |
| Répertoires essentiels | `/config/` (config) |
| À exclure | Rien |
| Taille estimée | < 50 Mo |
| Notes | Projet utilitaire, config minimale |

---

## Apps de gestion de photos

### Immich
| Champ | Valeur |
|---|---|
| Backup intégré | Oui. CLI `immich backup` (export photos + metadata) + `pg_dump` pour la base |
| Répertoires essentiels | `UPLOAD_LOCATION` (photos/vidéos), `DB_DATA` (Postgres), `/config/` (fichiers de config) |
| À exclure | `/cache/` (thumbnails encodés, recréés), `/library/` (raw uploads vs encoded) |
| Taille estimée | Dépend du nombre de photos. Les thumbnails peuvent représenter 30-50% de l'espace mais sont recréés |
| Notes | Stratégie recommandée : `immich backup` + `pg_dump`. Les thumbnails sont regénérés automatiquement avec `immich regenerate-thumbnails` |

---

## Apps de gestion de documents

### Paperless-ngx
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Backup via export des fichiers + base de données |
| Répertoires essentiels | `/usr/src/paperless/data/` (base SQLite), `/usr/src/paperless/media/` (documents), `/usr/src/paperless/export/` (optionnel) |
| À exclure | `/usr/src/paperless/data/index/` (index de recherche, recréé avec une réindexation) |
| Taille estimée | Documents : 1-50 Go, base : 10-100 Mo, index : 100-500 Mo |
| Notes | L'index de recherche peut être regénéré avec `document_index rebuild`. Si la base et les documents sont saufs, tout est récupérable |

---

## Apps d'authentification/utilisateurs

### Authentik
| Champ | Valeur |
|---|---|
| Backup intégré | Non officiel. Blueprints (export/import de configuration) |
| Répertoires essentiels | `/database/` (Postgres, données critiques : users, policies, flows), `/certs/` (certificats) |
| À exclure | `/media/` (fichiers uploadés, logos, recréables) |
| Taille estimée | Base : 50-200 Mo, media : < 50 Mo |
| Notes | La base Postgres est critique. `pg_dump` recommandé. Les blueprints permettent de restaurer la configuration (flows, policies) mais pas les users |

---

## Apps de bookmarking

### Linkwarden
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Backup via `pg_dump` + copie des données |
| Répertoires essentiels | `/data/data/` (base Postgres), `/data/uploads/` (screenshots, archives) |
| À exclure | Rien |
| Taille estimée | < 1 Go |
| Notes | Données peu volumineuses. `pg_dumpall -U postgres -c > backup.sql` pour la base |

---

## Apps de monitoring

### Uptime Kuma
| Champ | Valeur |
|---|---|
| Backup intégré | Oui. Export JSON dans l'interface (Settings → Backup) |
| Répertoires essentiels | `/app/data/` (base SQLite, config) |
| À exclure | Rien |
| Taille estimée | < 10 Mo |
| Notes | Backup via export JSON (recommandé) ou copie de la base SQLite. Données minimales |

### Tautulli
| Champ | Valeur |
|---|---|
| Backup intégré | Oui. Backup automatique dans l'interface (Settings → Backups) |
| Répertoires essentiels | `/config/` (base SQLite, config.ini, logs) |
| À exclure | `/config/logs/` (logs, recréés) |
| Taille estimée | < 50 Mo |
| Notes | Backup intégré : va dans `/config/backups/`. Sinon copie de la base SQLite |

### Zabbix
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Backup via `pg_dump` de la base + fichiers de config |
| Répertoires essentiels | Base de données (Postgres/MySQL), `/etc/zabbix/` (config), `/usr/share/zabbix/` (fichiers web) |
| À exclure | `/tmp/` (temporaire), `/var/log/` (logs) |
| Taille estimée | Base : 1-10 Go selon historique, config : < 10 Mo |
| Notes | L'historique (events, trends) est volumineux. Stratégie : backup complet hebdo + historique optionnel. Les templates et hosts sont dans la base |

### Speedtest Tracker
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Base + config |
| Répertoires essentiels | `/config/` (base SQLite, config) |
| À exclure | Rien |
| Taille estimée | < 50 Mo |
| Notes | Données peu volumineuses, historique des tests |

### Elastic Agent
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Config uniquement (les données sont dans Elasticsearch) |
| Répertoires essentiels | `/etc/elastic-agent/` (fichier de config YAML) |
| À exclure | Tout sauf la config (les données sont sur Elasticsearch) |
| Taille estimée | < 1 Mo |
| Notes | Ne backup que la config. Les logs/metrics sont dans Elasticsearch (backup séparé) |

---

## Apps de gestion de serveurs

### Guacamole
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Backup de la base de données (Postgres/MySQL) |
| Répertoires essentiels | Base de données (Postgres), `/config/` (fichiers de config) |
| À exclure | `/tmp/` (temporaire) |
| Taille estimée | < 50 Mo |
| Notes | La base contient les connexions, users, permissions. Extension guacamole-auth-jdbc |

### Proxmox Backup Server
| Champ | Valeur |
|---|---|
| Backup intégré | Oui. PBS a son propre système de backup (datastore) |
| Répertoires essentiels | `/etc/proxmox-backup/` (config), datastore (les backups eux-mêmes) |
| À exclure | `/var/log/` (logs), `/var/cache/` (cache) |
| Taille estimée | Config : < 10 Mo, datastore : plusieurs To |
| Notes | PBS est fait pour le backup. Ne pas backup le datastore avec un autre outil — utiliser les outils PBS |

---

## Apps de réseautique

### Homepage (gethomepage)
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Fichiers de config YAML uniquement |
| Répertoires essentiels | `/app/config/` (bookmarks.yaml, services.yaml, settings.yaml, widgets.yaml) |
| À exclure | Rien |
| Taille estimée | < 1 Mo |
| Notes | Config uniquement, tout est dans les fichiers YAML. Pas de base de données |

### Gluetun
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Fichier de config uniquement |
| Répertoires essentiels | Aucun (tout est dans les variables d'environnement du compose) |
| À exclure | Tout (données éphémères) |
| Taille estimée | 0 |
| Notes | Ne rien sauvegarder. Les credentials VPN sont dans le .env, le reste est dans docker-compose.yml |

### Netboot
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Config + fichiers TFTP |
| Répertoires essentiels | `/config/` (fichiers de config), `/assets/` (fichiers TFTP, ISO) |
| À exclure | `/tmp/` (temporaire) |
| Taille estimée | Config : < 10 Mo, assets : 1-5 Go (ISO) |
| Notes | Les ISO peuvent être re-téléchargés. Config uniquement si les ISOs sont conservés ailleurs |

---

## Apps de gestion de contenu

### Seer
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Fichiers de config + base |
| Répertoires essentiels | `/config/` (base SQLite, config) |
| À exclure | `/cache/` (miniatures, recréées) |
| Taille estimée | < 50 Mo |
| Notes | Lecteur de comics/manga. Cache des miniatures recréé |

### Tracearr
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Base SQLite + config |
| Répertoires essentiels | `/config/` (base SQLite, config) |
| À exclure | Rien |
| Taille estimée | < 50 Mo |
| Notes | Application de tracking pour les *arr apps |

### Lyrion Music Server (LMS)
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Base + bibliothèque musicale |
| Répertoires essentiels | `/config/` (base, preferences, cache), `/music/` (bibliothèque musicale) |
| À exclure | `/config/cache/` (cache, recréé) |
| Taille estimée | Config : < 100 Mo, musique : dépend de la collection |
| Notes | La bibliothèque musicale est souvent sur un volume séparé (NAS). Le cache peut être exclu |

---

## Apps utilitaires

### Watchtower
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Aucune donnée persistante |
| Répertoires essentiels | Aucun |
| À exclure | Tout |
| Taille estimée | 0 |
| Notes | Aucune donnée à backup. Tout est dans le compose/env |

### Notification (Apprise)
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Fichier de config |
| Répertoires essentiels | `/config/` (apprise.yml, config) |
| À exclure | Rien |
| Taille estimée | < 1 Mo |
| Notes | Config uniquement (URLs de notification). Très petit |

### OpenVAS
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Base + plugins |
| Répertoires essentiels | `/var/lib/openvas/` (plugins, résultats), `/etc/openvas/` (config) |
| À exclure | `/var/cache/openvas/` (cache, recréé) |
| Taille estimée | Plugins : 500 Mo - 1 Go, résultats : variable |
| Notes | Les plugins sont mis à jour via `greenbone-nvt-sync`. Les résultats de scan peuvent être volumineux |

### Pinepods
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Base + config |
| Répertoires essentiels | `/data/` (base + config) |
| À exclure | Rien |
| Taille estimée | < 100 Mo |
| Notes | Application de podcasts, données peu volumineuses |

### Maxify-SMBv1
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Fichier de config |
| Répertoires essentiels | `/config/` (smb.conf) |
| À exclure | Tout sauf config |
| Taille estimée | < 1 Mo |
| Notes | Proxy SMB, config uniquement |

### LuckyBackup
| Champ | Valeur |
|---|---|
| Backup intégré | Oui. C'est un outil de backup (frontend rsync) |
| Répertoires essentiels | `/config/` (profiles de backup, config) |
| À exclure | Rien (les données backupées sont définies par l'utilisateur) |
| Taille estimée | Config : < 1 Mo |
| Notes | Backup les profiles de configuration, pas les données elles-mêmes (rsync) |

### Unraid2Compose
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Aucune donnée persistante |
| Répertoires essentiels | Aucun |
| À exclure | Tout |
| Taille estimée | 0 |
| Notes | Outil de conversion, pas de données persistantes |

---

## Apps système

### Arcane (lui-même)
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Base SQLite + .env |
| Répertoires essentiels | `/app/data/` (base SQLite), `/app/` (compose, .env) |
| À exclure | `/app/cache/` |
| Taille estimée | Base : 10-50 Mo |
| Notes | **Critique.** La base contient tous les projets, compose files, état. Backup déjà géré par ABC avec `backup_arcane()` |

### Proxmox Backup (PBS)
| Champ | Valeur |
|---|---|
| Backup intégré | Oui (PBS lui-même) |
| Répertoires essentiels | `/etc/proxmox-backup/` (config) |
| À exclure | Datastore (déjà géré par PBS) |
| Taille estimée | Config : < 10 Mo |
| Notes | Ne pas backup le datastore avec un autre outil. PBS gère ses propres backups |

---

## Résumé des optimisations possibles

| App | Gain potentiel | Ce qu'on exclut |
|---|---|---|
| **Plex** | **~140 Go** | Cache, Metadata, Media |
| **Jellyfin** | **~5 Go** | cache, transcoding-temp |
| **Immich** | **~50 Go** | thumbnails encodés |
| **Paperless** | **~500 Mo** | index de recherche |
| **Authentik** | **~50 Mo** | media |
| **Tautulli** | **~10 Mo** | logs |
| **Zabbix** | **~10 Go** | historique ancien, logs |
| **Seer** | **~50 Mo** | cache miniatures |
| **LMS** | **~100 Mo** | cache |
| **OpenVAS** | **~500 Mo** | cache plugins |

**Apps sans données à backup :** Gluetun, Watchtower, Unraid2Compose
**Apps avec backup intégré préféré :** Jellyfin, Immich (CLI), Uptime Kuma (export), Tautulli (auto)

---

## Projet library (arr apps)

Projet composite contenant 11 services de gestion de médias. Les données sont sur l'hôte local.

### Sabnzbd
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Config + files de téléchargement |
| Répertoires essentiels | `/config/` (sabnzbd.ini) |
| À exclure | `/data/downloads/` (fichiers temporaires, archivés ailleurs) |
| Taille estimée | Config < 10 Mo |
| Chemin hôte | `/var/lib/appdata/sabnzbd/` |

### Sonarr / Sonarr Mangas
| Champ | Valeur |
|---|---|
| Backup intégré | Oui. Export JSON via System → Backup |
| Répertoires essentiels | `/config/` (config.xml, base SQLite) |
| À exclure | `/config/Backups/` (auto-gérés) |
| Chemin hôte | `/var/lib/appdata/sonarr/` |

### Radarr
| Champ | Valeur |
|---|---|
| Backup intégré | Oui. Export JSON via System → Backup |
| Répertoires essentiels | `/config/` (config.xml, base SQLite) |
| À exclure | `/config/Backups/` |
| Chemin hôte | `/var/lib/appdata/radarr/` |

### Lidarr
| Champ | Valeur |
|---|---|
| Backup intégré | Oui. Export JSON via System → Backup |
| Répertoires essentiels | `/config/` (config.xml, base SQLite) |
| À exclure | `/config/Backups/` |
| Chemin hôte | `/var/lib/appdata/lidarr/` |

### Bazarr / Bazarr Mangas
| Champ | Valeur |
|---|---|
| Backup intégré | Oui. Export JSON via System → Backup |
| Répertoires essentiels | `/config/` (config.yaml, base SQLite) |
| À exclure | `/config/backup/` |
| Chemin hôte | `/var/lib/appdata/bazarr/` |

### Prowlarr (binhex)
| Champ | Valeur |
|---|---|
| Backup intégré | Oui. Export JSON via System → Backup |
| Répertoires essentiels | `/config/` (config.xml, base SQLite) |
| À exclure | `/config/Backups/` |
| Chemin hôte | `/var/lib/appdata/binhex-prowlarr/` |

### Flaresolverr
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Aucune donnée persistante |
| Répertoires essentiels | Aucun |
| Taille estimée | 0 |
| Notes | Proxy Cloudflare, pas de données à backup |

### LazyLibrarian
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Config + base |
| Répertoires essentiels | `/config/` (base SQLite, config.ini) |
| À exclure | `/config/logs/` |
| Chemin hôte | `/var/lib/appdata/lazylibrarian/` |

### Profilarr
| Champ | Valeur |
|---|---|
| Backup intégré | Non. Config uniquement |
| Répertoires essentiels | `/config/` (profiles JSON) |
| Taille estimée | < 1 Mo |