# -*- coding: utf-8 -*-
"""
DvrMixin — DVR archiv: prehliadanie nahravok, kategorizacia, vyhladavanie,
posledne sledovane a prehravanie nahravok (play_dvr).

Vyclenene z provider.py (refaktor 0.80.0, krok 6). Najvacsi blok pluginu.
Mix sa pripaja do TvheadendContentProvider cez dedicnost; self.* sa riesi
za behu, vzajomne volania metod (_add_dvr_entry_item, _dvr_info_labels)
ostavaju v ramci tohto mixu.

Zavislosti: _common (datumy/cache/normalizacia), _watched_history (pozicie
sledovania), classifier (klasifikacia nahravok do kategorii/podzanrov).

Bez zmeny spravania — iba presun metod.

Kompatibilita: Python 2.7 + Python 3.x
"""

from __future__ import absolute_import, unicode_literals, print_function

import time
from datetime import datetime

from ._common import _date_key_from_ts, _get_dvr_finished_cached, _norm_name, _ts
from ._watched_history import (
	_load_watched_history, _get_watched_position, _is_fully_watched, _track_watched,
)
from .classifier import (
	_get_classified_dvr, _strip_accents_lower, _strip_tech_markers,
	_CAT_FILM, _CAT_SERIAL, _MOVIE_SUBCAT_LABELS, _SUBCAT_REGISTRY,
	_SUBTITLE_SERIES_PATTERN, _TITLE_EPISODE_PATTERN,
)


class DvrMixin(object):
	def _dvr_info_labels(self, label_title, entry):
		info = {'title': label_title}
		if not isinstance(entry, dict):
			return info

		def _pick(v):
			if not v:
				return ''
			if isinstance(v, dict):
				for k in ('slk', 'slo', 'cze', 'ces', 'eng'):
					if k in v and v[k]:
						return str(v[k]).strip()
				for _val in v.values():
					if _val:
						return str(_val).strip()
				return ''
			return str(v).strip()

		main = _pick(entry.get('disp_title') or entry.get('title'))
		sub  = _pick(entry.get('disp_subtitle') or entry.get('disp_summary') or entry.get('subtitle') or entry.get('summary'))
		desc = _pick(entry.get('disp_description') or entry.get('description'))

		plot_parts = [p for p in (main, sub, desc) if p]
		if plot_parts:
			info['plot'] = "\n".join(plot_parts)

		try:
			dur = entry.get('duration')
			if dur:
				info['duration'] = int(dur)
			else:
				start = int(entry.get('start_real') or entry.get('start') or 0)
				stop  = int(entry.get('stop_real')  or entry.get('stop')  or 0)
				if start and stop and stop > start:
					info['duration'] = stop - start
		except Exception:
			pass

		return info


	def archive_channels(self):
		if not self._check_tvh_silent():
			# FIX 0.48h: rozlíšenie stavov + retry pri transient
			_, reason, err = self.get_tvh_state()
			if reason == 'unreachable':
				self.add_dir(
					self._("⟳ TVH unreachable — tap to retry"),
					cmd=self.action_retry_tvh_root,
					info_labels={'title': self._("Retry TVH")})
				# FIX 0.48i: zobraz aj underlying error
				self._render_tvh_error_lines(err)
			else:
				self.add_dir(
					self._("✗ Tvheadend server not configured. Open Settings to fill in host, username, password."),
					cmd=self.settings_menu,
					info_labels={'title': self._("TVH not configured")})
			return

		try:
			entries  = _get_dvr_finished_cached(self.tvh)
			channels = self.tvh.get_channels()
		except Exception:
			# FIX 0.48h: namiesto tichého empty → retry
			try:
				self._invalidate_tvh_login_cache()
			except Exception:
				pass
			self.add_dir(self._("⟳ Failed to load archive — tap to retry"),
			             cmd=self.action_retry_tvh_root,
			             info_labels={'title': self._("Retry")})
			return

		ch_info = {}
		for ch in channels:
			cid = ch.get('uuid') or ''
			if not cid:
				continue
			ch_info[cid] = {
				'name':   ch.get('name') or cid,
				'number': int(ch.get('number') or 0),
				'icon':   self.tvh.make_icon_url(ch.get('icon_public_url') or None)
			}

		# Aplikuj limity z nastavení
		try:
			days_limit = int(self.get_setting('archive_days_limit') or 0)
			if days_limit > 0:
				cutoff = time.time() - days_limit * 86400
				entries = [e for e in entries if _ts(e) >= cutoff]
		except Exception:
			pass
		try:
			dvr_limit = int(self.get_setting('dvr_limit') or 0)
			if dvr_limit > 0:
				entries = entries[:dvr_limit]
		except Exception:
			pass

		counts = {}
		days   = {}
		for e in entries:
			cid = e.get('channel') or ''
			if not cid:
				continue
			counts[cid] = counts.get(cid, 0) + 1
			ts = _ts(e)
			if ts > 0:
				days.setdefault(cid, set()).add(_date_key_from_ts(ts))

		items = []
		for cid, cnt in counts.items():
			info    = ch_info.get(cid) or {}
			name    = info.get('name') or cid
			num     = info.get('number', 0)
			icon    = info.get('icon')
			day_cnt = len(days.get(cid) or set())
			items.append((num, _norm_name(name), cid, name, icon, cnt, day_cnt))

		items.sort(key=lambda x: (x[0] if x[0] > 0 else 999999, x[1]))

		for num, _, cid, name, icon, cnt, day_cnt in items:
			# FIX 0.48h: zobrazuj len názov kanála bez čísla v zátvorke.
			# Poradie zoznamu sa naďalej riadi `num` (sort key vyššie),
			# len label sa neformátuje s "(num)". Day count ('- 8 dní') ostáva.
			label = name
			if day_cnt > 0:
				label = '%s - %d %s' % (label, day_cnt, self._('days'))
			self.add_dir(
				label, img=icon, info_labels={'title': label},
				cmd=self.archive_dates, channel_id=cid, channel_name=name
			)


	def recently_watched(self):
		"""FIX 0.52beta: Render zoznamu posledne sledovaných DVR nahrávok.

		Načíta `_load_watched_history()` (JSON v data dir-u, persistent
		cez reboot E2), pre každý UUID hľadá aktuálnu DVR entry v cache.
		Ak entry už neexistuje (user ju vymazal v TVH), preskočí ju.
		Zoradenie podľa naposledy otvoreného (ts desc).

		Plus pridáva kontextové menu "Vymazať históriu" na vyčistenie
		zoznamu (cez ArchivCZSK menu/INFO tlačidlo... ale to je nice-to-have,
		pre teraz necháme bez clear akcie — user môže zmazať data dir
		manuálne ak chce reset).
		"""
		if not self._check_tvh_silent():
			_, reason, err = self.get_tvh_state()
			if reason == 'unreachable':
				self.add_dir(
					self._("⟳ TVH unreachable — tap to retry"),
					cmd=self.action_retry_tvh_root,
					info_labels={'title': self._("Retry TVH")})
				hint = self._guess_tvh_error_hint(err)
				if hint:
					self.add_dir(hint,
					             cmd=self.settings_menu,
					             info_labels={'title': self._("Open settings")})
			return

		history = _load_watched_history()
		if not history:
			self.add_dir(
				self._("History is empty — start watching something."),
				info_labels={'title': self._("Recently watched")})
			return

		try:
			entries = _get_dvr_finished_cached(self.tvh) or []
		except Exception:
			self.add_dir(
				self._("⟳ Failed to load archive — tap to retry"),
				cmd=self.action_retry_tvh_root,
				info_labels={'title': self._("Retry")})
			return

		# Index aktuálnych entries cez UUID pre rýchly lookup
		by_uuid = {}
		for e in entries:
			uuid = e.get('uuid')
			if uuid:
				by_uuid[uuid] = e

		# Sortuj history podľa naposledy otvoreného (ts desc)
		sorted_history = sorted(history.items(),
		                        key=lambda kv: kv[1].get('ts', 0),
		                        reverse=True)

		shown = 0
		stale = 0
		for uuid, hist_entry in sorted_history:
			fresh_entry = by_uuid.get(uuid)
			if fresh_entry is None:
				# Entry bola vymazaná z TVH archívu — preskoč.
				# Mohli by sme ju aj odstrániť z history JSON, ale
				# uložené dáta sú malé a možno sa entry obnoví neskôr.
				stale += 1
				continue
			# Render rovnaký formát ako iné DVR menu (0.55beta:
			# show_resume=True pridá " (▶ MM:SS)" sufix ak entry má
			# zaznamenanú resume pozíciu).
			self._add_dvr_entry_item(fresh_entry, episode_format=False,
			                          show_resume=True)
			shown += 1

		if shown == 0:
			# Všetky entries v history boli medzitým zmazané z TVH
			self.add_dir(
				self._("Watched entries no longer exist in DVR archive."),
				info_labels={'title': self._("Recently watched")})


	def search(self, keyword=None, search_id=''):
		"""FIX 0.52beta: Vyhľadávanie v DVR archíve podľa názvu (bez diakritiky).

		ArchivCZSK framework volá túto metódu po tom, čo používateľ klikol
		na položku pridanú cez `add_search_dir()` a zadal text v keyboard
		popup-e. Signature `(keyword, search_id)` musí presne sedieť — inak
		framework hodí TypeError.

		Match je case-insensitive a diacritic-insensitive — typing 'Na noze'
		nájde 'Na nože', 'Markiza' nájde 'Markíza', atď. Pomocná funkcia
		_strip_accents_lower (modul-level) normalizuje text cez NFD + Mn
		filter, rovnaký mechanizmus ako pri klasifikácii DVR entries.

		Match scope: 'disp_title' + 'disp_subtitle'. Description sa
		nematchuje aby sa user nedostal k záplave false-positive výsledkov.

		Deduplikácia kľúčom (title, subtitle[:80]) — 7×24 autorec
		duplikáty sa zoskupia do jedného výsledku.

		Limit: 200 výsledkov (UI by sa pri tisíckach položiek stalo
		nepoužiteľným). Pri overflow sa zobrazí info že je limit.
		"""
		if not self._check_tvh_silent():
			_, reason, err = self.get_tvh_state()
			if reason == 'unreachable':
				self.add_dir(
					self._("⟳ TVH unreachable — tap to retry"),
					cmd=self.action_retry_tvh_root,
					info_labels={'title': self._("Retry TVH")})
				hint = self._guess_tvh_error_hint(err)
				if hint:
					self.add_dir(hint,
					             cmd=self.settings_menu,
					             info_labels={'title': self._("Open settings")})
			else:
				self.add_dir(
					self._("✗ Tvheadend server not configured."),
					cmd=self.settings_menu,
					info_labels={'title': self._("TVH not configured")})
			return

		if not keyword or len(keyword.strip()) < 2:
			self.add_dir(
				self._("Please type at least 2 characters."),
				info_labels={'title': self._("Search")})
			return

		query = _strip_accents_lower(keyword.strip())

		try:
			entries = _get_dvr_finished_cached(self.tvh) or []
		except Exception:
			try:
				self._invalidate_tvh_login_cache()
			except Exception:
				pass
			self.add_dir(
				self._("⟳ Failed to load archive — tap to retry"),
				cmd=self.action_retry_tvh_root,
				info_labels={'title': self._("Retry")})
			return

		# Match + dedup + collect timestamps pre triedenie podľa recency
		seen = set()
		matches = []
		for e in entries:
			title = e.get('disp_title') or ''
			subtitle = e.get('disp_subtitle') or ''
			if not title and not subtitle:
				continue
			norm_t = _strip_accents_lower(title)
			norm_s = _strip_accents_lower(subtitle)
			if query not in norm_t and query not in norm_s:
				continue
			# Dedup kľúč (rovnaký ako _get_classified_dvr 7x24 dedup)
			key = (norm_t, norm_s[:80])
			if key in seen:
				continue
			seen.add(key)
			matches.append(e)

		if not matches:
			self.add_dir(
				self._("✗ Nothing found for: %s") % keyword,
				info_labels={
					'title': self._("Search"),
					'plot': self._("Try a shorter or simpler query. "
					               "Diacritics are ignored.")
				})
			return

		# Sortuj podľa najnovších záznamov (start_real desc)
		matches.sort(key=lambda e: e.get('start_real') or 0, reverse=True)

		# Limit + info ak je overflow
		LIMIT = 200
		total = len(matches)
		if total > LIMIT:
			self.add_dir(
				self._("Found %d results — showing first %d (most recent). "
				       "Refine the search for fewer results.") % (total, LIMIT),
				info_labels={'title': self._("Search")})
			matches = matches[:LIMIT]
		else:
			self.add_dir(
				self._("Found %d result(s) for: %s") % (total, keyword),
				info_labels={'title': self._("Search")})

		# Render results — rovnaký formát ako iné DVR menu (date · sub · channel)
		for e in matches:
			self._add_dvr_entry_item(e, episode_format=False)


	def archive_dates(self, channel_id, channel_name=None):
		# FIX 0.50beta: namiesto tichého empty zoznamu pri TVH transient
		# failure ukáž retry položku (paralela s archive_channels).
		# Predtým: user klikne na kanál v Archíve, TVH momentálne nedostupný,
		# zobrazí sa len ".." (parent) — vyzeralo to ako prázdny archív.
		if not self._check_tvh_silent():
			_, reason, err = self.get_tvh_state()
			if reason == 'unreachable':
				self.add_dir(self._("⟳ TVH unreachable — tap to retry"),
				             cmd=self.action_retry_tvh_root,
				             info_labels={'title': self._("Retry TVH")})
				self._render_tvh_error_lines(err)
			return

		try:
			entries = _get_dvr_finished_cached(self.tvh)
		except Exception:
			# FIX 0.50beta: tiež retry namiesto tichého empty
			try:
				self._invalidate_tvh_login_cache()
			except Exception:
				pass
			self.add_dir(self._("⟳ Failed to load archive — tap to retry"),
			             cmd=self.action_retry_tvh_root,
			             info_labels={'title': self._("Retry")})
			return

		entries = [e for e in entries if (e.get('channel') or '') == channel_id]

		by_date = {}
		for e in entries:
			ts = _ts(e)
			if ts <= 0:
				continue
			d = _date_key_from_ts(ts)
			by_date.setdefault(d, []).append(e)

		for d in sorted(by_date.keys(), reverse=True):
			cnt   = len(by_date[d])
			label = '%s (%d)' % (d, cnt)
			self.add_dir(label, info_labels={'title': label}, cmd=self.archive_day, channel_id=channel_id, date=d)


	def archive_day(self, channel_id, date):
		if not self._check_tvh_silent():
			return

		try:
			entries = _get_dvr_finished_cached(self.tvh)
		except Exception:
			return

		entries = [e for e in entries if (e.get('channel') or '') == channel_id]
		day = [e for e in entries if _ts(e) > 0 and _date_key_from_ts(_ts(e)) == date]
		day.sort(key=_ts, reverse=True)

		for e in day:
			title = e.get('disp_title') or e.get('title') or self._("Recording")
			ts    = _ts(e)
			tstr  = datetime.fromtimestamp(ts).strftime('%H:%M') if ts > 0 else ''
			label = '%s %s' % (tstr, title) if tstr else title
			label = self._append_watch_markers(label, e.get('uuid'), show_resume=True)
			icon  = self.tvh.make_icon_url(e.get('channel_icon') or None)
			self.add_video(
				label, img=icon, info_labels=self._dvr_info_labels(label, e),
				cmd=self.play_dvr, entry=e, download=False
			)

	# ------------------------------------------------------------------
	# FIX 0.49 (+0.49b): Top-level kategorizácia DVR
	# ------------------------------------------------------------------

	def archive_by_category(self, cat_id):
		"""Top-level otvorenie kategórie.

		FIX 0.49b:
		- Pre Filmy a Seriály ukáže najprv podžánre (Drama/Sci-fi/Komédia/...)
		- Pre ostatné kategórie priamo plochý zoznam záznamov
		- Pre Seriály v "Iné" zachová pôvodné správanie (zoznam sérií)
		"""
		if not self._check_tvh_silent():
			# FIX 0.48h: rozlíšenie stavov + retry pri transient
			_, reason, err = self.get_tvh_state()
			if reason == 'unreachable':
				self.add_dir(
					self._("⟳ TVH unreachable — tap to retry"),
					cmd=self.action_retry_tvh_root,
					info_labels={'title': self._("Retry TVH")})
				self._render_tvh_error_lines(err)
			else:
				self.add_dir(
					self._("✗ Tvheadend server not configured."),
					cmd=self.settings_menu,
					info_labels={'title': self._("TVH not configured")})
			return

		try:
			by_top, by_subcat, _counts, series_by_canonical, series_subcat_titles \
				= _get_classified_dvr(_get_dvr_finished_cached(self.tvh))
		except Exception:
			try:
				self._invalidate_tvh_login_cache()
			except Exception:
				pass
			self.add_dir(self._("⟳ Failed to load archive — tap to retry"),
			             cmd=self.action_retry_tvh_root,
			             info_labels={'title': self._("Retry")})
			return

		# Filmy → ukáž podžánre
		if cat_id == _CAT_FILM:
			for sub_id, sub_label in _MOVIE_SUBCAT_LABELS:
				entries = by_subcat.get((_CAT_FILM, sub_id), [])
				if not entries:
					continue
				self.add_dir(self._(sub_label),
				             info_labels={'title': self._(sub_label)},
				             cmd=self.archive_movie_subgenre,
				             sub_id=sub_id)
			return

		# Seriály → ukáž podžánre seriálov
		if cat_id == _CAT_SERIAL:
			for sub_id, sub_label in _MOVIE_SUBCAT_LABELS:
				titles = series_subcat_titles.get((_CAT_SERIAL, sub_id))
				if not titles:
					continue
				self.add_dir(self._(sub_label),
				             info_labels={'title': self._(sub_label)},
				             cmd=self.archive_series_subgenre,
				             sub_id=sub_id)
			return

		# FIX 0.49c/d: Ostatné kategórie s podžánrami cez registry
		# (Šport, Spravodajstvo, Šou, Detské, Hudba, Umenie, Dokumenty, Hobby)
		cfg = _SUBCAT_REGISTRY.get(cat_id)
		if cfg and cfg[1] is not None:
			labels = cfg[0]
			for sub_id, sub_label in labels:
				entries = by_subcat.get((cat_id, sub_id), [])
				if not entries:
					continue
				self.add_dir(self._(sub_label),
				             info_labels={'title': self._(sub_label)},
				             cmd=self.archive_generic_subgenre,
				             top_cat=cat_id, sub_id=sub_id)
			return

		# Kategórie bez podžánrov (napr. Nezaradené) — plochý zoznam
		entries = by_top.get(cat_id) or []
		for e in entries:
			self._add_dvr_entry_item(e)


	def archive_movie_subgenre(self, sub_id):
		"""FIX 0.49b: Plochý zoznam filmov v sub-žánre (napr. Filmy → Akčné)."""
		if not self._check_tvh_silent():
			return

		try:
			_by_top, by_subcat, _, _, _ = _get_classified_dvr(_get_dvr_finished_cached(self.tvh))
		except Exception:
			self.add_dir(self._("⟳ Failed to load — tap to retry"),
			             cmd=self.action_retry_tvh_root,
			             info_labels={'title': self._("Retry")})
			return

		entries = by_subcat.get((_CAT_FILM, sub_id)) or []
		for e in entries:
			self._add_dvr_entry_item(e)


	def archive_generic_subgenre(self, top_cat, sub_id):
		"""FIX 0.49d: Generická metóda na zobrazenie záznamov v podžánre
		ktoréhokoľvek top kategórie (Spravodajstvo, Šou, Detské, Hudba,
		Umenie, Dokumenty, Hobby, aj Šport).

		Pre Filmy a Seriály ostávajú samostatné metódy (archive_movie_subgenre,
		archive_series_subgenre) lebo Seriály ukazujú zoznam titulov nie
		zoznam epizód.
		"""
		if not self._check_tvh_silent():
			return

		try:
			_by_top, by_subcat, _, _, _ = _get_classified_dvr(_get_dvr_finished_cached(self.tvh))
		except Exception:
			self.add_dir(self._("⟳ Failed to load — tap to retry"),
			             cmd=self.action_retry_tvh_root,
			             info_labels={'title': self._("Retry")})
			return

		entries = by_subcat.get((top_cat, sub_id)) or []
		for e in entries:
			self._add_dvr_entry_item(e)


	def archive_series_subgenre(self, sub_id):
		"""FIX 0.49b: Zoznam sérií v rámci sub-žánru (napr. Seriály → Krimi).

		Po kliku na sériu sa otvorí zoznam jej epizód.
		"""
		if not self._check_tvh_silent():
			return

		try:
			_, _, _, series_by_canonical, series_subcat_titles \
				= _get_classified_dvr(_get_dvr_finished_cached(self.tvh))
		except Exception:
			self.add_dir(self._("⟳ Failed to load — tap to retry"),
			             cmd=self.action_retry_tvh_root,
			             info_labels={'title': self._("Retry")})
			return

		titles = series_subcat_titles.get((_CAT_SERIAL, sub_id)) or set()
		# Sort: najnovšia epizoda desc
		sorted_titles = sorted(
			titles,
			key=lambda t: _ts(series_by_canonical[t][0])
			              if series_by_canonical.get(t) else 0,
			reverse=True
		)
		for title in sorted_titles:
			eps = series_by_canonical.get(title) or []
			# Ikona z najnovšej epizódy
			icon = None
			if eps:
				try:
					icon = self.tvh.make_icon_url(
						eps[0].get('channel_icon') or None)
				except Exception:
					pass
			# FIX 0.49b: bez počtu epizód v zátvorke
			self.add_dir(title, img=icon,
			             info_labels={'title': title},
			             cmd=self.archive_series,
			             series_title=title)


	def archive_series(self, series_title):
		"""Zobrazí epizódy konkrétneho seriálu, najnovšie prvé.

		FIX 0.49b: series_title je teraz canonical title (bez "(N)" sufixu).
		"""
		if not self._check_tvh_silent():
			return

		try:
			_, _, _, series_by_canonical, _ = _get_classified_dvr(_get_dvr_finished_cached(self.tvh))
		except Exception:
			self.add_dir(self._("⟳ Failed to load — tap to retry"),
			             cmd=self.action_retry_tvh_root,
			             info_labels={'title': self._("Retry")})
			return

		eps = series_by_canonical.get(series_title) or []
		if not eps:
			self.add_dir(self._("(no episodes found)"),
			             cmd=self.root,
			             info_labels={'title': series_title})
			return

		for e in eps:
			self._add_dvr_entry_item(e, episode_format=True)


	def _append_watch_markers(self, label, uuid, show_resume=True):
		"""Pripojí markery na koniec labelu: ' *' ak je záznam pozretý (≥80 %)
		a/alebo ' (▶ M:SS)' resume pozíciu (ak je ≥30 s). Používané všetkými
		DVR listingmi. Diakritika-safe ASCII, bez Unicode glyph závislostí.
		"""
		try:
			if _is_fully_watched(uuid):
				label = label + ' *'
		except Exception:
			pass
		if show_resume:
			try:
				pos, _dur = _get_watched_position(uuid)
				if pos and pos >= 30:  # pod 30 s nemá zmysel zobrazovať
					mins = int(pos) // 60
					secs = int(pos) % 60
					if mins >= 60:
						hrs = mins // 60
						mins = mins % 60
						label = label + ' (▶ %d:%02d:%02d)' % (hrs, mins, secs)
					else:
						label = label + ' (▶ %d:%02d)' % (mins, secs)
			except Exception:
				pass
		return label

	def _add_dvr_entry_item(self, e, episode_format=False, show_resume=True):
		"""Pomocný helper — pridá jednu položku DVR záznamu do menu.

		Spoločný kód pre archive_by_category, archive_movie_subgenre,
		archive_series. episode_format=True dáva inu form-u labelu
		(prefer X/Y vs full title).

		FIX 0.55beta: show_resume=True pridá "▶ MM:SS" sufix za labelom
		ak entry má zaznamenanú resume pozíciu. Default False — používa
		sa len v recently_watched() aby user vedel kde môže pokračovať.
		"""
		title = e.get('disp_title') or e.get('title') or self._("Recording")
		sub = (e.get('disp_subtitle') or '').strip()
		ts = _ts(e)
		dstr = datetime.fromtimestamp(ts).strftime('%d.%m. %H:%M') if ts > 0 else ''
		ch = e.get('channelname') or ''

		if episode_format:
			# Vnútri konkrétneho seriálu: názov seriálu je v hlavičke a popis
			# epizódy v info paneli vpravo, takže v riadku stačí identifikátor
			# epizódy (séria/časť) + dátum + kanál (nech je vidno odkiaľ je).
			# (FIX 0.80.0 — bez dlhého názvu epizódy, ktorý sa aj tak odrezával.)
			m = _SUBTITLE_SERIES_PATTERN.match(sub)
			if m:
				ep_id = sub[:m.end()].strip()
			else:
				clean_title = _strip_tech_markers(title)
				m2 = _TITLE_EPISODE_PATTERN.search(clean_title)
				ep_id = ''
				if m2:
					cand = m2.group(1)
					try:
						if not (1900 <= int(cand) <= 2099):  # nie je rok
							ep_id = '(%s)' % cand
					except ValueError:
						ep_id = '(%s)' % cand
			parts = [p for p in (ep_id, dstr, ch) if p]
			label = ' · '.join(parts) if parts else title
		else:
			# Vonku (Filmy, Dokumenty, atď.) — "datum · title · channel"
			parts = [p for p in (dstr, title, ch) if p]
			label = ' · '.join(parts)

		# Markery (hviezdička pozreté + resume pozícia) na koniec labelu.
		label = self._append_watch_markers(label, e.get('uuid'), show_resume=show_resume)
		icon = self.tvh.make_icon_url(e.get('channel_icon') or None)
		self.add_video(
			label, img=icon,
			info_labels=self._dvr_info_labels(label, e),
			cmd=self.play_dvr, entry=e, download=False
		)


	def play_dvr(self, entry):
		if not self._check_tvh_silent():
			return

		# FIX 0.52beta: track open into watched history (root menu shortcut)
		try:
			_track_watched(entry)
		except Exception:
			pass

		url   = self.tvh.make_dvr_url(entry.get('url') or '')
		title = entry.get('disp_title') or entry.get('channelname') or self._("DVR")

		# FIX 0.55beta: resume playback od poslednej pozície (sosáč-style).
		# Ak má entry zaznamenanú position z predošlého stop event-u a
		# user má toggle 'save_last_play_pos' zapnutý (default ON), pošli
		# resume_time_sec do framework streamer-a — ArchivCZSK ho prevezme
		# z settings a streamer začne od tej sekundy.
		#
		# Nepokračuj ak position == 0 (entry už dokončená / auto-cleared
		# nad 95 %) alebo ak position je menšia ako 30s (príliš začiatok,
		# nemá zmysel resume-ovať pár sekúnd).
		settings = self._player_settings() or {}
		# 0.72.0: ak je in-app prehrávač = DVB (inapp_player=1), prehraj aj DVR
		# nahrávku cez native DVB (typ 1) → DVB titulky aj v archíve. Nahrávky
		# z TVH sú MPEG-TS s DVB sub streammi, takže typ 1 ich vie vykresliť.
		try:
			if str(self.get_setting('inapp_player')).strip() == '1':
				settings['forced_player'] = 1   # int! (viz play_live)
		except Exception:
			pass
		try:
			save_resume = self.get_setting('save_last_play_pos')
			save_resume = bool(save_resume) if isinstance(save_resume, bool) \
				else str(save_resume).strip().lower() in ('true', '1', 'yes')
		except Exception:
			save_resume = True  # default ON

		if save_resume:
			try:
				pos, _dur = _get_watched_position(entry.get('uuid'))
				if pos and pos >= 30:
					settings['resume_time_sec'] = int(pos)
			except Exception:
				pass

		# 0.55beta: send data_item=entry so stats() callback (volaný
		# frameworkom pri end/next playback eventu) môže correlate
		# position s konkrétnou DVR entry.
		# HTSP aj HTTP mód: DVR ide priamo cez TVH HTTP (dvrfile/<idStr>),
		# je to hotový súbor → download=True (rovnaké správanie).
		self.add_play(
			title, url,
			info_labels={'title': title},
			data_item=entry,
			settings=settings,
			live=False,
			download=True,
		)
