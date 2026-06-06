# -*- coding: utf-8 -*-
"""
LiveMixin — zive vysielanie (TV + radio) a prehravanie kanalov.

Vyclenene z provider.py (refaktor 0.80.0, krok 4). Mix sa pripaja do
TvheadendContentProvider cez dedicnost; odkazy na self.* sa riesia za behu.

Bez zmeny spravania — iba presun metod.

Kompatibilita: Python 2.7 + Python 3.x
"""

from __future__ import absolute_import, unicode_literals, print_function


from tools_archivczsk.contentprovider.exception import AddonErrorException
from tools_archivczsk.string_utils import _I
from ._common import _norm_name, _tag_sort_key


class LiveMixin(object):
	def live_root(self):
		if not self._check_tvh_silent():
			# FIX 0.48h: rozlíšenie stavov + retry položka pri transient failure
			_, reason, err = self.get_tvh_state()
			if reason == 'unreachable':
				self.add_dir(
					self._("⟳ TVH temporarily unreachable — tap to retry"),
					cmd=self.action_retry_tvh_root,
					info_labels={'title': self._("Retry TVH")})
				# FIX 0.50beta: hint → skip raw error (čistejšie UI)
				hint = self._guess_tvh_error_hint(err)
				if hint:
					self.add_dir(hint,
					             cmd=self.settings_menu,
					             info_labels={'title': self._("Open settings")})
				else:
					# FIX 0.48i: full multi-line error namiesto len err[:80]
					self._render_tvh_error_lines(err)
			else:
				self.add_dir(
					self._("✗ Tvheadend server not configured. Open Settings to fill in host, username, password."),
					cmd=self.settings_menu,
					info_labels={'title': self._("TVH not configured")})
			return

		self.add_dir(self._("All"), cmd=self.live_channels, cat_id='')

		try:
			tags = self.tvh.get_tags()
		except Exception:
			# FIX 0.48h: nezostať s len "All" tichom — invaliduj cache (lebo
			# get_tags zlyhalo aj keď check_login pred chvíľou OK) a ponúkni retry
			try:
				self._invalidate_tvh_login_cache()
			except Exception:
				pass
			self.add_dir(self._("⟳ Failed to load categories — tap to retry"),
			             cmd=self.action_retry_tvh_root,
			             info_labels={'title': self._("Retry")})
			return

		tags = sorted(tags, key=lambda t: (_tag_sort_key(t), _norm_name(t.get('name') or '')))

		for t in tags:
			name = t.get('name') or ''
			uuid = t.get('uuid') or ''
			if not uuid:
				continue
			self.add_dir(name, cmd=self.live_channels, cat_id=uuid)


	def live_channels(self, cat_id=''):
		if not self._check_tvh_silent():
			# FIX 0.48h: namiesto tichého prázdneho zoznamu ponúkni retry
			_, reason, err = self.get_tvh_state()
			if reason == 'unreachable':
				self.add_dir(
					self._("⟳ TVH unreachable — tap to retry"),
					cmd=self.action_retry_tvh_root,
					info_labels={'title': self._("Retry TVH")})
				# FIX 0.48i: zobraz aj underlying error pre diagnostiku
				self._render_tvh_error_lines(err)
			return

		try:
			channels = self.tvh.get_channels_by_tag(cat_id) if cat_id else self.tvh.get_channels()
		except Exception:
			# FIX 0.48h: rovnaký pattern — invaliduj cache + ponúkni retry
			try:
				self._invalidate_tvh_login_cache()
			except Exception:
				pass
			self.add_dir(self._("⟳ Failed to load channels — tap to retry"),
			             cmd=self.action_retry_tvh_root,
			             info_labels={'title': self._("Retry")})
			return

		def _num(x):
			try:
				return int(x.get('number') or 0)
			except Exception:
				return 0

		channels = sorted(channels, key=_num)

		epgnow = None
		try:
			epgnow = self.tvh.get_epg_now(limit=5000)
		except Exception:
			pass

		for ch in channels:
			ch_uuid      = ch.get('uuid') or ''
			channel_name = ch.get('name') or ch_uuid
			if not ch_uuid:
				continue

			service_uuid = ''
			try:
				services = ch.get('services') or []
				if services:
					service_uuid = services[0]
			except Exception:
				pass

			icon  = self.tvh.make_icon_url(ch.get('icon_public_url') or None)
			event = epgnow.get(ch_uuid) if isinstance(epgnow, dict) else None
			info  = self._live_info_labels(channel_name, event)

			# EPG titul vedľa názvu kanála (štýl iVysilani)
			display_title = channel_name
			try:
				epg_title = (event.get('title') if isinstance(event, dict) else None) or ''
				if isinstance(epg_title, dict):
					epg_title = next(iter(epg_title.values()), '') if epg_title else ''
				epg_title = str(epg_title).strip()
				if epg_title:
					display_title += _I('  (' + epg_title + ')')
			except Exception:
				pass

			self.add_video(
				display_title,
				img=icon,
				info_labels=info,
				cmd=self.play_live,
				channel_uuid=ch_uuid,
				service_uuid=service_uuid,
				channel_title=channel_name,
				download=False
			)


	def play_live(self, channel_uuid, service_uuid='', channel_title=None):
		if not self._check_tvh_silent():
			return

		url = self.tvh.make_live_stream_url(
			channel_uuid=channel_uuid,
			service_uuid=(service_uuid or None),
			channel_title=(channel_title or '')
		)

		play_title = channel_title or self._("Live stream")
		if not channel_title and service_uuid:
			try:
				ch_name = self.tvh.get_channel_name_by_service_uuid(service_uuid)
				if ch_name:
					play_title = ch_name
			except Exception:
				pass

		# 0.72.0: ak je in-app prehrávač = DVB (inapp_player=1), vynúť native
		# DVB player (forced_player=1) → HW demux → DVB titulky. "Default" (0)
		# necháva frameworkový prehrávač. DVB vyžaduje BASIC auth na serveri.
		play_settings = self._player_settings()
		try:
			if str(self.get_setting('inapp_player')).strip() == '1':
				play_settings = dict(play_settings)
				play_settings['forced_player'] = 1   # int! framework robi eServiceReference(stype,...)
				self.log_info('[Tvheadend] live in-app: forced_player=1 '
				              '(native DVB, DVB subtitles)')
		except Exception:
			pass

		self.add_play(
			play_title, url,
			info_labels={'title': play_title},
			settings=play_settings,
			live=True,
			download=False
		)

	# ------------------------------------------------------------------
	# ARCHÍV (DVR)
	# ------------------------------------------------------------------


	def _live_info_labels(self, channel_title, event):
		info = {'title': channel_title}
		if not event:
			return info
		epgt = event.get('title') or ''
		sub  = event.get('subtitle') or event.get('summary') or ''
		desc = event.get('description') or ''
		plot_parts = [p for p in (epgt, sub, desc) if p]
		if plot_parts:
			info['plot'] = "\n".join(plot_parts)
		try:
			info['duration'] = int(event.get('stop', 0)) - int(event.get('start', 0))
		except Exception:
			pass
		return info


	def get_url_by_channel_key(self, channel_uuid):
		# FIX 0.48: light-weight login namiesto plného login(silent=True).
		# Plný login zbytočne spúšťa cleanup, picon worker, bouquet refresh
		# check — to všetko pri každom playback-u.
		#
		# FIX 0.57.0 (skyjet PR #22 review #10): vstup je teraz plain
		# channel UUID (framework PlayliveTVHTTPRequestHandler.decode_channel_key()
		# robí base64 decode v handler-i). Predtým bol vstup base64-encoded
		# key a metoda robila vlastný decode block.
		if not self._quick_login_for_http_handler():
			# TVH momentálne neodpovedá → zatvor HTTP handler s 404
			raise AddonErrorException('Tvheadend not reachable')

		channel_uuid = (channel_uuid or '').strip()
		if not channel_uuid:
			raise AddonErrorException('Missing or empty channel uuid')

		service_uuid = None
		try:
			for ch in self.tvh.get_channels():
				if (ch.get('uuid') or '') == channel_uuid:
					services = ch.get('services') or []
					if services:
						service_uuid = services[0]
					break
		except Exception:
			pass

		return self.tvh.make_live_stream_url(
			channel_uuid=channel_uuid,
			service_uuid=service_uuid,
			channel_title=None
		)
