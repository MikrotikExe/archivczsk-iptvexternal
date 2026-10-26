# -*- coding: utf-8 -*-
#
# PlayLive.net pre ArchivCZSK.
#
# Katalóg (offer.json) je verejný, takže prehliadanie ponuky funguje bez prihlásenia.
# Pre prehrávanie platených prenosov
# je potrebné prihlásenie cez kód (Smart TV device flow) - na playlive.net/logcode
# sa vygeneruje 6-miestny kód, ktorý sa zadá v addone. Bez captchy.
# Upútavky sú dostupné bez prihlásenia (HLS).

from tools_archivczsk.contentprovider.provider import CommonContentProvider
from tools_archivczsk.string_utils import _I, clean_html
from tools_archivczsk.http_handler.hls import stream_key_to_hls_url
from tools_archivczsk.http_handler.dash import stream_key_to_dash_url

import re
from time import time, strftime, localtime

from .playlive_api import PlayLiveApi, error_text

PAGE_SIZE = 50


def _strip_html(text):
	if not text:
		return ''
	text = re.sub(r'<br\s*/?>', '\n', text)
	text = re.sub(r'</(p|li|ul|div)>', '\n', text)
	text = re.sub(r'<li>', u'• ', text)
	try:
		text = clean_html(text)
	except Exception:
		text = re.sub(r'<[^>]+>', '', text)
	text = re.sub(r'\n{3,}', '\n\n', text)
	return text.strip()


class PlayLiveContentProvider(CommonContentProvider):
	def __init__(self):
		CommonContentProvider.__init__(self, 'PlayLive')
		self.api = PlayLiveApi(self)
		self.tmp_dir = '/tmp/'
		self._login_session = None  # session z prihlásenia cez kód (cache na jedno spustenie)

	# ##################################################################################################################

	def root(self):
		self.add_search_dir()

		session = self._session()
		if session:
			self.add_dir(self._("My account"), cmd=self.list_account)
			# odhlásenie ako priečinok v hlavnom menu - refresh_screen(parent=True) potom obnoví toto menu
			# (ako video položka vnorená v "Môj účet" obnovovala zlý zoznam a enigma2 padla)
			self.add_dir(_I(self._("Sign out")), cmd=self.sign_out)
		else:
			# Prihlásenie cez TV kód - bez captchy, netreba prehliadač
			self.add_dir(_I(self._("Sign in with a code")), cmd=self.tv_code_login)

		self.add_dir(self._("Live now"), cmd=self.list_items, kind='now')
		self.add_dir(self._("Upcoming events"), cmd=self.list_items, kind='upcoming')
		self.add_dir(self._("Past events"), cmd=self.list_items, kind='live_archive')
		self.add_dir(self._("Videos / VOD"), cmd=self.list_items, kind='vod')
		self.add_dir(self._("Whole offer"), cmd=self.list_items, kind='all')

	# ##################################################################################################################

	def _session(self):
		"""Vráti session ID - z cache tohto spustenia alebo uloženú (z prihlásenia kódom)."""
		# 1. cache z tohto spustenia
		if self._login_session:
			return self._login_session

		# 2. uložená session (prežije reštart) - uložená po prihlásení cez kód
		try:
			saved = (self.get_setting('session') or '').strip()
		except Exception:
			saved = ''
		if saved:
			self._login_session = saved
			return saved

		return ''

	def _clear_session(self):
		"""Zmaže session (odhlásenie alebo vypršaná session) - v menu sa znova ukáže prihlásenie kódom."""
		self._login_session = None
		try:
			self.set_setting('session', '')
		except Exception:
			pass

	@staticmethod
	def _is_session_error(msg):
		return 'E_SESSION' in (msg or '')

	def sign_out(self):
		if not self.get_yes_no_input(self._("Do you really want to sign out?")):
			return
		self._clear_session()
		self.log_info("PlayLive signed out")
		self.show_info(self._("Signed out."), noexit=True)
		self.refresh_screen(parent=True)

	# ##################################################################################################################

	def tv_code_login(self):
		"""Prihlásenie cez kód (rovnako ako Smart TV appka) - bez captchy, bez prehliadača na krabici.

		1. Používateľ na playlive.net/logcode vygeneruje 6-miestny kód.
		2. Zadá ho sem.
		3. Addon cez connectDevice získa session a uloží ju.
		"""
		ready = self.show_info(self._(
			"Sign in with a code - no browser needed on this device.\n\n"
			"1. On your phone or PC open  playlive.net  and sign in\n"
			"2. Open  playlive.net/logcode  and press \"Generate code\"\n"
			"3. You will get a 6-digit code - then press OK here to type it"
		), noexit=True)
		if ready is False:
			return

		code = self.get_text_input(self._("Enter the 6-digit code from playlive.net/logcode"), input_type='number')
		if not code:
			return
		code = code.strip()

		try:
			sessid = self.api.connect_device_with_code(code)
		except Exception as e:
			msg = error_text(e)
			self.log_error("PlayLive connectDevice failed: %s" % msg)
			if 'bad_code' in msg:
				self.show_error(self._("The code is invalid or has expired. Generate a new one and try again."))
			else:
				self.show_error(self._("Sign in failed. Please try again later."))
			return

		self._login_session = sessid
		try:
			self.set_setting('session', sessid)
		except Exception:
			pass
		self.log_info("PlayLive signed in via code")
		self.show_info(self._("Signed in successfully."), noexit=True)
		self.refresh_screen(parent=True)

	# ##################################################################################################################
	# Môj účet
	# ##################################################################################################################

	def list_account(self):
		session = self._session()
		if not session:
			self.show_info(self._("Sign in first - use \"Sign in with a code\" in the menu."), noexit=True)
			return

		try:
			user, licenses = self.api.get_licenses(session)
		except Exception as e:
			msg = error_text(e)
			self.log_error("PlayLive account error: %s" % msg)
			if self._is_session_error(msg):
				self._clear_session()
				self.show_error(self._("Your sign-in has expired - sign in again with a code."))
			else:
				self.show_error(self._("The account could not be loaded. Try signing in again with a code."))
			return

		name = ('%s %s' % (user.get('firstname') or '', user.get('lastname') or '')).strip()
		if name:
			self.add_video(self._("Signed in as: %s") % name, None, {}, download=False, cmd=self.noop)

		if not licenses:
			self.show_info(self._("No purchased items found on the account."), noexit=True)
			return

		now = time()
		for lic in licenses:
			title = lic['title'] or lic['product_id']

			if lic['variant']:
				title = '%s (%s)' % (title, lic['variant'])

			if lic['valid_to']:
				valid = strftime('%d.%m.%Y %H:%M', localtime(lic['valid_to']))
				if lic['valid_to'] < now:
					title = '%s  %s' % (title, _I(self._("expired %s") % valid))
				else:
					title = '%s  %s' % (title, _I(self._("valid until %s") % valid))

			item = self.api.get_item(lic['product_id'])
			if item:
				self.add_dir(title, item.get('img'), {'title': lic['title'], 'plot': self._plot(item)},
				             cmd=self.open_item, product_id=lic['product_id'])
			else:
				self.add_video(title, None, {}, download=False, cmd=self.noop)

	# ##################################################################################################################

	def noop(self):
		return

	# ##################################################################################################################

	def _load(self, kind):
		try:
			return self.api.get_items(None if kind == 'all' else kind)
		except Exception as e:
			self.log_error("PlayLive offer error: %s" % error_text(e))
			self.show_error(self._("Failed to load the offer from PlayLive."))
			return None

	# ##################################################################################################################

	def list_items(self, kind, page=0):
		items = self._load(kind)
		if items is None:
			return

		if not items:
			self.show_info(self._("Nothing here yet."), noexit=True)
			return

		start = page * PAGE_SIZE
		for it in items[start:start + PAGE_SIZE]:
			self.add_item(it)

		if len(items) > start + PAGE_SIZE:
			self.add_next(self.list_items, kind=kind, page=page + 1)

	# ##################################################################################################################

	def search(self, keyword, search_id=None):
		try:
			items = self.api.search(keyword)
		except Exception as e:
			self.log_error("PlayLive search error: %s" % error_text(e))
			self.show_error(self._("Failed to load the offer from PlayLive."))
			return

		if not items:
			self.show_info(self._("Nothing found."), noexit=True)
			return

		for it in items[:PAGE_SIZE]:
			self.add_item(it)

	# ##################################################################################################################

	def add_item(self, item):
		title = item['title']

		if item.get('start'):
			title = '%s  %s' % (title, _I(strftime('%d.%m.%Y %H:%M', localtime(item['start']))))
		elif item.get('subtitle'):
			title = '%s  %s' % (title, _I(item['subtitle']))

		info_labels = {
			'title': item['title'],
			'plot': self._plot(item),
		}

		if item.get('duration'):
			info_labels['duration'] = item['duration']

		self.add_dir(title, item.get('img'), info_labels, cmd=self.open_item, product_id=item['id'])

	# ##################################################################################################################

	def _plot(self, item):
		lines = []

		if item.get('start'):
			lines.append(self._("Starts: %s") % strftime('%d.%m.%Y %H:%M', localtime(item['start'])))

		if item.get('location'):
			lines.append(self._("Venue: %s") % item['location'])

		prices = item.get('prices') or []
		if prices:
			lines.append(self._("Price: %s") % ', '.join(['%s %s' % (n, p) for n, p in prices]))

		plot = _strip_html(item.get('plot') or '')
		if plot:
			lines.append('')
			lines.append(plot)

		return '\n'.join(lines)

	# ##################################################################################################################

	def open_item(self, product_id):
		item = self.api.get_item(product_id)
		if not item:
			self.show_error(self._("This item is no longer in the offer."))
			return

		info_labels = {'title': item['title'], 'plot': self._plot(item)}

		# ak má používateľ session, ponúkneme prehratie prenosu
		if self._session():
			self.add_video(self._("Play broadcast"), item.get('img'), info_labels, download=False,
			               cmd=self.play_stream, product_id=item['id'])

		trailers = item.get('trailers') or []
		for idx, url in enumerate(trailers):
			title = self._("Trailer")
			if len(trailers) > 1:
				title = '%s %d' % (title, idx + 1)
			self.add_video(title, item.get('img'), info_labels, download=False,
			               cmd=self.play_trailer, url=url, title_text=title)

		self.add_video(self._("Event info"), item.get('img'), info_labels, download=False,
		               cmd=self.item_info, product_id=item['id'])

		if not self._session():
			self.add_video(_I(self._("Sign in with a code to play broadcasts")), item.get('img'),
			               info_labels, download=False, cmd=self.paid_info)

	# ##################################################################################################################

	def item_info(self, product_id):
		item = self.api.get_item(product_id)
		if not item:
			return

		lines = [item['title']]

		if item.get('subtitle'):
			lines.append(item['subtitle'])

		if item.get('start'):
			lines.append(self._("Starts: %s") % strftime('%d.%m.%Y %H:%M', localtime(item['start'])))
			if item['start'] > time():
				lines.append(self._("The event hasn't started yet."))

		if item.get('end'):
			lines.append(self._("Ends: %s") % strftime('%d.%m.%Y %H:%M', localtime(item['end'])))

		if item.get('location'):
			lines.append(self._("Venue: %s") % item['location'])

		for name, price in item.get('prices') or []:
			lines.append('%s: %s' % (name, price))

		lines.append('')
		lines.append(self._("Type: %s") % (', '.join(item.get('types') or []) or '-'))
		lines.append(self._("Product ID: %s") % item['id'])

		self.show_info('\n'.join(lines), noexit=True)

	# ##################################################################################################################

	def paid_info(self):
		self.show_info(self._(
			"PlayLive broadcasts are paid. To play one here, sign in using "
			"\"Sign in with a code\" in the menu and buy the broadcast on playlive.net. "
			"Trailers play without signing in."), noexit=True)

	# ##################################################################################################################

	def play_trailer(self, url, title_text):
		try:
			if '.m3u8' in url:
				if self.resolve_streams(url, title_text):
					return
			self.add_play(title_text, url, info_labels={'quality': 'auto'})
		except Exception as e:
			self.log_error("Failed to play trailer: %s" % error_text(e))
			self.show_error(self._("Failed to load the trailer."))

	# ##################################################################################################################

	def get_hls_info(self, stream_key):
		return {'url': stream_key['url'], 'bandwidth': stream_key['bandwidth']}

	# ##################################################################################################################

	def get_dash_info(self, stream_key):
		"""Vráti informácie pre DashHTTPRequestHandler - URL manifestu a licenčnú konfiguráciu."""
		info = {
			'url': stream_key['url'],
			'bandwidth': stream_key.get('bandwidth'),
			'drm': {
				'wv': {
					'license_url': stream_key.get('license_url', ''),
					'sessid': stream_key.get('sessid', ''),
					'privacy_mode': True,
					'headers': {
						'Origin': 'https://playlive.net',
						'Referer': 'https://playlive.net/',
					},
				}
			}
		}
		return info

	# ##################################################################################################################

	def resolve_streams(self, manifest_url, title=''):
		streams = self.get_hls_streams(manifest_url, self.api.req_session,
		                               max_bitrate=self.get_setting('max_bitrate'))
		if not streams:
			return False

		for s in streams:
			url = stream_key_to_hls_url(self.http_endpoint, {'url': s['playlist_url'], 'bandwidth': s['bandwidth']})
			info_labels = {'bandwidth': int(s['bandwidth'])}
			height = _resolution_height(s.get('resolution'))
			if height:
				info_labels['quality'] = height + 'p'
			self.add_play(title, url, info_labels=info_labels)

		return True

	# ##################################################################################################################

	def resolve_dash_streams(self, manifest_url, license_url, sessid, title=''):
		"""Rozlíši DASH streamy a pridá ich na prehrávanie."""
		streams = self.get_dash_streams(manifest_url, max_bitrate=self.get_setting('max_bitrate'))
		if not streams:
			return False

		for s in streams:
			stream_key = {
				'url': s['playlist_url'],
				'bandwidth': s['bandwidth'],
				'license_url': license_url,
				'sessid': sessid,
			}
			url = stream_key_to_dash_url(self.http_endpoint, stream_key)

			info_labels = {
				'bandwidth': int(s['bandwidth']),
				'quality': s['height'] + 'p' if s.get('height') else 'auto'
			}
			self.add_play(title, url, info_labels=info_labels)

		return True

	# ##################################################################################################################

	def play_stream(self, product_id):
		"""Prehrá platený prenos - zavolá /product/watch a spustí DASH stream."""
		session = self._session()
		if not session:
			self.show_info(self._("Sign in first - use \"Sign in with a code\" in the menu."), noexit=True)
			return

		item = self.api.get_item(product_id)
		title = item['title'] if item else ''

		try:
			stream = self.api.get_stream(session, product_id)
		except Exception as e:
			msg = error_text(e)
			self.log_error("PlayLive stream error: %s" % msg)
			low = msg.lower()
			if self._is_session_error(msg):
				self._clear_session()
				self.show_error(self._("Your sign-in has expired - sign in again with a code."))
			elif any(k in low for k in ('licen', 'purchas', 'buy', 'access_denied', 'no_access')):
				self.show_error(self._("You don't have access to this broadcast. Buy it at playlive.net first."))
			else:
				self.show_error(self._("Playback failed. Your sign-in may have expired - sign in again with a code."))
			return

		mpd_url = stream.get('url', '')
		license_url = stream.get('license_url', '')

		if not mpd_url:
			self.show_error(self._("No stream URL received from server."))
			return

		if not license_url:
			self.show_error(self._("No license URL received. The stream cannot be played."))
			return

		# len server - cesta aj query obsahujú prístupový token (wmt:...JWT)
		self.log_info("Playing stream from: %s" % (mpd_url.split('/')[2] if mpd_url.count('/') >= 2 else '?'))

		if not self.resolve_dash_streams(mpd_url, license_url, session, title=title):
			self.show_error(self._("Failed to load the DASH stream."))


# ##################################################################################################################

def _resolution_height(resolution):
	m = re.search(r'x(\d+)', resolution or '')
	return m.group(1) if m else None
