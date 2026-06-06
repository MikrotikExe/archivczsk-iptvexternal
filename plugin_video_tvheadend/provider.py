# -*- coding: utf-8 -*-
"""
TvheadendContentProvider – hlavný provider pre ArchivCZSK.

Kompatibilita: Python 2.7 + Python 3.x
Primárne testované na OpenATV / Python 3.

Implementuje:
  - login() metódu (nie login v root/konstruktore)
  - login_settings_names / login_optional_settings_names
  - _maybe_cleanup_poster_cache() volaná len z login() (nie z __init__)
  - export bouquet/EPG cez BouquetXmlEpgGenerator s TTL stampom
  - Python 2 kompatibilný fallback s užívateľsky zrozumiteľnou chybou
"""

from __future__ import absolute_import, unicode_literals, print_function


try:
	import lzma  # Py3
except ImportError:
	try:
		from backports import lzma
	except ImportError:
		lzma = None

from tools_archivczsk.contentprovider.provider import CommonContentProvider

# FIX 0.57.0 (skyjet PR #22 review): _I je vždy dostupné v
# tools_archivczsk.string_utils (guaranteed dependency).

# FIX 0.57.0 (skyjet PR #22 review): tools.archivczsk je explicit dependency
# v addon.xml (version 3.4+) — strip_accents je vždy dostupný, žiadny fallback
# netreba. Predtým bola tu duplicitná implementácia cez unicodedata.normalize
# pre prípad chýbajúceho framework helpera, čo nikdy nemôže nastať.

from .tvheadend import Tvheadend
from .diagnostics import DiagnosticsMixin
from .live import LiveMixin
from .tvh_actions import ActionsMixin
from .dvr import DvrMixin
from .tvh_connection import ConnectionMixin

try:
	from .bouquet import TvheadendBouquetXmlEpgGenerator
except Exception:
	TvheadendBouquetXmlEpgGenerator = None

# FIX 0.57.0 (skyjet PR #22 review #10/#11): M3U fíčúra extrahovaná
# do samostatného doplnku plugin.video.e2m3u2bouquet.


# --------------------------------------------------------------------------
# Konštanty
# --------------------------------------------------------------------------
# FIX 0.48j: stamp súbory sa ukladajú do persistent data dir-u namiesto /tmp,
# aby prežili reboot E2 (predtým sa všetky TTL gates pri reboot-e zresetli).
# Cache picons ostáva v /tmp lebo je regenerovateľná a tmpfs je rýchle.

from ._watched_history import _load_watched_history
from ._common import _get_dvr_finished_cached


from .classifier import (
	# Kategórie
	_CAT_LABELS_ORDER,
	# Display labels pre sub-kategórie (Filmy podžánre)
	# Sub-cat registry pre generic dispatch (Šport, Spravodajstvo, Šou, Detské, ...)
	# Regex patterns (používané v provider menu rendering pre series detection)
	# Helpers
	# Klasifikačné funkcie
	_get_classified_dvr,
)

class TvheadendContentProvider(DiagnosticsMixin, LiveMixin, ActionsMixin, DvrMixin, ConnectionMixin, CommonContentProvider):

	# Disneyplus-style: nepoužívame login_settings_names (blokuje root()
	# pri prázdnych values). Namiesto toho v login() ručne kontrolujeme
	# required settings a voláme show_info() ako disneyplus.
	#
	# login_optional_settings_names = framework zavolá login_data_changed()
	# keď user zmení niektoré z týchto settings (re-login auto trigger).
	login_settings_names = tuple()
	login_optional_settings_names = (
		'host', 'port', 'use_https',
		'username', 'password',
		'http_auth_mode', 'use_ticket_url',
		'profile', 'loading_timeout',
	)

	def __init__(self, *args, **kwargs):
		CommonContentProvider.__init__(self, *args, **kwargs)
		self.tvh = Tvheadend(self)
		self._bouquet_gen = None
		# FIX 0.70.2 (Juraj): flag pre vynútenú EPG injekciu raz za beh
		# pluginu. Po reštarte GUI / Enigma2 sa provider re-inicializuje,
		# takže flag sa resetuje na False → štartová injekcia zbehne znova.
		# Tým je splnené "EPG injekcia vždy po reštarte GUI/E2".
		self._epg_injected_this_boot = False
		# FIX 0.57.0: zaregistrovať log callbacky v sub-moduloch aby ich
		# diagnostiky šli do archivCZSK.log. Python logging.getLogger v
		# plugine NEJDE do archivCZSK.log — framework zachytí len
		# cp.log_info() / cp.log_debug() calls.
		try:
			from . import imdb_lookup as _imdb_mod
			if hasattr(_imdb_mod, 'set_log_callback'):
				_imdb_mod.set_log_callback(self.log_info)
			if hasattr(_imdb_mod, 'set_log_debug_callback'):
				_imdb_mod.set_log_debug_callback(self.log_debug)
		except Exception:
			pass
		try:
			from . import classifier as _cls_mod
			if hasattr(_cls_mod, 'set_log_callback'):
				_cls_mod.set_log_callback(self.log_info)
		except Exception:
			pass

	# ------------------------------------------------------------------
	# login() – volá sa automaticky pri štarte aj po zmene nastavení
	# ------------------------------------------------------------------

	def _player_settings(self):
		return {
			'user-agent': 'VLC/3.0.20 LibVLC/3.0.20',
			'extra-headers': {}
		}

	# ------------------------------------------------------------------
	# root() – hlavná ponuka
	# ------------------------------------------------------------------

	def root(self):
		"""Root menu - kontextové:
		- Nič nakonfigurované       → framework auto-zobrazí info dialog
		                              (login_settings_names check) — root()
		                              sa vôbec nezavolá
		- TVH dočasne nedostupný    → krátka chybová hláška + Retry položka
		                              (FIX 0.48h)
		- TVH login OK              → Live TV + Archive + Settings
		"""
		tvh_ok = self._check_tvh_silent()
		_, tvh_reason, tvh_err = self.get_tvh_state()

		# FIX 0.48h: rozlíšenie stavov.
		# - not_configured (reason): framework rieši cez login_settings_names
		# - unreachable (reason): krátka info hláška + Retry položka
		#   + Settings folder
		if not tvh_ok:
			if tvh_reason == 'unreachable':
				# TVH credentials sú vyplnené ale check_login zlyhal.
				# FIX 0.48i: namiesto modálneho dialógu (ktorý blokoval GUI
				# 3s) pridáme informačné položky priamo do menu — užívateľ
				# uvidí podstatu chyby (multi-line) a vie hneď tlačiť retry.
				# FIX 0.50beta: + user-friendly hint pre typické chyby
				# FIX 0.50beta (iter 3): ak hint matchol, NEUKAZUJ raw error
				# detail — duplicita ktorá mätie užívateľa. Raw error je
				# dostupný cez Settings → "Otestovať TVH login / spojenie".
				self.add_dir(self._("⟳ Retry TVH connection"),
				             cmd=self.action_retry_tvh_root,
				             info_labels={'title': self._("Retry")})
				self.add_dir(self._("TVH temporarily unreachable. "
				                    "Auto-recovery polling in background."),
				             cmd=self.action_retry_tvh_root,
				             info_labels={'title': self._("TVH status")})
				hint = self._guess_tvh_error_hint(tvh_err)
				if hint:
					self.add_dir(hint,
					             cmd=self.settings_menu,
					             info_labels={'title': self._("Open settings")})
				else:
					# Žiadny hint nematchol → ukáž raw multi-line error
					self._render_tvh_error_lines(tvh_err)
				self.add_dir(self._("Settings"),
				             cmd=self.settings_menu,
				             info_labels={'title': self._("Settings")})
				return

			# not_configured (chýbajúce host/user/pass) → framework už zobrazil
			# auto-info dialog cez login_settings_names check, kým sa táto
			# vetva dosiahne. Tu len return — root() ostane prázdny.
			return

		if tvh_ok:
			# FIX 0.52beta (iter 5): vrátený framework default `add_search_dir()`.
			# Predchádzajúci 1-click priamy-keyboard cez action_dvr_search()
			# síce eliminoval medzistránku, ale stratil **história hľadaní**.
			# Framework search_list ukáže:
			#   [Nové hľadanie - lupa]      ← 1 click → keyboard popup
			#   Markíza                     ← predošlé hľadanie, 1 click → priamy search
			#   Doktor Martin               ← bez znovuzadávania
			#   Na noze
			#   ...
			# Default 10 history entries (config 'keep-searches'). Framework
			# spravuje add/remove/edit predošlých — žiadny vlastný kód netreba.
			# Trade-off: 2-click na nový search (lupa → "Nové hľadanie" →
			# keyboard). Pre opakované hľadania (typický use-case) 1-click.
			try:
				dvr_entries_count = len(_get_dvr_finished_cached(self.tvh) or [])
				if dvr_entries_count > 0:
					self.add_search_dir(
						title=self._("Search archive"))
			except Exception as _e:
				try:
					self.log_info('[plugin.tvheadend]add_search_dir failed: %s' % _e)
				except Exception:
					pass

			self.add_dir(self._("Live TV"),
			             cmd=self.live_root,
			             info_labels={'title': self._("Live TV")})
			self.add_dir(self._("Archive"),
			             cmd=self.archive_channels,
			             info_labels={'title': self._("Archive")})

			# FIX 0.49 / 0.49b: Top-level kategórie (Filmy/Seriály/Šport/...)
			# Položka sa pridá len ak je v kategórii aspoň 1 záznam.
			# HTSP: DVR z prefetch cache; ak ešte nedobehol, kategórie
			# pribudnú pri ďalšom otvorení (po dokončení prefetchu / TTL).
			try:
				dvr_for_cats = _get_dvr_finished_cached(self.tvh)
				_, _, _counts, _, _ = _get_classified_dvr(dvr_for_cats)
				for cat_id, label_base in _CAT_LABELS_ORDER:
					n = _counts.get(cat_id, 0)
					if n <= 0:
						continue
					self.add_dir(self._(label_base),
					             info_labels={'title': self._(label_base)},
					             cmd=self.archive_by_category,
					             cat_id=cat_id)
			except Exception as _e:
				try:
					self.log_info('[Tvheadend] root: dvr classify '
					      'failed (skipping categories): %s' % _e)
				except Exception:
					pass

			# FIX 0.52beta: "Posledné sledované" — shortcut k naposledy
			# otvoreným DVR nahrávkam (max 50). Sleduje sa cez play_dvr()
			# hook. Položka sa zobrazí len ak history má aspoň 1 entry.
			# Umiestnenie: za žánrové kategórie a pred Nastavenia
			# (logické miesto pre "rýchly skok do nedávno sledovaného").
			try:
				_wh = _load_watched_history()
				if _wh:
					self.add_dir(self._("Recently watched"),
					             cmd=self.recently_watched,
					             info_labels={'title': self._("Recently watched")})
			except Exception:
				pass
		elif tvh_reason == 'unreachable':
			# FIX 0.48h: ukáž retry ak má užívateľ vyplnené TVH credentials
			# ale práve teraz nejde (transient)
			# FIX 0.50beta: + user-friendly hint
			# FIX 0.50beta (iter 3): hint → skip raw error (čistejšie UI)
			self.add_dir(self._("⟳ Retry TVH connection (currently unreachable)"),
			             cmd=self.action_retry_tvh_root,
			             info_labels={'title': self._("Retry TVH")})
			hint = self._guess_tvh_error_hint(tvh_err)
			if hint:
				self.add_dir(hint,
				             cmd=self.settings_menu,
				             info_labels={'title': self._("Open settings")})
			else:
				self._render_tvh_error_lines(tvh_err)

		# Settings folder vždy prístupný (užívateľ ho potrebuje aj keď TVH zlyhal).
		self.add_dir(self._("Settings"),
		             cmd=self.settings_menu,
		             info_labels={'title': self._("Settings")})
