[English](README.md) | [Slovenčina](README.sk.md) | [Čeština](README.cs.md)

# archivczsk-iptvexternal

Vlastný repozitár doplnkov pre [ArchivCZSK](https://github.com/archivczsk/archivczsk)
Enigma2 plugin.

Pridáva podporu externých IPTV zdrojov (Tvheadend server, M3U playlist)
generovaním Enigma2 userbouquetov a injektovaním EPG, takže kanály sa
zobrazia natívne v zozname kanálov Enigma2.

## Doplnky v tomto repe

### 🎬 plugin.video.tvheadend — Tvheadend klient

Klient pre [Tvheadend](https://tvheadend.org/) server cez ArchivCZSK
framework:

- Prehliadanie Tvheadend kanálov (živá TV + rádio) priamo z ArchivCZSK
- Dva režimy pripojenia: **HTTP API** (port 9981) alebo natívny protokol
  **HTSP** (port 9982) — voliteľné v nastaveniach. HTSP načíta všetky dáta
  (kanály, EPG, DVR, tagy) cez jedno spojenie; streamovanie ide vždy priamo
  cez Tvheadend HTTP endpoint (9981)
- Prehrávanie DVR archívu s vyhľadávaním podľa názvu a žánrovou kategorizáciou
- Sťahovanie piconov cez Tvheadend `imagecache` endpoint
- Auto-generovaný Enigma2 userbouquet pre TV + Rádio
- Priama EPG injekcia do Enigma2 `eEPGCache` (bez potreby epgimport pluginu)
- Natívny DVB prehrávač (OE≥2.5) pre zapovanie v userbouquete aj prehrávanie
  priamo v doplnku (live + DVR) — prehráva cez hardvérový demux prijímača, čím
  zachová natívne DVB titulky/teletext; vyžaduje na Tvheadend serveri povolenú
  plain/basic autentifikáciu (Authentication type = „Both plain and digest")
- Lokalizácia: 🇸🇰 slovenčina, 🇨🇿 čeština, 🇬🇧 angličtina

### 📡 plugin.video.e2m3u2bouquet — E2m3u2bouquet

Konvertuje M3U playlist do Enigma2 userbouquetu — funguje s akýmkoľvek
M3U zdrojom:

- Stiahnutie a parsovanie M3U playlistu (HTTP, HTTPS, gzip)
- Generuje `userbouquet.m3u_iptv.tv` (+ `.radio` pre rozhlasové stanice)
  s tvg-id mapovaním
- XMLTV EPG injekcia (vlastný feed alebo TVH XMLTV endpoint)
- Sťahovanie piconov z `tvg-logo` URL
- XML pre custom triedenie a premenovanie skupín
- Voliteľná Tvheadend integrácia (obohatenie kanálov cez TVH API)
- Auto-refresh playlistu a EPG na nastaviteľné intervaly (4h, 8h, 24h, 2d, 7d)

## Inštalácia v ArchivCZSK

V ArchivCZSK GUI prejdi na správu repozitárov a pridaj URL tohto repa.
Podrobný postup sa doplní keď bude funkcia oficiálne dostupná v ArchivCZSK.

## Vývoj a vydávanie nových verzií

Source code oboch doplnkov je v adresároch `plugin_video_tvheadend/` a
`plugin_video_e2m3u2bouquet/`.

### Pridanie nových lokalizačných stringov

Po pridaní volaní `_('...')` v Python kóde alebo nových labelov v
`settings.xml`:

```bash
./rebuild_lang.sh plugin_video_e2m3u2bouquet
```

Skript aktualizuje `resources/language/cs.po` a `sk.po` o nové stringy.
Otvor `.po` súbor v [POEditor](https://poeditor.com/) (alebo `poedit`)
a prelož prázdne `msgstr` záznamy.

### Vydanie novej verzie

Po úprave kódu a/alebo prekladov:

```bash
# Bump verziu v addon.xml + pridaj záznam do changelog.txt
vim plugin_video_e2m3u2bouquet/addon.xml      # version="0.1.4"
vim plugin_video_e2m3u2bouquet/changelog.txt  # 0.1.4 - changelog entry

# Build ZIP do repo/ + auto-update addons.xml + auto-commit
./make_release.py

# Push na GitHub
git push
```

ArchivCZSK auto-update detekuje vyššiu verziu v `addons.xml` na GitHub-e
a ponúkne update koncovým používateľom.

### Štruktúra repa

```
.
├── README.md                                ← anglická verzia
├── README.sk.md                             ← táto stránka (slovenská)
├── README.cs.md                             ← česká verzia
├── addons.xml                               ← manifest (auto-managed by make_release.py)
├── make_release.py                          ← release script
├── rebuild_lang.sh                          ← .po regenerátor
├── addon_settings2pot.py                    ← helper pre extrakciu stringov zo settings.xml
├── .gitignore
├── LICENSE                                  ← GPL-2.0 licenčný text
├── repo/                                    ← release ZIP-y (jeden sub-dir per addon)
│   ├── plugin.video.tvheadend/
│   │   └── plugin.video.tvheadend-0.72.0.zip
│   └── plugin.video.e2m3u2bouquet/
│       └── plugin.video.e2m3u2bouquet-0.2.1.zip
├── plugin_video_tvheadend/                  ← source: Tvheadend klient
└── plugin_video_e2m3u2bouquet/              ← source: M3U to Bouquet
```

### Lokalizácia — file layout

`.po` súbory sa nachádzajú **na top-leveli** language adresára:

```
plugin_video_e2m3u2bouquet/
└── resources/
    └── language/
        ├── cs.po       ← české preklady (commitované)
        └── sk.po       ← slovenské preklady (commitované)
```

`.mo` súbory **nie sú commitované** — `make_release.py` ich vytvorí cez
`msgfmt` priamo do release ZIP-u na
`resources/language/<lang>/LC_MESSAGES/<addon_id>.mo`.

## Pôvod

Tento repozitár obsahuje doplnky ktoré napájajú externé IPTV zdroje
(Tvheadend server, M3U playlist) do Enigma2 cez ArchivCZSK framework
a automaticky generujú userbouquety pre prehliadanie v Enigma2 channel
liste. Pôvodne navrhnuté ako PR do oficiálneho
[`archivczsk/archivczsk-doplnky`](https://github.com/archivczsk/archivczsk-doplnky)
repa — po review-i sme sa s autorom ArchivCZSK
([@skyjet18](https://github.com/skyjet18)) dohodli že pluginy budú
udržiavané v tomto samostatnom repe, takže release cyklus si robíme
sami a používatelia si ho pridajú do ArchivCZSK GUI len ak chcú.

## Licencia

Pod licenciou GNU General Public License v2.0 — plný text v súbore
[LICENSE](LICENSE). Rovnaká licencia ako ArchivCZSK.
