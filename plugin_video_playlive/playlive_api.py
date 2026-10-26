# -*- coding: utf-8 -*-
#
# Katalóg PlayLive.net (platforma Streamonline).
#
# Celá ponuka je verejná a leží v jedinom súbore na CDN:
#   https://cdn.streamonline.pro/playlive-net/offer.json
# Obsahuje dve polia:
#   contents - popisné dáta (názvy, popisy, plagáty, upútavky) podľa contentId
#   products - predávané položky (PPV eventy, VOD), každá odkazuje na contentId
#              a nesie varianty (basic/premium) s cenami a časom eventu
#
# Prihlásenie na čítanie katalógu nie je potrebné. Platené prenosy sú chránené
# DRM (BuyDRM/KeyOS - Widevine) a pre ich prehrávanie je potrebné prihlásenie
# cez kód (Smart TV device flow, connectDevice). Upútavky sú bez prihlásenia.
#
# Kód je písaný tak, aby bežal na Pythone 2 aj 3 (staršie Enigma2 image).

import sys
import base64
import hashlib
import random
import json as json_mod
from time import time

LANGS = ['sk', 'cs', 'en', 'pl']

OFFER_URL = "https://cdn.streamonline.pro/playlive-net/offer.json"
API_BASE = "https://api.playlive.net"
REFERER = "https://playlive.net/"
ORIGIN = "https://playlive.net"
CACHE_SECONDS = 600
LIVE_NO_END_SECONDS = 6 * 3600  # predpokladaná dĺžka eventu, keď offer neuvádza koniec

# Kľúče na podpis požiadaviek - sú zapečené v ich webovom skripte. Podpis je obyčajná
# kontrola, že požiadavka prišla od ich klienta; s DRM ani s prihlásením nesúvisí.
SIGN_KEYS = [
	"1058b45d-53a9-4e25-a47a-132fd046b6d4",
	"a897eced-b8f9-459f-851c-fae95c96632a",
	"ec1ea5f1-722e-46c6-afb6-a10f6d7d1415",
	"69523de2-0673-4dad-98a1-bb49fe5ad78b",
	"9f76209c-8037-44be-8ae0-ec4ce23af8a6",
	"84055283-aff3-4749-a1a7-40ffccf643d0",
	"d41cba3b-2133-499a-910b-f7422d0271b3",
	"9cd39efd-8c63-4bf7-b764-7cc33b5d13b4",
	"e57df921-2768-4382-bf26-ac047d404f73",
	"a4467403-b7f5-476e-b536-f9549b1710fc",
]


# ##################################################################################################################

def error_text(e):
	"""Text výnimky bezpečne pre Python 2 aj 3 (py2 str() padá na diakritike)."""
	msg = None
	try:
		if getattr(e, 'args', None):
			msg = e.args[0]
	except Exception:
		msg = None

	if msg is None:
		msg = e

	if sys.version_info[0] == 2:
		try:
			return unicode(msg)  # noqa: F821 - existuje len na py2
		except Exception:
			return ''

	try:
		return str(msg)
	except Exception:
		return ''


def norm_text(text):
	"""Malé písmená bez diakritiky - pre vyhľadávanie. Funguje na py2 aj py3."""
	if not text:
		return ''

	if sys.version_info[0] == 2:
		string_type = unicode  # noqa: F821 - existuje len na py2
	else:
		string_type = str

	if not isinstance(text, string_type):
		try:
			text = text.decode('utf-8')
		except Exception:
			try:
				return text.lower()
			except Exception:
				return ''

	try:
		import unicodedata
		text = unicodedata.normalize('NFKD', text)
		text = u''.join([c for c in text if not unicodedata.combining(c)])
	except Exception:
		pass

	return text.lower()


def lang_value(node):
	"""Hodnoty v offer.json sú slovníky podľa jazyka ({'pl': ..., 'en': ...})."""
	if isinstance(node, dict):
		for l in LANGS:
			if node.get(l):
				return node[l]
		for v in node.values():
			if v:
				return v
		return ''

	return node or ''


# ##################################################################################################################

class PlayLiveApi(object):
	def __init__(self, content_provider):
		self.cp = content_provider
		self.req_session = self.cp.get_requests_session()
		self._offer = None
		self._offer_time = 0

	# ##################################################################################################################

	def load_offer(self, force=False):
		if self._offer and not force and (time() - self._offer_time) < CACHE_SECONDS:
			return self._offer

		url = OFFER_URL + '?t=%d' % int(time() * 1000)
		response = self.req_session.get(url, headers={'Referer': REFERER}, timeout=30)
		response.raise_for_status()
		data = response.json()

		contents = {}
		for c in data.get('contents') or []:
			if c.get('id') is not None:
				contents[str(c['id'])] = c

		items = []
		for p in data.get('products') or []:
			item = self._parse_product(p, contents)
			if item:
				items.append(item)

		self._offer = items
		self._offer_time = time()
		self.cp.log_info("PlayLive offer loaded: %d products, %d contents" % (len(items), len(contents)))
		return items

	# ##################################################################################################################

	def _parse_product(self, product, contents):
		content = contents.get(str(product.get('contentId'))) or {}
		meta = content.get('meta') or {}

		title = lang_value(meta.get('title')) or lang_value((product.get('meta') or {}).get('title'))
		if not title:
			title = product.get('route') or product.get('id') or ''

		start, end = self._event_times(product)

		return {
			'id': str(product.get('id')),
			'route': product.get('route'),
			'client_id': product.get('clientId'),
			'content_id': str(product.get('contentId')),
			'types': product.get('type') or [],
			'live': 'live' in (product.get('type') or []),
			'title': title,
			'subtitle': lang_value(meta.get('label')) or lang_value(meta.get('subtitle')),
			'plot': lang_value(meta.get('desc')) or lang_value(meta.get('shortDesc')),
			'img': lang_value(meta.get('posterLandscapeUrl')) or lang_value(meta.get('posterPortraitUrl')),
			'location': lang_value(meta.get('eventLocation')),
			'duration': meta.get('duration') or 0,
			'trailers': self._trailers(meta),
			'start': start,
			'end': end,
			'created': product.get('created') or 0,
			'prices': self._prices(product),
			'variants': [lang_value((v.get('meta') or {}).get('shortDesc')) for v in (product.get('variants') or [])],
		}

	# ##################################################################################################################

	@staticmethod
	def _trailers(meta):
		ret = []
		for node in (meta.get('trailers'), meta.get('trailer')):
			if isinstance(node, list):
				ret.extend([t for t in node if t])
			elif isinstance(node, dict):
				value = lang_value(node)
				if isinstance(value, list):
					ret.extend([t for t in value if t])
				elif value:
					ret.append(value)
			elif node:
				ret.append(node)

		# len priamo prehrateľné manifesty / súbory
		# (na py2 prídu z json-u unicode reťazce, preto nie isinstance(t, str))
		out = []
		for t in ret:
			try:
				ok = ('.m3u8' in t or '.mpd' in t or t.endswith('.mp4'))
			except TypeError:
				ok = False
			if ok:
				out.append(t)

		return out

	# ##################################################################################################################

	@staticmethod
	def _event_times(product):
		start = 0
		end = 0
		for v in product.get('variants') or []:
			live = (v.get('accessPolicy') or {}).get('live') or {}
			if not isinstance(live, dict):
				continue
			s = live.get('eventStartsAt') or 0
			e = live.get('eventEndsAt') or 0
			if s and (not start or s < start):
				start = s
			if e and e > end:
				end = e

		return int(start / 1000) if start else 0, int(end / 1000) if end else 0

	# ##################################################################################################################

	@staticmethod
	def _prices(product):
		"""Vráti zoznam (názov variantu, cena ako text) z aktuálne platnej cenovej politiky."""
		now = int(time() * 1000)
		ret = []

		for v in product.get('variants') or []:
			vmeta = v.get('meta') or {}
			name = vmeta.get('tag') or v.get('id') or ''
			amount = None
			currency = None

			for policy in v.get('pricePolicy') or []:
				valid = policy.get('valid') or {}
				if valid.get('from') and valid['from'] > now:
					continue
				if valid.get('to') and valid['to'] < now:
					continue
				for region in policy.get('regions') or []:
					cur = region.get('currency') or {}
					for code in ('EUR', 'CZK', 'PLN'):
						node = cur.get(code)
						if not node:
							continue
						for channel in node.values():
							if isinstance(channel, dict) and channel.get('amount') is not None:
								amount = channel['amount']
								currency = code
								break
						if amount is not None:
							break
					if amount is not None:
						break
				if amount is not None:
					break

			if amount is not None:
				ret.append((name, '%s %s' % (amount, currency)))

		return ret

	# ##################################################################################################################

	def get_items(self, kind=None):
		"""kind: 'now' | 'upcoming' | 'live_archive' | 'vod' | None (všetko)"""
		items = self.load_offer()
		now = int(time())

		if kind == 'now':
			# event bez času konca berieme ako bežiaci max. LIVE_NO_END_SECONDS od začiatku
			ret = [i for i in items if i['start'] and i['start'] - 1800 <= now and
			       (i['end'] or i['start'] + LIVE_NO_END_SECONDS) >= now]
			ret.sort(key=lambda i: i['start'])
		elif kind == 'upcoming':
			ret = [i for i in items if i['start'] and i['start'] - 1800 > now]
			ret.sort(key=lambda i: i['start'])
		elif kind == 'live_archive':
			ret = [i for i in items if i['live'] and (not i['start'] or i['start'] <= now)]
			ret.sort(key=lambda i: (i['start'] or i['created']), reverse=True)
		elif kind == 'vod':
			ret = [i for i in items if not i['live']]
			ret.sort(key=lambda i: i['created'], reverse=True)
		else:
			ret = list(items)
			ret.sort(key=lambda i: (i['start'] or i['created']), reverse=True)

		return ret

	# ##################################################################################################################

	def search(self, keyword):
		needle = norm_text(keyword)
		if not needle:
			return []

		ret = []
		for i in self.load_offer():
			if needle in norm_text(i['title']) or needle in norm_text(i.get('subtitle') or ''):
				ret.append(i)

		ret.sort(key=lambda i: (i['start'] or i['created']), reverse=True)
		return ret

	# ##################################################################################################################
	# Prihlásenie a účet
	# ##################################################################################################################
	# Prihlásenie cez kód (Smart TV device flow) - BEZ captchy.
	#
	# Rovnaký postup aký používa Android TV appka:
	#   1. Používateľ sa prihlási na playlive.net a na /logcode klikne "Generate code"
	#      -> dostane 6-miestny kód (platný ~5 min). Captchu tam vyrieši prehliadač ako človek.
	#   2. Ten kód zadá do addonu.
	#   3. Addon zavolá session/connectDevice {code, dt:"st"} -> server vráti session.
	# Volanie je bez prihlásenia (sessid:false) a server nevyžaduje captcha token (_gcet).
	# ##################################################################################################################

	@staticmethod
	def _build_sessid(session):
		"""Poskladá sessid z session objektu: id.token.pk(.dt) - rovnako ako ich klient."""
		sid = '%s.%s.%s' % (session.get('id') or '', session.get('token') or '', session.get('pk') or '')
		dt = session.get('dt')
		if dt:
			sid = '%s.%s' % (sid, dt)
		return sid

	def connect_device_with_code(self, code, dt='st'):
		"""Prihlási zariadenie 6-miestnym kódom z playlive.net/logcode. Vráti sessid.

		Vyhodí Exception('bad_code') ak kód neexistuje/vypršal, inak pôvodnú chybu.
		"""
		payload = self.sign({'code': str(code).strip(), 'dt': dt})

		response = self.req_session.post(
			API_BASE + '/session/connectDevice', json=payload, timeout=20,
			headers={'Referer': REFERER, 'Origin': ORIGIN, 'Content-Type': 'application/json'})
		response.raise_for_status()
		result = response.json() or {}

		errors = result.get('error')
		if errors:
			if isinstance(errors, list):
				for err in errors:
					code_str = (err.get('code') or '').upper()
					if 'DEVICE_NOT_FOUND' in code_str or 'NOT_FOUND' in code_str or 'EXPIRED' in code_str:
						raise Exception('bad_code')
			self._raise_api_error(result, 'connect_device_failed')

		data = result.get('data') or {}
		session = data.get('session') or {}
		if not session.get('id'):
			raise Exception('bad_code')

		return self._build_sessid(session)

	@staticmethod
	def _js_str(value):
		# Object.values(...).join('|') v JS - null/undefined sú prázdny reťazec
		if value is None:
			return ''
		if value is True:
			return 'true'
		if value is False:
			return 'false'
		if isinstance(value, float) and value == int(value):
			return str(int(value))
		return '%s' % (value,)

	def sign(self, data):
		"""Doplní do tela požiadavky ts, r a podpis t - rovnako ako ich web."""
		keys = list(data.keys())
		count = len(keys)
		if count < 1:
			count = 1

		data['ts'] = int(time() * 1000)
		data['r'] = str(random.randint(10 ** 14, 10 ** 15 - 1))

		ordered = keys + ['ts', 'r']
		values = [self._js_str(data[k]) for k in ordered]
		raw = '%s::%s::%s' % ('|'.join(ordered), '|'.join(values), SIGN_KEYS[len(SIGN_KEYS) % count])

		digest = hashlib.sha1(raw.encode('utf-8')).digest()
		data['t'] = base64.b64encode(digest).decode('ascii')
		return data

	@staticmethod
	def _raise_api_error(result, default='API error'):
		"""PlayLive vracia chyby ako pole [{type, code, msg}] - poskladá z nich čitateľnú výnimku.

		Výsledný text obsahuje typ a kód (napr. "E_SESSION/NOT_FOUND: Access denied!"),
		aby volajúci vedel rozlíšiť vypršanú session od iných chýb.
		"""
		errors = result.get('error')
		parts = []
		if isinstance(errors, list):
			for err in errors:
				if isinstance(err, dict):
					parts.append('%s/%s: %s' % (err.get('type') or '', err.get('code') or '', err.get('msg') or ''))
		elif isinstance(errors, dict):
			parts.append('%s/%s: %s' % (errors.get('type') or '', errors.get('code') or '', errors.get('msg') or ''))

		raise Exception('; '.join(parts) or result.get('message') or default)

	def call_api(self, path, data=None, sign=True):
		payload = dict(data or {})
		if sign:
			payload = self.sign(payload)

		response = self.req_session.post(
			API_BASE + path, json=payload, timeout=20,
			headers={'Referer': REFERER, 'Origin': ORIGIN, 'Content-Type': 'application/json'})
		response.raise_for_status()
		result = response.json() or {}

		if result.get('error'):
			self._raise_api_error(result)

		return result.get('data') or {}

	def get_user(self, session_id):
		"""Vráti údaje o účte vrátane zakúpených licencií."""
		data = self.call_api('/users/me', {'_sessid': session_id})
		return data.get('user') or {}

	def get_licenses(self, session_id):
		"""Zakúpené položky: (product_id, názov, variant, platnosť do v sekundách)."""
		user = self.get_user(session_id)
		ret = []

		for lic in user.get('licenses') or []:
			product = lic.get('product') or {}
			valid_to = lic.get('validTo') or 0
			access = lic.get('accessTill') or {}
			if isinstance(access, dict):
				for value in access.values():
					if value and value > valid_to:
						valid_to = value

			ret.append({
				'product_id': str(product.get('id') or ''),
				'title': product.get('title') or '',
				'variant': product.get('variantId') or '',
				'valid_to': int(valid_to / 1000) if valid_to else 0,
			})

		ret.sort(key=lambda i: i['valid_to'], reverse=True)
		return user, ret

	# ##################################################################################################################

	def get_item(self, product_id):
		for i in self.load_offer():
			if i['id'] == str(product_id):
				return i
		return None

	# ##################################################################################################################
	# DRM stream - volanie /product/watch a dešifrovanie odpovede
	# ##################################################################################################################

	@staticmethod
	def _decrypt_response(data_bytes, session_id, etas_header):
		"""Dešifruje binárnu odpoveď z /product/watch.

		Šifrovanie: XOR s kľúčom (UUID časť session_id) + index + etas.
		Po dešifrovaní: UTF-8 reťazec rozdelený '#', prvý a posledný segment
		sú padding, stred je JSON.
		"""
		key = session_id.split('.')[0]
		if sys.version_info[0] >= 3:
			key = key.encode('utf-8')

		etas = int(etas_header)
		result = bytearray(data_bytes)

		for i in range(len(result)):
			if sys.version_info[0] >= 3:
				k = key[i % len(key)]
			else:
				k = ord(key[i % len(key)])
			result[i] = (result[i] ^ (k + i + etas)) % 256

		decoded = result.decode('utf-8')
		parts = decoded.split('#')
		# prvý a posledný segment sú padding (časti session_id)
		if len(parts) >= 3:
			parts.pop(0)
			parts.pop(-1)
		json_str = '#'.join(parts)
		return json_mod.loads(json_str)

	def get_stream(self, session_id, product_id):
		"""Zavolá /product/watch a vráti dáta o streame vrátane DRM licenčných URL.

		Vracia dict s kľúčmi:
		  url        - MPD manifest URL
		  license_url - Widevine license proxy URL (s lap parametrom)
		  pr_license_url - PlayReady license proxy URL (voliteľné)
		"""
		payload = {
			'pid': str(product_id),
			'fp': '',
			'im': 0,
			'a': 'cs',
			'stype': 'DASH',
			'map': True,
			'_sessid': session_id,
		}
		payload = self.sign(payload)

		response = self.req_session.post(
			API_BASE + '/product/watch',
			json=payload,
			timeout=30,
			headers={
				'Referer': REFERER,
				'Origin': ORIGIN,
				'Content-Type': 'application/json',
			}
		)
		response.raise_for_status()

		content_type = response.headers.get('content-type', '')
		etas = response.headers.get('etas')

		if etas and 'octet-stream' in content_type:
			# šifrovaná odpoveď
			data = self._decrypt_response(response.content, session_id, etas)
		else:
			# nešifrovaná (fallback)
			data = response.json()

		if data.get('error'):
			self._raise_api_error(data, 'Stream error')

		stream_data = data.get('data') or {}
		pgm = stream_data.get('pgm') or {}
		sources = pgm.get('source') or []

		if not sources:
			raise Exception('No stream sources available')

		source = sources[0]
		protection = source.get('protection') or {}
		buy_drm = protection.get('buyDRM') or {}
		wv = buy_drm.get('widevine') or {}
		pr = buy_drm.get('playready') or {}
		wmt = protection.get('wmtNagra') or {}

		# MPD URL - prednostne z wmtNagra (obsahuje WMT token), fallback na source.url
		mpd_url = wmt.get('wmtUrl') or source.get('url') or ''

		result = {
			'url': mpd_url,
			'license_url': wv.get('laProxyUrl') or '',
			'pr_license_url': pr.get('laProxyUrl') or '',
		}

		return result
