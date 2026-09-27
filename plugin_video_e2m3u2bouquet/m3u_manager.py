# -*- coding: utf-8 -*-
"""
M3URefreshManager - high-level orchestrator that runs the full
fetch -> parse -> apply mapping -> write bouquet -> picons -> epgimport
pipeline. Designed to be called from addon.py either on demand (menu)
or via an eTimer at a configured refresh interval.

Python 2.7 + Python 3.x compatible.
"""

from __future__ import absolute_import, unicode_literals, print_function

import os
import io
import sys
import gzip
import time
import threading

try:
	import lzma  # Py3
except ImportError:
	try:
		from backports import lzma
	except ImportError:
		lzma = None

try:
	from .m3u_provider import M3UProvider, mask_url
	from .m3u_bouquet import M3UBouquetWriter, cleanup_m3u_bouquet, \
		DEFAULT_BOUQUET_DIR, DEFAULT_PICON_DIR, DEFAULT_EPGIMPORT_DIR, \
		M3U_BOUQUET_PREFIX, \
		build_url_to_sref_from_bouquet  # FIX 0.48h
	from .m3u_mapping import M3UMappingOverride
	try:
		from .m3u_tvh_enricher import enrich_with_tvh, derive_tvh_xmltv_url, \
			looks_like_tvh_playlist
	except ImportError:
		enrich_with_tvh = None
		derive_tvh_xmltv_url = None
		looks_like_tvh_playlist = None
	try:
		from .m3u_epg_injector import inject_epg_into_enigma
	except ImportError:
		inject_epg_into_enigma = None
	try:
		from .m3u_tvh_auth import (parse_tvh_url, build_token_client_from_url,
		                            fetch_tvh_tags_via_url)
	except ImportError:
		parse_tvh_url = None
		build_token_client_from_url = None
		fetch_tvh_tags_via_url = None
except (ValueError, ImportError):
	# Standalone import (test or scripted use outside the plugin package)
	from m3u_provider import M3UProvider, mask_url
	from m3u_bouquet import M3UBouquetWriter, cleanup_m3u_bouquet, \
		DEFAULT_BOUQUET_DIR, DEFAULT_PICON_DIR, DEFAULT_EPGIMPORT_DIR, \
		M3U_BOUQUET_PREFIX, \
		build_url_to_sref_from_bouquet
	from m3u_mapping import M3UMappingOverride
	try:
		from m3u_tvh_enricher import enrich_with_tvh, derive_tvh_xmltv_url, \
			looks_like_tvh_playlist
	except ImportError:
		enrich_with_tvh = None
		derive_tvh_xmltv_url = None
		looks_like_tvh_playlist = None
	try:
		from m3u_epg_injector import inject_epg_into_enigma
	except ImportError:
		inject_epg_into_enigma = None
	try:
		from m3u_tvh_auth import (parse_tvh_url, build_token_client_from_url,
		                          fetch_tvh_tags_via_url)
	except ImportError:
		parse_tvh_url = None
		build_token_client_from_url = None
		fetch_tvh_tags_via_url = None


try:
	# FIX 0.48j: persistent data dir helper
	from ._paths import data_path
except (ValueError, ImportError):
	from _paths import data_path


_PY2 = sys.version_info[0] < 3

# FIX 0.48j: stampy v persistent data dir-u (nie v /tmp — prežijú reboot)
_STAMP_FILE = data_path('m3u_last_refresh.stamp')
# FIX 0.48g: separátny stamp pre EPG injection (analógia s
# _EPG_INJECT_STAMP v TVH bouquet.py). Zapisuje sa po každej úspešnej
# direct injection — či už cez refresh_now() alebo cez inject_epg_only().
_EPG_INJECT_STAMP_M3U = data_path('m3u_epg_inject.stamp')
_LOCK = threading.Lock()

# FIX 1.0.0 (J): po neúspešnom stiahnutí playlistu (box nabootoval skôr
# ako sieť) sa skúsi refresh znova o _RETRY_DELAY sekúnd, max
# _RETRY_MAX pokusov za sebou — potom už len periodický scheduler.
_RETRY_DELAY = 300
_RETRY_MAX = 3
# čas štartu tohto procesu — fallback pre _boot_time()
_PROCESS_START = time.time()


def _stamp_age(path):
	"""Vek stamp súboru v sekundách alebo None ak neexistuje."""
	try:
		return max(0, time.time() - os.path.getmtime(path))
	except Exception:
		return None


def _boot_time():
	"""Unix čas štartu boxu (/proc/uptime); bez neho čas štartu procesu.
	Použité pre refresh_due() pri intervale 0 = "raz po štarte"."""
	try:
		with open('/proc/uptime', 'r') as f:
			up = float(f.read().split()[0])
		return time.time() - up
	except Exception:
		return _PROCESS_START


class M3URefreshManager(object):
	"""
	Orchestrates an end-to-end refresh of the M3U source.

	settings dict expected keys (all values may also be str-typed):
	    enable_m3u_source        bool
	    m3u_url                  str
	    m3u_epg_url              str
	    m3u_service_type         str        '1' / '4097' / '5001' / '5002'
	    m3u_bouquet_name         str
	    m3u_refresh_interval     int        seconds (0 = manual only)
	    m3u_epg_inject_interval  int        seconds (0 = disabled)
	    m3u_picons_from_logo     bool
	    enable_radio_bouquet     bool
	    m3u_use_mapping          bool
	    m3u_mapping_file         str        path to override XML

	Cesty (bouquet dir, picon dir, epgimport dir) sú konštanty
	DEFAULT_* v m3u_bouquet.py — FIX 1.0.0 (M): settings
	`m3u_bouquet_dir`/`m3u_picon_dir`/`m3u_epgimport_dir` sa čítali, ale
	nikdy neboli definované v settings.xml.
	"""

	def __init__(self, settings_getter, log=None, tvh_client=None,
	             translate=None):
		"""
		settings_getter: callable(key, default=None) -> value
		  (so caller can pass any framework-specific accessor)
		log: print-like callable
		tvh_client: optional Tvheadend instance (from tvheadend.py).
		  If provided, M3U channels whose URLs point to TVH will be
		  enriched with TVH tags (for group-title) and UUIDs (for tvg-id).
		translate: callable(msgid) -> localised string (framework `_`)
		"""
		self._get = settings_getter
		self.log = log or (lambda *a, **k: None)
		self._tvh = tvh_client
		self._translate = translate
		self._timer = None          # periodický bouquet refresh
		self._epg_timer = None      # FIX 1.0.0 (G): periodický EPG inject
		self._stop = False
		# FIX 1.0.0 (J): hook pre odložený retry — provider sem dosadí
		# bgservice.run_delayed wrapper: callable(delay_seconds, fn)
		self.run_delayed = None
		self._retry_pending = False
		self._retry_count = 0
		# FIX 1.0.0 (B): ručná akcia "plný reset piconov" — flag spotrebuje
		# najbližší _do_refresh (wipe beží pod _LOCK, nie paralelne so
		# sťahovaním)
		self._picon_wipe_requested = False
		# FIX 1.0.0 (review): stavové príznaky okolo _LOCK (pod _flag_lock):
		#   _refresh_pending  — refresh čaká na zámok alebo beží (súbežné
		#                       ŽIADOSTI o refresh sa zlúčia do jednej)
		#   _refresh_again    — počas bežiaceho refreshu prišla žiadosť,
		#                       ktorá musí bežať (plný reset piconov) — po
		#                       skončení sa spustí ešte raz
		#   _cleanup_pending  — cleanup prišiel keď bol zámok obsadený;
		#                       vykoná ho držiteľ zámku po svojej práci
		self._flag_lock = threading.Lock()
		self._refresh_pending = False
		self._refresh_again = False
		self._cleanup_pending = False

	# ------------------ Helper accessors ------------------

	def _bool(self, key, default=False):
		v = self._get(key, default)
		if isinstance(v, bool):
			return v
		# FIX 1.0.0: na Py2 prichádza zo settings `unicode`, nie `str`
		if isinstance(v, (str, type(u''))):
			return v.strip().lower() in ('1', 'true', 'yes', 'on')
		return bool(v)

	def _str(self, key, default=''):
		v = self._get(key, default)
		return '' if v is None else str(v).strip()

	def _int(self, key, default=0):
		try:
			return int(self._get(key, default))
		except (TypeError, ValueError):
			return default

	def _bouquet_paths(self):
		tv = os.path.join(DEFAULT_BOUQUET_DIR,
		                  'userbouquet.{}.tv'.format(M3U_BOUQUET_PREFIX))
		radio = os.path.join(DEFAULT_BOUQUET_DIR,
		                     'userbouquet.{}.radio'.format(M3U_BOUQUET_PREFIX))
		return tv, radio

	# ------------------ Public API ------------------

	def is_enabled(self):
		return self._bool('enable_m3u_source', False)

	def can_run(self):
		return self.is_enabled() and bool(self._str('m3u_url'))

	def set_tvh_client(self, tvh_client):
		"""FIX 1.0.0: po zmene M3U URL sa token client odvodzuje nanovo."""
		self._tvh = tvh_client

	def refresh_due(self):
		"""FIX 1.0.0 (J): boot cooldown. True ak treba spustiť refresh:
		stamp chýba, bouquet súbory chýbajú, alebo stamp je starší ako
		m3u_refresh_interval. Predtým login() spúšťal refresh pri každom
		štarte bez ohľadu na to, kedy prebehol posledný.

		Interval 0 ("Disabled") = žiadny periodický refresh, ale RAZ PO
		KAŽDOM ŠTARTE boxu áno (ako v 0.2.1, kde sa refreshovalo pri
		každom štarte): due, keď je stamp starší ako čas štartu boxu."""
		if not self.can_run():
			return False
		age = _stamp_age(_STAMP_FILE)
		if age is None:
			return True
		tv, radio = self._bouquet_paths()
		if not (os.path.isfile(tv) or os.path.isfile(radio)):
			return True
		interval = self._int('m3u_refresh_interval', 0)
		if interval <= 0:
			try:
				return os.path.getmtime(_STAMP_FILE) < _boot_time()
			except Exception:
				return True
		return age >= interval

	def refresh_now(self, is_retry=False, follow_up=False):
		"""
		Run a refresh synchronously. Safe to call from a manager thread or
		eTimer callback.

		FIX 1.0.0 (review): čaká na _LOCK BLOKUJÚCO — keď zámok drží
		EPG-only inject (24h refresh a 4h tick sa raz za deň stretnú),
		refresh sa už nestratí na 24 h. Zlučujú sa len súbežné ŽIADOSTI
		o refresh (_refresh_pending): druhá vráti False; s follow_up=True
		(plný reset piconov) sa po dobehnutí aktuálneho refreshu spustí
		ešte raz. is_retry=False resetuje počítadlo odložených pokusov
		(timer/ručná akcia = nový začiatok).
		"""
		with self._flag_lock:
			if self._refresh_pending:
				if follow_up:
					self._refresh_again = True
					self.log('[M3U-mgr] refresh in progress — another one queued')
				else:
					self.log('[M3U-mgr] refresh already in progress, skipping')
				return False
			self._refresh_pending = True
			if not is_retry:
				self._retry_count = 0
		_LOCK.acquire()
		try:
			return self._do_refresh()
		finally:
			try:
				self._run_pending_cleanup()
			except Exception:
				pass
			try:
				_LOCK.release()
			except Exception:
				pass
			with self._flag_lock:
				self._refresh_pending = False
				again = self._refresh_again
				self._refresh_again = False
			if again:
				self.refresh_async(follow_up=True)

	def refresh_async(self, is_retry=False, follow_up=False):
		"""Fire-and-forget refresh on a background thread."""
		t = threading.Thread(target=self.refresh_now, name='M3URefresh',
		                     kwargs={'is_retry': is_retry, 'follow_up': follow_up})
		t.daemon = True
		t.start()
		return t

	def full_picon_refresh(self):
		"""FIX 1.0.0 (B): ručná akcia — zmaže VŠETKY picony doplnku (aj
		kategórií, ktoré už v playliste nie sú) a spustí refresh, ktorý ich
		stiahne nanovo. Wipe robí writer vnútri refreshu (pod _LOCK); ak
		práve beží iný refresh, spustí sa hneď po ňom (follow_up)."""
		if not self.can_run():
			return False
		self._picon_wipe_requested = True
		self.refresh_async(follow_up=True)
		return True

	def _cleanup_locked(self):
		"""Vlastný cleanup — volať LEN s držaným _LOCK."""
		# Timery zrušiť len keď je export vypnutý; pri ručnom "Remove M3U
		# bouquet" so zapnutým exportom periodický refresh beží ďalej
		# (FIX 1.0.0 review: predtým sa po ručnom cleanup-e už nikdy
		# neobnovil)
		if not self.can_run():
			self.cancel_all()
		try:
			# FIX 0.48f: prefix už nie je configurable setting — hardcoded
			stats = cleanup_m3u_bouquet(
				bouquet_prefix=M3U_BOUQUET_PREFIX,
				bouquet_dir=DEFAULT_BOUQUET_DIR,
				epgimport_dir=DEFAULT_EPGIMPORT_DIR,
				picon_dir=DEFAULT_PICON_DIR,
				log=self.log,
			)
			self.log('[M3U-mgr] cleanup done: %s' % stats)
			return stats
		except Exception as e:
			self.log('[M3U-mgr] cleanup failed: %s' % e)
			return None

	def _run_pending_cleanup(self):
		"""Držiteľ _LOCK (refresh / inject) vykoná cleanup, ktorý prišiel
		počas jeho práce."""
		with self._flag_lock:
			pending = self._cleanup_pending
			self._cleanup_pending = False
		if pending:
			self.log('[M3U-mgr] running deferred cleanup')
			self._cleanup_locked()

	def cleanup(self):
		"""
		Remove generated bouquet + epgimport files + picons. Called when
		user disables M3U source in settings or from the manual action.
		Idempotent: safe to call repeatedly.

		FIX 1.0.0 (I/review): beží pod _LOCK — inak mohol bežiaci refresh
		(writer.run) súbory znova vytvoriť hneď po ich zmazaní. NEČAKÁ
		(volá sa aj z GUI vlákna): ak je zámok obsadený, nastaví
		_cleanup_pending a cleanup vykoná držiteľ zámku hneď po svojej
		práci (refresh si navyše pred zápisom znova overí can_run()).
		Vtedy vráti {'deferred': True}.
		"""
		acquired = _LOCK.acquire(False)
		if not acquired:
			with self._flag_lock:
				self._cleanup_pending = True
			if not self.can_run():
				self.cancel_all()
			self.log('[M3U-mgr] cleanup: refresh/inject running — deferred '
			         'until it finishes')
			return {'deferred': True}
		try:
			with self._flag_lock:
				self._cleanup_pending = False
			return self._cleanup_locked()
		finally:
			try:
				_LOCK.release()
			except Exception:
				pass

	# ------------------ Core ------------------

	def _derive_epg_url(self, provider):
		"""Auto-derive EPG URL from TVH if user did not set one explicitly.

		FIX 1.0.0 (H): odvodí sa LEN keď parsed playlist naozaj vyzerá ako
		TVH (looks_like_tvh_playlist) — is_tvh_url matchuje akúkoľvek URL
		s /playlist/ /stream/ /api/, takže sa predtým /xmltv/channels
		skúšalo aj na cudzích IPTV serveroch. Token client teraz vracia
		URL aj s auth tokenom (_url_with_creds)."""
		epg_url = self._str('m3u_epg_url')
		if epg_url or not self._bool('m3u_enrich_from_tvh', True):
			return epg_url
		channels = provider.get_all_channels()
		if looks_like_tvh_playlist is not None and not looks_like_tvh_playlist(channels):
			return ''

		# Path 1: primary TVH client (full credentials / auth token)
		if self._tvh is not None and derive_tvh_xmltv_url is not None:
			try:
				epg_url = derive_tvh_xmltv_url(self._tvh, channels) or ''
			except Exception:
				epg_url = ''

		# Path 2: derive directly from M3U URL using auth token
		# (works even without TVH credentials in plugin settings)
		if not epg_url and parse_tvh_url is not None:
			base, token = parse_tvh_url(self._str('m3u_url'))
			if base:
				epg_url = base + '/xmltv/channels'
				if token:
					epg_url += '?auth=' + token

		if epg_url:
			self.log('[M3U-mgr] auto-using TVH XMLTV: %s' % mask_url(epg_url))
		return epg_url

	def _schedule_retry(self, reason):
		"""FIX 1.0.0 (J): jednorazový odložený retry po zlyhaní fetchu."""
		if self._retry_pending:
			return
		if self._retry_count >= _RETRY_MAX:
			self.log('[M3U-mgr] %s — retry limit (%d) reached, waiting for '
			         'periodic refresh' % (reason, _RETRY_MAX))
			return
		self._retry_count += 1
		self._retry_pending = True

		def _fire():
			self._retry_pending = False
			if self.can_run():
				self.refresh_async(is_retry=True)

		try:
			if callable(self.run_delayed):
				self.run_delayed(_RETRY_DELAY, _fire)
			else:
				t = threading.Timer(_RETRY_DELAY, _fire)
				t.daemon = True
				t.start()
			self.log('[M3U-mgr] %s — retry %d/%d in %ds'
			         % (reason, self._retry_count, _RETRY_MAX, _RETRY_DELAY))
		except Exception as e:
			self._retry_pending = False
			self.log('[M3U-mgr] cannot schedule retry: %s' % e)

	def _do_refresh(self):
		if not self.can_run():
			self.log('[M3U-mgr] disabled or URL empty, skipping')
			return False

		start = time.time()

		provider = M3UProvider(
			m3u_url=self._str('m3u_url'),
			epg_url='',
			log=self.log,
		)
		try:
			provider.fetch_and_parse(fetch_epg=False)
		except Exception as e:
			self.log('[M3U-mgr] fetch/parse failed: %s' % e)
			self._schedule_retry('playlist fetch failed')
			return False

		# FIX 1.0.0: prázdny playlist (0 kanálov) nesmie zmazať existujúci
		# bouquet — správať sa ako zlyhanie fetchu
		if provider.channel_count() == 0:
			self.log('[M3U-mgr] playlist parsed but contains 0 channels — '
			         'keeping existing bouquet')
			self._schedule_retry('empty playlist')
			return False
		self._retry_count = 0

		# EPG URL (explicit alebo odvodená z TVH) — až po parsovaní playlistu
		epg_url = self._derive_epg_url(provider)
		if epg_url:
			provider.fetch_epg(epg_url)

		# Enrich M3U channels from TVH API (fills tags=group, uuid=tvg-id, icon)
		if (enrich_with_tvh is not None
		        and self._bool('m3u_enrich_from_tvh', True)
		        and (looks_like_tvh_playlist is None
		             or looks_like_tvh_playlist(provider.get_all_channels()))):

			# Try primary TVH client first (uses host/username/password from settings)
			tvh_client = self._tvh
			primary_ok = False
			if tvh_client is not None:
				try:
					tvh_client.check_login()
					primary_ok = True
				except Exception:
					primary_ok = False

			# Fallback: derive token-only client from M3U URL auth=<token>
			if not primary_ok and build_token_client_from_url is not None:
				m3u_url_str = self._str('m3u_url')
				token_client = build_token_client_from_url(
					m3u_url_str, log=self.log)
				if token_client is not None:
					try:
						token_client.check_login()
						tvh_client = token_client
						self.log('[M3U-mgr] using auth-token TVH client '
						         '(derived from M3U URL)')
					except Exception as e:
						self.log('[M3U-mgr] auth-token fallback failed: %s' % e)
						tvh_client = None

			if tvh_client is not None:
				try:
					enrich_with_tvh(provider, tvh_client, log=self.log)
				except Exception as e:
					self.log('[M3U-mgr] TVH enrichment failed: %s' % e)
			else:
				self.log('[M3U-mgr] no TVH client available, skipping API enrichment')

			# ----- Path 3: URL-based tags via /playlist/tags endpoint -----
			# Aplikuje sa AJ ked predošle enrichment cesty fungovali, ale len pre
			# kanály ktoré skončili v 'Uncategorized'. Použije ten istý auth
			# token ako M3U URL — funguje bez API permissions na tickete.
			uncategorized = sum(1 for ch in provider.get_all_channels()
			                    if (ch.get('group') or '').lower() in
			                       ('', 'uncategorized', 'unknown'))
			if uncategorized > 0 and fetch_tvh_tags_via_url is not None:
				self.log('[M3U-mgr] %d uncategorized channels — trying '
				         'URL-based tag fetch via /playlist/tags' %
				         uncategorized)
				try:
					m3u_url_str = self._str('m3u_url')
					tag_map = fetch_tvh_tags_via_url(m3u_url_str,
					                                  log=self.log)
					if tag_map:
						updated = provider.apply_tag_mapping(tag_map)
						self.log('[M3U-mgr] URL-based tags: assigned tags '
						         'to %d previously uncategorized channels' %
						         updated)
					else:
						self.log('[M3U-mgr] URL-based tags fetch returned '
						         'empty map')
				except Exception as e:
					self.log('[M3U-mgr] URL-based tags fetch failed: %s' % e)

		# Optional mapping override (applied AFTER enrichment so user can
		# rename/reorder the TVH-derived categories)
		if self._bool('m3u_use_mapping'):
			mapping_path = self._str('m3u_mapping_file') or os.path.join(
				DEFAULT_BOUQUET_DIR, 'm3u-sort-override.xml')
			mapper = M3UMappingOverride(path=mapping_path, log=self.log)
			if mapper.load():
				self._apply_mapping_to_provider(provider, mapper)

		# Bouquet writer config
		settings = {
			# FIX 0.48f: bouquet_prefix už nie je configurable, vždy hardcoded
			'bouquet_prefix': M3U_BOUQUET_PREFIX,
			'bouquet_display_name': self._str('m3u_bouquet_name') or 'IPTV M3U',
			'service_type': self._str('m3u_service_type') or '1',
			'add_category_markers': True,
			'bouquet_dir': DEFAULT_BOUQUET_DIR,
			'picon_dir': DEFAULT_PICON_DIR,
			'download_picons': self._bool('m3u_picons_from_logo', True),
			# FIX 1.0.0 (F): setting existoval v settings.xml, ale writer ho
			# nedostával — rádiá išli do .radio bouquetu vždy
			'enable_radio_bouquet': self._bool('enable_radio_bouquet', True),
			# Pozn. (audit): write_epgimport_files() bola odstránená — direct
			# EPG injection cez m3u_epg_injector ju nahradila. Cleanup ale
			# stále spracúva legacy /etc/epgimport/<prefix>.channels.xml +
			# .sources.xml — to robí cleanup_m3u_bouquet() ktoré si epgimport_dir
			# berie z vlastného argumentu (M3URefreshManager.cleanup() vyššie).
		}

		# FIX 1.0.0 (I): user mohol export vypnúť počas fetchu — znova
		# skontrolovať tesne pred zápisom, inak by sa bouquet obnovil hneď
		# po cleanup-e
		if not self.can_run():
			self.log('[M3U-mgr] export disabled during refresh — not writing bouquet')
			return False

		writer = M3UBouquetWriter(provider, settings, log=self.log,
		                          translate=self._translate)
		if self._picon_wipe_requested:
			self._picon_wipe_requested = False
			writer._picon_wipe_pending = True
		try:
			writer.run()
		except Exception as e:
			self.log('[M3U-mgr] bouquet write failed: %s' % e)
			return False

		# Direct EPG injection into Enigma2 eEPGCache.
		# This makes EPG appear immediately without requiring the
		# external epgimport plugin to run.
		#
		# FIX 0.48g: gated by m3u_epg_inject_interval > 0 (predtým bool
		# m3u_inject_epg_to_enigma). Bool nahrádza single keyenum
		# kde 0 = Disabled, >0 = interval pre auto-inject mimo bouquet
		# refresh-u. Pri každom bouquet refreshi sa EPG vždy injektuje
		# (ak interval > 0), keďže máme práve čerstvé XMLTV.
		# FIX 0.48h: stats check — stamp len pri reálne >0 events injected
		# (rovnaký vzor ako inject_epg_only, defensive consistency).
		epg_inject_interval = self._int('m3u_epg_inject_interval', 14400)
		if (inject_epg_into_enigma is not None
		        and epg_inject_interval > 0
		        and provider._raw_epg_bytes):
			try:
				raw = self._decompress_epg(provider._raw_epg_bytes)
				stats = inject_epg_into_enigma(provider, raw, log=self.log)
				events_injected = (stats or {}).get('events_total', 0)
				if events_injected > 0:
					self._write_stamp(_EPG_INJECT_STAMP_M3U)
				else:
					self.log('[M3U-mgr] refresh_now: EPG injector returned 0 '
					         'events — not stamping inject success')
			except Exception as e:
				self.log('[M3U-mgr] EPG injection failed: %s' % e)

		# Update timestamp
		self._write_stamp(_STAMP_FILE)

		elapsed = time.time() - start
		self.log('[M3U-mgr] Refresh complete in %.1fs '
		         '(%d channels, %d categories)' %
		         (elapsed, provider.channel_count(),
		          len(provider.get_categories())))
		return True

	@staticmethod
	def _decompress_epg(raw):
		"""Decompress if needed (XMLTV may arrive gzipped/xz-ed)."""
		if raw[:2] == b'\x1f\x8b':
			raw = gzip.decompress(raw) if hasattr(gzip, 'decompress') \
				else gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
		elif raw[:6] == b'\xfd7zXZ\x00' and lzma is not None:
			raw = lzma.decompress(raw)
		return raw

	@staticmethod
	def _write_stamp(path):
		try:
			with open(path, 'w') as f:
				f.write(str(int(time.time())))
		except Exception:
			pass

	def inject_epg_only(self):
		"""FIX 0.48g: light-weight EPG-only refresh (bez bouquet rebuild-u).

		Stiahne M3U (potrebné pre tvg_id -> service_ref mapping) + XMLTV,
		injektuje cez eEPGCache. Preskakuje: enrichment, mapping override,
		bouquet write, picon download.

		Použitie: periodický EPG refresh medzi bouquet refresh-ami
		(M3U bouquet typicky 24h interval, EPG 4h interval).

		Vracia: True ak injection prebehla, False pri chybe alebo vypnutom
		feature.

		FIX 0.48h: dva kritické bugy z 0.48g:
		  BUG 1: žiadny kanál nemal '_service_ref' nastavený (to robí len
		    M3UBouquetWriter pri full refresh-i), takže
		    `inject_epg_into_enigma()` postavila prázdnu id_to_ref mapu
		    a injektnula 0 events. Stav: tichý fail, žiadny errror v UI.
		    FIX: pred volaním injektora prečítaj existujúci userbouquet
		    z disku, postav {url: short_sref} mapu, namapuj na channels.
		  BUG 2: stamp `_EPG_INJECT_STAMP_M3U` sa zapisoval AJ KEĎ 0 events
		    boli injektnuté (injector nehadzal exception, len vrátil stats).
		    Auto-retry sa potom odložil o celý interval (typicky 4h).
		    FIX: skontroluj stats.events_total > 0 pred zapísaním stamp.

		FIX 1.0.0 (I): beží pod _LOCK (neblokujúco) — ak práve beží full
		refresh, preskočí sa (refresh EPG injektuje sám).
		"""
		acquired = _LOCK.acquire(False)
		if not acquired:
			self.log('[M3U-mgr] inject_epg_only: refresh in progress, skipping')
			return False
		try:
			return self._do_inject_epg_only()
		finally:
			try:
				self._run_pending_cleanup()
			except Exception:
				pass
			try:
				_LOCK.release()
			except Exception:
				pass

	def _do_inject_epg_only(self):
		if not self._bool('enable_m3u_source', False):
			return False
		if inject_epg_into_enigma is None:
			self.log('[M3U-mgr] inject_epg_only: injector unavailable')
			return False
		epg_inject_interval = self._int('m3u_epg_inject_interval', 14400)
		if epg_inject_interval <= 0:
			return False

		m3u_url = self._str('m3u_url')
		if not m3u_url:
			return False

		# FIX 0.48h (BUG 1, časť 1): postav url→sref mapu z existujúceho bouquetu
		# FIX 0.1.3: skenuj aj radio bouquet (radio channels sú v ňom oddelene)
		# FIX 1.0.0: jedna URL môže mať viac refov (viac kategórií) — zoznam
		tv_bouquet, radio_bouquet = self._bouquet_paths()
		url_to_srefs = build_url_to_sref_from_bouquet(tv_bouquet)
		if os.path.isfile(radio_bouquet):
			for url, refs in build_url_to_sref_from_bouquet(radio_bouquet).items():
				lst = url_to_srefs.setdefault(url, [])
				for r in refs:
					if r not in lst:
						lst.append(r)
		if not url_to_srefs:
			self.log('[M3U-mgr] inject_epg_only: bouquet %s (and .radio) not '
			         'found or empty — run full M3U refresh first '
			         '(Settings → Refresh M3U now)' % tv_bouquet)
			return False
		self.log('[M3U-mgr] inject_epg_only: loaded %d url→sref mappings '
		         'from existing bouquet(s)' % len(url_to_srefs))

		start = time.time()

		# Fetch M3U
		try:
			provider = M3UProvider(m3u_url=m3u_url, epg_url='', log=self.log)
			provider.fetch_and_parse(fetch_epg=False)
		except Exception as e:
			self.log('[M3U-mgr] inject_epg_only: fetch/parse failed: %s' % e)
			return False

		# Auto-derive EPG URL — rovnaký vzor ako v refresh_now
		epg_url = self._derive_epg_url(provider)
		if not epg_url:
			self.log('[M3U-mgr] inject_epg_only: no EPG URL available')
			return False
		provider.fetch_epg(epg_url)

		if not provider._raw_epg_bytes:
			self.log('[M3U-mgr] inject_epg_only: empty EPG response')
			return False

		# FIX 0.48h (BUG 1, časť 2): pripoj _service_ref(s) k channels podľa URL
		matched_sref = 0
		for ch in provider._channels:
			url = ch.get('url')
			refs = url_to_srefs.get(url) if url else None
			if refs:
				ch['_service_ref'] = refs[0]
				ch['_service_refs'] = list(refs)
				matched_sref += 1

		if matched_sref == 0:
			self.log('[M3U-mgr] inject_epg_only: 0 channels matched to bouquet '
			         '(URLs changed since bouquet was generated?) — skip')
			return False
		self.log('[M3U-mgr] inject_epg_only: %d/%d channels mapped to '
		         'service refs from bouquet' %
		         (matched_sref, provider.channel_count()))

		# Decompress + inject
		try:
			raw = self._decompress_epg(provider._raw_epg_bytes)
			# FIX 0.48h (BUG 2): skontroluj stats a NEUKLADAJ stamp pri 0 events
			stats = inject_epg_into_enigma(provider, raw, log=self.log)
			events_injected = (stats or {}).get('events_total', 0)
			if events_injected <= 0:
				self.log('[M3U-mgr] inject_epg_only: 0 events injected — NOT '
				         'updating stamp (will retry next watchdog tick)')
				return False
			# Reálny úspech → stamp
			self._write_stamp(_EPG_INJECT_STAMP_M3U)
			elapsed = time.time() - start
			self.log('[M3U-mgr] inject_epg_only complete in %.1fs '
			         '(%d events across %d services)' %
			         (elapsed, events_injected,
			          (stats or {}).get('services_total', 0)))
			return True
		except Exception as e:
			self.log('[M3U-mgr] inject_epg_only: injection failed: %s' % e)
			return False

	def _apply_mapping_to_provider(self, provider, mapper):
		"""Rewrite provider's internal channel list using mapping rules.

		FIX 1.0.0 (C): jediný prechod cez kanály. Predtým pass 2 ("orphan"
		kanály) vrátil späť kanály z kategórie s enabled="false" (neboli
		v pass 1, tak sa pridali na koniec) a premenovanie kategórie na
		názov existujúcej kategórie (nameOverride="Sport" pri existujúcom
		"Sport") zduplikovalo jej kanály (pass 1 prešiel `ordered` dvakrát
		a `ch['group'] == orig` sedel po premenovaní znova).
		"""
		# Filter channels (mutates each channel dict in place)
		kept = []
		for ch in provider._channels:
			if mapper.apply_channel_rule(ch):
				kept.append(ch)
		provider._channels = kept

		# Reorder/filter categories: ordered = [(orig_name, display_name), ...]
		src_cats = provider._categories
		ordered = mapper.filter_and_order_categories(src_cats)
		display_map = {}
		for orig, disp in ordered:
			display_map.setdefault(orig, disp)

		# Kategórie, ktoré mapping explicitne vypol (enabled="false") —
		# ich kanály sa zahodia (aj keby ich categoryOverride nepresunul)
		disabled_cats = set()
		for rule in getattr(mapper, '_cat_rules', []):
			if not rule.get('enabled', True):
				disabled_cats.add(rule['name'])

		# Jediný prechod: každý kanál raz — premapuj group na display name.
		# Kanály presunuté cez categoryOverride do kategórie mimo mappingu
		# ostávajú (vytvorí sa pre ne kategória na konci).
		by_display = {}
		for ch in provider._channels:
			orig = ch['group']
			if orig in disabled_cats:
				continue
			disp = display_map.get(orig, orig)
			ch['group'] = disp
			by_display.setdefault(disp, []).append(ch)

		# Poradie kategórií: podľa mappingu (display names, bez duplicít),
		# potom ostatné v poradí prvého výskytu
		final_categories = []
		seen = set()
		for _orig, disp in ordered:
			if disp in by_display and disp not in seen:
				final_categories.append(disp)
				seen.add(disp)
		for ch in provider._channels:
			disp = ch['group']
			if disp in by_display and disp not in seen:
				final_categories.append(disp)
				seen.add(disp)

		final_channels = []
		for disp in final_categories:
			final_channels.extend(by_display.get(disp, []))

		provider._channels = final_channels
		provider._categories = final_categories

	# ------------------ Scheduler ------------------

	def _start_periodic(self, name, interval, callback, etimer_class=None,
	                    bgservice=None):
		"""Spustí periodické volanie `callback` každých `interval` sekúnd.
		Vráti handle pre _stop_periodic alebo None pri zlyhaní.

		Poradie mechanizmov:
		  1. framework bgservice.run_in_loop (ak je k dispozícii) — POZOR:
		     spustí callback aj hneď raz; callback musí byť lacný (tick),
		     lebo BG worker vlákno je spoločné pre všetky doplnky.
		  2. Enigma2 eTimer (etimer_class) v hlavnom vlákne.
		  3. threading fallback: jeden daemon thread + Event.wait.

		FIX 0.48:
		  - eTimer.callback: vyčistí pôvodný zoznam pred append-om,
		    aby sa pri prípadnom opakovanom volaní neakumulovali volania.
		  - threading.Timer fallback: nahradený jediným daemon threadom
		    s threading.Event.wait() — žiadny Timer-rebuild leak na
		    každom tiku.
		"""
		if bgservice is not None and hasattr(bgservice, 'run_in_loop'):
			try:
				handle = bgservice.run_in_loop('m3u_' + name, interval, callback)
				self.log('[M3U-mgr] %s: bgservice loop scheduled, interval=%ds'
				         % (name, interval))
				return {'kind': 'bgservice', 'handle': handle, 'svc': bgservice}
			except Exception as e:
				self.log('[M3U-mgr] %s: bgservice.run_in_loop failed: %s' % (name, e))

		if etimer_class is not None:
			# Enigma2 native eTimer
			try:
				timer = etimer_class()
				# Bezpečné resetnutie callback listu (nie všetky enigma buildy
				# majú stabilný .callback API, preto try/except)
				try:
					del timer.callback[:]
				except Exception:
					pass
				timer.callback.append(callback)
				timer.start(interval * 1000, False)
				self.log('[M3U-mgr] %s: eTimer scheduled, interval=%ds'
				         % (name, interval))
				return {'kind': 'etimer', 'timer': timer}
			except Exception as e:
				self.log('[M3U-mgr] %s: eTimer setup failed: %s' % (name, e))
				return None

		# Fallback: jediný daemon thread + Event.wait (žiadne kaskádové Timer-y)
		stop_event = threading.Event()

		def _loop():
			while not stop_event.wait(interval):
				if self._stop or stop_event.is_set():
					return
				try:
					callback()
				except Exception as e:
					self.log('[M3U-mgr] %s: scheduled call error: %s' % (name, e))

		t = threading.Thread(target=_loop, name='M3U-' + name)
		t.daemon = True
		t.start()
		self.log('[M3U-mgr] %s: threading scheduler started, interval=%ds'
		         % (name, interval))
		return {'kind': 'thread', 'thread': t, 'stop_event': stop_event}

	def _stop_periodic(self, handle):
		if not handle:
			return
		try:
			kind = handle.get('kind')
			if kind == 'bgservice':
				svc = handle.get('svc')
				if hasattr(svc, 'run_in_loop_stop'):
					svc.run_in_loop_stop(handle.get('handle'))
			elif kind == 'etimer':
				handle['timer'].stop()
			elif kind == 'thread':
				handle['stop_event'].set()
		except Exception:
			pass

	def schedule(self, etimer_class=None):
		"""
		Start periodic bouquet refresh (m3u_refresh_interval).

		FIX 0.48: re-entrancy guard — ak už timer beží, nevytvor druhý.
		Predtým sa pri každom plugin login()-e zavolal `schedule()` a
		vytvoril sa NOVÝ eTimer/Timer bez zrušenia starého -> paralelné
		refresh-e + thread leak.

		Pozn.: bgservice.run_in_loop spúšťa callback hneď — pre refresh sa
		preto bgservice nepoužíva (boot cooldown rieši login()), len
		eTimer/threading.
		"""
		interval = self._int('m3u_refresh_interval', 0)
		if interval <= 0:
			self.log('[M3U-mgr] periodic refresh disabled')
			# ak bol predtým nastavený, zruš ho
			self.cancel()
			return

		# Už beží — neduplikuj
		if self._timer is not None:
			self.log('[M3U-mgr] scheduler already running, skipping new schedule()')
			return

		self._stop = False
		self._timer = self._start_periodic('refresh', interval,
		                                   self.refresh_async,
		                                   etimer_class=etimer_class)

	def cancel(self):
		"""Zruší periodický bouquet refresh."""
		if self._timer is not None:
			self._stop_periodic(self._timer)
			self._timer = None

	def _epg_inject_tick(self):
		"""FIX 1.0.0 (G): tick periodického EPG injectu. Lacný — len skontroluje
		vek stampu (bouquet refresh EPG injektuje a stampuje tiež, takže po
		čerstvom refreshi sa nič nerobí) a spustí inject_epg_only vo
		vlastnom vlákne (nezdržiava BG worker / hlavné vlákno)."""
		interval = self._int('m3u_epg_inject_interval', 0)
		if interval <= 0 or not self.can_run():
			return
		# FIX 1.0.0 (review): ak je na rade bouquet refresh (24h refresh a
		# 4h tick sa raz za deň stretnú), EPG injektuje on — nesúťažiť
		# s ním o _LOCK
		if self.refresh_due():
			self.log('[M3U-mgr] epg tick: bouquet refresh is due, leaving EPG to it')
			return
		age = _stamp_age(_EPG_INJECT_STAMP_M3U)
		if age is not None and age < interval * 0.9:
			self.log('[M3U-mgr] epg tick: last inject %ds ago (< %ds), skipping'
			         % (int(age), interval))
			return
		t = threading.Thread(target=self.inject_epg_only, name='M3UEpgInject')
		t.daemon = True
		t.start()

	def schedule_epg_inject(self, etimer_class=None, bgservice=None):
		"""FIX 1.0.0 (G): periodický EPG-only inject podľa
		m3u_epg_inject_interval. Predtým setting len gate-oval injection
		pri bouquet refreshi a status riadok tvrdil "(every 4h)", ale nič
		to neplánovalo — medzi 24h bouquet refreshami EPG vyprchalo."""
		interval = self._int('m3u_epg_inject_interval', 0)
		if interval <= 0:
			self.log('[M3U-mgr] periodic EPG inject disabled')
			self.cancel_epg()
			return
		if self._epg_timer is not None:
			self.log('[M3U-mgr] EPG scheduler already running, skipping')
			return
		self._stop = False
		self._epg_timer = self._start_periodic('epg_inject', interval,
		                                       self._epg_inject_tick,
		                                       etimer_class=etimer_class,
		                                       bgservice=bgservice)

	def cancel_epg(self):
		"""Zruší periodický EPG inject."""
		if self._epg_timer is not None:
			self._stop_periodic(self._epg_timer)
			self._epg_timer = None

	def cancel_all(self):
		self._stop = True
		self.cancel()
		self.cancel_epg()


# -------------------------------------------------
# Smoke test
# -------------------------------------------------
if __name__ == '__main__':
	if len(sys.argv) < 2:
		print('Usage: m3u_manager.py <m3u_url> [<epg_url>]')
		sys.exit(1)

	cfg = {
		'enable_m3u_source': True,
		'm3u_url': sys.argv[1],
		'm3u_epg_url': sys.argv[2] if len(sys.argv) > 2 else '',
		'm3u_service_type': '1',
		'm3u_bouquet_name': 'IPTV M3U Test',
		'm3u_picons_from_logo': False,
		'm3u_refresh_interval': 0,
	}

	mgr = M3URefreshManager(
		settings_getter=lambda k, d=None: cfg.get(k, d),
		log=print,
	)
	ok = mgr.refresh_now()
	print('Refresh OK:', ok)
