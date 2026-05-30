[English](README.md) | [Slovenčina](README.sk.md) | [Čeština](README.cs.md)

# archivczsk-iptvexternal

Custom addon repository for the [ArchivCZSK](https://github.com/archivczsk/archivczsk)
Enigma2 plugin.

Adds support for external IPTV sources (Tvheadend server, M3U playlist) by
generating Enigma2 userbouquets and injecting EPG so channels appear
natively in the Enigma2 channel list.

## Addons in this repository

### 🎬 plugin.video.tvheadend — Tvheadend client

Client for a [Tvheadend](https://tvheadend.org/) server via the ArchivCZSK
framework:

- Browse Tvheadend channels (live TV + radio) directly from ArchivCZSK
- Two connection modes: **HTTP API** (port 9981) or native **HTSP** protocol
  (port 9982) — selectable in settings. HTSP fetches all data (channels, EPG,
  DVR, tags) over a single connection; streaming always goes directly via the
  Tvheadend HTTP endpoint (9981)
- DVR archive playback with title search and genre categorisation
- Picon download via the Tvheadend `imagecache` endpoint
- Auto-generated Enigma2 userbouquet for TV + Radio
- Direct EPG injection into the Enigma2 `eEPGCache` (no epgimport plugin required)
- Native DVB player option (OE≥2.5) for userbouquet zapping and in-app playback
  (live + DVR) — plays via the receiver's hardware demux for native DVB
  subtitles/teletext; requires the Tvheadend server to allow plain/basic auth
  (Authentication type = "Both plain and digest")
- Localised in 🇸🇰 Slovak, 🇨🇿 Czech and 🇬🇧 English

### 📡 plugin.video.e2m3u2bouquet — E2m3u2bouquet

Converts an M3U playlist into an Enigma2 userbouquet — works with any
M3U source:

- M3U playlist download + parse (HTTP, HTTPS, gzip)
- Generates `userbouquet.m3u_iptv.tv` (+ `.radio` for radio stations) with
  tvg-id mapping
- XMLTV EPG injection (your own feed or the TVH XMLTV endpoint)
- Picon download from `tvg-logo` URLs
- Channel mapping override XML for custom sort/group naming
- Optional Tvheadend integration (channel enrichment via the TVH API)
- Auto-refresh of playlist and EPG on configurable intervals (4h, 8h, 24h, 2d, 7d)

## Installing in ArchivCZSK

In the ArchivCZSK GUI, open repository management and add the URL of this
repository. Step-by-step instructions will be added once the feature is
officially available in ArchivCZSK.

## Development and releases

Source code for both addons lives in `plugin_video_tvheadend/` and
`plugin_video_e2m3u2bouquet/`.

### Adding new localisation strings

After adding a `_('...')` call in Python code or a new label in
`settings.xml`:

```bash
./rebuild_lang.sh plugin_video_e2m3u2bouquet
```

The script updates `resources/language/cs.po` and `sk.po` with any new
strings. Open the `.po` file in [POEditor](https://poeditor.com/) (or
`poedit`) and translate the empty `msgstr` entries.

### Cutting a new release

After editing code and/or translations:

```bash
# Bump version in addon.xml + add an entry to changelog.txt
vim plugin_video_e2m3u2bouquet/addon.xml      # version="0.1.4"
vim plugin_video_e2m3u2bouquet/changelog.txt  # 0.1.4 - changelog entry

# Build ZIP into repo/ + auto-update addons.xml + auto-commit
./make_release.py

# Push to GitHub
git push
```

ArchivCZSK auto-update detects the bumped version in `addons.xml` on
GitHub and offers the update to end users.

### Repository layout

```
.
├── README.md                                ← this page
├── README.sk.md                             ← Slovak version
├── README.cs.md                             ← Czech version
├── addons.xml                               ← manifest (auto-managed by make_release.py)
├── make_release.py                          ← release script
├── rebuild_lang.sh                          ← .po regenerator
├── addon_settings2pot.py                    ← helper that extracts strings from settings.xml
├── .gitignore
├── LICENSE                                  ← GPL-2.0 license text
├── repo/                                    ← release ZIPs (one sub-dir per addon)
│   ├── plugin.video.tvheadend/
│   │   └── plugin.video.tvheadend-0.72.0.zip
│   └── plugin.video.e2m3u2bouquet/
│       └── plugin.video.e2m3u2bouquet-0.2.1.zip
├── plugin_video_tvheadend/                  ← source: Tvheadend client
└── plugin_video_e2m3u2bouquet/              ← source: M3U to Bouquet
```

### Localisation — file layout

`.po` files live at the **top of the language directory**:

```
plugin_video_e2m3u2bouquet/
└── resources/
    └── language/
        ├── cs.po       ← Czech translations (committed)
        └── sk.po       ← Slovak translations (committed)
```

`.mo` files are **not committed** — `make_release.py` builds them via
`msgfmt` straight into the release ZIP at
`resources/language/<lang>/LC_MESSAGES/<addon_id>.mo`.

## Origin

This repository hosts addons that connect external IPTV sources
(Tvheadend server, M3U playlist) to Enigma2 through the ArchivCZSK
framework and auto-generate userbouquets so channels show up in the
Enigma2 channel list. Originally proposed as a PR to the official
[`archivczsk/archivczsk-doplnky`](https://github.com/archivczsk/archivczsk-doplnky)
repository — after review with the ArchivCZSK maintainer
([@skyjet18](https://github.com/skyjet18)) we agreed to keep them in
this separate repository so the release cycle is independent and users
can opt in by adding it in the ArchivCZSK GUI.

## License

Licensed under the GNU General Public License v2.0 — see the
[LICENSE](LICENSE) file for the full text. Same license as
ArchivCZSK.
