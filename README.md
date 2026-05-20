# archivczsk-iptvexternal

Vlastný repozitár doplnkov pre [ArchivCZSK](https://github.com/archivczsk/archivczsk)
Enigma2 plugin.

Obsahuje doplnky **Tvheadend client** a **E2m3u2bouquet** ktoré rozšíria
ArchivCZSK o sledovanie živej TV, rádia a IPTV M3U playlistov.

## Doplnky v tomto repe

### 🎬 plugin.video.tvheadend — Tvheadend client

Klient pre [Tvheadend](https://tvheadend.org/) server cez ArchivCZSK framework:

- Browse Tvheadend channels (live TV + radio) priamo z ArchivCZSK
- DVR archive playback s vyhľadávaním a žánrovou kategorizáciou
- Picon download cez Tvheadend `imagecache`
- Auto-generated Enigma2 userbouquet pre TV + Radio
- Direct EPG injection do Enigma2 `eEPGCache`
- Lokalizácia: 🇸🇰 slovenčina, 🇨🇿 čeština, 🇬🇧 angličtina

### 📡 plugin.video.e2m3u2bouquet — E2m3u2bouquet

Konvertor M3U playlistov do Enigma2 bouquetu — pre akýkoľvek M3U zdroj:

- M3U playlist download + parse (HTTP, HTTPS, gzip)
- Generovanie `userbouquet.m3u_iptv.tv` (+ `.radio` pre rádiá) s tvg-id mapping
- XMLTV EPG injection (vlastný feed alebo TVH XMLTV endpoint)
- Picon download z `tvg-logo` URL
- Channel mapping override XML pre custom sort/group naming
- Optional Tvheadend integration (channel enrichment z TVH API)
- Auto-refresh playlistu a EPG na intervaly (4h, 8h, 24h, 2d, 7d)

## Inštalácia v ArchivCZSK

V ArchivCZSK GUI prejdi na správu repozitárov a pridaj URL tohto repa.
Detaily sa pridajú keď bude funkcia oficiálne dostupná v ArchivCZSK.

## Vývoj a nové verzie

Source code oboch doplnkov je v `plugin_video_tvheadend/` a
`plugin_video_e2m3u2bouquet/` adresároch.

### Pridanie nových stringov na lokalizáciu

Po pridaní `_('...')` volaní v Python kóde alebo nových labelov v
`settings.xml`:

```bash
./rebuild_lang.sh plugin_video_e2m3u2bouquet
```

Skript zaktualizuje `resources/language/cs.po` + `sk.po` o nové stringy.
Otvor `.po` súbor v [POEditor](https://poeditor.com/) (alebo `poedit`) a
prelož msgid → msgstr.

### Vydanie novej verzie

Po úprave kódu a/alebo prekladov:

```bash
# Bump verziu v addon.xml + pridaj entry do changelog.txt
vim plugin_video_e2m3u2bouquet/addon.xml      # version="0.1.4"
vim plugin_video_e2m3u2bouquet/changelog.txt  # 0.1.4 - changelog entry

# Build ZIP do repo/ + auto-update addons.xml + auto-commit
./make_release.py

# Push na GitHub
git push
```

ArchivCZSK auto-update detekuje vyššiu verziu v `addons.xml` na GitHub-e
a ponúkne update užívateľovi.

### Štruktúra repa

```
.
├── README.md                                ← táto stránka
├── addons.xml                               ← manifest (auto-managed by make_release.py)
├── make_release.py                          ← release script
├── rebuild_lang.sh                          ← .po regenerator
├── addon_settings2pot.py                    ← helper pre extrakciu strings zo settings.xml
├── .gitignore
├── repo/                                    ← release ZIP-y (jedno sub-dir per addon)
│   ├── plugin.video.tvheadend/
│   │   └── plugin.video.tvheadend-0.58.2.zip
│   └── plugin.video.e2m3u2bouquet/
│       └── plugin.video.e2m3u2bouquet-0.1.3.zip
├── plugin_video_tvheadend/                  ← source: Tvheadend client
└── plugin_video_e2m3u2bouquet/              ← source: M3U to Bouquet
```

### Lokalizácia — file layout

`.po` súbory sa nachádzajú na **top-level**:

```
plugin_video_e2m3u2bouquet/
└── resources/
    └── language/
        ├── cs.po       ← Czech translations (committed in git)
        └── sk.po       ← Slovak translations (committed in git)
```

`.mo` súbory **nie sú v git-e** — `make_release.py` ich vyrobí cez
`msgfmt` priamo do ZIP-u pri každom release-i, do
`resources/language/<lang>/LC_MESSAGES/<addon_id>.mo`.

## Pôvod

Tento repozitár obsahuje doplnky ktoré napájajú externé IPTV zdroje
(Tvheadend server, M3U playlist) do Enigma2 cez ArchivCZSK framework
a automaticky generujú userbouquety pre prehliadanie v Enigma2 channel
liste. Pôvodne navrhnuté ako PR do oficiálneho
[`archivczsk/archivczsk-doplnky`](https://github.com/archivczsk/archivczsk-doplnky)
repa, po review-i sa s autorom ArchivCZSK
([@skyjet18](https://github.com/skyjet18)) dohodlo že pluginy budú
udržiavané v tomto samostatnom repe — release cyklus si robíme sami
a používatelia si ho pridajú do ArchivCZSK GUI len ak chcú.

## Licencia

GPL-2.0 (rovnaká ako ArchivCZSK)
