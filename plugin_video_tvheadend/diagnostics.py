# -*- coding: utf-8 -*-
"""
DiagnosticsMixin — nastavenia, status, diagnostika, stats.

Vyclenene z provider.py (refaktor 0.80.0, krok 3). Metody menu/akcii pre
nastavenia a diagnostiku. Mix sa pripaja do TvheadendContentProvider cez
dedicnost; vsetky odkazy na self.* sa riesia za behu v zostavenej triede.

Bez zmeny spravania — iba presun metod.

Kompatibilita: Python 2.7 + Python 3.x
"""

from __future__ import absolute_import, unicode_literals, print_function

import os
import sys
import time
from datetime import datetime

from ._paths import data_path, get_data_dir
from ._common import _FAST_RECOVERY_STATE, _BOUQUET_REFRESH_STAMP
from ._watched_history import _set_watched_position


class DiagnosticsMixin(object):
	def settings_menu(self):
		"""Hlavné menu Nastavenia:
		Status info (TVH server, picon cache, EPG last inject, ...)
		+ TVH actions (refresh, EPG inject, picons, test login).
		"""
		tvh_ok = self._check_tvh_silent()

		# --- Status sekcia (vždy) ---
		for line in self._build_status_lines():
			self.add_dir(line, cmd=self.settings_menu,
			             info_labels={'title': line})

		# --- TVH sekcia (len ak je TVH nakonfigurované a prihlásené) ---
		if tvh_ok:
			self.add_dir("─" * 32, cmd=self.settings_menu,
			             info_labels={'title': self._("Tvheadend Actions")})

			self.add_dir(self._("Refresh TVH bouquet + XML EPG now"),
			             cmd=self.action_tvh_bouquet_refresh,
			             info_labels={'title': self._("Refresh TVH bouquet")})
			# FIX 0.58.2: "Inject EPG now" menu item odstránený — framework
			# triggeruje EPG inject po každom bouquet refresh, takže
			# "Refresh TVH bouquet + XML EPG now" robí oboje.
			self.add_dir(self._("Download TVH picons now"),
			             cmd=self.action_tvh_picons,
			             info_labels={'title': self._("TVH picons")})
			# FIX 0.59.2: plný refresh — zmaže staré TVH picony a stiahne
			# nanovo (pre aktualizáciu zmenených loga zo servera).
			self.add_dir(self._("Force re-download all TVH picons (delete + fresh)"),
			             cmd=self.action_tvh_picons_full_refresh,
			             info_labels={'title': self._("Full picon refresh")})
			# FIX 0.48b: tlačidlo na vyčistenie 404 negatívnej cache.
			# Užitočné keď user opraví broken ikony v TVH webUI a chce
			# okamžitý retry namiesto čakania 1h na auto-expire.
			self.add_dir(self._("Clear 404 picon cache (retry broken icons)"),
			             cmd=self.action_clear_picon_404_cache,
			             info_labels={'title': self._("Clear 404 cache")})
			self.add_dir(self._("Invalidate TVH channel cache"),
			             cmd=self.action_tvh_invalidate_cache,
			             info_labels={'title': self._("Clear TVH cache")})
			self.add_dir(self._("Test TVH login / connection"),
			             cmd=self.action_tvh_test_login,
			             info_labels={'title': self._("Test login")})

		# --- Diagnostika (vždy ale relevantné položky) ---
		self.add_dir("─" * 32, cmd=self.settings_menu,
		             info_labels={'title': self._("Diagnostics")})


		# Show paths - vždy užitočné
		self.add_dir(self._("Show paths and generated files"),
		             cmd=self.action_show_paths,
		             info_labels={'title': self._("Paths")})

	# ------------------------------------------------------------------
	# Status info pre Settings menu
	# ------------------------------------------------------------------


	def _build_status_lines(self):
		"""Vráti zoznam status riadkov pre úvod Settings menu."""
		lines = []

		def _fmt_age(stamp_path):
			try:
				t = int(os.path.getmtime(stamp_path))
				dt = datetime.fromtimestamp(t).strftime('%d.%m.%Y %H:%M')
				age = int(time.time()) - t
				if age < 60:
					age_s = "%ds" % age
				elif age < 3600:
					age_s = "%dm" % (age // 60)
				elif age < 86400:
					age_s = "%dh %dm" % (age // 3600, (age % 3600) // 60)
				else:
					age_s = "%dd" % (age // 86400)
				return "%s (%s ago)" % (dt, age_s)
			except Exception:
				return self._("never")

		tvh_ok = self._check_tvh_silent()

		# TVH connection - len ak je nakonfigurované
		if tvh_ok:
			try:
				host = self.get_setting('host') or '127.0.0.1'
				port = self.get_setting('port') or '9981'
				lines.append("◆ %s: %s:%s" %
				             (self._("TVH server"), host, port))
			except Exception:
				pass

			# TVH bouquet refresh stamp
			try:
				lines.append("◆ %s: %s" %
				             (self._("Last TVH bouquet refresh"),
				              _fmt_age(_BOUQUET_REFRESH_STAMP)))
			except Exception:
				pass

			# FIX 0.58.2: EPG inject status line odstránená — framework
			# auto-triggeruje EPG inject po každom bouquet refresh,
			# takže "Last bouquet refresh" implicitne pokrýva aj EPG.

			# FIX 0.48b: pocet broken-icon channels (404 cache)
			try:
				from .tvheadend import _picon_404_count
				cnt = _picon_404_count()
				if cnt > 0:
					lines.append("◆ %s: %d" %
					             (self._("Picons with broken icons (404 cache)"),
					              cnt))
			except Exception:
				pass

		# Ak nič nie je nakonfigurované, ukáž aspoň hint
		if not tvh_ok:
			lines.append("◆ %s" %
			             self._("Configure TVH credentials in plugin settings"))

		return lines

	# ------------------------------------------------------------------
	# Action callbacks
	# ------------------------------------------------------------------






	def action_retry_tvh_root(self):
		"""FIX 0.48h: užívateľský retry — invaliduj cache + re-render root.

		Volaná z root() retry položky a z live_root() pri transient failures.
		Po stlačení sa nasleduje ďalšie volanie root() ktoré spraví fresh
		check_login (cache=0 po invalidate).

		FIX 0.48i: aj zruší prípadný bežiaci fast-recovery poll (lebo
		urobíme manuálny check teraz, netreba zbytočne paralelne).
		"""
		try:
			self._invalidate_tvh_login_cache()
			# Vlastný TVH auth cache (separátny od _TVH_LOGIN_CACHE) tiež reset
			self.tvh.invalidate_auth_cache()
		except Exception:
			pass
		# FIX 0.48i: cancel fast-recovery poll ak beží
		try:
			ev = _FAST_RECOVERY_STATE.get('stop_event')
			if ev is not None and _FAST_RECOVERY_STATE.get('running'):
				ev.set()
		except Exception:
			pass
		# Re-render root menu — pridá nové items do tej istej "stránky"
		# (framework si ich vyberie ako návratový obsah z action_*)
		self.root()

	# ------------------------------------------------------------------
	# Settings menu - ručné akcie (refresh TVH/EPG/picons, status, atď.)
	# ------------------------------------------------------------------


	def action_tvh_test_login(self):
		"""Otestuje TVH login + zobrazí informáciu o serveri."""
		try:
			self.tvh.check_login()
			# Pokus o get_channels pre overenie permissions
			chs = self.tvh.get_channels(force=True)
			tags = self.tvh.get_tags()
			self.add_dir(self._("✓ TVH login successful"),
			             cmd=self.settings_menu)
			self.add_dir(self._("Channels: ") + str(len(chs or [])),
			             cmd=self.settings_menu)
			self.add_dir(self._("Tags: ") + str(len(tags or [])),
			             cmd=self.settings_menu)
		except Exception as e:
			self.add_dir(self._("✗ Login failed: ") + str(e),
			             cmd=self.settings_menu)
		self.add_dir(self._("« Back"), cmd=self.settings_menu)



	def action_show_paths(self):
		"""Zobrazí cesty k vygenerovaným súborom + ich veľkosti."""
		paths_to_check = [
			('/etc/enigma2/bouquets.tv', self._("Bouquets index")),
			('/usr/share/enigma2/picon', self._("Picon directory")),
			# FIX 0.48j: stampy sú teraz v persistent data dir-u, nie v /tmp
			(data_path('tvh_bouquet_refresh.stamp'),
				self._("TVH refresh stamp")),
			# Plugin data adresár — ukáže prehľad
			(get_data_dir(), self._("Plugin data dir")),
			# ArchivCZSK common log
			('/tmp/archivCZSK.log', self._("ArchivCZSK log")),
		]
		for path, label in paths_to_check:
			if os.path.exists(path):
				try:
					if os.path.isdir(path):
						count = sum(1 for _ in os.listdir(path)
						            if not _.startswith('.'))
						info = "%s (%d items)" % (path, count)
					else:
						sz = os.path.getsize(path)
						if sz > 1024 * 1024:
							sz_str = "%.1fMB" % (sz / 1024.0 / 1024.0)
						elif sz > 1024:
							sz_str = "%.1fKB" % (sz / 1024.0)
						else:
							sz_str = "%dB" % sz
						info = "%s (%s)" % (path, sz_str)
					self.add_dir("✓ " + label + ": " + info,
					             cmd=self.settings_menu)
				except Exception:
					self.add_dir("? " + label + ": " + path,
					             cmd=self.settings_menu)
			else:
				self.add_dir("✗ " + label + ": " + path + self._(" (missing)"),
				             cmd=self.settings_menu)
		self.add_dir(self._("« Back"), cmd=self.settings_menu)

	# ------------------------------------------------------------------
	# LIVE
	# ------------------------------------------------------------------


	def stats(self, data_item, action, duration=None, position=None, **extra_params):
		"""FIX 0.55beta: ArchivCZSK framework callback po skončení
		playback-u. Pri action=='end' / 'next' framework dáva position
		(sekundy kde sa playback zastavil) a duration (celková dĺžka).

		Uložíme do watched_history.json aby _is_fully_watched()
		vedela označiť entry hviezdičkou v archive listing-u, a
		_get_watched_position() vedela ponúknuť resume pri ďalšom play.

		Auto-clear: ak position prekročí 95 % z duration, _set_watched_position
		vynuluje position (film dohraný, pri ďalšom play sa pustí od
		začiatku — ale hviezdičkový marker zostane lebo duration sa
		zachovala).
		"""
		try:
			if not isinstance(data_item, dict):
				return
			action_lower = (action or '').lower()
			if action_lower not in ('end', 'next'):
				return
			# Skontroluj setting (user môže mať tracking úplne vypnutý)
			try:
				save_resume = self.get_setting('save_last_play_pos')
				save_resume = bool(save_resume) if isinstance(save_resume, bool) \
					else str(save_resume).strip().lower() in ('true', '1', 'yes')
			except Exception:
				save_resume = True
			if not save_resume:
				return
			_set_watched_position(data_item, position, duration)
		except Exception:
			# Defensive — stats() callback nesmie nikdy crashnúť plugin,
			# je to ne-kritická feature.
			try:
				self.log_info('stats() callback failed (silently): %s' % sys.exc_info()[1])
			except Exception:
				pass

	# ------------------------------------------------------------------
	# get_url_by_channel_key – volané z HTTP handlera a bouquet generátora
	# ------------------------------------------------------------------
