# -*- coding: utf-8 -*-
"""
Zdielane modul-level veci pre TvheadendContentProvider a jeho mixiny.

Vyclenene z provider.py (refaktor 0.80.0, krok 2) — konstanty, process-wide
stav (login cache, watchdog, fast-recovery, DVR cache, stampy) a male volne
pomocne funkcie. provider.py aj jednotlive mixiny si odtialto importuju to,
co potrebuju → ziadny cyklicky import a stav ostava skutocny singleton.

Bez zmeny spravania — iba presun definicii.

Kompatibilita: Python 2.7 + Python 3.x
"""

from __future__ import absolute_import, unicode_literals, print_function

import os
import time
from datetime import datetime

from tools_archivczsk.string_utils import strip_accents
from ._paths import data_path


# FIX 0.48: predtým ukazoval na /tmp/archivczsk_poster (cache iného doplnku);
# plugin reálne píše svoje obrázky do /tmp/archivczsk_tvheadend_img, takže
# cleanup roky nič nemazal a /tmp na boxoch s nonstop behom donekonečna rástol.
# FIX 0.48j: cache obrázkov ostáva v /tmp (regenerovateľné dáta).
_POSTER_CACHE_DIR   = "/tmp/archivczsk_tvheadend_img"
_POSTER_CLEAN_STAMP = data_path("tvh_poster_clean.stamp")
_POSTER_TTL_DAYS    = 7

# Export bouquet/EPG sa nespúšťa pri každom silent login-e (napr. z HTTP handlera)
_EXPORT_TRIGGER_STAMP   = data_path("tvh_exports_trigger.stamp")
_EXPORT_TRIGGER_TTL_SEC = 1800  # 30 min

# DVR entries cache – používame tools_archivczsk ExpiringLRUCache.
# FIX 0.57.0 (skyjet PR #22 review): tools.archivczsk je guaranteed dependency
# (addon.xml require version 3.4+), žiadny fallback netreba.
from tools_archivczsk.cache import ExpiringLRUCache as _ExpiringLRUCache
_DVR_CACHE = _ExpiringLRUCache(1, default_timeout=600)

# Bouquet auto-refresh stamp
_BOUQUET_REFRESH_STAMP = data_path("tvh_bouquet_refresh.stamp")

# FIX 0.58.2: `_EPG_INJECT_STAMP` constant odstránený — framework
# trigger-uje EPG inject automaticky cez bgservice loop.




# FIX 0.48: TTL cache pre _check_tvh_silent() — zabraňuje N×/sec HTTP requestom
# na /api/serverinfo počas navigácie v menu.
# FIX 0.48h: asymetrické TTL — pozitívny check 30s (znižuje GUI lag),
# negatívny len 5s (rýchla recovery po TVH transient failure). Predtým
# spoločné 30s znamenalo že keď TVH zlyhal na 1 request, ďalších 30s
# plugin tvrdil že je offline aj keď sa medzitým obnovil. To je presne
# čo užívatelia zažili v logu — TVH bol späť za pár sekúnd, ale plugin
# 30s ďalej hlásil "not configured".
# 'reason' tracking: rozlíšenie 'not_configured' (chýbajú credentials)
# vs 'unreachable' (sú vyplnené ale API call zlyhal) → root() ukáže
# odlišnú chybovú hlášku.
_TVH_LOGIN_CACHE_TTL_OK   = 30
_TVH_LOGIN_CACHE_TTL_FAIL = 5
_TVH_LOGIN_CACHE = {'ts': 0, 'ok': False, 'reason': None, 'last_error': ''}

# FIX 0.48: globálny stav watchdog timera — drží referenciu, aby ho GC nezahodil
_WATCHDOG_STATE = {'timer': None, 'last_state': None, 'started': False}
_WATCHDOG_INTERVAL_MS = 5 * 60 * 1000  # 5 minút

# FIX 0.48i: fast-recovery poll state. Keď _check_tvh_silent detekuje
# zlyhanie, spustí sa background thread ktorý každých 10 sekúnd skúša
# TVH check (max 30 pokusov = 5 minút). Keď TVH naskočí, cache sa
# silently obnoví na ok=True — ďalšia užívateľská navigácia uvidí
# fungujúci plugin bez ručného retry.
# FIX 0.50beta: pridaný _FAST_RECOVERY_LOCK na ochranu pred race
# condition keď 2+ threads (napr. watchdog tick + user navigation)
# zavolajú _maybe_start_fast_recovery_poll súčasne — predtým mohli
# obaja prejsť `if not running` checkom a spustiť 2 paralelné poll
# loops. V praxi vzácne (5min watchdog vs user interakcia), ale
# stand-alone test scenárov to vyrobí.
import threading as _threading_for_state
_FAST_RECOVERY_STATE = {'running': False, 'thread': None}
_FAST_RECOVERY_LOCK = _threading_for_state.Lock()
_FAST_RECOVERY_INTERVAL_SEC = 10
_FAST_RECOVERY_MAX_ATTEMPTS = 30   # 30 × 10s = 5 minút


# --------------------------------------------------------------------------
# Pomocné funkcie
# --------------------------------------------------------------------------

def _maybe_cleanup_poster_cache():
	"""Čistí starý poster cache – max raz za _POSTER_TTL_DAYS dní.

	FIX 0.48: prísnejšia logika
	  - maže LEN súbory s prefixom 'imagecache_' (plugin-ove ikony),
	    nie iné súbory ktoré tam môžu byť (.stamp, .lock atď.)
	  - vynechá súbory čerstvejšie ako TTL (predtým mazalo všetko)
	  - vynechá '.tmp' rozpracované downloads, aby sa nepokazil prebiehajúci picon worker
	"""
	try:
		if not os.path.isdir(_POSTER_CACHE_DIR):
			return
		now = int(time.time())
		ttl = int(_POSTER_TTL_DAYS) * 24 * 3600
		last = 0
		try:
			last = int(os.path.getmtime(_POSTER_CLEAN_STAMP))
		except Exception:
			pass
		if last and (now - last) < ttl:
			return
		removed = 0
		for fn in os.listdir(_POSTER_CACHE_DIR):
			# Bezpečnostné filtre: maž len skutočne svoje cached ikony
			if not fn.startswith('imagecache_'):
				continue
			if fn.endswith('.tmp'):
				continue  # rozpracovaný download
			fp = os.path.join(_POSTER_CACHE_DIR, fn)
			try:
				if os.path.isfile(fp) and (now - int(os.path.getmtime(fp))) >= ttl:
					os.remove(fp)
					removed += 1
			except Exception:
				pass
		try:
			with open(_POSTER_CLEAN_STAMP, 'w') as f:
				f.write(str(now))
		except Exception:
			pass
		# Pôvodne tu bol log call cez `self.log_info(...)`, ale táto funkcia
		# je module-level (nie metóda triedy), takže `self` neexistovalo a
		# riadok vždy hodil NameError zachytený try/except — log sa nikdy
		# nezapísal. Cleanup beží správne, len bez log oznámenia.
	except Exception:
		pass


def _get_dvr_finished_cached(tvh):
	"""Vráti DVR nahrávky. Cachuje len NEPRÁZDNY výsledok (TTL podľa
	_DVR_CACHE = 10 min). Po vypršaní sa pri ďalšom otvorení natiahne
	čerstvý DVR vrátane nových nahrávok. Prázdny výsledok sa necachuje
	(nech sa skúsi znova kým prefetch dobehne)."""
	try:
		if _DVR_CACHE is not None:
			cached = _DVR_CACHE.get('dvr')
			if cached:
				return cached
	except Exception:
		pass
	try:
		result = tvh.get_dvr_finished()
	except Exception:
		return []
	try:
		if _DVR_CACHE is not None and result:
			_DVR_CACHE.put('dvr', result)
	except Exception:
		pass
	return result or []


# ============================================================================
# FIX 0.49 (+revízia 0.49b): DVR klasifikácia s podžánrami a viacúrovňovou
# heuristikou.
# ============================================================================
# Cieľ: namiesto vŕtania sa cez kanál → dátum → záznam ponúknuť žánrovú
# navigáciu. Klasifikácia sa robí na klientovi z polí ktoré TVH posiela
# v DVR entries.
#
# Aplikované signály (v poradí priority):
#   1) Channel-based hint   — názov kanála (CT :D = deti, Sport = šport, ...)
#                              prevažuje nad content_type lebo kanálové
#                              značky sú spoľahlivejšie ako EIT meta.
#   2) Series detection     — "X/Y" v subtitle (25/31), "(N)" sufix v title
#                              kde N nie je rok (Otec Brown IV (1)), alebo
#                              keyword "seriál"/"díl"/"epizoda" v popise.
#   3) Content_type fixed   — DVB EIT main category (top nibble genre byte):
#                              ct=2→News, ct=4→Sport, ct=5→Deti, atď.
#   4) Keyword fallback     — pre ct=0/11 (undefined) prejde popis + title
#                              cez Czech/Slovak žánrové keywords.
#
# Sub-žánre (len pre Filmy + Seriály):
#   - DVB genre low nibble (keď je dostupný) — primary signal
#   - Keyword scan v description + title       — secondary signal
#   - 'Iné' ak žiadny nezmatchol               — fallback
#
# Cache: rovnaké 60s TTL ako DVR cache.

# FIX 0.57.0 (skyjet PR #22 review #4): celá klasifikačná logika
# (~1390 LoC: kategórie, sub-kategórie, regex patterns, channel hints,
# keyword fallback, IMDb integration) presunutá do samostatného modulu
# classifier.py. Provider.py teraz importuje public API namiesto in-line
# definícií.



def _norm_name(s):
	# FIX 0.57.0 (skyjet PR #22 review #13): používa tools_archivczsk
	# `strip_accents` priamo, namiesto custom fallback wrapper-u.
	if not s:
		return ''
	return strip_accents(s).lower()


def _ts(e):
	try:
		return int(e.get('start_real') or e.get('start') or 0)
	except Exception:
		return 0


def _date_key_from_ts(ts):
	return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d')


def _tag_sort_key(tag):
	for k in ('index', 'sort_index', 'sortIndex', 'order', 'num'):
		v = tag.get(k)
		if v is None:
			continue
		try:
			return int(v)
		except Exception:
			pass
	return 999999


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------
