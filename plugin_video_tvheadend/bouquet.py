# -*- coding: utf-8 -*-

import hashlib
import os
import time

# -------------------------------------------------
# Python 2/3 compatibility
# -------------------------------------------------
# FIX 0.57.0 (skyjet PR #22 review): threading je vždy dostupný v
# Py2.7+ aj Py3 (stdlib module); fallback try/except odstránený.
import threading




# FIX 0.57.0 (skyjet PR #22 review): urllib Py2/Py3 fallback nahradený
# centrálnym tools_archivczsk.compat helper-om. Predtým bol 8-riadkový
# try/except nested fallback (urllib.parse → urlparse → no-op lambda)
# vrátane mŕtveho `urllib_parse = None` ošetrenia. Compat helper rieši
# Py2/Py3 import path interne, módy a knižnice sú guaranteed dostupné.
from tools_archivczsk.compat import urlparse as _url_urlparse

from tools_archivczsk.generator.bouquet_xmlepg import BouquetXmlEpgGenerator, BouquetGenerator
# FIX 0.57.0: framework volá download_picons cez parent triedu
# (BouquetGeneratorTemplate.download_picons), nie cez child (BouquetGenerator).
# Pre monkey-patch loggingu musíme patch-núť parent.
from tools_archivczsk.generator.bouquet import BouquetGeneratorTemplate

# FIX 0.57.0 (skyjet PR #22 review): tools.archivczsk je guaranteed dependency
# (addon.xml require version 3.4+) — žiadny fallback netreba.
from tools_archivczsk.string_utils import strip_accents

try:
	from ._picons import _picon_ready_event as _tvh_picon_ready
except Exception:
	_tvh_picon_ready = None

# FIX 0.48j: import persistent data path helper
from ._paths import data_path
from ._bouquet_common import BouquetCommonMixin
from ._bouquet_tags import BouquetTagsMixin
from ._bouquet_radio import BouquetRadioMixin
from ._bouquet_dvb import BouquetDvbMixin


# FIX 0.48j: _PICON_LOG odstránené — logy idú cez print() do archivCZSK.log
# (sledovať cez `grep '\[plugin.tvheadend' /tmp/archivCZSK.log`)

# FIX 0.58.2 (skyjet PR #22 review #11 follow-up): `_EPG_INJECT_STAMP`
# odstránený spolu s celou custom inject_tvh_epg_into_enigma() cestou.
# Framework `BouquetXmlEpgGenerator` trigger-uje EPG inject automaticky.

# FIX 0.48b: module-level debounce stamp pre download_picons_from_bouquets.
# Framework BouquetXmlEpgGenerator volá _post() pri každom channel_type
# (tv + radio = 2×). Bez debouncu sa logika kopírovania a fallback
# downloadov spúšťa 2× po sebe → log spam + zbytočná záťaž.
# FIX 0.50beta: locky inicializované eagerly pri importe modulu namiesto
# lazy v rámci funkcie. Lazy init `if X is None: X = Lock()` mal race
# condition keď 2 thready prešli kontrolou predtým ako jeden vytvoril
# inštanciu — druhý prepísal lock cudzou inštanciou a debouncing stratil
# atomicitu. Eager init je O(1) pri starte a deterministické.
_DOWNLOAD_PICONS_LOCK = threading.Lock()

# FIX 0.48c: debounce pre celý refresh_userbouquet_start._post() callback.
# Predtým debouncoval len picon copy step (#FIX 0.48b), ale celý _post()
# obsahuje aj _fix_radio_bouquet_filenames() + eDVBDB.reloadBouquets() +
# OpenWebif HTTP request — tie bežali 2× za sebou (raz pre TV, raz pre Radio
# channel_type). Aplikujeme rovnakú debounce logiku ako pri picon copy,
# 30 sekundové okno je dostatočné na pokrytie tv+radio framework cyklu.
# FIX 0.50beta: eager init (rovnaký dôvod ako _DOWNLOAD_PICONS_LOCK).
_POST_CALLBACK_LOCK = threading.Lock()
_LAST_POST_CALLBACK_TS = [0]
_POST_CALLBACK_DEBOUNCE_SEC = 30


# FIX 0.57.0: framework BouquetGeneratorTemplate.download_picons() volá
# s.get(url) s URL formátu http://user:pass@host/path — Python requests
# IGNORUJE inline credentials a nepošle HTTP Basic Auth header (security
# policy z CVE-2023-32681). Výsledok: TVH vracia HTTP 401 pre VŠETKY
# requests a framework len silently skip-ne file. Tento monkey-patch
# nahradí framework download_picons vlastnou implementáciou ktorá
# explicitne aplikuje auth (Basic alebo Digest auto-detect cez probe).
#
# Patch je idempotent (sentinel flag na triede) a aplikuje sa raz pri
# prvom _TvhBouquetGenerator init-e per session.
#
# POZNÁMKA k vzťahu _patched_dp ↔ _remap_picons_to_bouquet (0.60.0):
# _patched_dp rieši AUTH + integráciu s framework picon flow (framework
# ho volá počas generovania bouquetu). _remap_picons_to_bouquet rieši
# FINÁLNE SID-presné umiestnenie picon súborov (meno = service ref
# z userbouquetu). Obe sú potrebné: bez _patched_dp framework download
# zlyhá na 401; bez _remap by picony mali nesprávny SID a Enigma2 by
# ich nezobrazila. _remap beží po _patched_dp a uloží picony pod
# správnym menom.
def _install_picon_download_patch(cp):
	"""Monkey-patch framework BouquetGeneratorTemplate.download_picons.

	Args:
	    cp: TvheadendContentProvider instance (pre logging cez cp.log_info/log_debug).
	"""
	BGT = BouquetGeneratorTemplate
	if getattr(BGT, '_tvh_dp_patched', False):
		return

	_cp_ref = cp

	def _patched_dp(picons):
		try:
			cnt = len(picons) if picons else 0
			_cp_ref.log_debug('[Tvheadend.debug] download_picons thread started: '
				'%d picons' % cnt)
		except Exception:
			pass

		picon_dir = '/usr/share/enigma2/picon'
		try:
			if not os.path.exists(picon_dir):
				os.mkdir(picon_dir)
		except Exception as _e:
			_cp_ref.log_error('[Tvheadend.picons] mkdir %s failed: %s' % (picon_dir, _e))
			return

		try:
			import requests as _req
			from requests.auth import HTTPDigestAuth as _DigestAuth
		except ImportError as _ie:
			_cp_ref.log_error('[Tvheadend.picons] requests library missing: %s' % _ie)
			return

		# Extract credentials z URL netloc + clean URLs (bez user:pass)
		user = None
		pwd = None
		cleaned = {}
		for _ref, _url in picons.items():
			if not _url:
				continue
			try:
				_p = _url_urlparse(_url)
				if _p.username:
					user = _p.username
					pwd = _p.password or ''
					_netloc = _p.hostname
					if _p.port:
						_netloc += ':' + str(_p.port)
					from tools_archivczsk.compat import urlunparse as _urlunparse_compat
					_clean_url = _urlunparse_compat((
						_p.scheme, _netloc, _p.path,
						_p.params, _p.query, _p.fragment))
					cleaned[_ref] = _clean_url
				else:
					cleaned[_ref] = _url
			except Exception:
				cleaned[_ref] = _url

		# Auto-detect Basic vs Digest auth cez probe na prvý URL
		sess = _req.Session()
		auth_mode = 'none'
		if user is not None and cleaned:
			_probe_url = next(iter(cleaned.values()))
			try:
				_pr = sess.get(_probe_url, auth=(user, pwd), timeout=8)
				if _pr.status_code == 200:
					sess.auth = (user, pwd)
					auth_mode = 'basic'
				elif _pr.status_code == 401:
					_pr2 = sess.get(_probe_url, auth=_DigestAuth(user, pwd), timeout=8)
					if _pr2.status_code == 200:
						sess.auth = _DigestAuth(user, pwd)
						auth_mode = 'digest'
					else:
						auth_mode = 'failed_both_basic_and_digest'
				else:
					auth_mode = 'failed_status_%d' % _pr.status_code
			except Exception as _pe:
				auth_mode = 'probe_error_%s' % _pe

		_cp_ref.log_info('[Tvheadend.picons] auth probe: %s' % auth_mode)

		# Skutočný download loop
		written = 0
		errs_404 = 0
		errs_other = 0
		exceptions = 0
		skipped_exists = 0

		def _picon_filename(ref):
			# FIX 0.59.2 (audit, Juraj): Enigma2 pri picon lookupe VŽDY
			# normalizuje service type (prvé pole service ref) na "1" — pre
			# všetky streamované typy (1/4097/5001/5002/...). Framework ale
			# generuje _ref s reálnym player_id (napr. "5002_0_1_..."), takže
			# picon súbor sa uložil ako "5002_0_1_...png" a Enigma2 ho pri
			# zobrazení (kde hľadá "1_0_1_...png") nikdy nenašiel. Preto
			# TVH picony "nesedeli" hoci boli na disku.
			# Oprava: normalizuj prvé pole na "1" pri ukladaní.
			parts = ref.split('_')
			if len(parts) >= 1 and parts[0] != '1':
				parts[0] = '1'
			return '_'.join(parts)

		try:
			for _ref, _url in cleaned.items():
				if not _url:
					continue
				_fileout = picon_dir + '/' + _picon_filename(_ref) + '.png'
				if os.path.exists(_fileout):
					skipped_exists += 1
					continue
				try:
					_r = sess.get(_url, timeout=10)
					if _r.status_code == 200 and _r.content:
						_ct = _r.headers.get('content-type', '').lower()
						if _ct.startswith('image/') or len(_r.content) > 100:
							with open(_fileout, 'wb') as _f:
								_f.write(_r.content)
							written += 1
						else:
							errs_other += 1
					elif _r.status_code == 404:
						errs_404 += 1
					else:
						errs_other += 1
				except Exception:
					exceptions += 1
		except Exception as _le:
			_cp_ref.log_error('[Tvheadend.picons] download loop crashed: %s' % _le)

		_cp_ref.log_info('[Tvheadend.picons] download done: '
			'written=%d, skipped_exists=%d, errs_404=%d, '
			'errs_other=%d, exceptions=%d' %
			(written, skipped_exists, errs_404, errs_other, exceptions))

	BGT.download_picons = staticmethod(_patched_dp)
	BGT._tvh_dp_patched = True


class _TvhBouquetGenerator(BouquetGenerator):
	"""
	Tenký override framework BouquetGenerator — aplikuje user-overridden
	bouquet display name z plugin settings (userbouquet_custom_name_tv /
	userbouquet_custom_name_radio). Všetka common init logika (prefix,
	profile suffix, namespace, TID, ONID, atď.) dedeí z framework parent.

	FIX 0.57.0 (skyjet PR #22 review #3): predtým bola tu plne vlastná
	CustomBouquetGenerator(BouquetGeneratorTemplate) trieda (~65 LoC)
	ktorá duplikovala celý parent init. Skyjet's feedback: "stačí self.name =
	... v BouquetXmlEpgGenerator triede". Zachytené ako 20-LoC minimal
	subclass-with-override.
	"""

	def __init__(self, bxeg, channel_type=None):
		# Framework parent vytvorí prefix, default name, namespace, TID, atď.
		BouquetGenerator.__init__(self, bxeg, channel_type)

		# 0.72.0: player_name="3" (resp. legacy "4") = "DVB (OE>=2.5)".
		# Framework nepozná náš DVB index spoľahlivo (jeho 3=DMM/8193), preto
		# mu pre generovanie dáme bezpečný základ exteplayer3 (5002) — vzniknú
		# čisté riadky s Playlive proxy URL. Skutočný prepis na typ 1 + priamu
		# TVH URL spraví refresh_bouquet -> _rewrite_bouquets_to_dvb (triggeruje
		# sa podľa uloženej hodnoty player_name=3/4, nie podľa tejto lokálnej).
		if str(self.player_name) in ('3', '4'):
			try:
				bxeg.cp.log_info('[Tvheadend.bouquet] player_name="%s" (DVB) — '
				                 'framework base = exteplayer3, refs sa prepíšu '
				                 'na native DVB (typ 1 + priama URL)'
				                 % self.player_name)
			except Exception:
				pass
			self.player_name = '2'

		# FIX 0.57.0: install picon download patch (idempotent, runs once)
		try:
			_install_picon_download_patch(bxeg.cp)
		except Exception as _e:
			try:
				bxeg.cp.log_error('[Tvheadend.picons] patch install failed: %s' % _e)
			except Exception:
				pass

		# Custom name override z user settings — nahradí framework default
		# "bxeg.name + ' ' + channel_type" ak je nastavený.
		try:
			if channel_type == 'radio':
				custom = (bxeg.get_setting('userbouquet_custom_name_radio') or '').strip()
			else:
				custom = (bxeg.get_setting('userbouquet_custom_name_tv') or '').strip()
		except Exception:
			custom = ''

		if custom:
			# Zachovať profile suffix ktorý framework appendol cez
			# bxeg.get_profile_info() — pre multi-profile setups.
			profile_info = None
			try:
				profile_info = bxeg.get_profile_info()
			except Exception:
				pass
			if profile_info is not None:
				self.name = custom + ' - ' + profile_info[1]
			else:
				self.name = custom


class TvheadendBouquetXmlEpgGenerator(BouquetCommonMixin, BouquetTagsMixin, BouquetRadioMixin, BouquetDvbMixin, BouquetXmlEpgGenerator):
	"""
	Tvheadend -> ArchivCZSK bouquet + xmlepg + enigmaepg generator
	"""

	def __init__(self, content_provider):
		self.cp = content_provider

		# POZOR: enable_userbouquet_cam u teba neexistuje -> spôsobovalo AttributeError
		self.bouquet_settings_names = (
			'enable_userbouquet',
			'enable_userbouquet_radio',
			# 'enable_userbouquet_cam',   # ❌ removed due to AttributeError
			'userbouquet_categories',

			# ✅ NEW settings (custom bouquet display names)
			'userbouquet_custom_name_tv',
			'userbouquet_custom_name_radio',

			# FIX 0.58.2 (skyjet PR #22 review #11 follow-up):
			# `tvh_epg_inject_interval` + `enigmaepg_days` nahradené
			# framework default setting names `enable_xmlepg` + `xmlepg_days`.
			# Custom direct-injection cesta odstránená — framework
			# `EnigmaEpgGenerator` to robí natívne cez `get_xmlepg_channels()`
			# + `get_epg()` (existujú v tomto súbore).
			'enable_xmlepg',
			'xmlepg_days',

			'enable_picons',
			'player_name',
			'bouquet_refresh_interval',
		)

		# ✅ support TV + RADIO
		BouquetXmlEpgGenerator.__init__(self, content_provider, channel_types=('tv', 'radio'))

		# ✅ override bouquet generator — minimal subclass over framework
		# default (viď _TvhBouquetGenerator), aplikuje len custom display name
		self.bouquet_generator = _TvhBouquetGenerator

		self._channels = []
		self._key_to_url = {}
		self._epg_cache = None
		self._epg_cache_ts = 0  # FIX 0.48c: TTL stamp pre _epg_cache
		self._tagmap = None

		# ✅ TAG ORDER CACHE (sorting categories according to TVH "index")
		self._taguuid_to_order = None        # uuid -> index
		self._tagnorm_to_order = None        # normalized-name -> index

	# -------------------------------------------------
	# logging helper
	# -------------------------------------------------


	# -------------------------------------------------
	# Settings helpers
	# -------------------------------------------------




	# -------------------------------------------------

	def logged_in(self):
		return True

	# -------------------------------------------------
	# ✅ TVH TAGS -> RADIO DETECT (+ categories)
	# -------------------------------------------------







	# -------------------------------------------------
	# CHANNELS
	# -------------------------------------------------

	def get_channels_checksum(self, channel_type):
		if channel_type not in ('tv', 'radio'):
			return '0'

		if not self._channels:
			self.load_channel_list()

		want_radio = (channel_type == 'radio')

		h = hashlib.md5()
		for ch in self._channels:
			if bool(ch.get('is_radio')) != want_radio:
				continue
			s = "%s|%s|%s|%s|%s" % (
				ch.get('uuid', ''),
				ch.get('name', ''),
				ch.get('id', 0),
				ch.get('icon_public_url') or '',
				'R' if ch.get('is_radio') else 'T'
			)
			h.update(s.encode('utf-8', errors='ignore'))
		return h.hexdigest()

	def load_channel_list(self):
		# FIX 0.59.7 (audit, Juraj): NEModifikuj self._channels priebežne.
		# Buduj lokálny list a atomicky ho priraď na konci. Keď bežali dva
		# refresh thready naraz (auto-refresh + manuálny, alebo dvojklik),
		# jeden resetoval self._channels=[] zatiaľ čo druhý appendoval →
		# kanály sa zdvojili (pozorované 587 → 1173 → 1174 v logu, kanály
		# duplicitné v bouquete). Lokálny build + dedup podľa uuid to rieši:
		# výsledok je vždy unikátny bez ohľadu na počet súbežných volaní.
		self._epg_cache = None
		self._epg_cache_ts = 0   # FIX 0.48c: reset TTL stamp pri reload kanálov
		self._tagmap = None

		# reset tag-order cache
		self._taguuid_to_order = None
		self._tagnorm_to_order = None

		try:
			channels = self.cp.tvh.get_channels() or []
		except Exception:
			channels = []

		channels = [c for c in channels if c.get('enabled', True)]

		def _num(x):
			try:
				return int(x.get('number') or 0)
			except Exception:
				return 0

		channels = sorted(channels, key=_num)

		local_channels = []
		local_key_to_url = {}
		seen_uuids = set()
		fallback_id = 10000
		for ch in channels:
			uuid = ch.get('uuid') or ''
			if not uuid:
				continue
			# DEDUP: ak rovnaký uuid už spracovaný, preskoč (ochrana proti
			# duplikátom z TVH API alebo opakovaného spracovania)
			if uuid in seen_uuids:
				continue
			seen_uuids.add(uuid)

			name = ch.get('name') or uuid
			number = _num(ch)

			service_uuid = ''
			try:
				services = ch.get('services') or []
				if services:
					service_uuid = services[0]
			except Exception:
				service_uuid = ''

			try:
				url = self.cp.tvh.make_live_stream_url(
					channel_uuid=uuid,
					service_uuid=(service_uuid or None)
				)
			except Exception:
				continue

			icon_public_url = (ch.get('icon_public_url') or '').strip()

			try:
				is_radio = self._is_radio_by_tags(ch.get('tags') or [])
			except Exception:
				is_radio = False

			ch_id = number if number > 0 else fallback_id
			if number <= 0:
				fallback_id += 1

			item = {
				'uuid': uuid,
				'name': name,
				'id': int(ch_id),
				'key': uuid,
				'adult': False,
				# FIX 0.57.0 (skyjet PR #22 review #11-#14): picon: URL
				# namiesto None. Framework BouquetGeneratorTemplate.download_picons()
				# si stiahne picons priamo z TVH HTTP API endpoint-u, sám
				# vyrieši SRP-based naming, PNG conversion (vrátane SVG/JPEG),
				# dedup, skip-existing. Custom plugin picon flow odstránený
				# (~700 LoC). Pre channels bez icon_public_url vráti
				# make_icon_http_url None — framework skip-uje.
				'picon': self.cp.tvh.make_icon_http_url(icon_public_url),
				'icon_public_url': icon_public_url,
				'is_radio': bool(is_radio),
				'tags': ch.get('tags') or [],
			}

			local_channels.append(item)
			local_key_to_url[uuid] = url

		# Atomické priradenie — až teraz, keď je lokálny list kompletný.
		self._channels = local_channels
		self._key_to_url = local_key_to_url

		# FIX 0.57.0 debug: koľko channels skončilo s picon URL nastavenou
		try:
			with_picon = sum(1 for c in self._channels if c.get('picon'))
			without_picon = len(self._channels) - with_picon
			sample = next((c['picon'] for c in self._channels if c.get('picon')), None)
			self._log("load_channel_list: %d channels total, %d with picon URL, %d without. "
			          "Sample picon URL: %r" % (len(self._channels), with_picon,
			                                     without_picon, sample))
		except Exception:
			pass

		return True

	def get_url_by_channel_key(self, channel_key):
		return self._key_to_url.get(channel_key, '')

	def get_bouquet_channels(self, channel_type=None):
		if not self._channels:
			self.load_channel_list()

		want_radio = (channel_type == 'radio')
		use_categories = self.get_setting("userbouquet_categories")

		# FIX: if separate radio bouquet is enabled, TV bouquet must not contain radio channels
		separate_radio = self.get_setting("enable_userbouquet_radio")

		if not use_categories:
			for ch in self._channels:
				if (channel_type == 'tv') and separate_radio and bool(ch.get('is_radio')):
					continue

				if bool(ch.get('is_radio')) != want_radio:
					continue
				yield {
					'name': ch['name'],
					'id': ch['id'],
					'key': ch['key'],
					'adult': False,
					'picon': ch.get('picon'),
					'is_separator': False,
				}
			return

		categories = {}
		for ch in self._channels:
			if (channel_type == 'tv') and separate_radio and bool(ch.get('is_radio')):
				continue

			if bool(ch.get('is_radio')) != want_radio:
				continue

			cats = self._get_channel_categories(ch)
			if not cats:
				cats = ["Ostatné"]

			for c in cats:
				categories.setdefault(c, []).append(ch)

		for cat in sorted(
			categories.keys(),
			key=lambda x: (
				self._category_order(x),
				strip_accents(x).lower() if x else ''
			)
		):
			yield {
				'name': "--- %s ---" % cat,
				'is_separator': True,
			}
			for ch in categories[cat]:
				yield {
					'name': ch['name'],
					'id': ch['id'],
					'key': ch['key'],
					'adult': False,
					'picon': ch.get('picon'),
					'is_separator': False,
				}

	def get_xmlepg_channels(self):
		if not self._channels:
			self.load_channel_list()

		for ch in self._channels:
			id_content = (ch['uuid'] or '').replace('-', '_')
			yield {
				'name': ch['name'],
				'id': ch['id'],
				'id_content': id_content,
				'key': ch['uuid'],
			}








	# Framework EPG injection: BouquetXmlEpgGenerator.refresh_xmlepg()
	# automaticky volá EnigmaEpgGenerator.run() → iteruje cez
	# get_xmlepg_channels() + get_epg() → eEPGCache.importEvent().

	def refresh_bouquet(self, *args, **kwargs):
		# FIX 0.58.5 (audit, Juraj): override framework `refresh_bouquet()`.
		# Toto je kľúčový hook ktorý sa volá pri:
		#   1. plugin init (po dokončení dependency resolve)
		#   2. settings_changed → bouquet_settings_changed → __bouquet_refreshed
		#      (TJ. keď user toggle-uje `enable_userbouquet` alebo
		#      `enable_userbouquet_radio` v UI cez "Auto-generovanie")
		#   3. periodic refresh cez `bouquet_refresh_interval`
		#
		# Predtým plugin override-oval iba `refresh_userbouquet_start()`,
		# ktorá sa volá iba pri (1) plus manuálnom export. Pri (2) toggle
		# path framework cestou `bouquet_settings_changed → refresh_bouquet`
		# vygeneroval userbouquet.tvheadend_radio.tv ale _fix_radio_bouquet_
		# filenames sa nikdy nezavolala → súbor zostal v .tv ext a v
		# bouquets.tv namiesto byť presunutý do bouquets.radio.
		#
		# Tento override volá parent refresh_bouquet a po jeho dobehnutí
		# zavolá _fix_radio_bouquet_filenames synchrónne. Framework metóda
		# je synchronous (nie async/threaded), takže keď return-uje,
		# userbouquet súbory sú už na disku.
		#
		# FIX 0.58.6 (audit, Juraj): Pri vypnutí `enable_userbouquet` framework
		# volá `userbouquet_remove()` ktorý hľadá súbor `userbouquet.<prefix>.tv`
		# (lebo `BouquetGeneratorTemplate.__init__` natvrdo nastavil
		# `userbouquet_file_name = "userbouquet.%s.tv" % self.prefix`). Náš
		# `_fix_radio_bouquet_filenames` ho ale predtým premenoval na
		# `userbouquet.tvheadend_radio.radio` — framework `.tv` súbor nenájde,
		# takže `Tvheadend Radio` zostane visieť v `bouquets.radio`. Cleanup
		# orphaned `.radio` súborov tu po parent calle ak je setting vypnutý.
		enabled_before = bool(self.get_setting('enable_userbouquet'))

		self._log("refresh_bouquet: starting (framework hook, enabled=%s)" % enabled_before)
		try:
			ret = BouquetXmlEpgGenerator.refresh_bouquet(self, *args, **kwargs)
		except Exception as e:
			self._log("refresh_bouquet: parent call failed: %s" % e)
			ret = None

		if enabled_before:
			# Enable path: rename .tv -> .radio + presun referencií
			try:
				self._fix_radio_bouquet_filenames()
			except Exception as e:
				self._log("refresh_bouquet: _fix_radio_bouquet_filenames raised: %s" % e)

			# 0.72.0: ak je vybraný player "DVB (OE>=2.5)" (player_name=3,
			# resp. legacy 4), prepíš service refs (typ 1 + priama TVH URL)
			# PRED remap picons, aby picon mená (1_0_1_...) sedeli.
			try:
				if str(self.get_setting('player_name')) in ('3', '4'):
					self._rewrite_bouquets_to_dvb()
			except Exception as e:
				self._log("refresh_bouquet: _rewrite_bouquets_to_dvb raised: %s" % e)

			# FIX 0.59.4 (audit, Juraj): premapuj picony na bouquet service
			# refs. Framework ukladá picon súbory s menom odvodeným z
			# interného SID páringu (napr. 5002_0_1_100_...), ktoré NEsedí
			# s service ref v userbouquete (1_0_1_2_...). Preto Enigma2
			# picony pri kanáloch nezobrazila. Táto metóda stiahne/premenuje
			# picony na presné meno = service ref z userbouquetu.
			try:
				self._remap_picons_to_bouquet()
			except Exception as e:
				self._log("refresh_bouquet: _remap_picons_to_bouquet raised: %s" % e)
		else:
			# Disable path: framework nevie zmazať .radio súbory (hľadá .tv).
			# Doupratujeme orphaned tvheadend .radio súbory + ich referencie
			# v bouquets.radio.
			try:
				self._cleanup_orphaned_radio_bouquets()
			except Exception as e:
				self._log("refresh_bouquet: _cleanup_orphaned_radio_bouquets raised: %s" % e)

		# Reload Enigma2 bouquet cache aby UI ihneď reflektoval rename
		# (.tv -> .radio) a presun referencií medzi bouquets.tv / .radio.
		#
		# FIX 0.59.6 (audit, Juraj): pridaný PLNÝ reload (reloadServicelist
		# + OpenWebif servicelistreload), nie len reloadBouquets. Bez
		# reloadServicelist Enigma2 drží staré picony v pamäti až do
		# reštartu GUI — preto manuálny toggle+reštart fungoval, ale menu
		# akcie (ktoré volajú refresh_bouquet) nie. E2m3u2bouquet plugin
		# (ktorého picony fungujú bez reštartu) volá presne túto sekvenciu:
		# reloadBouquets → reloadServicelist → OpenWebif servicelistreload.
		# Replikujeme ju 1:1 aby TVH picony sedeli rovnako bez reštartu.
		try:
			from enigma import eDVBDB
			db = eDVBDB.getInstance()
			db.reloadBouquets()
			self._log("refresh_bouquet: eDVBDB.reloadBouquets() OK")
			# KĽÚČOVÉ: reloadServicelist prinúti Enigma2 znova načítať
			# service list vrátane picon priradenia (bez reštartu GUI).
			try:
				db.reloadServicelist()
				self._log("refresh_bouquet: eDVBDB.reloadServicelist() OK")
			except Exception as _e:
				self._log("refresh_bouquet: reloadServicelist failed: %s" % _e)
		except ImportError:
			pass
		except Exception as e:
			self._log("refresh_bouquet: eDVBDB reload failed: %s" % e)

		# OpenWebif servicelistreload — dodatočný trigger ktorý vyčistí
		# aj skin picon cache (M3U plugin to robí rovnako).
		try:
			try:
				from urllib.request import urlopen as _urlopen
			except ImportError:
				from urllib2 import urlopen as _urlopen
			resp = _urlopen('http://127.0.0.1/web/servicelistreload?mode=2',
			                timeout=5)
			try:
				resp.read()
			finally:
				try:
					resp.close()
				except Exception:
					pass
			self._log("refresh_bouquet: OpenWebif servicelistreload OK")
		except Exception:
			# OpenWebif nemusí byť spustený — to je v poriadku
			pass

		return ret







	def refresh_userbouquet_start(self, *args, **kwargs):
		try:
			ret = BouquetXmlEpgGenerator.refresh_userbouquet_start(self, *args, **kwargs)
		except Exception:
			ret = None

		def _post():
			# FIX 0.58.5 (audit, Juraj): entry log pre diagnostiku.
			# Bez tohto logu sa nedalo zistiť či timer thread vôbec spustil
			# callback po refresh_userbouquet_start. Ak `enable_userbouquet_radio`
			# bol True pri starte refresh-u ale userbouquet.tvheadend_radio
			# stále mal .tv extension, znamenalo to že _post() sa buď
			# nevolal (timer thread crash), alebo bol debouncom preskočený,
			# alebo _fix_radio_bouquet_filenames zlyhalo silentne.
			self._log("refresh_userbouquet_start._post: starting (1s after refresh)")

			# FIX 0.48c: debounce celého _post() callbacku.
			# Framework volá refresh_userbouquet_start raz pre channel_type='tv'
			# a raz pre 'radio', takže _post() je v rade 2× ~1s od seba.
			# Bez debouncu by sa _fix_radio_bouquet_filenames(),
			# download_picons() (interne má svoj vlastný debounce) a 2×
			# eDVBDB.reloadBouquets() + OpenWebif request spustili dvojnásobne.
			# FIX 0.50beta: lock je teraz module-level eager init, nie lazy
			now_ts = int(time.time())
			if _POST_CALLBACK_LOCK is not None:
				with _POST_CALLBACK_LOCK:
					since = now_ts - _LAST_POST_CALLBACK_TS[0]
					if since < _POST_CALLBACK_DEBOUNCE_SEC:
						self._log("refresh_userbouquet_start._post called %ds "
						          "after last run (debounce %ds) — skipping "
						          "duplicate" % (since, _POST_CALLBACK_DEBOUNCE_SEC))
						return
					_LAST_POST_CALLBACK_TS[0] = now_ts

			# FIX 0.58.5 (audit, Juraj): log výnimky pred swallow-om. Pred
			# audit-om bol blok `try: _fix_radio(); except: pass` ktorý
			# silentne pohltil každú chybu — výsledok bol že keď
			# _fix_radio_bouquet_filenames zlyhala (z akéhokoľvek dôvodu),
			# userbouquet.tvheadend_radio.tv zostal v bouquets.tv namiesto
			# byť presunutý do bouquets.radio, ale nikde sa to nedalo zistiť.
			try:
				self._fix_radio_bouquet_filenames()
			except Exception as e:
				self._log("_fix_radio_bouquet_filenames raised: %s" % e)
			# Počkaj kým picon worker dobeží (max 120 sekúnd)
			# Používame threading.Event namiesto sleep slučky – efektívnejšie
			# FIX 0.48c: event sa teraz správne čistí na začiatku worker-a
			# (predtým bol set forever a wait() sa vracal okamžite).
			try:
				if _tvh_picon_ready is not None:
					_tvh_picon_ready.wait(timeout=120)
				else:
					# fallback na stamp kontrolu
					# FIX 0.48j: persistent data dir (rovnaký path ako tvheadend._PICON_STAMP)
					_picon_stamp_path = data_path("tvh_picon.stamp")
					for _ in range(120):
						if os.path.isfile(_picon_stamp_path):
							break
						time.sleep(1)
			except Exception:
				pass
			# FIX 0.57.0 (skyjet PR #22 review #14): explicit self.download_picons()
			# call removed — BouquetGeneratorTemplate.run() automaticky volá
			# download_picons() v background thread keď enable_picons=True.

			# FIX 0.48: po dokončení refresh-u prinúť Enigma2 znovu načítať
			# bouquet súbory z disku — bez tohto user musel reštartovať Enigma2
			# aby uvidel nové kanály v live TV. M3UBouquetWriter to už robí
			# (eDVBDB + OpenWebif), pridávame ekvivalent aj sem.
			try:
				from enigma import eDVBDB
				db = eDVBDB.getInstance()
				db.reloadBouquets()
				self._log("eDVBDB.reloadBouquets() OK after TVH bouquet refresh")
				try:
					db.reloadServicelist()
				except Exception:
					pass
			except ImportError:
				# bežíme mimo Enigma2 (testy) — preskoč
				pass
			except Exception as e:
				self._log("eDVBDB.reloadBouquets() failed: %s" % e)

			# OpenWebif fallback — funguje aj keď enigma cache caching skin
			try:
				try:
					from urllib.request import urlopen as _urlopen
				except ImportError:
					from urllib2 import urlopen as _urlopen
				resp = _urlopen('http://127.0.0.1/web/servicelistreload?mode=2',
				                timeout=5)
				try:
					resp.read()
				finally:
					try:
						resp.close()
					except Exception:
						pass
				self._log("OpenWebif servicelistreload OK")
			except Exception:
				# OpenWebif nemusí byť spustený — to je v poriadku
				pass

			# FIX 0.58.2 (skyjet PR #22 review #11 follow-up): custom EPG
			# injection v _post() callback-u odstránená. Framework
			# `BouquetXmlEpgGenerator.bouquet_settings_changed` po
			# `refresh_bouquet` automaticky volá `refresh_xmlepg` ktorá
			# spustí `EnigmaEpgGenerator.run()` → iteruje cez
			# `get_xmlepg_channels()` + `get_epg()` → priame importEvent()
			# volania. Tým sa odstránila duplicita.

		try:
			t = threading.Timer(1.0, _post)
			t.daemon = True
			t.start()
		except Exception:
			pass

		return ret

	# -------------------------------------------------
	# FAST EPG
	# -------------------------------------------------


	# FIX 0.48c: TTL pre EPG cache.
	# Predtým: _epg_cache sa naplnil pri prvom volaní get_epg() a držal sa
	# navždy. Pri preload="yes" plugine s 24/7 boxom to znamenalo že po
	# týždni mal generátor stále EPG zo dňa štartu E2. Teraz: 30 min TTL,
	# po expirácii sa nasledujúce volanie naparuje fresh data.
	_EPG_CACHE_TTL_SEC = 1800  # 30 min

	def get_epg(self, channel, fromts, tots):
		ch_uuid = channel.get('key') or ''
		if not ch_uuid:
			return

		fromts_i = int(fromts)
		tots_i = int(tots)

		# FIX 0.48c: TTL check pre _epg_cache
		now_ts = int(time.time())
		cache_ts = getattr(self, '_epg_cache_ts', 0)
		if (self._epg_cache is not None and cache_ts > 0
		        and (now_ts - cache_ts) >= self._EPG_CACHE_TTL_SEC):
			self._epg_cache = None
			try:
				self._log("EPG cache expired (age %ds > TTL %ds), reloading" %
				          (now_ts - cache_ts, self._EPG_CACHE_TTL_SEC))
			except Exception:
				pass

		if self._epg_cache is None:
			self._epg_cache = {}
			self._epg_cache_ts = now_ts
			if getattr(self.cp.tvh, 'is_htsp_mode', lambda: False)():
				# HTSP mód: EPG z HTSP metadát (channelUuid = str(channelId))
				try:
					data = self.cp.tvh.htsp_fetch_metadata(with_epg=True) or {}
					for ev in data.get('events', []):
						cid = ev.get('channelId')
						if cid is None:
							continue
						start = int(ev.get('start') or 0)
						stop = int(ev.get('stop') or 0)
						if not start or not stop:
							continue
						if stop <= fromts_i or start >= tots_i:
							continue
						self._epg_cache.setdefault(str(cid), []).append({
							'start': start, 'stop': stop,
							'title': ev.get('title') or '',
							'description': ev.get('description') or ev.get('summary') or '',
						})
				except Exception:
					pass
			else:
				try:
					data = self.cp.tvh.api_get(
						"api/epg/events/grid",
						{"limit": 999999, "sort": "start", "dir": "ASC"}
					) or {}
					entries = data.get("entries") or []
				except Exception:
					entries = []

				for ev in entries:
					try:
						cuuid = ev.get("channelUuid")
						if not cuuid:
							continue
						start = int(ev.get("start") or 0)
						stop = int(ev.get("stop") or 0)
						if not start or not stop:
							continue
						if stop <= fromts_i or start >= tots_i:
							continue
						self._epg_cache.setdefault(cuuid, []).append(ev)
					except Exception:
						continue

		for ev in self._epg_cache.get(ch_uuid, []):
			try:
				start = int(ev.get("start") or 0)
				stop = int(ev.get("stop") or 0)

				title = (self._pick(ev.get("title")) or '').strip()
				desc = (self._pick(ev.get("description")) or self._pick(ev.get("summary")) or '').strip()

				if not title:
					continue

				yield {"start": start, "end": stop, "title": title, "desc": desc}
			except Exception:
				continue
