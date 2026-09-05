# -*- coding: utf-8 -*-
"""
Tvheadend HTTP API klient.

Kompatibilita: Python 2.7 + Python 3.x
- urllib parse importy cez tools_archivczsk.six (preferované) alebo priame fallbacky
- queue/Queue compat
- threading je stdlib, dostupné všade
"""

import os
import re
import time
import threading

# --------------------------------------------------------------------------
# urllib compat (py2/py3)
# --------------------------------------------------------------------------
try:
	from tools_archivczsk.six.moves.urllib.parse import urlparse
except Exception:
	try:
		from urllib.parse import urlparse
	except ImportError:
		from urlparse import urlparse

# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
try:
	from tools_archivczsk.cache import ExpiringLRUCache
except Exception:
	ExpiringLRUCache = None

# --------------------------------------------------------------------------
# requests extras
# --------------------------------------------------------------------------
# Pozn.: stock requests.auth.HTTPDigestAuth sa nepoužíva — plugin má vlastnú
# HTTPDigestAuthMulti nižšie (MD5 + SHA-256 + SHA-512-256).
try:
	from requests.auth import AuthBase as _RequestsAuthBase
except Exception:
	_RequestsAuthBase = object

import hashlib as _hashlib


# --------------------------------------------------------------------------
# Vlastná Digest auth s podporou MD5 / SHA-256 / SHA-512-256.
# Dôvod: requests.auth.HTTPDigestAuth spoľahlivo zvláda len MD5. Tvheadend
# od novších verzií ponúka Digest hash type SHA-256 / SHA-512/256, ktoré
# stock HTTPDigestAuth nezvládne → pripojenie zlyhá aj so správnym heslom.
# Táto trieda číta algorithm z WWW-Authenticate hlavičky a počíta digest
# správnym hashom (RFC 2617 pre MD5, RFC 7616 pre SHA varianty).
# Kompatibilné s Python 2.7 aj 3.x.
# --------------------------------------------------------------------------
def _digest_hash_factory(algorithm):
	"""Vráti (hash_funkcia, je_sess) podľa názvu algoritmu z challenge."""
	algo = (algorithm or 'MD5').upper().strip()
	sess = algo.endswith('-SESS')
	if sess:
		algo = algo[:-5]
	if algo == 'SHA-256':
		def _h(data):
			return _hashlib.sha256(data).hexdigest()
	elif algo == 'SHA-512-256':
		def _h(data):
			try:
				return _hashlib.new('sha512_256', data).hexdigest()
			except (ValueError, TypeError):
				return _hashlib.sha256(data).hexdigest()
	else:
		def _h(data):
			return _hashlib.md5(data).hexdigest()
	return _h, sess


def _digest_to_bytes(s):
	if isinstance(s, bytes):
		return s
	return s.encode('utf-8')


def _digest_parse_challenge(header):
	"""Rozparsuje WWW-Authenticate Digest hlavičku do dict."""
	if header.lower().startswith('digest'):
		header = header[6:].strip()
	result = {}
	pattern = re.compile(r'(\w+)=(?:"([^"]*)"|([^,]+))')
	for m in pattern.finditer(header):
		key = m.group(1).lower()
		val = m.group(2) if m.group(2) is not None else m.group(3)
		result[key] = val.strip()
	return result


class HTTPDigestAuthMulti(_RequestsAuthBase):
	"""Digest auth s MD5/SHA-256/SHA-512-256. Drop-in za HTTPDigestAuth."""

	def __init__(self, username, password):
		self.username = username
		self.password = password
		self._nonce_count = 0
		self._last_challenge = {}

	def _build_header(self, method, url, challenge):
		path = url
		m = re.match(r'^[a-zA-Z]+://[^/]+(/.*)$', url)
		if m:
			path = m.group(1)
		elif not url.startswith('/'):
			path = '/' + url

		realm = challenge.get('realm', '')
		nonce = challenge.get('nonce', '')
		qop = challenge.get('qop')
		algorithm = challenge.get('algorithm', 'MD5')
		opaque = challenge.get('opaque')

		hfunc, is_sess = _digest_hash_factory(algorithm)

		ha1 = hfunc(_digest_to_bytes('%s:%s:%s' % (self.username, realm, self.password)))
		ha2 = hfunc(_digest_to_bytes('%s:%s' % (method, path)))

		self._nonce_count += 1
		nc = '%08x' % self._nonce_count
		cnonce = _hashlib.sha1(
			_digest_to_bytes(str(time.time()) + str(os.urandom(8)))
		).hexdigest()[:16]

		if is_sess:
			ha1 = hfunc(_digest_to_bytes('%s:%s:%s' % (ha1, nonce, cnonce)))

		if qop:
			resp_data = '%s:%s:%s:%s:%s:%s' % (ha1, nonce, nc, cnonce, 'auth', ha2)
			response = hfunc(_digest_to_bytes(resp_data))
		else:
			response = hfunc(_digest_to_bytes('%s:%s:%s' % (ha1, nonce, ha2)))

		parts = [
			'username="%s"' % self.username,
			'realm="%s"' % realm,
			'nonce="%s"' % nonce,
			'uri="%s"' % path,
			'response="%s"' % response,
		]
		if algorithm:
			parts.append('algorithm=%s' % algorithm)
		if opaque:
			parts.append('opaque="%s"' % opaque)
		if qop:
			parts.append('qop=auth')
			parts.append('nc=%s' % nc)
			parts.append('cnonce="%s"' % cnonce)
		return 'Digest ' + ', '.join(parts)

	def _handle_401(self, r, **kwargs):
		if r.status_code != 401:
			return r
		auth_header = r.headers.get('www-authenticate', '')
		if 'digest' not in auth_header.lower():
			return r
		challenge = _digest_parse_challenge(auth_header)
		self._last_challenge = challenge
		r.content
		r.close()
		prep = r.request.copy()
		try:
			prep.headers['Authorization'] = self._build_header(
				prep.method, prep.url, challenge)
		except Exception:
			return r
		_r = r.connection.send(prep, **kwargs)
		_r.history.append(r)
		_r.request = prep
		return _r

	def __call__(self, r):
		if self._last_challenge:
			try:
				r.headers['Authorization'] = self._build_header(
					r.method, r.url, self._last_challenge)
			except Exception:
				pass
		try:
			r.register_hook('response', self._handle_401)
		except Exception:
			pass
		return r


from tools_archivczsk.contentprovider.exception import AddonErrorException

from ._htsp_api import TvhHtspApiMixin
from ._stream_urls import TvhStreamUrlMixin
from ._data_api import TvhDataApiMixin
from ._picons import TvhPiconMixin


class Tvheadend(TvhHtspApiMixin, TvhStreamUrlMixin, TvhDataApiMixin, TvhPiconMixin, object):
	"""
	Klient Tvheadend servera.

	Dva režimy podľa nastavenia `connection_mode`:
	  - http (9981) — metadáta cez REST JSON API
	  - htsp (9982) — metadáta cez HTSP protokol (mixin TvhHtspApiMixin)
	Streamovanie ide v OBOCH režimoch cez HTTP endpointy na 9981
	(stream/channelid/, dvrfile/).
	"""

	PREFER_CHANNEL_STREAM = True
	USE_TITLE_PARAM = True

	STREAM_CH_ENDPOINT  = 'stream/channel/%s'
	STREAM_CHID_ENDPOINT = 'stream/channelid/%s'
	STREAM_SVC_ENDPOINT  = 'stream/service/%s'

	# Cache pre kanály s TTL 60 sekúnd
	_channels_cache = ExpiringLRUCache(1, default_timeout=60) if ExpiringLRUCache else None

	def __init__(self, cp):
		self.cp = cp
		self._ = cp._
		self.req = cp.get_requests_session()
		self._img_cache_dir = '/tmp/archivczsk_tvheadend_img'
		# HTSP mód (0.62.0): cache metadát (kanály/tagy/EPG/DVR z jedného fetch)
		self._htsp_meta = None
		self._htsp_meta_ts = 0
		self._htsp_meta_epg = None
		self._htsp_meta_epg_ts = 0
		# FIX 1.0.1: stav odmietnutia pre limit spojeni + sonda kontroly loginu
		self._htsp_connlimit_ts = 0
		self._htsp_connlimit_msg = ''
		self._login_probe_http_ok = None
		self._login_probe_ts = 0
		# lock: aby nebežali dva paralelné HTSP fetche naraz (dva EPG buffery
		# v RAM servera = riziko OOM). Druhý počká a vezme čerstvú cache.
		import threading as _th
		self._htsp_fetch_lock = _th.Lock()
		try:
			if not os.path.isdir(self._img_cache_dir):
				os.makedirs(self._img_cache_dir)
		except Exception:
			pass
		# FIX 0.48: thread-safe auth handling
		# _apply_auth_to_session() mutuje sess.auth. Background tasky
		# (picon worker, bouquet generator, EPG generator) sa môžu križovať
		# s GUI volaniami a meniť auth medzi requestami => 401/403 race.
		# Riešenie:
		#  - per-Tvheadend lock (self._req_lock) chráni primárnu session
		#  - _auth_sig kešuje aktuálne nastavenie, _apply_auth_to_session()
		#    sa nemusí volať pred každým requestom — len keď sa zmení
		self._req_lock = threading.RLock()
		self._auth_sig = None
		# picon inicializácia sa spúšťa lazily – nie v __init__ aby neblokovala GUI

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------

	def _timeout(self):
		try:
			t = int(self.cp.get_setting('loading_timeout'))
		except Exception:
			t = 15
		return None if t == 0 else t

	def base_url(self):
		host = (self.cp.get_setting('host') or '').strip()
		if not host:
			raise AddonErrorException(self._("Missing Tvheadend server address in settings."))

		if host.startswith('http://') or host.startswith('https://'):
			u = urlparse(host)
			scheme = u.scheme
			hostname = u.hostname or ''
			# FIX 1.0.0 (audit): pre https bez portu sa predtým dosadil 9982,
			# čo je HTSP port — HTTP API tam nepočúva, takže "https://host"
			# bez portu vždy zlyhalo. Default je 9981 pre obe schémy; ak má
			# užívateľ HTTPS na inom porte, uvedie ho priamo v URL.
			port = str(u.port or 9981)
			return '%s://%s:%s' % (scheme, hostname, port)

		port = str(self.cp.get_setting('port') or '9981').strip()
		use_https = bool(self.cp.get_setting('use_https'))
		scheme = 'https' if use_https else 'http'
		return '%s://%s:%s' % (scheme, host, port)

	def _auth_signature(self):
		"""Vráti tuple identifikujúci aktuálne auth nastavenia.
		Použité na cache invalidation v _apply_auth_to_session()."""
		try:
			return (
				(self.cp.get_setting('username') or '').strip(),
				(self.cp.get_setting('password') or ''),
				(self.cp.get_setting('http_auth_mode') or 'auto').strip().lower(),
			)
		except Exception:
			return None

	def _apply_auth_to_session(self, sess=None, force=False):
		"""Nastaví autentifikáciu na session (default self.req).

		FIX 0.48: keď cieľová session je self.req (zdieľaná), idempotentne
		— ak sa auth signature nezmenila, nič nerobíme. Toto + RLock chráni
		pred race conditions z paralelných background tasks.
		"""
		if sess is None or sess is self.req:
			sess = self.req
			with self._req_lock:
				sig = self._auth_signature()
				if not force and sig == self._auth_sig and self._auth_sig is not None:
					return
				self._do_apply_auth(sess, sig)
				self._auth_sig = sig
			return

		# Externá session (napr. picon worker) — nezdieľaná, neriešime cache
		sig = self._auth_signature()
		self._do_apply_auth(sess, sig)

	def _do_apply_auth(self, sess, sig):
		"""Skutočné nastavenie .auth na session-e."""
		if sig is None:
			user, pwd, mode = '', '', 'auto'
		else:
			user, pwd, mode = sig

		if not user or mode == 'none':
			sess.auth = None
			return
		if mode in ('digest', 'auto'):
			# Vlastná digest auth s podporou MD5/SHA-256/SHA-512-256.
			# Stock requests HTTPDigestAuth zvláda len MD5 — preto vlastná.
			sess.auth = HTTPDigestAuthMulti(user, pwd)
		else:
			sess.auth = (user, pwd)

	def _url(self, path):
		path = (path or '').lstrip('/')
		return self.base_url().rstrip('/') + '/' + path

	def invalidate_auth_cache(self):
		"""Volaj keď zmeníš nastavenia (login_data_changed) — vynúti
		re-apply pri ďalšom api_get."""
		with self._req_lock:
			self._auth_sig = None

	# ------------------------------------------------------------------
	# API volania
	# ------------------------------------------------------------------

	# FIX 0.48: retry-with-backoff pre transient errors.
	# Predtým: jeden 502/503 alebo TCP timeout → AddonErrorException →
	# zlyhá celý refresh bouquetu / EPG / picon scan. Po novom skúsime
	# 3× s exponenciálnym backoff (0.5s, 1s, 2s), čo prežije EPG-scan
	# blip v Tvheadend bez dopadu na užívateľa.
	_RETRY_ATTEMPTS = 3
	_RETRY_BACKOFF_BASE = 0.5  # sekundy
	_RETRY_STATUS_CODES = (500, 502, 503, 504, 408, 429)

	def api_get(self, path, params=None, timeout_override=None):
		"""HTTP GET na TVH API endpoint.

		timeout_override: ak je zadaný, použije sa namiesto _timeout()
		(setting 'loading_timeout', default 15s). Užitočné pre check_login
		ktorý chce zlyhať rýchlo (FIX 0.48i: 5s namiesto 15s, recovery
		zariadi background poll).
		"""
		url = self._url(path)
		last_err = None
		req_timeout = timeout_override if timeout_override is not None else self._timeout()
		for attempt in range(self._RETRY_ATTEMPTS):
			# Auth + request musia byť pod jedným lockom — inak iný thread
			# môže prepnúť auth medzi _apply_auth a self.req.get
			with self._req_lock:
				self._apply_auth_to_session()
				try:
					resp = self.req.get(url, params=params or {},
					                    timeout=req_timeout)
				except Exception as e:
					last_err = e
					resp = None

			if resp is not None:
				status = getattr(resp, 'status_code', 0)
				if status == 200:
					try:
						return resp.json()
					except Exception:
						raise AddonErrorException(
							self._("Tvheadend returned invalid JSON."))
				if status not in self._RETRY_STATUS_CODES:
					# 401/403/404 atď. — retry nemá zmysel
					try:
						resp.raise_for_status()
					except Exception as e:
						raise AddonErrorException('%s\n%s' % (
							self._("Tvheadend API request failed."), str(e)))
				last_err = Exception("HTTP %s for %s" % (status, url))

			# retry s backoff
			if attempt < self._RETRY_ATTEMPTS - 1:
				try:
					time.sleep(self._RETRY_BACKOFF_BASE * (2 ** attempt))
				except Exception:
					pass

		raise AddonErrorException('%s\n%s' % (
			self._("Tvheadend API request failed."),
			str(last_err) if last_err else 'unknown error'))

	def api_get_all(self, path, params=None, page_limit=500):
		"""Automatické stránkovanie – vracia všetky záznamy."""
		params = dict(params or {})
		start  = int(params.get('start', 0))
		limit  = int(params.get('limit', page_limit)) or page_limit

		entries = []
		total   = None
		for _ in range(200):
			params['start'] = start
			params['limit'] = limit
			data = self.api_get(path, params)
			page = data.get('entries') or []
			entries.extend(page)

			if total is None:
				try:
					total = int(data.get('total'))
				except Exception:
					total = None

			if total is not None and len(entries) >= total:
				break
			if not page or len(page) < limit:
				break
			start += limit

		return entries

	# ------------------------------------------------------------------
	# Login
	# ------------------------------------------------------------------

	def is_configured(self):
		host = (self.cp.get_setting('host') or '').strip()
		user = (self.cp.get_setting('username') or '').strip()
		pwd  = (self.cp.get_setting('password') or '')
		return bool(host and user and pwd)

	# FIX 0.48i: krátky timeout pre check_login (5s). Pre nečinný TVH
	# server chceme zlyhať rýchlo (žiadne 15s GUI hangy) — recovery
	# zariadi background poll v provider._maybe_start_fast_recovery_poll.
	_CHECK_LOGIN_TIMEOUT = 5

	def check_login(self, force_reauth=False):
		"""Overí spojenie volaním /api/serverinfo. Vyhodí výnimku pri chybe.

		FIX 0.48i: force_reauth=True invaliduje auth signature pred volaním,
		takže _apply_auth_to_session() re-inštanciuje HTTPDigestAuth.
		Použité v provider._check_tvh_silent pri auto-retry po prvom zlyhaní —
		rieši digest auth nonce expiry (TVH server odhodí nonce po N minútach
		idle, požaduje re-negotiation; requests knižnica to vie ale občas
		state ostane stale medzi thread-mi).
		"""
		if force_reauth:
			try:
				self.invalidate_auth_cache()
			except Exception:
				pass
		if self.is_htsp_mode():
			return self._check_login_htsp_mode()
		self.api_get('api/serverinfo', params={},
		             timeout_override=self._CHECK_LOGIN_TIMEOUT)
		return True

	# FIX 1.0.1: v HTSP režime sa kontrola dostupnosti robila plnym HTSP
	# handshakom. Watchdog ju vola kazdych 5 minut, co je 288 zbytocnych
	# TCP+auth spojeni denne (namerane v logu: 686 spojeni za dva dni,
	# presne 12 za hodinu). Horsie — HTTP API endpointy `api/*` v Tvheadende
	# NEPRECHADZAJU cez limiter spojeni (tcp_connection_launch vola len
	# stream/dvrfile/HTSP/SAT>IP cesta), takze HTTP kontrola prejde aj vtedy,
	# ked HTSP odmieta. Preto: skus najprv HTTP, HTSP az ako zaloha.
	# Vysledok sondy si pamatame, aby sme pri servere bez HTTP pristupu
	# nerobili zbytocnu dvojicu pokusov pri kazdej kontrole.
	_HTTP_PROBE_RETRY_SEC = 3600

	def _check_login_htsp_mode(self):
		import time as _t
		now = _t.time()
		http_ok = getattr(self, '_login_probe_http_ok', None)
		probe_ts = getattr(self, '_login_probe_ts', 0)
		if http_ok is False and (now - probe_ts) > self._HTTP_PROBE_RETRY_SEC:
			http_ok = None   # po hodine skus HTTP znova
		if http_ok is not False:
			try:
				self.api_get('api/serverinfo', params={},
				             timeout_override=self._CHECK_LOGIN_TIMEOUT)
				if http_ok is not True:
					self._log('kontrola spojenia ide cez HTTP api/serverinfo '
					          '(nezatazuje limit spojeni)')
				self._login_probe_http_ok = True
				self._login_probe_ts = now
				return True
			except Exception as e:
				self._login_probe_http_ok = False
				self._login_probe_ts = now
				self._log('HTTP kontrola nedostupna (%s) — prepinam na HTSP '
				          'handshake' % e)
		# Zaloha: plny HTSP handshake
		from . import htsp as _htsp
		host, port, user, pwd = self._htsp_params()
		client = _htsp.HTSPClient(host, port, user, pwd, cp=self.cp,
		                          timeout=self._CHECK_LOGIN_TIMEOUT)
		try:
			client.connect()   # vyhodí výnimku ak auth/spojenie zlyhá
		except _htsp.HTSPConnectionLimit as e:
			# Server bezi, len je obsadeny — to nie je zlyhanie prihlasenia.
			self._note_connlimit(e)
			return True
		client.close()
		self._clear_connlimit()
		return True

	# ------------------------------------------------------------------
	# Stream URL
	# ------------------------------------------------------------------





	def _log(self, msg):
		"""Log do archivCZSK.log cez framework.

		FIX 1.0.0 (audit): pred touto opravou tu ostali po refaktore 0.90.0
		tri osirelé `@staticmethod` dekorátory (metódy _write_stamp,
		_ctype_to_ext a _sniff_ext sa presunuli do _picons.py, ale ich
		dekorátory nie). Zreťazili sa na _log, takže KAŽDÉ volanie
		self._log(...) hodilo TypeError. Dekorátory sú odstránené a
		príslušné metódy v _picons.py majú @staticmethod späť."""
		try:
			if self.cp is not None and hasattr(self.cp, 'log_info'):
				self.cp.log_debug(str(msg))
		except Exception:
			pass













