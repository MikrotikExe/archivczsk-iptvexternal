# -*- coding: utf-8 -*-
"""
E2M3U2Bouquet content provider — main UI orchestrator pre M3U → Enigma2 bouquet workflow.

Extract z plugin.video.tvheadend 0.57.0 (skyjet PR #22 review #10/#11).
"""

from __future__ import absolute_import, unicode_literals, print_function

import io
import os
import time
import threading

from tools_archivczsk.contentprovider.provider import CommonContentProvider
from tools_archivczsk.contentprovider.exception import (
	AddonErrorException, AddonInfoException,
)

from .m3u_manager import M3URefreshManager
from .m3u_bouquet import M3U_BOUQUET_PREFIX, DEFAULT_BOUQUET_DIR, DEFAULT_PICON_DIR
from .m3u_provider import mask_url
from ._paths import data_path

try:
	from urllib.parse import urlparse
except ImportError:
	from urlparse import urlparse


# Plugin player_name enum (0-3) → Enigma2 service_type.
# Plugin nastavenie je v antik-style enum (`player_name`); m3u_manager.py
# vnútorne pracuje so service_type string-om, takže provider robí konverziu.
_PLAYER_NAME_TO_SERVICE_TYPE = {
	'0': '4097',  # Default (servicemp3 / default Enigma2 player)
	'1': '5001',  # gstplayer
	'2': '5002',  # exteplayer3
	'3': '1',     # DVB (OE >= 2.5)
}

# Settings, pri zmene ktorých sa bouquet regeneruje (viď
# _maybe_init_m3u_manager / _on_bouquet_settings_changed)
_BOUQUET_SETTINGS = (
	'm3u_url',
	'm3u_epg_url',
	'enable_userbouquet',
	'enable_radio_bouquet',
	'm3u_bouquet_name',
	'm3u_picons_from_logo',
	'm3u_use_mapping',
	'm3u_mapping_file',
	'm3u_enrich_from_tvh',
	'm3u_refresh_interval',
	'm3u_epg_inject_interval',
	'player_name',
)


def _bouquet_files():
	"""(tv_path, radio_path) generovaného userbouquetu."""
	return (
		os.path.join(DEFAULT_BOUQUET_DIR,
		             'userbouquet.{}.tv'.format(M3U_BOUQUET_PREFIX)),
		os.path.join(DEFAULT_BOUQUET_DIR,
		             'userbouquet.{}.radio'.format(M3U_BOUQUET_PREFIX)),
	)


def _count_bouquet_channels(path):
	"""FIX 1.0.0 (L): počet REÁLNYCH kanálov v bouquet súbore — preskočí
	kategoriálne markery (1:64:...). Predtým sa počítali všetky #SERVICE
	riadky, takže "Bouquet file: N channels" zahŕňalo aj markery."""
	count = 0
	try:
		# io.open + errors='replace': na Py2 by open() vrátil bajty a
		# diakritika v názvoch by mohla padnúť pri porovnaní
		with io.open(path, 'r', encoding='utf-8', errors='replace') as f:
			for line in f:
				if not line.startswith('#SERVICE '):
					continue
				parts = line[len('#SERVICE '):].split(':')
				if len(parts) > 1 and parts[1] == '64':
					continue
				count += 1
	except Exception:
		pass
	return count


def _shorten_url(u, n=40):
	"""FIX 1.0.0 (K): skrátená URL pre status riadok — host + začiatok
	cesty, tajné query hodnoty maskované. Predtým sa zobrazoval aj koniec
	URL (posledných 12 znakov), t.j. koniec auth tokenu."""
	if not u:
		return '(not set)'
	masked = mask_url(u)
	try:
		p = urlparse(masked)
		if p.scheme and p.netloc:
			short = '{}://{}{}'.format(p.scheme, p.netloc, p.path)
			if p.query:
				short += '?…'
		else:
			short = masked
	except Exception:
		short = masked
	if len(short) <= n:
		return short
	return short[:n - 1] + '…'


class E2M3U2BouquetContentProvider(CommonContentProvider):
	"""Hlavný content provider pre M3U → Enigma2 bouquet."""

	name = 'e2m3u2bouquet'

	def __init__(self):
		CommonContentProvider.__init__(self, name=self.name)
		self._m3u_manager = None
		self._m3u_lock = threading.Lock()

		# FIX 1.0.0 (I): `login_optional_settings_names` ZÁMERNE nenastavujem.
		# Framework (engine/addon.py Settings.__call_change_notifier) drží
		# odložené notifikácie počas otvoreného settings dialógu v dict-e
		# kľúčovanom NÁZVOM settingu: `delayed_notifiers[name] = (cbk, value)`.
		# Keď je ten istý setting registrovaný v dvoch mechanizmoch
		# (login_optional_settings_names → login_data_changed a
		# add_setting_change_notifier → _on_bouquet_settings_changed), druhý
		# zápis prvý PREPÍŠE a po zatvorení dialógu sa zavolá len jeden
		# callback — preto sa `_on_bouquet_settings_changed` pri vypnutí
		# `enable_userbouquet` "proste nezavolal" (0.1.2 to riešilo poll-om
		# v login()). Všetky bouquet settings sú teraz LEN v addon notifieri
		# (_BOUQUET_SETTINGS), ktorý framework volá priamo (synchrónne,
		# bez `login_refresh_running` guardu). Aby bol notifier registrovaný
		# aj keď je M3U URL pri prvom login-e prázdna, manager sa
		# inicializuje v login() ešte pred kontrolou URL.

	def login(self, silent):
		"""Disneyplus-style login: kontrola required setting + show_info.

		Plus pri každom login init manager + auto-refresh ak je
		enable_userbouquet ON. Framework volá login() pri každom otvorení
		pluginu — pri prvom otvorení s vyplnenou URL sa bouquet vygeneruje
		automaticky.

		FIX 0.1.2 (audit, Juraj): Pridaný poll-based cleanup check: ak je
		setting OFF a generated súbory ešte sú na disku, spustí sa cleanup.

		FIX 1.0.0 (J): boot cooldown — refresh sa spustí len ak je stamp
		starší ako m3u_refresh_interval (alebo chýba); predtým každý štart
		boxu stiahol playlist + picony nanovo. Po zapnutí exportu sa
		znova naplánuje periodický refresh (cleanup() ho ruší).
		"""
		# Manager + notifiery init VŽDY (aj pri prázdnej URL — viď __init__)
		mgr = self._maybe_init_m3u_manager()

		# Disneyplus-style: required settings check + info dialog
		if not (self.get_setting('m3u_url') or '').strip():
			if not silent:
				self.show_info(self._(
				    "To display content, you must enter M3U playlist URL "
				    "in the addon settings"), noexit=True)
			return False

		if mgr is None:
			return True

		try:
			if mgr.is_enabled() and mgr.can_run():
				# Setting ON path: refresh len ak je na čase (boot cooldown)
				if mgr.refresh_due():
					mgr.refresh_async()
				else:
					self.log_info('[m3u] login: last refresh is recent — '
					              'skipping boot refresh')
				# scheduler mohol byť zrušený predošlým cleanup() — re-arm
				self._schedule_timers(mgr)
			else:
				# Setting OFF path (poll-based detekcia toggle off):
				# ak sú generated súbory ešte na disku, vyčisti.
				self._cleanup_if_files_present(mgr, 'login')
		except Exception as e:
			try:
				self.log_error('[m3u] login: auto-refresh/cleanup failed: %s' % e)
			except Exception:
				pass
		return True

	def root(self):
		"""Root menu: 'Nastavenia' folder so statusom + manuálnymi akciami
		(TVH-style sub-menu). Prázdna M3U URL sem nedôjde — login() vtedy
		vráti False a framework root() nevolá."""
		self.add_dir(self._('Settings'),
		             cmd=self.settings_menu,
		             info_labels={'title': self._('Settings')})

	def _to_bool(self, v):
		if v is None or v == '':
			return False
		s = str(v).lower()
		return s in ('1', 'true', 'yes', 'on')

	# ------------------------------------------------------------------
	# Settings sub-menu (status + manuálne actions, TVH-style)
	# ------------------------------------------------------------------

	def settings_menu(self):
		"""Nastavenia sub-menu — status info + manuálne actions + diagnostika.

		Štruktúra (TVH style):
		  - Status lines (URL, posledný refresh, EPG age, ...)
		  - Separator
		  - Manuálne actions (Refresh, Inject EPG, Cleanup, ...)
		  - Diagnostika separator
		  - Show paths
		"""
		m3u_url = (self.get_setting('m3u_url') or '').strip()
		enable_userbouquet = self._to_bool(self.get_setting('enable_userbouquet'))

		# --- Status sekcia (vždy) ---
		for line in self._build_status_lines():
			self.add_dir(line, cmd=self.settings_menu,
			             info_labels={'title': line})

		# --- Action sekcia (len ak M3U URL je vyplnená) ---
		if m3u_url:
			self.add_dir('─' * 32, cmd=self.settings_menu,
			             info_labels={'title': self._('M3U Actions')})

			if enable_userbouquet:
				# FIX 0.2.1 (audit, Juraj): zjednotené s Tvheadend — všetky
				# ťažké operácie bežia na pozadí, takže UI/menu nezamrzne.
				self.add_dir(self._('Refresh M3U playlist + EPG now'),
				             cmd=self.action_m3u_refresh_async)
				self.add_dir(self._('Inject EPG only (no playlist refresh)'),
				             cmd=self.action_m3u_inject_epg)
				if self._to_bool(self.get_setting('m3u_picons_from_logo')):
					# FIX 1.0.0 (B): plný reset piconov (zmazať + nanovo)
					self.add_dir(self._('Force re-download all M3U picons (delete + fresh)'),
					             cmd=self.action_m3u_picon_refresh)
			else:
				self.add_dir(self._('⚠ Userbouquet export is disabled in settings'),
				             cmd=self.settings_menu,
				             info_labels={'title': self._('Disabled')})

			# Cleanup (ak existuje bouquet — TV alebo Radio)
			ub_tv, ub_radio = _bouquet_files()
			if os.path.isfile(ub_tv) or os.path.isfile(ub_radio):
				self.add_dir(self._('✗ Remove M3U bouquet'),
				             cmd=self.action_m3u_cleanup)

		# --- Diagnostika (vždy) ---
		self.add_dir('─' * 32, cmd=self.settings_menu,
		             info_labels={'title': self._('Diagnostics')})

		self.add_dir(self._('Show paths and generated files'),
		             cmd=self.action_show_paths,
		             info_labels={'title': self._('Paths')})

	def _build_status_lines(self):
		"""Status info pre Settings sub-menu (M3U side)."""
		lines = []
		m3u_url = (self.get_setting('m3u_url') or '').strip()
		epg_url = (self.get_setting('m3u_epg_url') or '').strip()
		enable_userbouquet = self._to_bool(self.get_setting('enable_userbouquet'))

		# M3U URL
		lines.append('◆ %s: %s' % (self._('M3U URL'), _shorten_url(m3u_url)))

		# EPG URL
		lines.append('◆ %s: %s' % (self._('XMLTV EPG URL'), _shorten_url(epg_url)))

		# Master toggle status
		if m3u_url:
			if enable_userbouquet:
				lines.append('◆ %s: %s' % (
					self._('Userbouquet export'), self._('enabled')))
			else:
				lines.append('◆ %s: %s' % (
					self._('Userbouquet export'), self._('disabled')))

		# Last refresh timestamp (z data_path/m3u_last_refresh.stamp)
		try:
			stamp = data_path('m3u_last_refresh.stamp')
			if os.path.isfile(stamp):
				age = time.time() - os.path.getmtime(stamp)
				lines.append('◆ %s: %s' % (
					self._('Last M3U refresh'), self._fmt_age(age)))
			elif m3u_url:
				lines.append('◆ %s: %s' % (
					self._('Last M3U refresh'), self._('never')))
		except Exception:
			pass

		# Last EPG inject timestamp (z m3u_epg_inject.stamp)
		# FIX 1.0.0 (G): "(every X)" je teraz pravdivé — interval naozaj
		# plánuje inject_epg_only (schedule_epg_inject), nielen gate.
		try:
			stamp = data_path('m3u_epg_inject.stamp')
			if os.path.isfile(stamp):
				age = time.time() - os.path.getmtime(stamp)
				# Plus aj inject interval
				interval_s = int(self.get_setting('m3u_epg_inject_interval') or 0)
				if interval_s > 0:
					if interval_s >= 86400:
						iv = '%dd' % (interval_s // 86400)
					else:
						iv = '%dh' % (interval_s // 3600)
					lines.append('◆ %s: %s (every %s)' % (
						self._('Last EPG inject'), self._fmt_age(age), iv))
				else:
					lines.append('◆ %s: %s' % (
						self._('Last EPG inject'), self._fmt_age(age)))
		except Exception:
			pass

		# Bouquet file presence
		try:
			ub_tv, ub_radio = _bouquet_files()
			tv_count = radio_count = 0
			if os.path.isfile(ub_tv):
				tv_count = _count_bouquet_channels(ub_tv)
			if os.path.isfile(ub_radio):
				radio_count = _count_bouquet_channels(ub_radio)
			if tv_count and radio_count:
				lines.append('◆ %s: %d TV + %d radio' % (
					self._('Bouquet file'), tv_count, radio_count))
			elif tv_count:
				lines.append('◆ %s: %d channels' % (
					self._('Bouquet file'), tv_count))
			elif radio_count:
				lines.append('◆ %s: %d radio channels' % (
					self._('Bouquet file'), radio_count))
		except Exception:
			pass

		return lines

	def _fmt_age(self, age_sec):
		"""Format age in seconds → human readable (e.g. '3h 24m ago')."""
		try:
			age = int(age_sec)
			if age < 60:
				return '%ds ago' % age
			if age < 3600:
				return '%dm ago' % (age // 60)
			if age < 86400:
				h = age // 3600
				m = (age % 3600) // 60
				return '%dh %dm ago' % (h, m)
			d = age // 86400
			h = (age % 86400) // 3600
			return '%dd %dh ago' % (d, h)
		except Exception:
			return '?'

	def action_show_paths(self):
		"""Zobrazí paths a vygenerované súbory."""
		ub_tv, ub_radio = _bouquet_files()
		paths = [
			(ub_tv, self._('M3U bouquet (TV)')),
			(ub_radio, self._('M3U bouquet (Radio)')),
			(DEFAULT_PICON_DIR, self._('Picon directory')),
			(data_path('m3u_sids.json'), self._('Stable SID map')),
			(data_path('m3u_picon_sources.json'), self._('Picon source index')),
			(data_path('m3u_last_refresh.stamp'),
			 self._('Last refresh stamp')),
			(data_path('m3u_epg_inject.stamp'),
			 self._('Last EPG inject stamp')),
		]

		lines = []
		for path, label in paths:
			exists = '✓' if os.path.exists(path) else '✗'
			lines.append('{} {}: {}'.format(exists, label, path))

		raise AddonInfoException('\n'.join(lines))

	# ------------------------------------------------------------------
	# Manager init + scheduler + settings notifier
	# ------------------------------------------------------------------

	def _schedule_timers(self, mgr):
		"""Naplánuje periodický bouquet refresh (eTimer) + EPG inject
		(bgservice, ak je; inak eTimer/threading). Oba schedule_* sú
		idempotentné — ak už bežia, nič sa nedeje."""
		bgservice = getattr(self, 'bgservice', None)
		try:
			from enigma import eTimer
		except ImportError:
			# Mimo Enigma2 prostredia (test) — fallback na threading
			eTimer = None
		try:
			mgr.schedule(etimer_class=eTimer)
		except Exception as e:
			try:
				self.log_error('[m3u] scheduler start failed: %s' % e)
			except Exception:
				pass
		try:
			mgr.schedule_epg_inject(etimer_class=eTimer, bgservice=bgservice)
		except Exception as e:
			try:
				self.log_error('[m3u] EPG scheduler start failed: %s' % e)
			except Exception:
				pass

	def _cleanup_if_files_present(self, mgr, where):
		"""Setting OFF path: ak sú generated súbory ešte na disku, vyčisti
		(idempotentné) a reloadni Enigma2 bouquet cache.

		FIX 1.0.0 (review): beží v daemon vlákne — volá sa zo settings
		notifiera v GUI vlákne a cleanup môže chvíľu trvať (mazanie
		piconov, reload bouquetov); GUI nesmie zamrznúť."""
		ub_tv, ub_radio = _bouquet_files()
		if not (os.path.isfile(ub_tv) or os.path.isfile(ub_radio)):
			return
		self.log_info('[m3u] %s: enable_userbouquet=OFF detected '
		              '+ generated files present — auto-cleanup' % where)
		# timery zrušiť tu (volajúce vlákno), nie z pracovného vlákna
		if not mgr.can_run():
			mgr.cancel_all()

		def _bg_cleanup():
			try:
				mgr.cleanup()
			except Exception as e:
				try:
					self.log_error('[m3u] %s: cleanup failed: %s' % (where, e))
				except Exception:
					pass
			# Reload Enigma2 bouquet cache aby zmeny boli viditeľné
			# v UI ihneď bez reštartu
			try:
				from enigma import eDVBDB
				eDVBDB.getInstance().reloadBouquets()
				self.log_info('[m3u] %s: eDVBDB.reloadBouquets() OK' % where)
			except ImportError:
				pass
			except Exception as e:
				self.log_info('[m3u] %s: reloadBouquets failed: %s' % (where, e))

		t = threading.Thread(target=_bg_cleanup, name='M3UCleanup')
		t.daemon = True
		t.start()
		return t

	def _maybe_init_m3u_manager(self):
		"""Lazy init M3URefreshManager. Vráti instance alebo None."""
		if self._m3u_manager is not None:
			return self._m3u_manager
		with self._m3u_lock:
			if self._m3u_manager is not None:
				return self._m3u_manager
			try:
				def _settings_get(key, default=None):
					try:
						# Mapovanie: m3u_manager očakáva 'm3u_service_type'
						# v string formáte ('1'/'4097'/'5001'/'5002'),
						# plugin setting je v antik-style 'player_name'
						# enum (0-3).
						if key == 'm3u_service_type':
							pn = self.get_setting('player_name') or '0'
							return _PLAYER_NAME_TO_SERVICE_TYPE.get(str(pn), '4097')
						# m3u_manager.is_enabled() volá 'enable_m3u_source'
						# (legacy key z plug TVH 0.56beta). V e2m3u2bouquet
						# 0.1.0 je antik-style key 'enable_userbouquet'.
						if key == 'enable_m3u_source':
							key = 'enable_userbouquet'
						v = self.get_setting(key)
						if v is None or v == '':
							return default
						return v
					except Exception:
						return default

				def _m3u_log(*parts):
					msg = ' '.join(str(p) for p in parts)
					try:
						self.log_info('[m3u] ' + msg)
					except Exception:
						pass

				tvh_client = self._maybe_build_tvh_client()

				mgr = M3URefreshManager(
					settings_getter=_settings_get,
					log=_m3u_log,
					tvh_client=tvh_client,
					translate=self._,
				)

				# FIX 1.0.0 (J): odložený retry po zlyhaní fetchu cez
				# framework bgservice (eTimer v hlavnom vlákne, task v BG
				# worker-i); bez bgservice si manager pomôže threading.Timer
				bgservice = getattr(self, 'bgservice', None)
				if bgservice is not None and hasattr(bgservice, 'run_delayed'):
					def _run_delayed(delay, fn):
						bgservice.run_delayed('m3u_refresh_retry', delay, None, fn)
					mgr.run_delayed = _run_delayed

				self._m3u_manager = mgr

				# Auto-rebuild bouquet keď user zmení niektoré z týchto
				# settings (rovnaký pattern ako framework BouquetXmlEpgGenerator
				# v tools_archivczsk/generator/bouquet_xmlepg.py:158).
				#
				# Plus dôležite: 'player_name' tu zaisťuje že keď user zmení
				# prehrávač (Default/gstplayer/exteplayer3/DMM/DVB), bouquet sa
				# regeneruje so správnym service_type.
				# FIX 1.0.0 (I): m3u_url/m3u_epg_url/enable_userbouquet sú
				# LEN tu (viď __init__ prečo nie v login_optional_settings_names).
				try:
					self.add_setting_change_notifier(
						_BOUQUET_SETTINGS, self._on_bouquet_settings_changed)
				except Exception:
					# Framework nemusí mať add_setting_change_notifier
					# (staršie verzie) — auto-rebuild bude zlyhať silent
					pass

				# FIX 0.1.1: spustenie periodického refresh scheduler-a podľa
				# m3u_refresh_interval setting. FIX 1.0.0 (G): + EPG inject.
				self._schedule_timers(mgr)

				return self._m3u_manager
			except Exception as e:
				try:
					self.log_error('[m3u] manager init failed: %s' % e)
				except Exception:
					pass
				return None

	def _on_bouquet_settings_changed(self, *args, **kwargs):
		"""Callback keď user zmení nejaký bouquet-related setting v UI.

		Spustí background refresh aby sa bouquet regeneroval s novými
		hodnotami (napr. nový player_name → nový service_type).

		FIX 0.1.2 (audit, Juraj): detekcia vypnutia `enable_userbouquet` —
		ak je setting OFF a generated súbory ešte existujú, spustí sa
		`mgr.cleanup()` ktorý ich zmaže + odstráni referencie z master
		bouquet súborov.

		FIX 1.0.0: pri zmene M3U URL sa nanovo odvodí TVH token client;
		po cleanup-e (ktorý ruší timery) sa pri opätovnom zapnutí timery
		znova naplánujú.
		"""
		try:
			self.log_info('[m3u] bouquet settings changed (%s) — triggering refresh'
			              % (args[0] if args else '?'))
		except Exception:
			pass

		mgr = self._m3u_manager
		if mgr is None:
			return
		try:
			if args and args[0] == 'm3u_url':
				mgr.set_tvh_client(self._maybe_build_tvh_client())
			if mgr.is_enabled() and mgr.can_run():
				# Setting ON path: refresh bouquet s novými hodnotami
				mgr.refresh_async()
			else:
				# Setting OFF path (enable_userbouquet toggle-d off):
				# vyčisti orphaned bouquet súbory ak ešte sú na disku.
				self._cleanup_if_files_present(mgr, 'settings-change')
		except Exception as e:
			try:
				self.log_error('[m3u] settings-change handler failed: %s' % e)
			except Exception:
				pass

		# FIX 0.1.1: re-schedule periodické timery ak user zmenil interval.
		# schedule_*() interne skontroluje či už beží — pre apply nového
		# intervalu treba najprv cancel a potom schedule znova.
		try:
			mgr.cancel_all()
			if mgr.can_run():
				self._schedule_timers(mgr)
		except Exception as e:
			try:
				self.log_error('[m3u] scheduler re-schedule failed: %s' % e)
			except Exception:
				pass

	def _maybe_build_tvh_client(self):
		"""Vráti TVH token client ak M3U URL je TVH URL formátu
		`http://host:port/playlist/auth/?auth=<token>`. Inak None.

		Pre TVH-hostované M3U playlisty extrahuje auth token zo samotného
		URL a vytvorí TvhAuthTokenClient (token-based access, žiadne
		username/password potrebné). Použité pre m3u_tvh_enricher (channel
		tags pre group-title, UUID pre tvg-id).

		Standalone M3U use-case (TVH URL nie je v M3U): vráti None,
		manager beží bez TVH enrichment.

		FIX 1.0.0 (H): framework vracia pre type="bool" setting `bool`, nie
		string — `(True or '').lower()` padal na AttributeError a except
		vrátil None (enrichment vypnutý práve keď bol ZAPNUTÝ) a naopak
		`False` prešiel ako zapnuté. Teraz riadna bool koercia.
		"""
		try:
			# Toggle settings — user môže explicitne vypnúť TVH enrichment
			v = self.get_setting('m3u_enrich_from_tvh')
			enrich = True if v is None or v == '' else self._to_bool(v)
			if not enrich:
				return None

			m3u_url = (self.get_setting('m3u_url') or '').strip()
			if not m3u_url:
				return None

			from .m3u_tvh_auth import build_token_client_from_url

			def _log(*parts):
				try:
					self.log_info('[m3u.tvh] ' + ' '.join(str(p) for p in parts))
				except Exception:
					pass

			return build_token_client_from_url(m3u_url, log=_log)
		except Exception:
			return None

	# ------------------------------------------------------------------

	def action_m3u_refresh_async(self):
		mgr = self._maybe_init_m3u_manager()
		if mgr is None or not mgr.is_enabled():
			raise AddonInfoException(self._(
				'M3U source is not configured. Open Settings to fill in M3U URL.'))
		mgr.refresh_async()
		raise AddonInfoException(self._('✓ M3U refresh started in background'))

	def action_m3u_picon_refresh(self):
		"""FIX 1.0.0 (B): plný reset piconov — zmaže všetky picony doplnku
		(aj kategórií, ktoré už v playliste nie sú) a stiahne ich nanovo
		v rámci refreshu na pozadí."""
		mgr = self._maybe_init_m3u_manager()
		if mgr is None or not mgr.is_enabled():
			raise AddonInfoException(self._(
				'M3U source is not configured. Open Settings to fill in M3U URL.'))
		mgr.full_picon_refresh()
		raise AddonInfoException(self._('✓ Picon reset + refresh started in background'))

	def action_m3u_inject_epg(self):
		# FIX 0.2.1 (audit, Juraj): EPG injection beží na pozadí (rovnako ako
		# Tvheadend), lebo pri veľkom XMLTV (tisíce events) môže trvať
		# niekoľko sekúnd a blokovala by UI.
		mgr = self._maybe_init_m3u_manager()
		if mgr is None or not mgr.is_enabled():
			raise AddonInfoException(self._(
				'M3U source is not configured. Open Settings to fill in M3U URL.'))

		def _bg_inject():
			try:
				mgr.inject_epg_only()
			except Exception as e:
				try:
					self.log_error('[m3u] inject EPG failed: %s' % e)
				except Exception:
					pass

		_t = threading.Thread(target=_bg_inject, name='M3UInjectEPG')
		_t.daemon = True
		_t.start()
		raise AddonInfoException(self._('✓ EPG injection started in background'))

	def action_m3u_cleanup(self):
		mgr = self._maybe_init_m3u_manager()
		if mgr is None:
			# Fallback bez manager-a — manual file delete
			removed = 0
			for fn in _bouquet_files():
				try:
					if os.path.isfile(fn):
						os.remove(fn)
						removed += 1
				except Exception:
					pass
			try:
				from enigma import eDVBDB
				eDVBDB.getInstance().reloadBouquets()
			except Exception:
				pass
			raise AddonInfoException(
				self._('✓ Removed M3U bouquet files: {}').format(removed))

		try:
			stats = mgr.cleanup()
			# FIX 1.0.0 (review): ručný cleanup so zapnutým exportom nesmie
			# nechať plánovač mŕtvy — timery ostávajú/obnovia sa
			if mgr.can_run():
				self._schedule_timers(mgr)
			if stats and stats.get('deferred'):
				raise AddonInfoException(self._(
					'M3U cleanup queued — a refresh is running, it will finish first'))
			if stats:
				raise AddonInfoException(
					self._('✓ M3U cleanup done: {}').format(stats))
			else:
				raise AddonInfoException(self._('Cleanup returned no stats'))
		except AddonInfoException:
			raise
		except Exception as e:
			raise AddonErrorException(self._('Cleanup failed: {}').format(e))
