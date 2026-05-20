[English](README.md) | [Slovenčina](README.sk.md) | [Čeština](README.cs.md)

# archivczsk-iptvexternal

Vlastní repozitář doplňků pro [ArchivCZSK](https://github.com/archivczsk/archivczsk)
Enigma2 plugin.

Přidává podporu externích IPTV zdrojů (Tvheadend server, M3U playlist)
generováním Enigma2 userbouquetů a injektováním EPG, takže kanály se
zobrazí nativně v seznamu kanálů Enigma2.

## Doplňky v tomto repu

### 🎬 plugin.video.tvheadend — Tvheadend klient

Klient pro [Tvheadend](https://tvheadend.org/) server přes ArchivCZSK
framework:

- Procházení Tvheadend kanálů (živá TV + rádio) přímo z ArchivCZSK
- Přehrávání DVR archivu s vyhledáváním podle názvu a žánrovou kategorizací
- Stahování piconů přes Tvheadend `imagecache` endpoint
- Auto-generovaný Enigma2 userbouquet pro TV + Rádio
- Přímá EPG injekce do Enigma2 `eEPGCache` (bez potřeby epgimport pluginu)
- Lokalizace: 🇸🇰 slovenština, 🇨🇿 čeština, 🇬🇧 angličtina

### 📡 plugin.video.e2m3u2bouquet — E2m3u2bouquet

Konvertuje M3U playlist do Enigma2 userbouquetu — funguje s jakýmkoliv
M3U zdrojem:

- Stažení a parsování M3U playlistu (HTTP, HTTPS, gzip)
- Generuje `userbouquet.m3u_iptv.tv` (+ `.radio` pro rozhlasové stanice)
  s tvg-id mapováním
- XMLTV EPG injekce (vlastní feed nebo TVH XMLTV endpoint)
- Stahování piconů z `tvg-logo` URL
- XML pro custom třídění a přejmenování skupin
- Volitelná Tvheadend integrace (obohacení kanálů přes TVH API)
- Auto-refresh playlistu a EPG na nastavitelné intervaly (4h, 8h, 24h, 2d, 7d)

## Instalace v ArchivCZSK

V ArchivCZSK GUI přejdi na správu repozitářů a přidej URL tohoto repa.
Podrobný postup se doplní, jakmile bude funkce oficiálně dostupná v ArchivCZSK.

## Vývoj a vydávání nových verzí

Source code obou doplňků je v adresářích `plugin_video_tvheadend/` a
`plugin_video_e2m3u2bouquet/`.

### Přidání nových lokalizačních stringů

Po přidání volání `_('...')` v Python kódu nebo nových labelů v
`settings.xml`:

```bash
./rebuild_lang.sh plugin_video_e2m3u2bouquet
```

Skript aktualizuje `resources/language/cs.po` a `sk.po` o nové stringy.
Otevři `.po` soubor v [POEditor](https://poeditor.com/) (nebo `poedit`)
a přelož prázdné `msgstr` záznamy.

### Vydání nové verze

Po úpravě kódu a/nebo překladů:

```bash
# Bump verzi v addon.xml + přidej záznam do changelog.txt
vim plugin_video_e2m3u2bouquet/addon.xml      # version="0.1.4"
vim plugin_video_e2m3u2bouquet/changelog.txt  # 0.1.4 - changelog entry

# Build ZIP do repo/ + auto-update addons.xml + auto-commit
./make_release.py

# Push na GitHub
git push
```

ArchivCZSK auto-update detekuje vyšší verzi v `addons.xml` na GitHubu
a nabídne update koncovým uživatelům.

### Struktura repa

```
.
├── README.md                                ← anglická verze
├── README.sk.md                             ← slovenská verze
├── README.cs.md                             ← tato stránka (česká)
├── addons.xml                               ← manifest (auto-managed by make_release.py)
├── make_release.py                          ← release script
├── rebuild_lang.sh                          ← .po regenerátor
├── addon_settings2pot.py                    ← helper pro extrakci stringů ze settings.xml
├── .gitignore
├── repo/                                    ← release ZIPy (jeden sub-dir per addon)
│   ├── plugin.video.tvheadend/
│   │   └── plugin.video.tvheadend-0.58.4.zip
│   └── plugin.video.e2m3u2bouquet/
│       └── plugin.video.e2m3u2bouquet-0.1.3.zip
├── plugin_video_tvheadend/                  ← source: Tvheadend klient
└── plugin_video_e2m3u2bouquet/              ← source: M3U to Bouquet
```

### Lokalizace — file layout

`.po` soubory jsou **na top-levelu** language adresáře:

```
plugin_video_e2m3u2bouquet/
└── resources/
    └── language/
        ├── cs.po       ← české překlady (commitované)
        └── sk.po       ← slovenské překlady (commitované)
```

`.mo` soubory **nejsou commitované** — `make_release.py` je vytvoří přes
`msgfmt` přímo do release ZIPu na
`resources/language/<lang>/LC_MESSAGES/<addon_id>.mo`.

## Původ

Tento repozitář obsahuje doplňky které napájejí externí IPTV zdroje
(Tvheadend server, M3U playlist) do Enigma2 přes ArchivCZSK framework
a automaticky generují userbouquety pro procházení v Enigma2 channel
listu. Původně navrženo jako PR do oficiálního
[`archivczsk/archivczsk-doplnky`](https://github.com/archivczsk/archivczsk-doplnky)
repa — po review jsme se s autorem ArchivCZSK
([@skyjet18](https://github.com/skyjet18)) dohodli, že pluginy budou
udržovány v tomto samostatném repu, takže release cyklus si děláme
sami a uživatelé si ho přidají do ArchivCZSK GUI jen pokud chtějí.

## Licence

GPL-2.0 (stejná jako ArchivCZSK)
