# qbittorrent-mam seed config

Copied ONCE into `compose/qbittorrent-mam-config/qBittorrent/` (gitignored) before
the first `up`; after that qBittorrent owns the live copy. Change settings in the
WebUI, then port any key that matters back here. Why each setting exists:
`docs/runbook.md` → qbittorrent-mam. qBittorrent drops comments when it
rewrites a conf, so the reasons live in the runbook, not in `qBittorrent.conf`.
