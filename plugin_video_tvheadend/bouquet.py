# -*- coding: utf-8 -*-

import hashlib
import re
import time

from tools_archivczsk.generator.bouquet_xmlepg import BouquetXmlEpgGenerator, BouquetGenerator
# FIX 0.57.0: framework volá download_picons cez parent triedu
# (BouquetGeneratorTemplate.download_picons), nie cez child (BouquetGenerator).
# Pre monkey-patch loggingu musíme patch-núť parent.
from tools_archivczsk.generator.bouquet import BouquetGeneratorTemplate

# FIX 0.57.0 (skyjet PR #22 review): tools.archivczsk je guaranteed dependency
# (addon.xml require version 3.4+) — žiadny fallback netreba.
from tools_archivczsk.string_utils import strip_accents

from ._bouquet_common import BouquetCommonMixin
from ._bouquet_tags import BouquetTagsMixin
from ._bouquet_radio import BouquetRadioMixin
from ._bouquet_dvb import BouquetDvbMixin
from ._bouquet_picons import BouquetPiconsMixin
from ._bouquet_sids import StableSidMap


# FIX 0.48j: _PICON_LOG odstránené — logy idú cez print() do archivCZSK.log
# (sledovať cez `grep '\[plugin.tvheadend' /tmp/archivCZSK.log`)

# FIX 0.58.2 (skyjet PR #22 review #11 follow-up): `_EPG_INJECT_STAMP`
# odstránený spolu s celou custom inject_tvh_epg_into_enigma() cestou.
# Framework `BouquetXmlEpgGenerator` trigger-uje EPG inject automaticky.

# FIX 1.0.0 (audit): odstránené `_DOWNLOAD_PICONS_LOCK` (nikde nepoužitý —
# pozostatok po vlastnom picon flow zrušenom v 0.57.0) a debounce stav
# `_POST_CALLBACK_LOCK` / `_LAST_POST_CALLBACK_TS` / `_POST_CALLBACK_DEBOUNCE_SEC`,
# ktorý slúžil výhradne metóde refresh_userbouquet_start() — tá je tiež
# odstránená, viď poznámku pri refresh_bouquet().


# FIX 1.0.1: maskovanie prihlasovacich udajov v URL pred zapisom do logu.
# Pouziva sa vsade, kde by sa do archivCZSK.log mohla dostat URL v tvare
# scheme://user:heslo@host/cesta.
_CREDS_IN_URL = re.compile(r'(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@')


def _mask_credentials(url):
	"""Nahradi user:heslo v URL za ***. Nie-URL hodnoty vrati nezmenene."""
	if not url:
		return url
	try:
		return _CREDS_IN_URL.sub(lambda m: m.group('scheme') + '***@', str(url))
	except Exception:
		return '***'


# FIX 0.57.0: framework BouquetGeneratorTemplate.download_picons() volal
# s.get(url) s URL formátu http://user:pass@host/path — Python requests
# IGNORUJE inline credentials (CVE-2023-32681) → 401 na všetko. Preto sa
# framework download_picons monkey-patchuje.
#
# FIX 1.0.2 (Juraj): patch je teraz NO-OP. Predtým obsahoval vlastný
# download loop, ktorý bežal vo framework vlákne PARALELNE s
# _remap_picons_to_bouquet (viď _bouquet_picons.py) — obe stahovali tie isté
# súbory (rovnaké mená po normalizácii typu na 1), robili dve auth sondy a
# framework loop preskakoval existujúce súbory bez ohľadu na to, či sedia
# s kanálom. Jediná cesta je odteraz _remap_picons_to_bouquet: má presné
# mená podľa userbouquetu, index zdrojov (zmena loga = stiahnuť znova) a
# beží až po dokončení generovania, keď sú súbory na disku.
def _install_picon_download_patch(cp):
	"""Monkey-patch framework BouquetGeneratorTemplate.download_picons na no-op
	(idempotentne, raz za session). Picony stahuje _remap_picons_to_bouquet."""
	BGT = BouquetGeneratorTemplate
	if getattr(BGT, '_tvh_dp_patched', False):
		return

	_cp_ref = cp

	def _patched_dp(picons):
		try:
			_cp_ref.log_debug('[Tvheadend.picons] framework download_picons (%d) '
			                  'skipped — handled by _remap_picons_to_bouquet'
			                  % (len(picons) if picons else 0))
		except Exception:
			pass

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


class TvheadendBouquetXmlEpgGenerator(BouquetCommonMixin, BouquetTagsMixin, BouquetRadioMixin, BouquetDvbMixin, BouquetPiconsMixin, BouquetXmlEpgGenerator):
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
		# FIX 1.0.2: perzistentna mapa kanal -> SID (viď _bouquet_sids.py).
		# Vytvara sa tu (nie lenivo), lebo load_channel_list bezi aj z dvoch
		# vlakien naraz a dve sucasne inicializacie by dali dve mapy.
		try:
			self._sid_map = StableSidMap(log=self._log)
		except Exception as e:
			self._sid_map = None
			try:
				self.cp.log_error('[Tvheadend.bouquet] StableSidMap init failed: %s' % e)
			except Exception:
				pass
		# True = pred najblizsim generovanim bouquetu zmazat vsetky TVH
		# picony a stiahnut ich nanovo (jednorazovo po zavedeni stabilnych
		# SID, alebo na ziadost akcie "plny refresh piconov").
		self._picon_wipe_pending = False
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

		# FIX 1.0.2: stabilne SID. Cislo kanala uz NIE je 'id' (=SID) kanala;
		# sluzi len na zoradenie bouquetu. SID sa prideli raz a drzi sa
		# kanala (podla jeho uuid) aj po precislovani/pridani kanalov,
		# takze picon subory 1_0_1_<SID>_... ostavaju spravne priradene.
		sid_map = self._sid_map
		seeding_now = bool(sid_map is not None and sid_map.seeded)

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
			if sid_map is not None:
				try:
					stable = sid_map.get(uuid, seed_id=ch_id)
					if stable:
						ch_id = stable
				except Exception as e:
					self._log("load_channel_list: sid_map.get(%s) failed: %s" % (uuid, e))

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

		if sid_map is not None:
			try:
				saved = sid_map.save()
			except Exception:
				saved = False
			# Jednorazovy plny reset piconov AZ ked je mapa bezpecne na disku.
			# Keby sa neulozila (plny flash, read-only data dir), kazdy start
			# by znova "seedoval" a znova mazal picony — to nechceme.
			if seeding_now:
				if saved:
					self._picon_wipe_pending = True
					self._log("load_channel_list: SID mapa vytvorena (%d kanalov) — "
					          "picony sa jednorazovo stiahnu nanovo" % len(sid_map))
				else:
					try:
						self.cp.log_error('[Tvheadend.bouquet] SID mapu sa nepodarilo '
						                  'ulozit do %s — stabilne SID nebudu fungovat, '
						                  'skontroluj volne miesto/prava' % sid_map.path)
					except Exception:
						pass

		# FIX 0.57.0 debug: koľko channels skončilo s picon URL nastavenou
		try:
			with_picon = sum(1 for c in self._channels if c.get('picon'))
			without_picon = len(self._channels) - with_picon
			sample = next((c['picon'] for c in self._channels if c.get('picon')), None)
			# FIX 1.0.1: URL piconu obsahuje inline credentials
			# (http://user:heslo@host/...). Tento riadok sa do logu zapisal pri
			# kazdom load_channel_list — v dvojdnovom logu 36x — takze kazdy,
			# kto priloz archivCZSK.log k hlaseniu chyby, zverejnil svoje meno
			# a heslo k TVH serveru. Maskujeme.
			self._log("load_channel_list: %d channels total, %d with picon URL, %d without. "
			          "Sample picon URL: %r" % (len(self._channels), with_picon,
			                                     without_picon,
			                                     _mask_credentials(sample)))
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
			# FIX 1.0.2: jednorazovy plny reset piconov — po zavedeni
			# stabilnych SID (mapa prave vznikla) alebo na ziadost akcie
			# "Force re-download all TVH picons". Maze sa AZ TU (po parent
			# volani), lebo load_channel_list, ktory priznak nastavuje, bezi
			# vnutri parent refresh_bouquet. _remap nizsie potom stiahne vsetko.
			if self._picon_wipe_pending and self.get_setting('enable_picons'):
				self._picon_wipe_pending = False
				try:
					self._wipe_tvh_picons()
				except Exception as e:
					self._log("refresh_bouquet: _wipe_tvh_picons raised: %s" % e)

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

	# FIX 1.0.0 (audit): odstránená metóda refresh_userbouquet_start()
	# aj s jej ~120-riadkovým _post() callbackom. Dôvody:
	#   1) volala BouquetXmlEpgGenerator.refresh_userbouquet_start(), ktorá
	#      vo frameworku VÔBEC NEEXISTUJE (overené v tools_archivczsk:
	#      generator/bouquet_xmlepg.py má len refresh_bouquet, refresh_xmlepg,
	#      refresh_xmlepg_start a bouquet_settings_changed) — parent call teda
	#      vždy skončil na AttributeError v except vetve;
	#   2) _post() robil presne to isté čo robí refresh_bouquet() vyššie
	#      (_fix_radio_bouquet_filenames + eDVBDB reload + OpenWebif reload),
	#      len o sekundu neskôr a s vlastným debouncom;
	#   3) volal ju jediný kód v doplnku — fallback vetva v tvh_actions.py,
	#      ktorá bola tiež mŕtva, lebo bouquet_settings_changed() nezlyháva.
	# Spolu s ňou padol aj _picon_ready_event (nikto naň už nečaká).


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

		# FIX 1.0.0 (audit): cache sa buduje do LOKÁLNEHO dictu a priradí
		# až hotová (rovnaký vzor ako load_channel_list po 0.59.7). get_epg()
		# je generátor volaný frameworkom pre KAŽDÝ kanál pri exporte EPG;
		# medzitým môže iný thread (load_channel_list pri bouquet refreshi)
		# nastaviť self._epg_cache = None. Predtým to znamenalo buď
		# polopostavenú cache, alebo AttributeError uprostred exportu.
		cache = self._epg_cache
		if cache is None:
			cache = {}
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
						cache.setdefault(str(cid), []).append({
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
						cache.setdefault(cuuid, []).append(ev)
					except Exception:
						continue

			# atomické priradenie až po dokončení
			self._epg_cache = cache
			self._epg_cache_ts = now_ts

		for ev in cache.get(ch_uuid, []):
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
