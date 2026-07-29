# plugin_video_oktagontv

Doplnok archivczsk pre OKTAGON.tv (OKTAGON MMA – živé prenosy, záznamy, balíčky/PPV).

Architektúra bola zistená z HAR záznamu (28.7.2026) a je zapracovaná v kóde:

## Ako to funguje

Prihlásenie je 3-krokové (súbor `oktagontv.py`, metóda `login()`):

1. OKTAGON účet – Firebase projekt `oktagonprod`
   `accounts:signInWithPassword?key=AIzaSyDTDAMftECKq34nQn0F_6fGWIXui-SSl24`
   → OKTAGON idToken + userId
2. Premostenie do Tivio – cloud funkcia
   `europe-west3-tivio-production.cloudfunctions.net/signInWithTenant`  (`{userId, token}`)
   → Firebase custom token (tenant `XA6ZOtuDD90uHsRkXNyj-e2xvz`)
3. Výmena za Tivio idToken – Firebase projekt `tivio-production`
   `relyingparty/verifyCustomToken?key=AIzaSyB02udgMkNLADkLJ_w5YNBMR2VR1WHfusI`
   → Tivio idToken (obnovuje sa cez securetoken)

Katalóg je z vlastného verejného API OKTAGONu (súbor `oktagon_api.py`):
`https://api.oktagonmma.com/v1/banners?types[]=STREAM|VIDEO|BUNDLE&...`
Každá položka nesie `videoSourceType: "TIVIO"` a `videoSource: <Tivio video id>`.

Prehrávanie: `videoSource` sa pošle do Tivio `getSourceUrl` (s Tivio idTokenom) →
manifest (DASH/HLS) → prehrá sa cez HLS/DASH proxy.

Overené identifikátory (v `oktagontv.py`):
- ORGANIZATION_ID `ZA6ZOtuDD90uHsRkXNyj`
- tivioUserId `nAuteACH46DJqORmqXEW` (pre daný účet)

## Štruktúra webu (2. HAR, 28.7.2026 – prechod Turnaje → Zápasy → Pořady → Domov)

Web je Next.js SSG – súbory `/_next/data/.../sk/*.json` neobsahujú žiadny obsah
(iba preklady), všetko sa načítava z Firestore až v prehliadači. Zistené zdroje:

| stránka webu | zdroj dát |
|---|---|
| `/sk/` (Domov) | `screens` kde `screenId == screen-himhHzoaJZS4tEPP-5Fh-` → `rows` |
| `/sk/fights/` (Zápasy) | `screens` kde `screenId == screen-UUal-pc3NL1T9fwTiupn9` → `rows` → videá podľa tagu |
| `/sk/tournaments/` (Turnaje) | `tags` kde `tagTypeRef == organizations/<org>/tagTypes/JW171vftakPSYzzVl7LW`, `orderBy created DESC`, limit 21 |
| `/sk/shows/` (Pořady) | riadok `row-CBNuWIfjpCKTMflELreLN` (dlaždice typu TAG) |
| zápasy/epizódy tagu | `videos` kde `organizationRef` + `transcodingStatus == ENCODING_DONE` + `publishedStatus == PUBLISHED` + `tags ARRAY_CONTAINS_ANY [<tagRef>]`, `orderBy created DESC` |

Od verzie 0.5.0 doplnok posiela tie isté dopyty – dopyt na videá aj na turnaje je
1:1 zhodný s tým, čo posiela prehliadač (overené porovnaním vygenerovaného JSONu s HAR-om).

Ostatné zistenia z HAR-u: `me.oktagon.tv` je len analytika (page_view), stránka
`/banners` vracia iba nadchádzajúce turnaje (5 ks), položky majú `redirectRules`
(v DE/AT/CH/LU/LI web presmerúva na RTL+, v PL na TVP Sport).

## Stav (overené na prijímači Vu+ Uno4K SE, OpenATV, ArchivCZSK 3.7.0)

Funguje: prihlásenie, katalóg, prehrávanie (čisté DASH **bez Widevine DRM**, `tools_cenc`
netreba), YouTube promo videá cez `plugin.video.yt`, preložené chybové hlášky.

Menu:
- Vyhľadať – filtrovanie na strane doplnku (turnaje, relácie, bojovníci, najnovšie videá)
- Živé prenosy / turnaje – `/banners?types[0]=STREAM`
- Zápasy – „Najnovšie" (videá organizácie) + riadky obrazovky `/sk/fights`
- Turnaje – tagy typu `JW171vftakPSYzzVl7LW`, zoskupené do sérií
  (OKTAGON, PML, THE RING, …) → zápasy turnaja
- Relácie – riadok `row-CBNuWIfjpCKTMflELreLN` → epizódy
- Videá / záznamy – `/banners?types[0]=VIDEO`
- Balíčky / PPV – `/banners?types[0]=BUNDLE`

Typy tagov v Tiviu OKTAGONu: `show_tag`, `event_tag`, `genre_tag`, `video_type_tag`,
`organization_tag` + veľa tagov bez typu (bojovníci a pod.).

## Kompatibilita

Kód je písaný pre Python 2 aj 3 (staršie Enigma2 image) – bez f-stringov, anotácií
a `super()` bez argumentov; texty výnimiek idú cez `error_text()`, aby na py2
nepadali na diakritike.

## Čo ešte chýba

- Vyhľadávanie na strane servera (endpoint nebol odchytený) – doplnok od 0.5.1
  filtruje načítaný katalóg lokálne (turnaje, relácie, tagy bojovníkov, najnovšie videá).
- Obrazovka „Domov" (`screen-himhHzoaJZS4tEPP-5Fh-`) vracia z Tivia 0 riadkov,
  preto bola od 0.5.2 z menu odstránená (kód `list_screen` ostáva).
- Turnaje, ktoré ešte neprebehli, nemajú zápasy – zobrazí sa prázdny zoznam (očakávané).

## Inštalácia

Rozbaľ priečinok `plugin_video_oktagontv` do adresára doplnkov archivczsk na prijímači
alebo pridaj do vlastného repozitára a nainštaluj cez ArchivCZSK / Manažér doplnkov.
Do nastavení doplnku zadaj e-mail a heslo k OKTAGON.tv účtu.
