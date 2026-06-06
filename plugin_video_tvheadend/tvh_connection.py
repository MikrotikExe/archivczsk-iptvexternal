# -*- coding: utf-8 -*-
"""
ConnectionMixin — pripojenie/prihlasenie k Tvheadend serveru, kontrola stavu,
login cache, watchdog a fast-recovery polling.

Vyclenene z provider.py (refaktor 0.80.0, krok 7, posledny). Najcentralnejsi
blok — riadi zivotny cyklus spojenia. Mix sa pripaja do
TvheadendContentProvider cez dedicnost.

DOLEZITE: stav _TVH_LOGIN_CACHE / _WATCHDOG_STATE / _FAST_RECOVERY_STATE su
mutable objekty z _common, ktore tieto metody menia VYHRADNE in-place
(ziadny `global` + rebind). Import z _common preto zdiela ten isty objekt
naprieč modulmi a mutacie su viditelne vsade — overene pri refaktore.

Vlaknove workery (_loop, _poll_loop, _tick, _prefetch, _boot_epg_inject),
eTimer, threading a imdb_lookup su nested/lokalne importy a cestuju s metodami.

Bez zmeny spravania — iba presun metod.

Kompatibilita: Python 2.7 + Python 3.x
"""

from __future__ import absolute_import, unicode_literals, print_function

import sys
import time
from ._common import (
	_BOUQUET_REFRESH_STAMP,
	_TVH_LOGIN_CACHE, _TVH_LOGIN_CACHE_TTL_OK, _TVH_LOGIN_CACHE_TTL_FAIL,
	_WATCHDOG_STATE, _WATCHDOG_INTERVAL_MS,
	_FAST_RECOVERY_STATE, _FAST_RECOVERY_LOCK,
	_FAST_RECOVERY_INTERVAL_SEC, _FAST_RECOVERY_MAX_ATTEMPTS,
	_maybe_cleanup_poster_cache,
)

# Bouquet generator je volitelny (mirror provider/actions): graceful
# degradacia ak sa .bouquet modul nepodari naimportovat. Metody straz
# cez `... and TvheadendBouquetXmlEpgGenerator is not None`.
try:
	from .bouquet import TvheadendBouquetXmlEpgGenerator
except Exception:
	TvheadendBouquetXmlEpgGenerator = None


class ConnectionMixin(object):
	def login(self, silent=False):
		# Disneyplus-style: required settings check + info dialog
		if not (self.get_setting('host') or '').strip() \
		   or not (self.get_setting('username') or '').strip() \
		   or not (self.get_setting('password') or '').strip():
			if not silent:
				self.show_info(self._(
				    "To display content, you must enter TVH server "
				    "(host, username, password) in the addon settings"),
				    noexit=True)
			return False

		# Python 2 – best-effort beh, len jednorazové upozornenie do logu
		if sys.version_info[0] < 3:
			if not getattr(self, '_py2_warned', False):
				try:
					self.log_info("[Tvheadend] WARNING: running on "
					              "Python 2.x — best-effort mode")
				except Exception:
					pass
				self._py2_warned = True

		# Vyčistenie poster cache (max raz za týždeň) – tu, nie v __init__
		try:
			_maybe_cleanup_poster_cache()
		except Exception:
			pass

		# FIX 0.54beta: sync IMDb lookup feature flag s aktuálnym
		# setting. Volá sa pri každom login() — toggle v Settings sa
		# prejaví bez reštartu (framework volá login() po zmene
		# settingov cez login_data_changed).
		# FIX 0.57.0: cp.log_info() (framework logger) ide do archivCZSK.log.
		# Predtým bol logging.getLogger() ktorý šiel do stdlib logger ktorý
		# E2 nezachytí.
		try:
			from . import imdb_lookup as _imdb
			raw = self.get_setting('online_metadata_lookup')
			# ArchivCZSK vracia bool pre type="bool" settings, ale pre
			# istotu akceptujeme aj "true"/"1"/True/1.
			if isinstance(raw, bool):
				enabled = raw
			else:
				enabled = str(raw).strip().lower() in ('true', '1', 'yes')
			_imdb.set_enabled(enabled)
			self.log_info('IMDb lookup setting raw=%r resolved=%s' %
			              (raw, 'ON' if enabled else 'OFF'))
		except Exception as _e:
			try:
				self.log_info('IMDb lookup setup failed: %s' % _e)
			except Exception:
				pass

		# FIX 0.48: invalid-uj login cache pri každom login() volaní cez
		# settings change. (Framework volá login_data_changed → login(silent=True)
		# po zmene credentials.)
		self._invalidate_tvh_login_cache()
		try:
			self.tvh.invalidate_auth_cache()
		except Exception:
			pass

		# Test TVH connectivity (without raising — neúspech nezablokuje plugin,
		# Settings menu ostane prístupný aj keď TVH dočasne nedostupný)
		tvh_ok = False
		if self.tvh.is_configured():
			try:
				self.tvh.check_login()
				tvh_ok = True
			except Exception:
				tvh_ok = False
		# Aktualizuj cache aby _check_tvh_silent() nešiel hneď znova na server
		_TVH_LOGIN_CACHE['ts'] = int(time.time())
		_TVH_LOGIN_CACHE['ok'] = tvh_ok

		# TVH-related background work len pri funkčnom spojení
		if tvh_ok:
			# Lazy inicializácia generátora bouquetu
			if self._bouquet_gen is None and TvheadendBouquetXmlEpgGenerator is not None:
				try:
					self._bouquet_gen = TvheadendBouquetXmlEpgGenerator(self)
				except Exception:
					self._bouquet_gen = None

			# Spustiť picon download na pozadí (non-blocking)
			try:
				self.tvh.init_picons_async()
			except Exception:
				pass

			# HTSP: prefetch plných metadát (kanály+tagy+EPG+DVR) na pozadí,
			# nech sú v cache keď user otvorí menu/archív. Beží v threade —
			# neblokuje GUI. Prvé otvorenie počká ak prefetch ešte nedobehol.
			try:
				if self.tvh.is_htsp_mode():
					import threading as _th
					import time as _t
					def _prefetch():
						# 1) najprv RÝCHLO len kanály (channels_only, ~1-2s) —
						#    naplní cache aby prvý klik na kanál po reboote
						#    nemusel čakať na plný EPG+DVR fetch (~18s).
						try:
							self.tvh.htsp_fetch_metadata(with_epg=False,
							                             channels_only=True)
						except Exception:
							pass
						# 2) krátka pauza nech prípadný prvý stream/bouquet
						#    refresh stihne použiť kanály z cache (lock voľný)
						try:
							_t.sleep(8)
						except Exception:
							pass
						# 3) potom plný fetch (EPG+DVR) pre archív/EPG
						try:
							self.tvh.htsp_fetch_metadata(with_epg=True)
						except Exception:
							pass
					_th.Thread(target=_prefetch, daemon=True).start()
			except Exception:
				pass

			# FIX 0.70.2 (Juraj): EPG injekcia VŽDY po štarte GUI/Enigma2.
			_boot_inject_started = False
			# Framework pri štarte sám spúšťa loop(refresh bouquet) +
			# loop_changed(refresh xmlepg) pre každý doplnok — ALE xmlepg bez
			# force, takže keď sa checksum nezmenil (epg.dat prežil reštart),
			# export sa PRESKOČÍ a pri vyčistenej cache by EPG chýbalo.
			# Riešenie: NEvoláme vlastný refresh_bouquet (to robí framework —
			# volať ho druhýkrát súbežne = race condition), ale počkáme kým
			# framework dokončí štartový bouquet refresh (bouquet_refresh_running
			# flag) a potom VYNÚTIME jeden xmlepg export (force=True). Beží
			# na pozadí (thread) aby neblokoval login/GUI. Raz za beh pluginu.
			if self._bouquet_gen is not None and not self._epg_injected_this_boot:
				try:
					if bool(self._bouquet_gen.get_setting('enable_userbouquet')):
						self._epg_injected_this_boot = True
						_boot_inject_started = True
						import threading as _threading
						import time as _time_mod

						def _boot_epg_inject():
							try:
								# Počkaj kým framework dokončí svoj štartový
								# bouquet refresh (max ~90s), nech nekolíduje
								# load_channel_list ani enigma EPG zápis.
								for _ in range(90):
									if not getattr(self._bouquet_gen,
									                'bouquet_refresh_running', False):
										break
									_time_mod.sleep(1)
								# malá rezerva nech dobehne aj framework-ov
								# (neforced) xmlepg ktorý beží hneď po bouquete
								_time_mod.sleep(3)
								self.log_info('[Tvheadend.bouquet] štartová EPG '
								              'injekcia (po reštarte GUI/E2) — '
								              'vynucujem export (force)')
								self._bouquet_gen.refresh_xmlepg(force=True)
								self.log_info('[Tvheadend.bouquet] štartová EPG '
								              'injekcia dokončená')
							except Exception as _be:
								try:
									self.log_error('[Tvheadend.bouquet] štartová '
									               'EPG injekcia zlyhala: %s' % _be)
								except Exception:
									pass

						_bt = _threading.Thread(target=_boot_epg_inject,
						                        name='TVHBootEpgInject')
						_bt.daemon = True
						_bt.start()
				except Exception as e:
					try:
						self.log_error('[Tvheadend.bouquet] štartová EPG injekcia '
						               'naplánovanie zlyhalo: %s' % e)
					except Exception:
						pass

			# Export bouquet/EPG na pozadí s TTL ochranou (pre silent re-login
			# z HTTP handlera). Preskočíme ak práve bežala štartová injekcia
			# vyššie — tá už robí plný force refresh, druhé volania by len
			# zbytočne kolidovali (bouquet_refresh_running by ich aj tak skipol).
			if self._bouquet_gen is not None and not _boot_inject_started:
				try:
					self._maybe_trigger_exports(silent=bool(silent))
				except Exception:
					pass

				# Auto-refresh bouquetu + EPG podľa nastaveného intervalu (4/8/16/24h)
				try:
					self._maybe_auto_refresh_bouquet()
				except Exception:
					pass

			# FIX 0.58.2 (skyjet PR #22 review #11 follow-up): nezávislý EPG
			# auto-inject odstránený — framework `BouquetXmlEpgGenerator`
			# automaticky volá `refresh_xmlepg()` každé 4 hodiny + pri
			# settings change keď je `enable_xmlepg=True`. Plus podľa
			# `bouquet_refresh_interval` sa znova aj triggeruje cez
			# `_maybe_auto_refresh_bouquet()` ktoré sa volá vyššie.

		# FIX 0.57.0 (skyjet PR #22 review #10/#11): M3U manager init
		# odstránený — extrahovaný do plugin.video.e2m3u2bouquet.

		# FIX 0.48: watchdog timer — spustí sa raz pri prvom login()
		# (preload="yes" v addon.xml znamená že login beží pri boot-e E2).
		# Pravidelne (5 min) volá _check_tvh_silent(force=True), a keď
		# detekuje OFFLINE → ONLINE prechod, automaticky spustí bouquet
		# refresh + picon download. Tým sa odstráni potreba manuálne
		# otvoriť plugin po reštarte TVH servera.
		try:
			self._maybe_start_watchdog()
		except Exception:
			pass

		# Pozn.: Dialóg "Plugin nie je nakonfigurovaný" sa zobrazí z root()
		# vyhodením AddonErrorException — framework ho v run() zachytí cez
		# `except AddonErrorException: client.showError(str(e))`. Tu v login()
		# nezobrazujeme nič (vždy vrátime True), aby framework dostal kontrolu
		# nad volaním root().

		# Vždy vracia True aby framework načítal root() - bez ohľadu na TVH stav.
		# Jednotlivé root() / live_root() / archive_channels() si overia
		# TVH login podľa potreby cez _check_tvh_silent().
		return True


	def _quick_login_for_http_handler(self):
		"""FIX 0.48: light-weight login pre HTTP handler.

		Predtým HTTP handler `get_url_by_channel_key()` volal plný `login(silent=True)`,
		ktorý pri každom playback-u kanála spravil:
		  - _maybe_cleanup_poster_cache (rýchle, ale beží)
		  - lazy init bouquet generator
		  - init_picons_async (spawn threadu zakaždým — _picon_worker_lock
		    zabráni paralelnému downloadu, ale stále zbytočný thread overhead)
		  - _maybe_trigger_exports + _maybe_auto_refresh_bouquet

		Pre HTTP handler stačí overiť TVH konektivitu cez cache.
		Bouquet refresh / picon download spustí watchdog alebo plný login().
		"""
		if not self.tvh.is_configured():
			return False
		try:
			# Použi cache (default 30s) — pri streamovaní mnoho channels
			# za sekundu by sme inak hammerovali TVH /api/serverinfo.
			return self._check_tvh_silent()
		except Exception:
			return False


	def _maybe_start_watchdog(self):
		"""Spustí periodický watchdog timer ktorý detekuje návrat TVH online.

		FIX 0.48: bez tohto musel užívateľ po reštarte TVH servera buď
		otvoriť plugin v GUI alebo počkať na ďalší pokus o stream.
		Watchdog beží na pozadí (eTimer + fallback threading) a:
		  - každých 5 minút volá _check_tvh_silent(force=True)
		  - ak detekuje OFFLINE→ONLINE prechod, spustí na pozadí:
		      a) bouquet refresh (cez existujúci _bouquet_gen)
		      b) picon download (cez init_picons_async)
		  - ak je ONLINE, ešte navyše kontroluje či nezbehol auto-refresh
		    bouquet interval (užitočné keď používateľ ROZHODNE neotvára
		    plugin v GUI a HTTP handler sa tiež nepoužíva)
		"""
		if _WATCHDOG_STATE['started']:
			return

		def _tick():
			try:
				prev = _WATCHDOG_STATE.get('last_state')
				now_ok = self._check_tvh_silent(force=True)
				_WATCHDOG_STATE['last_state'] = now_ok

				if now_ok and prev is False:
					# OFFLINE → ONLINE prechod
					try:
						self.log_info('[Tvheadend] watchdog: TVH back online — '
						      'triggering bouquet + picon refresh')
					except Exception:
						pass
					# Lazy-init bouquet gen ak ešte nie je
					if self._bouquet_gen is None and TvheadendBouquetXmlEpgGenerator is not None:
						try:
							self._bouquet_gen = TvheadendBouquetXmlEpgGenerator(self)
						except Exception:
							pass
					# Background refresh (non-blocking)
					if self._bouquet_gen is not None:
						try:
							# FIX 0.71.0 (audit): predtým refresh_userbouquet_start()
							# — tá v base class NEEXISTUJE → ticho padla do except,
							# takže po návrate TVH online sa bouquet/EPG NEobnovil.
							# bouquet_settings_changed reťazí refresh_bouquet → EPG.
							self._bouquet_gen.bouquet_settings_changed('watchdog_online', None)
							with open(_BOUQUET_REFRESH_STAMP, 'w') as f:
								f.write(str(int(time.time())))
						except Exception:
							pass
					try:
						self.tvh.init_picons_async()
					except Exception:
						pass

				# Pravidelná kontrola auto-refresh aj keď user neotvoril plugin
				if now_ok and self._bouquet_gen is not None:
					try:
						self._maybe_auto_refresh_bouquet()
					except Exception:
						pass
					# FIX 0.58.2: EPG auto-inject odstránený z watchdog —
					# framework sám trigger-uje refresh_xmlepg po
					# refresh_bouquet keď je enable_xmlepg=True.
			except Exception as e:
				try:
					self.log_info('[plugin.tvheadend]watchdog error: %s' % e)
				except Exception:
					pass

		# Skús enigma eTimer, fallback threading
		try:
			from enigma import eTimer
			t = eTimer()
			try:
				del t.callback[:]
			except Exception:
				pass
			t.callback.append(_tick)
			# False = opakovaný timer (nie singleshot)
			t.start(_WATCHDOG_INTERVAL_MS, False)
			_WATCHDOG_STATE['timer'] = t
			_WATCHDOG_STATE['started'] = True
			try:
				self.log_info('[Tvheadend] watchdog started '
				      '(eTimer, interval=%d min)' % (_WATCHDOG_INTERVAL_MS // 60000))
			except Exception:
				pass
		except ImportError:
			# Fallback: daemon thread + Event.wait
			import threading as _th
			ev = _th.Event()
			_WATCHDOG_STATE['stop_event'] = ev
			interval_sec = _WATCHDOG_INTERVAL_MS // 1000

			def _loop():
				while not ev.wait(interval_sec):
					_tick()

			th = _th.Thread(target=_loop, name='TVHWatchdog')
			th.daemon = True
			th.start()
			_WATCHDOG_STATE['timer'] = th
			_WATCHDOG_STATE['started'] = True


	def _check_tvh_silent(self, force=False):
		"""Vráti True ak TVH server je nakonfigurovaný a prihlásenie funguje.

		FIX 0.48: TTL cache.
		FIX 0.48h: asymetrické TTL (30s pri úspechu, 5s pri zlyhaní) — keď
		TVH transient failne, ďalší pokus zbehne čoskoro. + 'reason' tracking
		('not_configured' / 'unreachable' / 'ok') pre rozlíšenie chybových
		stavov v root().
		FIX 0.48i:
		  - pri prvom zlyhaní okamžitý retry s force_reauth=True (handluje
		    digest auth nonce expiry — TVH server občas odhodí nonce po
		    niekoľkých minútach idle, requests knižnica si občas nestihne
		    obnoviť stav medzi thread-mi)
		  - keď check stále zlyhá, spustí sa background fast-recovery poll
		    cez _maybe_start_fast_recovery_poll() — užívateľ nebude musieť
		    tlačiť retry manuálne; po naskočení TVH sa cache aktualizuje
		    ticho a ďalšia navigácia zafunguje

		Volajúci môže zistiť dôvod cez get_tvh_state() metódu nižšie.

		FIX 0.50beta: zdieľaný core (_do_tvh_login_check) s
		_check_tvh_silent_no_recurse_for_poll — eliminuje DRY violation
		ktorá vyžadovala udržiavať dve takmer identické kópie tej istej
		logiky (oba volajú check_login + force_reauth retry + cache
		update). Verzia bez recurse je iba flag `start_recovery_on_fail`.
		"""
		now = int(time.time())
		c = _TVH_LOGIN_CACHE
		if not force:
			ttl = _TVH_LOGIN_CACHE_TTL_OK if c['ok'] else _TVH_LOGIN_CACHE_TTL_FAIL
			if (now - c['ts']) < ttl:
				return c['ok']
		return self._do_tvh_login_check(start_recovery_on_fail=True)


	def _do_tvh_login_check(self, start_recovery_on_fail):
		"""FIX 0.50beta: zdieľaný core pre _check_tvh_silent +
		_check_tvh_silent_no_recurse_for_poll.

		Vykoná dvojfázový check (prvý pokus + force_reauth retry),
		aktualizuje module-level _TVH_LOGIN_CACHE, a (ak je
		start_recovery_on_fail=True) pri zlyhaní spustí background
		fast-recovery poll cez _maybe_start_fast_recovery_poll.

		Vráti True/False.
		"""
		now = int(time.time())
		c = _TVH_LOGIN_CACHE

		if not self.tvh.is_configured():
			c['ts'] = now
			c['ok'] = False
			c['reason'] = 'not_configured'
			c['last_error'] = ''
			return False

		# Dvojfázový check: prvý pokus na existujúcom auth state,
		# druhý pokus s freshým HTTPDigestAuth (force_reauth=True)
		# rieši digest auth nonce expiry po idle period.
		err = ''
		ok = False
		try:
			self.tvh.check_login()
			ok = True
		except Exception as e:
			err = str(e)
			try:
				time.sleep(0.3)  # malé čakanie na sieťovú stabilizáciu
				self.tvh.check_login(force_reauth=True)
				ok = True
				err = ''
				try:
					self.log_info('[Tvheadend] check_login: recovered on retry '
					      'with force_reauth (was: %s)' % e)
				except Exception:
					pass
			except Exception as e2:
				err = str(e2)

		c['ts'] = now
		c['ok'] = ok
		c['reason'] = 'ok' if ok else 'unreachable'
		c['last_error'] = err

		if not ok and start_recovery_on_fail:
			try:
				self._maybe_start_fast_recovery_poll()
			except Exception:
				pass

		return ok


	def get_tvh_state(self):
		"""FIX 0.48h: vráti tuple (ok, reason, last_error) pre nadradenú logiku."""
		c = _TVH_LOGIN_CACHE
		return (c['ok'], c.get('reason'), c.get('last_error') or '')


	def _invalidate_tvh_login_cache(self):
		"""Vynúti čerstvý check pri ďalšom _check_tvh_silent()."""
		_TVH_LOGIN_CACHE['ts'] = 0


	def _maybe_start_fast_recovery_poll(self):
		"""FIX 0.48i: spustí background poll thread ktorý každých 10s skúša
		TVH check, kým TVH neobnovuje. Max 5 minút (30 pokusov), potom sa
		zastaví — watchdog tick (každých 5 min) obnoví normálny cyklus.

		Cieľ: užívateľ nemusí ručne stláčať Retry po TVH transient failure.
		Po naskočení TVH sa cache silently aktualizuje na ok=True a ďalšia
		navigácia uvidí všetko v poriadku.

		Idempotentné: ak už beží, druhé volanie nič nespraví.

		FIX 0.50beta: check-and-set chránený _FAST_RECOVERY_LOCK proti
		race condition keď 2+ threads zavolajú túto metódu súčasne.
		"""
		import threading as _th
		# FIX 0.50beta: atomic check-and-set namiesto zraniteľnej
		# kombinácie `if not running: ... running = True`
		with _FAST_RECOVERY_LOCK:
			if _FAST_RECOVERY_STATE.get('running'):
				return  # už beží
			ev_stop = _th.Event()
			_FAST_RECOVERY_STATE['stop_event'] = ev_stop
			_FAST_RECOVERY_STATE['running'] = True

		def _poll_loop():
			try:
				self.log_info('[Tvheadend] fast-recovery poll started '
				      '(every %ds, max %d attempts)' %
				      (_FAST_RECOVERY_INTERVAL_SEC, _FAST_RECOVERY_MAX_ATTEMPTS))
			except Exception:
				pass
			for attempt in range(_FAST_RECOVERY_MAX_ATTEMPTS):
				# Event.wait s timeout — kedykoľvek možno cancelnúť cez set()
				if ev_stop.wait(_FAST_RECOVERY_INTERVAL_SEC):
					break  # cancelled
				try:
					# force=True aby sme obišli TTL cache (5s je ešte v platnosti)
					ok = self._check_tvh_silent_no_recurse_for_poll()
					if ok:
						try:
							self.log_info('[Tvheadend] fast-recovery: TVH back '
							      'online after %d attempts (%ds total)' %
							      (attempt + 1,
							       (attempt + 1) * _FAST_RECOVERY_INTERVAL_SEC))
						except Exception:
							pass
						break
				except Exception:
					pass
			# FIX 0.50beta: reset running flag pod lockom, rovnaký lock
			# ako check-and-set v _maybe_start_fast_recovery_poll, aby
			# následné volanie videlo running=False atomicky
			with _FAST_RECOVERY_LOCK:
				_FAST_RECOVERY_STATE['running'] = False
			try:
				self.log_info('[plugin.tvheadend] fast-recovery poll ended')
			except Exception:
				pass

		t = _th.Thread(target=_poll_loop, name='TVHFastRecovery')
		t.daemon = True
		_FAST_RECOVERY_STATE['thread'] = t
		t.start()


	def _check_tvh_silent_no_recurse_for_poll(self):
		"""FIX 0.48i: variant _check_tvh_silent ktorý sa NEZAVOLÁVA
		fast-recovery (lebo my SME fast-recovery). Pomáha vyhnúť sa
		rekurzii / opakovanému spawnu thread-ov.

		FIX 0.50beta: namiesto duplicitnej kópie celej _check_tvh_silent
		logiky (auth retry, cache update, ...) volá zdieľaný core
		_do_tvh_login_check(start_recovery_on_fail=False).
		"""
		return self._do_tvh_login_check(start_recovery_on_fail=False)




	def _guess_tvh_error_hint(self, err):
		"""FIX 0.50beta: z technickej chybovej hlášky requests/urllib odhadne
		user-friendly hint čo má užívateľ skontrolovať v Nastaveniach.

		Pokrýva typické dôvody zlyhania pripojenia na TVH:
		- DNS chyba (Name or service not known, gaierror, getaddrinfo failed)
		  → "Server name not found — check 'host' in Settings"
		- Connection refused (TVH neštartol alebo zlý port)
		  → "Connection refused — wrong port or TVH not running"
		- Timeout (sieťová routovacia chyba alebo blokovaný firewall)
		  → "Connection timeout — check IP/host and firewall"
		- 401 Unauthorized (zlé credentials)
		  → "Authentication failed — check username/password"
		- 403 Forbidden (user nemá oprávnenia)
		  → "Forbidden — TVH user lacks permissions"
		- 404 Not Found (zlá API cesta — neštandardný TVH build?)
		  → "API endpoint not found — wrong TVH version?"
		- Iné: vráti None (volajúci ukáže len raw error riadok)
		"""
		if not err:
			return None
		e = str(err).lower()
		# Poradie matters — niektoré errors môžu mať viacero kľúčových slov
		if ('name or service not known' in e or 'gaierror' in e or
		    'getaddrinfo failed' in e or 'temporary failure in name resolution' in e or
		    'nodename nor servname' in e):
			return self._("⚠ Server name not found — check 'host' field in Settings")
		if 'connection refused' in e or 'econnrefused' in e:
			return self._("⚠ Connection refused — wrong port, or TVH server not running")
		if 'timed out' in e or 'timeout' in e:
			return self._("⚠ Connection timeout — check IP/host, network and firewall")
		if '401' in e or 'unauthorized' in e or 'authentication failed' in e:
			return self._("⚠ Authentication failed — check username and password")
		if '403' in e or 'forbidden' in e:
			return self._("⚠ Forbidden — TVH user lacks API permissions")
		if '404' in e or 'not found' in e:
			return self._("⚠ API endpoint not found — wrong TVH version or path?")
		if 'no route to host' in e or 'ehostunreach' in e or 'network is unreachable' in e:
			return self._("⚠ No route to host — check network connection")
		if 'ssl' in e or 'certificate' in e:
			return self._("⚠ SSL/certificate error — try disabling HTTPS or fix cert")
		return None


	def _render_tvh_error_lines(self, err, max_lines=3, max_chars=150):
		"""FIX 0.48i: rozdelí multi-line error string a pridá ho ako 1-3
		add_dir položky. Cieľ: aby užívateľ videl aj underlying error
		(typicky druhý riadok z api_get wrapper-a), nie len wrapper text
		"Tvheadend API request failed.".

		Volajúci je zodpovedný za pridanie retry položky pred týmto.
		"""
		if not err:
			return
		# Rozdeľ na riadky, vyfiltruj prázdne, oreže každý na max_chars
		parts = [p.strip() for p in err.split('\n') if p.strip()]
		for i, part in enumerate(parts[:max_lines]):
			prefix = "✗ " if i == 0 else "  → "
			title = self._("Last error") if i == 0 else self._("Detail")
			self.add_dir(prefix + part[:max_chars],
			             cmd=self.action_retry_tvh_root,
			             info_labels={'title': title})
