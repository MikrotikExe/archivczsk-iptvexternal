# -*- coding: utf-8 -*-
"""
Watched-history storage pre TvheadendContentProvider.

Vyčlenené z provider.py (refaktor 0.73.0, krok 1/6) — čisté modul-level
funkcie bez závislosti na provider inštancii. Sleduje otvorené DVR nahrávky,
resume pozíciu a "fully watched" marker. Stav je persistent JSON v data dir-u.

Kompatibilita: Python 2.7 + Python 3.x
"""

from __future__ import absolute_import, unicode_literals, print_function

import os
import time

from ._paths import data_path

# FIX 0.52beta: persistent JSON storage pre "posledné sledované" history.
# FIX 0.55beta: rozšírené o resume position + duration tracking (sosáč-style).
# Plugin sleduje ktoré DVR entries užívateľ otvoril, kedy, a kde skončil
# v prehrávaní — aby mohol pokračovať od poslednej pozície a zobraziť
# hviezdičkový marker pri už pozretých nahrávkach v archive listing-u.
#
# Štruktúra (0.55beta forward-compatible upgrade z 0.52beta):
#   {'<entry_uuid>': {
#       'ts':          <epoch>,    # kedy bola entry naposledy otvorená
#       'title':       '...',
#       'subtitle':    '...',
#       'channelname': '...',
#       'start_real':  <epoch>,
#       'position':    <int seconds>,  # 0.55beta: kde sa playback zastavil
#       'duration':    <int seconds>,  # 0.55beta: celková dĺžka
#     }, ...}
#
# Pre entries z 0.52-0.54beta (bez position/duration) sa polia doplnia
# pri prvom save a chýbajúce keys vrátia None — žiadne dáta sa nezahodia,
# žiadny migration step required.
#
# Limit: 50 najnovších entries (FIFO discard). Pri reštarte E2 stav prežíva.
_WATCHED_HISTORY_PATH = data_path("watched_history.json")
_WATCHED_HISTORY_MAX = 50

# 0.55beta thresholds (sosáč-style):
#   _WATCHED_MARK_PCT — pri ≥80 % pozretia zobraz hviezdičku v listingu
#   _WATCHED_AUTO_CLEAR_DEFAULT_PCT — pri ≥95 % auto-clear position (film
#     dohraný, pri ďalšom play sa pustí od začiatku, ale marker zostane)
_WATCHED_MARK_PCT = 80
_WATCHED_AUTO_CLEAR_DEFAULT_PCT = 95

def _load_watched_history():
	"""Načítaj JSON s watched history. Pri chybe vráti prázdny dict."""
	try:
		import json
		if not os.path.isfile(_WATCHED_HISTORY_PATH):
			return {}
		with open(_WATCHED_HISTORY_PATH, 'r') as f:
			data = json.load(f)
		if not isinstance(data, dict):
			return {}
		return data
	except Exception:
		return {}


def _save_watched_history(history):
	"""Atomic write JSON s watched history. Silent fail (TVH plugin nemá
	hlásiť chyby pri tracking-u — to je nepriamy feature)."""
	try:
		import json
		tmp = _WATCHED_HISTORY_PATH + '.tmp'
		with open(tmp, 'w') as f:
			json.dump(history, f, ensure_ascii=False)
		if hasattr(os, 'replace'):
			os.replace(tmp, _WATCHED_HISTORY_PATH)
		else:
			if os.path.exists(_WATCHED_HISTORY_PATH):
				os.remove(_WATCHED_HISTORY_PATH)
			os.rename(tmp, _WATCHED_HISTORY_PATH)
	except Exception:
		pass


def _track_watched(entry):
	"""FIX 0.52beta: zapíš DVR entry do watched history. Volaná z
	play_dvr() pri každom otvorení nahrávky.

	Idempotent — ak entry už v history, len aktualizuje timestamp (a
	zachová existujúce position/duration ak boli zaznamenané pri
	predošlom skončení playback-u). Limit 50 najnovších; staršie sa
	odstránia pri save keď dict prekročí limit.
	"""
	uuid = entry.get('uuid')
	if not uuid:
		return
	try:
		history = _load_watched_history()
		# Forward-compatible: zachovaj existujúce position/duration ak sú
		existing = history.get(uuid, {}) if isinstance(history.get(uuid), dict) else {}
		history[uuid] = {
			'ts': int(time.time()),
			'title': entry.get('disp_title') or '',
			'subtitle': entry.get('disp_subtitle') or '',
			'channelname': entry.get('channelname') or '',
			'start_real': entry.get('start_real') or 0,
			# 0.55beta — zachovaj predošlú resume pozíciu, ak nie je
			# žiadna tak None
			'position': existing.get('position'),
			'duration': existing.get('duration'),
		}
		# Trim na _WATCHED_HISTORY_MAX najnovších
		if len(history) > _WATCHED_HISTORY_MAX:
			sorted_items = sorted(history.items(),
			                      key=lambda kv: kv[1].get('ts', 0),
			                      reverse=True)
			history = dict(sorted_items[:_WATCHED_HISTORY_MAX])
		_save_watched_history(history)
	except Exception:
		pass


def _get_watched_position(uuid):
	"""FIX 0.55beta: vráti (position, duration) pre entry, alebo
	(None, None) ak entry nie je v history alebo nemá zaznamenanú
	pozíciu. Position a duration sú v sekundách (int) alebo None.
	"""
	if not uuid:
		return (None, None)
	try:
		history = _load_watched_history()
		rec = history.get(uuid)
		if not isinstance(rec, dict):
			return (None, None)
		return (rec.get('position'), rec.get('duration'))
	except Exception:
		return (None, None)


def _set_watched_position(entry, position, duration):
	"""FIX 0.55beta: zapíš resume position pre entry. Volaná z stats()
	handler-a pri end/next playback eventu.

	Auto-clear semantika: ak position prekročí _WATCHED_AUTO_CLEAR_DEFAULT_PCT
	z duration, position sa vynuluje (film dohraný, nemá zmysel resume-ovať
	posledné 5 % titulkov/credits). Marker v listingu zostane lebo ide nad
	_WATCHED_MARK_PCT threshold.

	Position-only persist (žiadny title/subtitle update) — to robí
	_track_watched pri play_dvr() otvorení.
	"""
	uuid = entry.get('uuid') if isinstance(entry, dict) else None
	if not uuid:
		return
	try:
		pos = int(position) if position else 0
		dur = int(duration) if duration else 0
	except (TypeError, ValueError):
		return
	try:
		history = _load_watched_history()
		rec = history.get(uuid)
		if not isinstance(rec, dict):
			# Entry nebola v history — vytvor minimálny záznam aby sa pri
			# ďalšom play vedelo, že existuje resume pozícia.
			rec = {
				'ts': int(time.time()),
				'title': entry.get('disp_title') or '',
				'subtitle': entry.get('disp_subtitle') or '',
				'channelname': entry.get('channelname') or '',
				'start_real': entry.get('start_real') or 0,
			}
			history[uuid] = rec
		# Auto-clear ak >= 95 % (film dohraný) — pozícia 0, ale duration
		# si zachováme aby _is_fully_watched mohla vrátiť True a marker
		# v listingu sa zobrazil.
		if dur > 0 and pos >= (dur * _WATCHED_AUTO_CLEAR_DEFAULT_PCT) // 100:
			rec['position'] = 0
		else:
			rec['position'] = pos
		rec['duration'] = dur if dur > 0 else rec.get('duration')
		# Aktualizuj timestamp aby entry vyplávala v recently_watched
		rec['ts'] = int(time.time())
		_save_watched_history(history)
	except Exception:
		pass


def _is_fully_watched(uuid):
	"""FIX 0.55beta: vráti True ak entry má position alebo duration
	naznačujúcu že bola pozretá nad _WATCHED_MARK_PCT (default 80 %)
	hranicu — používa sa pre hviezdičkový marker v listingu.

	Špeciál: ak position == 0 ale duration > 0 (auto-cleared after 95 %),
	entry je tiež považovaná za pozretú (marker sa zobrazí).
	"""
	if not uuid:
		return False
	try:
		history = _load_watched_history()
		rec = history.get(uuid)
		if not isinstance(rec, dict):
			return False
		pos = rec.get('position')
		dur = rec.get('duration')
		if not dur or dur <= 0:
			return False
		# Auto-cleared (95 %+) → marker show
		if pos == 0 and rec.get('ts'):
			# Heuristika: position==0 a record existuje s duration → bola
			# auto-cleared (a teda dosiahla 95 %+). True positive.
			# (Pred 0.55beta entries nemajú duration, dostane sa sem
			# len keď bol playback zaznamenaný v 0.55beta+ formáte.)
			return True
		if pos is None:
			return False
		return pos >= (dur * _WATCHED_MARK_PCT) // 100
	except Exception:
		return False
