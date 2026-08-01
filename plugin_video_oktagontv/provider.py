# -*- coding: utf-8 -*-
from tools_archivczsk.contentprovider.provider import CommonContentProvider
from tools_archivczsk.string_utils import _I, clean_html
from tools_archivczsk.http_handler.hls import stream_key_to_hls_url
from tools_archivczsk.http_handler.dash import stream_key_to_dash_url

import re
from time import time, strftime, localtime
from tools_archivczsk.date_utils import iso8601_to_timestamp
from .oktagontv import OktagonTV, error_text
from .oktagon_api import OktagonApi


class OktagonTVContentProvider(CommonContentProvider):
	def __init__(self):
		CommonContentProvider.__init__(self, 'OktagonTV')
		self.login_settings_names = ('username', 'password')
		self.oktagontv = OktagonTV(self)   # Tivio strana - prihlásenie + prehrávanie (getSourceUrl)
		self.api = OktagonApi(self)         # OKTAGON katalóg (api.oktagonmma.com) - verejný
		self.tmp_dir = '/tmp/'

	# ##################################################################################################################

	def login(self, silent):
		self.oktagontv.login()
		return True

	# ##################################################################################################################

	def root(self):
		self.add_search_dir()
		self.add_dir(self._("Live broadcasts / events"), cmd=self.list_catalog, kind='STREAM')
		self.add_dir(self._("Fights"), cmd=self.list_fights)
		self.add_dir(self._("Tournaments"), cmd=self.list_tournaments)
		self.add_dir(self._("Shows"), cmd=self.list_shows)
		self.add_dir(self._("Videos / recordings"), cmd=self.list_catalog, kind='VIDEO')
		self.add_dir(self._("Packages / PPV"), cmd=self.list_catalog, kind='BUNDLE')

	# ##################################################################################################################
	# Vyhľadávanie (OKTAGON nemá verejný vyhľadávací endpoint - filtrujeme na strane doplnku)
	# ##################################################################################################################

	def search(self, keyword, search_id=None):
		try:
			result = self.oktagontv.search(keyword)
		except Exception as e:
			self.log_error("OKTAGON search error: %s" % error_text(e))
			self.show_error(self._("Failed to load catalog from OKTAGON."))
			return

		tags = result.get('tags') or []
		videos = result.get('videos') or []

		if not tags and not videos:
			self.show_info(self._("Nothing found."), noexit=True)
			return

		for it in tags:
			info_labels = {'plot': _strip_html(it.get('plot') or ''), 'title': it['title']}
			self.add_dir(it['title'], it.get('img'), info_labels, cmd=self.list_tag_videos, tag_id=it['id'])

		for it in videos:
			self.add_tivio_video(it)

	# ##################################################################################################################
	# Obrazovky a riadky Tivia (rovnaká štruktúra, akú zobrazuje web)
	# ##################################################################################################################

	def list_screen(self, screen_id, page=0):
		try:
			items = self.oktagontv.get_screen_items(screen_id, page)
		except Exception as e:
			self.log_error("OKTAGON screen %s error: %s" % (screen_id, error_text(e)))
			# záloha: ak Tivio cloud funkcia nie je dostupná, ponúkni aspoň turnaje
			if screen_id == self.oktagontv.client.SCREEN_FIGHTS:
				return self.list_tournaments()
			self.show_error(self._("Failed to load catalog from OKTAGON."))
			return

		self.add_items(items, cmd_next=self.list_screen, next_args={'screen_id': screen_id, 'page': page + 1})

	# ##################################################################################################################

	def list_row(self, row_id, page=0):
		try:
			items = self.oktagontv.get_row_items(row_id, page)
		except Exception as e:
			self.log_error("OKTAGON row %s error: %s" % (row_id, error_text(e)))
			self.show_error(self._("Failed to load catalog from OKTAGON."))
			return

		self.add_items(items, cmd_next=self.list_row, next_args={'row_id': row_id, 'page': page + 1})

	# ##################################################################################################################

	def add_items(self, items, cmd_next=None, next_args=None):
		if not items:
			self.show_info(self._("Nothing here yet."), noexit=True)
			return

		for it in items:
			itype = it.get('type')

			if itype == 'row':
				self.add_dir(it['title'], cmd=self.list_row, row_id=it['id'])
			elif itype in ('tag', 'series'):
				info_labels = {'plot': _strip_html(it.get('plot') or ''), 'title': it['title']}
				self.add_dir(it['title'], it.get('img'), info_labels, cmd=self.list_tag_videos, tag_id=it['id'])
			elif itype == 'video':
				self.add_tivio_video(it)
			elif itype == 'next':
				if cmd_next:
					self.add_next(cmd_next, **(next_args or {}))
			elif itype in ('fav', 'watchlist', 'tvChannel'):
				# nepodporované sekcie (obľúbené / pokračovať v sledovaní / live TV kanály)
				continue
			else:
				self.log_error("Unsupported OKTAGON item type: %s" % itype)

	# ##################################################################################################################
	# Zápasy: to isté, čo web zobrazuje na /sk/fights - riadky obrazovky + najnovšie zápasy
	# ##################################################################################################################

	def list_fights(self):
		# 1) najnovšie zápasy priamo z Firestore (nezávislé na Tivio cloud funkciách)
		self.add_dir(self._("Latest"), cmd=self.list_latest)

		# 2) riadky, ktoré má web na stránke Zápasy (Tivio getRowsInScreen3)
		rows = 0
		try:
			for it in self.oktagontv.get_screen_items(self.oktagontv.client.SCREEN_FIGHTS):
				if it.get('type') == 'row':
					self.add_dir(it['title'], cmd=self.list_row, row_id=it['id'])
					rows += 1
		except Exception as e:
			self.log_error("OKTAGON fights screen error: %s" % error_text(e))

		# 3) záloha: ak sa riadky nenačítali, ponúkni aspoň turnaje (inak je zoznam v Turnajoch)
		if rows == 0:
			self.log_info("Fights screen returned no rows - falling back to tournaments")
			for it in self._tournament_items():
				info_labels = {'plot': _strip_html(it.get('plot') or ''), 'title': it['title']}
				self.add_dir(it['title'], it.get('img'), info_labels, cmd=self.list_tag_videos, tag_id=it['id'])

	# ##################################################################################################################

	def list_latest(self):
		items = self.oktagontv.get_latest_videos()
		if not items:
			self.show_info(self._("Nothing here yet."), noexit=True)
			return

		for it in items:
			self.add_tivio_video(it)

	# ##################################################################################################################
	# Turnaje -> zápasy v turnaji
	# ##################################################################################################################

	def _tournament_items(self):
		try:
			return self.oktagontv.get_tournaments() or []
		except Exception as e:
			self.log_error("OKTAGON tournaments error: %s" % error_text(e))
			return []

	def _add_tournament_items(self, items):
		for it in items:
			info_labels = {'plot': _strip_html(it.get('plot') or ''), 'title': it['title']}
			self.add_dir(it['title'], it.get('img'), info_labels, cmd=self.list_tag_videos, tag_id=it['id'])

	def list_tournaments(self):
		items = self._tournament_items()

		if not items:
			# záloha: turnaje z verejného katalógu (len tie, ktoré ešte len budú)
			self.log_info("Falling back to banners for tournaments")
			return self.list_events()

		groups = _group_tournaments(items)

		# ak vyšla len jedna skupina, nemá zmysel robiť medzikrok
		if len(groups) < 2:
			self._add_tournament_items(sorted(items, key=_tournament_sort_key))
			return

		self.add_dir(self._("All tournaments"), cmd=self.list_tournament_group)
		for key, name, group_items in groups:
			title = '%s (%d)' % (name or self._("Other"), len(group_items))
			self.add_dir(title, cmd=self.list_tournament_group, group=key)

	# ##################################################################################################################

	def list_tournament_group(self, group=None):
		items = self._tournament_items()

		if group:
			selected = []
			for key, name, group_items in _group_tournaments(items):
				if key == group:
					selected = group_items
					break
			items = selected
		else:
			items = sorted(items, key=_tournament_sort_key)

		if not items:
			self.show_info(self._("Nothing here yet."), noexit=True)
			return

		self._add_tournament_items(items)

	# ##################################################################################################################

	def list_events(self):
		try:
			items = self.api.get_banners(types=['STREAM'])
		except Exception as e:
			self.log_error("OKTAGON events error: %s" % error_text(e))
			self.show_error(self._("Failed to load catalog from OKTAGON."))
			return

		events = [it for it in items if it.get('event_tag_id')]
		if not events:
			self.show_info(self._("Nothing here yet."), noexit=True)
			return

		for it in events:
			info_labels = {'plot': _strip_html(it.get('plot') or ''), 'title': it['title']}
			self.add_dir(it['title'], it.get('img'), info_labels, cmd=self.list_tag_videos,
			             tag_id=it['event_tag_id'])

	# ##################################################################################################################

	def list_tag_videos(self, tag_id):
		items = self.oktagontv.get_videos_by_tag(tag_id)
		if not items:
			self.show_info(self._("Nothing here yet."), noexit=True)
			return

		for it in items:
			self.add_tivio_video(it)

	# ##################################################################################################################
	# Pořady (relácie) -> epizódy
	# ##################################################################################################################

	def list_shows(self):
		try:
			shows = self.oktagontv.get_shows()
		except Exception as e:
			self.log_error("OKTAGON shows error: %s" % error_text(e))
			self.show_error(self._("Failed to load catalog from OKTAGON."))
			return

		if not shows:
			self.show_info(self._("Nothing here yet."), noexit=True)
			return

		for s in shows:
			info_labels = {'plot': _strip_html(s.get('plot') or ''), 'title': s['title']}
			self.add_dir(s['title'], s.get('img'), info_labels, cmd=self.list_tag_videos, tag_id=s['id'])

	# ##################################################################################################################

	def add_tivio_video(self, it):
		# položka priamo z Tivia (zápas / epizóda) - prehráva sa cez getSourceUrl
		title = it['title']
		if it.get('playable') == 0:
			title += _I(' *')

		info_labels = {'plot': _strip_html(it.get('plot') or ''), 'title': it['title']}
		if it.get('duration'):
			info_labels['duration'] = it['duration']
		if it.get('year'):
			info_labels['year'] = it['year']

		item = {
			'title': it['title'],
			'type': 'VIDEO',
			'video_source_type': 'TIVIO',
			'video_source': it['id'],
		}
		self.add_video(title, it.get('img'), info_labels, cmd=self.resolve_item, item=item)

	# ##################################################################################################################

	def list_catalog(self, kind):
		# BUNDLE/PASS berieme spolu ako balíčky
		types = ['BUNDLE'] if kind == 'BUNDLE' else [kind]
		try:
			items = self.api.get_banners(types=types)
		except Exception as e:
			self.log_error("OKTAGON catalog error: %s" % error_text(e))
			self.show_error(self._("Failed to load catalog from OKTAGON."))
			return

		if not items:
			self.show_info(self._("Nothing here yet."), noexit=True)
			return

		for it in items:
			self.add_catalog_item(it)

	# ##################################################################################################################

	def add_catalog_item(self, it):
		title = it['title']
		if it.get('subtitle'):
			title += '  {}'.format(_I(it['subtitle']))

		info_labels = {
			'plot': _strip_html(it.get('plot') or ''),
			'title': it['title'],
		}

		# PASS/BUNDLE nie sú prehrateľné video - len info
		if it['type'] in ('BUNDLE', 'PASS') or not it.get('video_source'):
			self.add_video(title, it.get('img'), info_labels, download=False, cmd=self.noop)
			return

		self.add_video(title, it.get('img'), info_labels, cmd=self.resolve_item, item=it)

	# ##################################################################################################################

	def noop(self):
		self.show_info(self._("This item is not directly playable (package/subscription)."), noexit=True)

	# ##################################################################################################################

	def resolve_item(self, item):
		video_source = item.get('video_source')
		if not video_source:
			return

		vst = item.get('video_source_type')

		# Niektoré promo videá sú hostované na YouTube - prehráme cez doplnok plugin.video.yt
		if vst == 'YOUTUBE':
			yt_url = 'https://www.youtube.com/watch?v=' + video_source
			self.call_another_addon('plugin.video.yt', {'url': yt_url, 'title': item['title']}, 'resolve')
			return

		if vst != 'TIVIO':
			self.show_error(self._("Unsupported video source: %s") % vst)
			return

		# Živý prenos pred svojím začiatkom na Tivio CDN ešte neexistuje - manifest vráti HTTP 500.
		# Nemá zmysel to skúšať, radšej rovno povedzme, kedy prenos začína.
		start_ts = _start_timestamp(item)
		if start_ts and start_ts > time() + 60:
			self.show_info(self._("The broadcast hasn't started yet. It starts at %s.") % strftime('%d.%m.%Y %H:%M', localtime(start_ts)), noexit=True)
			return

		# STREAM = živý event, VIDEO = záznam. Tivio documentType:
		# TODO(oktagon): over presný typ z getSourceUrl (video vs tvChannel vs event).
		video_type = 'video'
		live = item.get('type') == 'STREAM'

		# Pri živých eventoch Tivio vracia zakaždým nový zdroj (sessionId) a niekedy taký,
		# ktorý ešte nevysiela (manifest = HTTP 500). Preto to pri live skúsime viackrát.
		attempts = 3 if live else 1
		tried = []

		for attempt in range(attempts):
			try:
				info = self.oktagontv.get_video_source_info(video_source, video_type) or {}
			except Exception as e:
				msg = error_text(e)
				self.log_error("getSourceUrl failed: %s" % msg)
				# napr. budúci event (zatiaľ bez streamu), alebo chýba predplatné/PPV
				self.show_error(msg or self._("This content is not available (subscription/PPV needed)."))
				return

			url = info.get('url')
			self.log_info("OKTAGON stream URL (%d/%d): %s" % (attempt + 1, attempts, url))

			if not url or url in tried:
				# ten istý zdroj, ktorý pred chvíľou nefungoval - netreba znova
				continue

			tried.append(url)

			if live:
				# Živý stream: manifest je dynamický, segmenty majú v URL query parameter
				# (?start=...) a proxy si s tým neporadí - prehrávač skončil na čiernej
				# obrazovke / EOF. Preto ideme priamo do prehrávača (exteplayer3 + ffmpeg
				# zvládne MPEG-DASH sám) a proxy necháme len ako ďalšiu možnosť.
				if not self._manifest_available(url):
					continue

				ret = self.add_live_streams(url, info.get('sourceHistory') or [], item['title'])
			else:
				try:
					ret = self.resolve_streams(url, item['title'])
				except Exception as e:
					self.log_error("Failed to load stream manifest: %s" % error_text(e))
					ret = False

			if ret:
				return ret

		self.show_error(self._("Stream is not available - the broadcast probably hasn't started yet."))
		return False

	# ##################################################################################################################

	def _manifest_available(self, url):
		# rýchla kontrola, či zdroj naozaj vysiela (nefunkčný vracia HTTP 500)
		try:
			response = self.oktagontv.client.req_session.get(url, timeout=10, stream=True)
			status = response.status_code
			response.close()
		except Exception as e:
			self.log_error("Manifest check failed: %s" % error_text(e))
			return False

		if status != 200:
			self.log_error("Manifest not available (HTTP %d): %s" % (status, url))
			return False

		return True

	# ##################################################################################################################

	def add_live_streams(self, url, source_history, title):
		# 1) priamo manifest so session (prehráva od začiatku prenosu)
		self.add_play(title, url, info_labels={'quality': 'auto'}, live=True)

		# 2) živá hrana - ten istý stream bez session a bez ?start (zo sourceHistory)
		for source_url in source_history:
			if source_url and source_url != url:
				self.add_play('%s  %s' % (title, _I(self._("(live edge)"))), source_url,
				              info_labels={'quality': 'auto'}, live=True)

		# 3) a nakoniec cez proxy s výberom kvality (keby priamy manifest nešiel)
		try:
			self.resolve_streams(url, '%s  %s' % (title, _I(self._("(via proxy)"))), True)
		except Exception as e:
			self.log_error("Failed to load stream manifest: %s" % error_text(e))

		return True

	# ##################################################################################################################

	def get_hls_info(self, stream_key):
		return {'url': stream_key['url'], 'bandwidth': stream_key['bandwidth']}

	def get_dash_info(self, stream_key):
		return {'url': stream_key['url'], 'bandwidth': stream_key['bandwidth']}

	# ##################################################################################################################

	def resolve_streams(self, manifest_url, title='', live=False):
		if not manifest_url:
			return False

		if '.m3u8' in manifest_url:
			streams = self.get_hls_streams(manifest_url, self.oktagontv.client.req_session, max_bitrate=self.get_setting('max_bitrate'))
			if not streams:
				return False
			for s in streams:
				url = stream_key_to_hls_url(self.http_endpoint, {'url': s['playlist_url'], 'bandwidth': s['bandwidth']})
				self.add_play(title, url, info_labels={'bandwidth': int(s['bandwidth'])}, live=live)
		else:
			streams = self.get_dash_streams(manifest_url, self.oktagontv.client.req_session, max_bitrate=self.get_setting('max_bitrate'))
			if not streams:
				return False

			for s in streams:
				url = stream_key_to_dash_url(self.http_endpoint, {'url': s['playlist_url'], 'bandwidth': s['bandwidth']})
				info_labels = {'bandwidth': int(s['bandwidth']), 'quality': (s['height'] + 'p') if s.get('height') else '720p'}
				self.add_play(title, url, info_labels=info_labels, live=live)

		return True


# ##################################################################################################################
# Zoskupenie turnajov do sérií (OKTAGON, PML, THE RING, ...) - v Tiviu sú všetky
# v jednom zozname, na prijímači je prehľadnejšie mať ich v samostatných zložkách.
# ##################################################################################################################

OTHER_GROUP_KEY = '__other__'


def _series_name(title):
	# názov série = časť názvu pred prvým číslom ("OKTAGON 45: Štvanice" -> "OKTAGON")
	title = (title or '').strip()
	name = re.split(r'\d', title, 1)[0]
	name = name.strip(' :-–,.')
	return name or title


def _tournament_number(title):
	m = re.search(r'\d+', title or '')
	try:
		return int(m.group(0)) if m else -1
	except Exception:
		return -1


def _tournament_sort_key(item):
	title = item.get('title') or ''
	return (-_tournament_number(title), title.upper())


def _group_tournaments(items):
	"""
	Vráti zoznam trojíc (kľúč, zobrazovaný názov alebo None, položky).
	Kľúč sa posiela ako parameter príkazu, preto musí byť stabilný reťazec.
	"""
	prelim = {}
	display = {}
	for it in items:
		name = _series_name(it.get('title') or '')
		key = name.upper()
		if key not in prelim:
			prelim[key] = []
			display[key] = name
		prelim[key].append(it)

	# "THE RING PRAHA" patrí pod "THE RING" - zlúčime dlhšie názvy pod kratšie
	keys = sorted(prelim.keys(), key=lambda k: (len(k), k))
	groups = {}
	group_display = {}
	for k in keys:
		target = k
		for base in keys:
			if base != k and len(base) < len(k) and k.startswith(base + ' '):
				target = base
				break
		if target not in groups:
			groups[target] = []
			group_display[target] = display[target]
		groups[target].extend(prelim[k])

	ret = []
	singles = []
	for k in groups:
		if len(groups[k]) < 2:
			singles.extend(groups[k])
		else:
			ret.append((k, group_display[k], sorted(groups[k], key=_tournament_sort_key)))

	ret.sort(key=lambda g: (-len(g[2]), g[0]))

	if singles:
		ret.append((OTHER_GROUP_KEY, None, sorted(singles, key=_tournament_sort_key)))

	return ret

# ##################################################################################################################

def _start_timestamp(item):
	# začiatok eventu z katalógu OKTAGONu (napr. "2026-08-01T15:40:00.000Z") ako UTC timestamp
	start_date = item.get('start_date')
	if not start_date:
		return None

	try:
		return iso8601_to_timestamp(start_date, True)
	except Exception:
		return None

# ##################################################################################################################

def _strip_html(text):
	# popisy z OKTAGON API sú HTML - použijeme čistič z frameworku (rieši aj entity a <br>)
	if not text:
		return ''
	try:
		return clean_html(text).strip()
	except Exception:
		return text
