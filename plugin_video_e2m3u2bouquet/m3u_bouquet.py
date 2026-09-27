# -*- coding: utf-8 -*-
"""
M3UBouquetWriter - converts M3UProvider channel data into:
  - userbouquet_*.tv file with fake-DVB (type=1) service refs
  - bouquets.tv inclusion line
  - epgimport channels/sources XML files
  - per-channel picons downloaded from tvg-logo URLs

Service refs use the 'fake DVB' format:
    1:0:1:{SID hex}:{TSID hex}:{ONID hex}:0:0:0:0:{URL}:{Name}

This format makes Enigma2 treat the HTTP stream as a native DVB service,
which enables HW DVB subtitle rendering (unlike 4097/5001/5002 paths).

Python 2.7 + Python 3.x compatible.
"""

from __future__ import absolute_import, unicode_literals, print_function

import os
import io
import re
import sys
import json
import hashlib
import threading

try:
	from urllib.request import Request, urlopen
	from urllib.parse import quote as urlquote
	from urllib.parse import unquote as urlunquote
except ImportError:
	from urllib2 import Request, urlopen
	from urllib import quote as urlquote
	from urllib import unquote as urlunquote

try:
	from .m3u_sids import StableSidMap, shared_tvg_ids, \
		load_known_category_pairs
	from ._paths import data_path
except (ValueError, ImportError):
	from m3u_sids import StableSidMap, shared_tvg_ids, \
		load_known_category_pairs
	from _paths import data_path

try:
	basestring
except NameError:
	basestring = (str, bytes)

_PY2 = sys.version_info[0] < 3


# Standard Enigma2 paths (some images use /etc/enigma2/, custom builds vary)
DEFAULT_BOUQUET_DIR = '/etc/enigma2'
DEFAULT_PICON_DIR = '/usr/share/enigma2/picon'
DEFAULT_EPGIMPORT_DIR = '/etc/epgimport'

# FIX 0.1.3: Radio category detection. Channels v týchto kategóriách
# (case-insensitive contains match) idú do `userbouquet.<prefix>.radio`
# namiesto `userbouquet.<prefix>.tv`. Enigma2 ich potom zobrazí len pod
# Radio button-om, nie TV button-om.
_RADIO_CATEGORY_KEYWORDS = (
	'rádia', 'rádiá', 'radia', 'radio', 'rádio', 'rozhlas', 'rozhlasy',
)


def _is_radio_category(category_name):
	"""Returns True if category name suggests it contains radio stations."""
	if not category_name:
		return False
	n = category_name.lower().strip()
	return any(k in n for k in _RADIO_CATEGORY_KEYWORDS)


# FIX 0.48f: prefix názvu súboru M3U userbouquetu — predtým configurable
# setting `m3u_bouquet_prefix`, teraz hardcoded. UI tým zostáva čistejšie
# (prefix bol pre 100% userov irelevantný — Enigma2 ho nikdy nezobrazuje,
# je len súčasť názvu súboru na disku). Pre power-users ktorí by chceli
# custom prefix: zmeniť túto konštantu v zdrojáku.
M3U_BOUQUET_PREFIX = 'm3u_iptv'

# FIX 1.0.0: index zdrojov piconov (data dir): picon meno -> tvg-logo URL.
# Keď sa kanálu zmení logo, picon sa stiahne znova aj keď súbor už existuje
# (predtým "existuje = preskoč"). Sťahovanie beží v malom poole vlákien.
_PICON_SOURCES_FILE = 'm3u_picon_sources.json'
_PICON_WORKERS = 4
_PICON_UA = 'Mozilla/5.0 (Enigma2) M3UProvider/1.0'


# FIX 0.48h: build_url_to_sref_from_bouquet — pomocný parser pre existujúci
# userbouquet.<prefix>.tv. Vracia {decoded_url: [short_service_ref_with_colon, ...]}.
#
# Použité v M3URefreshManager.inject_epg_only(), ktorý NEVYTVÁRA nový bouquet
# (to robí len full refresh) — potrebuje teda namapovať live channels späť na
# service refs zapísané pri minulom bouquet refresh-i, aby ich vedel naviazať
# na XMLTV programmy v eEPGCache.
#
# FIX 1.0.0: jedna URL môže byť v bouquete viackrát (ten istý kanál vo
# viacerých kategóriách = rôzne TSID/ONID), preto zoznam refov, nie jeden.
def build_url_to_sref_from_bouquet(bouquet_path):
	"""Parsuje existujúci userbouquet.<prefix>.tv. Vráti dict
	{decoded_stream_url: [short_service_ref_with_trailing_colon, ...]}.

	Vynechá kategoriálne markery (1:64:...). Vráti {} ak súbor neexistuje
	alebo nemá žiadne stream entries.

	Short sref formát: '1:0:1:sid:tsid:onid:0:0:0:0:' — taký aký očakáva
	eEPGCache.importEvent() (prvých 10 polí + trailing ':').
	"""
	out = {}
	if not bouquet_path or not os.path.isfile(bouquet_path):
		return out
	try:
		with io.open(bouquet_path, 'r', encoding='utf-8') as f:
			for line in f:
				if not line.startswith('#SERVICE '):
					continue
				sref_full = line[len('#SERVICE '):].strip()
				parts = sref_full.split(':')
				# Markery nemajú stream URL — skip
				if len(parts) < 11:
					continue
				if parts[1] == '64':  # marker type
					continue
				# Polia 0..9 = standard service ref, pole 10 = encoded URL,
				# pole 11+ = názov kanála (môže obsahovať ':').
				short_sref = ':'.join(parts[:10]) + ':'
				url_encoded = parts[10]
				if not url_encoded:
					continue
				try:
					url_decoded = urlunquote(url_encoded)
				except Exception:
					url_decoded = url_encoded
				if url_decoded:
					refs = out.setdefault(url_decoded, [])
					if short_sref not in refs:
						refs.append(short_sref)
	except Exception:
		# pri akejkoľvek chybe nech vráti čo má, alebo prázdne
		pass
	return out


def _atomic_write(path, data, mode='w', encoding='utf-8'):
	"""Write file atomically via temp + rename."""
	tmp = path + '.tmp'
	if 'b' in mode:
		with open(tmp, mode) as f:
			f.write(data)
	else:
		with io.open(tmp, mode, encoding=encoding) as f:
			f.write(data)
	if hasattr(os, 'replace'):
		os.replace(tmp, path)
	else:  # py2
		if os.path.exists(path):
			os.remove(path)
		os.rename(tmp, path)


def _safe_filename(s):
	"""Make a string safe for use as a filename slug."""
	if not isinstance(s, basestring):
		s = str(s)
	if isinstance(s, bytes):
		s = s.decode('utf-8', errors='ignore')
	s = re.sub(r'[^A-Za-z0-9_\-]+', '_', s.strip().lower())
	return s.strip('_') or 'bouquet'


def _e2_url_encode(url):
	"""
	Encode URL for inclusion in an E2 service ref string.

	The service ref uses ':' as field separator, so ':' MUST be encoded.
	'/' is safe to leave as-is (E2 does not use it as separator).
	'?', '&', '=' are encoded for parity with e2m3u2bouquet output
	(its bouquets are proven to work with DVB-subtitle path).

	FIX 1.0.0: na Py2 urllib.quote() padá na unicode s ne-ASCII znakmi
	(KeyError) — najprv zakódovať do UTF-8 bajtov.
	"""
	if _PY2 and not isinstance(url, bytes):
		url = url.encode('utf-8')
	# safe= musí byť natívny str — s unicode_literals by na Py2 bol unicode
	# a urllib.quote robí bytes.rstrip(unicode) -> UnicodeDecodeError
	quoted = urlquote(url, safe=str('/'))
	if _PY2 and isinstance(quoted, bytes):
		quoted = quoted.decode('ascii')
	return quoted


def _picon_name_from_fields(stype, sid_hex, tsid_hex, onid_hex):
	"""Meno picon súboru pre service ref polia = PRESNÉ meno refu z bouquetu
	(<typ>_0_1_<SID>_<TSID>_<ONID>_0_0_0_0.png), vrátane typu prehrávača.

	FIX 1.0.0 (test Juraj, OpenATV): prvý návrh normalizoval typ na 1
	(`1_0_1_...`). To funguje pre Tvheadend doplnok (namespace 7070000),
	ale NIE pre M3U refy s namespace 0: OpenATV getPiconName najprv skúsi
	presné meno, potom — keď namespace nekončí na "0000" — nahradí ho
	("0" -> "0000") a AŽ POTOM skúša typ 1, teda hľadá
	`1_0_1_<SID>_<TSID>_<ONID>_0000_0_0_0`, nikdy `..._0_0_0_0`. Presné meno
	refu nájde OpenATV aj OpenPLi v prvom kroku. Pri zmene prehrávača
	(iný typ) sa picony stiahnu nanovo pod novým menom; staré zmaže
	wipe podľa vzoru (typ je v ňom ľubovoľný).
	"""
	return '{}_0_1_{}_{}_{}_0_0_0_0.png'.format(stype or '1', sid_hex, tsid_hex, onid_hex)


def _picon_name_re_for_pairs(pairs):
	"""Regex na picon súbory tohto doplnku pre dané (TSID, ONID) páry:
	<typ>_0_<stype>_<SID>_<TSID>_<ONID>_0_0_0_0.png. Typ môže byť 1 aj
	legacy 4097/5001/5002 z verzií pred normalizáciou (0.2.1 a staršie)."""
	tails = []
	for pair in pairs or ():
		try:
			t, o = str(pair[0]).upper(), str(pair[1]).upper()
		except Exception:
			continue
		if re.match(r'^[0-9A-F]{1,4}$', t) and re.match(r'^[0-9A-F]{1,4}$', o):
			tails.append(re.escape('_%s_%s_0_0_0_0' % (t, o)))
	if not tails:
		return None
	return re.compile(r'^\d+_0_[0-9A-F]+_[0-9A-F]+(?:' + '|'.join(tails)
	                  + r')\.png$', re.I)


def _picon_sources_path():
	return data_path(_PICON_SOURCES_FILE)


def _picon_sources_load():
	try:
		with open(_picon_sources_path(), 'r') as f:
			d = json.load(f)
		return d if isinstance(d, dict) else {}
	except Exception:
		return {}


def _picon_sources_save(d, log=None):
	log = log or (lambda *a, **k: None)
	p = _picon_sources_path()
	tmp = p + '.tmp'
	try:
		with open(tmp, 'w') as f:
			json.dump(d, f, sort_keys=True)
			f.flush()
			os.fsync(f.fileno())
		os.rename(tmp, p)
	except Exception as e:
		log('[M3U] picon_sources: cannot write %s: %s' % (p, e))
		try:
			os.remove(tmp)
		except Exception:
			pass


def wipe_m3u_picons(picon_dir, pairs, log=None):
	"""Zmaže VŠETKY picony patriace tomuto doplnku — každý
	`*_<TSID>_<ONID>_0_0_0_0.png` pre dané (TSID, ONID) páry kategórií —
	plus index zdrojov. Picony iných doplnkov/satelitné ostávajú
	(ich TSID/ONID nie sú v zozname). Vráti počet zmazaných súborov.

	`pairs` = páry kategórií aktuálneho playlistu + páry zapamätané
	v m3u_sids.json (aj kategórie, ktoré už v playliste nie sú).
	"""
	log = log or (lambda *a, **k: None)
	rx = _picon_name_re_for_pairs(pairs)
	deleted = 0
	if rx is None:
		log('[M3U] wipe_picons: no category pairs known, nothing deleted')
	elif not os.path.isdir(picon_dir):
		log('[M3U] wipe_picons: picon dir %s missing, nothing deleted' % picon_dir)
	else:
		try:
			names = os.listdir(picon_dir)
		except Exception as e:
			log('[M3U] wipe_picons: cannot list %s: %s' % (picon_dir, e))
			names = []
		for fn in names:
			if not rx.match(fn):
				continue
			try:
				os.remove(os.path.join(picon_dir, fn))
				deleted += 1
			except Exception:
				pass
	try:
		os.remove(_picon_sources_path())
	except Exception:
		pass
	log('[M3U] wipe_picons: deleted %d picon files (%d category pairs)'
	    % (deleted, len(pairs or ())))
	return deleted


def _ns_for_category(category_name):
	"""
	Deterministic TSID + ONID from category name.
	Stable across refreshes => stable picon naming.

	Vracia UPPERCASE hex BEZ leading zeros. Enigma2 normalizuje service
	ref pri internej reprezentácii a odstráni leading zeros z hex polí
	(napr. `08D0` -> `8D0`). Bouquet entry musí zodpovedať tomu čo Enigma2
	interne vidí, inak picon filename (postavený z rovnakého hash) nesedí
	s tým, čo Enigma2 hľadá v searchPath.
	"""
	h = hashlib.md5(category_name.encode('utf-8')).hexdigest()
	# int -> 'X' format strips leading zeros (e.g. '08d0' -> '8D0')
	tsid_hex = format(int(h[0:4], 16), 'X')
	onid_hex = format(int(h[4:8], 16), 'X')
	return tsid_hex, onid_hex


class M3UBouquetWriter(object):
	"""
	Renders an M3UProvider into Enigma2 bouquet + picon + EPG files.
	"""

	def __init__(self, provider, settings, log=None, translate=None):
		"""
		settings: dict-like with keys:
		    bouquet_prefix          (str)   e.g. 'm3u_iptv'  -> userbouquet_m3u_iptv.tv
		    bouquet_display_name    (str)   shown in E2 menu
		    service_type            (str)   '1' / '4097' / '5001' / '5002'
		    add_category_markers    (bool)  insert category separators
		    bouquet_dir             (str)   default /etc/enigma2
		    picon_dir               (str)   default /usr/share/enigma2/picon
		    download_picons         (bool)
		    enable_radio_bouquet    (bool)  radio channels -> .radio bouquet
		    sid_map                 (StableSidMap, optional, tests)
		translate: callable(msgid) -> localised string (framework `_`)

		Note: `epgimport_dir`, `write_epgimport`, `epg_source_url`,
		`epg_source_description` keys boli odstránené v audite — write
		path do /etc/epgimport/ nahradila direct injection cez
		m3u_epg_injector. Cleanup ale ešte tieto súbory vie zmazať pre
		legacy inštalácie (cez `cleanup_m3u_bouquet()`).
		"""
		self.provider = provider
		self.s = settings
		self.log = log or (lambda *a, **k: None)
		# FIX 1.0.0: prekladová funkcia pre názov radio bouquetu
		self._ = translate or (lambda s: s)
		# FIX 1.0.0: perzistentná SID mapa (viď m3u_sids.py). Test môže
		# poslať vlastnú inštanciu cez settings['sid_map'].
		# (nie `or None` — prázdna mapa má len()==0 a bola by falsy)
		self.sid_map = settings.get('sid_map')
		if self.sid_map is not None and not isinstance(self.sid_map, StableSidMap):
			self.sid_map = None
		# jednorazový plný reset piconov po vzniku SID mapy (viď write_bouquet)
		self._picon_wipe_pending = False
		# (TSID, ONID) páry kategórií aktuálneho playlistu
		self._category_pairs = []

	def _get_sid_map(self):
		if self.sid_map is None:
			self.sid_map = StableSidMap(log=self.log)
		return self.sid_map

	# ------------------ Service ref building ------------------

	def _build_service_ref(self, sid, channel, category_name):
		stype = str(self.s.get('service_type', '1')).strip() or '1'
		sid_hex = format(sid, 'X')   # UPPERCASE hex (zhodne s framework)
		tsid_hex, onid_hex = _ns_for_category(category_name)

		# Build URL for stream. If channel has custom headers (User-Agent
		# from #EXTVLCOPT), we *cannot* encode those into the service ref;
		# they would need a proxy script. Log a warning if present.
		if channel.get('_extra_headers'):
			self.log('[M3U] WARN: channel "%s" has custom HTTP headers '
			         '(User-Agent/Referer) that E2 cannot pass directly; '
			         'consider a stream-proxy.' % channel['name'])

		url_encoded = _e2_url_encode(channel['url'])

		# Some E2 builds also need ':' in the name escaped; replace with ' '
		safe_name = (channel['name'] or '').replace(':', ' ').replace('\n', ' ')

		ref = '{stype}:0:1:{sid}:{tsid}:{onid}:0:0:0:0:{url}:{name}'.format(
			stype=stype, sid=sid_hex, tsid=tsid_hex, onid=onid_hex,
			url=url_encoded, name=safe_name,
		)
		return ref

	def _build_category_marker(self, category_idx, category_name):
		"""
		Category separator line (greyed-out marker in bouquet list).
		Format: 1:64:{idx hex}:0:0:0:0:0:0:0::{Name}
		"""
		safe_name = (category_name or '').replace(':', ' ').replace('\n', ' ')
		return '1:64:{:x}:0:0:0:0:0:0:0::{}'.format(category_idx, safe_name)

	# ------------------ Bouquet file ------------------

	def write_bouquet(self):
		# FIX 0.48f: bouquet_prefix sa už nečíta zo settings (UI cleanup) —
		# vždy hardcoded constant M3U_BOUQUET_PREFIX. self.s.get(...) ostáva
		# pre prípad že caller explicitne pošle override, inak fallback.
		prefix = self.s.get('bouquet_prefix') or M3U_BOUQUET_PREFIX
		prefix = _safe_filename(prefix)
		display_name = (self.s.get('bouquet_display_name')
		                or 'IPTV M3U').strip()

		bouquet_dir = self.s.get('bouquet_dir', DEFAULT_BOUQUET_DIR)
		add_markers = bool(self.s.get('add_category_markers', True))
		enable_radio = bool(self.s.get('enable_radio_bouquet', True))

		if not os.path.isdir(bouquet_dir):
			raise RuntimeError('Bouquet dir does not exist: %s' % bouquet_dir)

		tv_filename = 'userbouquet.{}.tv'.format(prefix)
		radio_filename = 'userbouquet.{}.radio'.format(prefix)
		tv_path = os.path.join(bouquet_dir, tv_filename)
		radio_path = os.path.join(bouquet_dir, radio_filename)
		# FIX 1.0.0: prípona cez prekladovú funkciu (msgid "Radio")
		radio_display = display_name + ' ' + self._('Radio')

		tv_lines = ['#NAME {}'.format(display_name)]
		radio_lines = ['#NAME {}'.format(radio_display)]

		tv_count = 0
		radio_count = 0

		# FIX 1.0.0: stabilné SID z perzistentnej mapy (m3u_sids.py).
		# Poradové číslo kanála (doterajšia schéma SID = globálny index cez
		# všetky kategórie v poradí playlistu) slúži už len ako seed pri
		# prvom vytvorení mapy, aby existujúce inštalácie zachovali refy.
		sid_map = self._get_sid_map()
		seeding_now = sid_map.seeded
		all_channels = self.provider.get_all_channels()
		shared_ids = shared_tvg_ids(all_channels)
		self._category_pairs = []
		seed_idx = {}
		idx = 1
		for cat in self.provider.get_categories():
			for ch in self.provider.get_channels_by_category(cat):
				seed_idx[id(ch)] = idx
				idx += 1

		for cat_idx, cat in enumerate(self.provider.get_categories()):
			chs = self.provider.get_channels_by_category(cat)
			if not chs:
				continue

			tsid_hex, onid_hex = _ns_for_category(cat)
			if (tsid_hex, onid_hex) not in self._category_pairs:
				self._category_pairs.append((tsid_hex, onid_hex))
			sid_map.remember_category(tsid_hex, onid_hex)

			# FIX 1.0.0: radio = názov kategórie (heuristika) ALEBO atribút
			# radio="true" v #EXTINF kanála. Kategória sa môže rozdeliť:
			# rádiá do .radio, ostatné do .tv — každá časť s vlastným markerom.
			cat_is_radio = enable_radio and _is_radio_category(cat)
			groups = []   # [(is_radio, [channels...])]
			if cat_is_radio:
				groups.append((True, chs))
			else:
				tv_chs = [c for c in chs if not (enable_radio and c.get('radio'))]
				radio_chs = [c for c in chs if enable_radio and c.get('radio')]
				if tv_chs:
					groups.append((False, tv_chs))
				if radio_chs:
					groups.append((True, radio_chs))

			for is_radio, part in groups:
				target = radio_lines if is_radio else tv_lines

				if add_markers:
					target.append('#SERVICE '
					              + self._build_category_marker(cat_idx, cat))
					target.append('#DESCRIPTION ' + cat)

				for ch in part:
					# identita: URL primárne, tvg-id alias (viď m3u_sids.py)
					seed = seed_idx.get(id(ch), 1)
					sid = sid_map.sid_for_channel(ch, shared_ids, seed_id=seed)
					if sid is None:
						sid = seed
					ref = self._build_service_ref(sid, ch, cat)
					ch['_service_ref'] = ref       # remember for picon/epg use
					target.append('#SERVICE ' + ref)
					target.append('#DESCRIPTION ' + (ch['name'] or ''))
					if is_radio:
						radio_count += 1
					else:
						tv_count += 1

		# Mapa na disk EŠTE PRED zápisom bouquetu — keby sa neuložila,
		# refy v bouquete by pri ďalšom refreshi mohli byť iné.
		saved = sid_map.save()
		if not saved:
			self.log('[M3U] ERROR: SID map could not be saved to %s — service '
			         'refs will NOT be stable across refreshes; check free '
			         'space / permissions of the data dir' % sid_map.path)
		elif seeding_now:
			# Jednorazový plný reset piconov AŽ keď je mapa bezpečne na
			# disku. Keby sa neuložila, každý refresh by znova "seedoval"
			# a znova mazal picony — to nechceme.
			self._picon_wipe_pending = True
			self.log('[M3U] SID map created (%d channels) — picons will be '
			         're-downloaded once' % len(sid_map))

		if tv_count > 0:
			_atomic_write(tv_path, '\n'.join(tv_lines) + '\n')
			self.log('[M3U] Wrote TV bouquet: %s (%d channels)'
			         % (tv_path, tv_count))
			# Update bouquets.tv to include our TV bouquet
			self._ensure_bouquet_included(
				bouquet_dir, tv_filename, display_name, is_radio=False)
		else:
			# FIX 1.0.0: prázdny TV bouquet nezapisovať ani neregistrovať
			# (napr. playlist len s rádiami) — a odstrániť starý ak visí.
			if os.path.isfile(tv_path):
				try:
					os.remove(tv_path)
					self.log('[M3U] Removed stale TV bouquet (no TV channels): %s'
					         % tv_path)
				except Exception:
					pass
			self._strip_bouquet_inclusion(
				bouquet_dir, tv_filename, is_radio=False)
			self.log('[M3U] No TV channels — TV bouquet not written')

		if enable_radio and radio_count > 0:
			_atomic_write(radio_path, '\n'.join(radio_lines) + '\n')
			self.log('[M3U] Wrote Radio bouquet: %s (%d channels)'
			         % (radio_path, radio_count))
			# Update bouquets.radio to include our Radio bouquet
			self._ensure_bouquet_included(
				bouquet_dir, radio_filename, radio_display, is_radio=True)
		else:
			# Radio bouquet disabled or empty — make sure stale file
			# from previous refresh doesn't linger.
			if os.path.isfile(radio_path):
				try:
					os.remove(radio_path)
					self.log('[M3U] Removed stale radio bouquet (no radio '
					         'channels found or disabled): %s' % radio_path)
				except Exception:
					pass
			self._strip_bouquet_inclusion(
				bouquet_dir, radio_filename, is_radio=True)

		return tv_path if tv_count > 0 else None

	def _ensure_bouquet_included(self, bouquet_dir, filename, display_name,
	                              is_radio=False):
		"""Add our userbouquet to bouquets.tv (or bouquets.radio) if not
		already there. If already present, refresh the #DESCRIPTION line so
		renaming the bouquet in settings actually changes the name shown in
		the E2 menu.
		"""
		bq_index = os.path.join(
			bouquet_dir, 'bouquets.radio' if is_radio else 'bouquets.tv')
		# Radio bouquets use service type 2 in the reference (`1:7:2:...`).
		bouquet_type = '2' if is_radio else '1'
		ref_line = ('#SERVICE 1:7:{}:0:0:0:0:0:0:0:'
		            'FROM BOUQUET "{}" ORDER BY bouquet'
		            .format(bouquet_type, filename))
		desc_line = '#DESCRIPTION ' + display_name

		try:
			with io.open(bq_index, 'r', encoding='utf-8') as f:
				content = f.read()
		except IOError:
			content = ('#NAME Bouquets (Radio)\n' if is_radio
			           else '#NAME Bouquets (TV)\n')

		lines = content.rstrip().split('\n')

		# Find our #SERVICE line (match by filename, the rest may differ
		# slightly across images - quotes, ORDER BY, whitespace).
		svc_idx = -1
		for i, ln in enumerate(lines):
			if ln.startswith('#SERVICE ') and filename in ln:
				svc_idx = i
				break

		master_name = 'bouquets.radio' if is_radio else 'bouquets.tv'

		if svc_idx == -1:
			# Not present yet - append both lines at the end.
			lines.append(ref_line)
			lines.append(desc_line)
			_atomic_write(bq_index, '\n'.join(lines) + '\n')
			self.log('[M3U] Added to %s: %s (name="%s")'
			         % (master_name, filename, display_name))
			return

		# Already present - make sure the following #DESCRIPTION matches
		# the current display_name (this is what E2 shows in the bouquet
		# list). Without this, renaming in settings has no visible effect.
		changed = False
		if (svc_idx + 1 < len(lines)
		        and lines[svc_idx + 1].startswith('#DESCRIPTION')):
			if lines[svc_idx + 1] != desc_line:
				lines[svc_idx + 1] = desc_line
				changed = True
		else:
			# #SERVICE without #DESCRIPTION - insert one.
			lines.insert(svc_idx + 1, desc_line)
			changed = True

		if changed:
			_atomic_write(bq_index, '\n'.join(lines) + '\n')
			self.log('[M3U] Updated %s #DESCRIPTION for %s -> "%s"'
			         % (master_name, filename, display_name))

	def _strip_bouquet_inclusion(self, bouquet_dir, filename, is_radio=False):
		"""Remove our userbouquet entry from bouquets.tv or bouquets.radio.
		Used when the radio bouquet feature is disabled or when no radio
		channels were detected — keeps the master file clean.
		"""
		bq_index = os.path.join(
			bouquet_dir, 'bouquets.radio' if is_radio else 'bouquets.tv')
		try:
			with io.open(bq_index, 'r', encoding='utf-8') as f:
				content = f.read()
		except IOError:
			return

		if not content:
			return

		lines = content.rstrip().split('\n')
		out = []
		i = 0
		removed = 0
		while i < len(lines):
			ln = lines[i]
			if ln.startswith('#SERVICE ') and filename in ln:
				removed += 1
				i += 1
				if i < len(lines) and lines[i].startswith('#DESCRIPTION'):
					i += 1
				continue
			out.append(ln)
			i += 1

		if removed:
			master_name = 'bouquets.radio' if is_radio else 'bouquets.tv'
			try:
				_atomic_write(bq_index, '\n'.join(out) + '\n')
				self.log('[M3U] Removed stale entry from %s for %s'
				         % (master_name, filename))
			except Exception as e:
				self.log('[M3U] Cannot update %s: %s' % (master_name, e))

	# ------------------ Picon download ------------------

	def _picon_filename_from_ref(self, service_ref):
		"""
		E2 picon naming convention (matches openatv/openpli):
		Replace ':' with '_', take first 10 fields (drop URL+name).
		Toto je Service Reference Pattern (SRP).

		DÔLEŽITÉ: NEROBIŤ .lower() — Enigma2 `getPiconName` (OpenATV
		`Picon.py`) hľadá súbor s rovnakým case ako je v service ref:
		`fields = serviceName.split(":", 10)[:10]; "_".join(fields)`.
		Service ref vždy obsahuje CAPS hex (`533B`, `3DD2`) — picon
		filename musí mať tiež CAPS aby Enigma2 ho našla.

		FIX 1.0.0: meno = presný ref vrátane typu prehrávača (4097/5001/
		5002/1) — viď _picon_name_from_fields, prečo sa NEnormalizuje na 1.
		"""
		fields = service_ref.split(':')
		# Service ref structure: type:flags:stype:sid:tsid:onid:ns:p1:p2:p3:url:name
		if len(fields) < 6:
			return None
		return _picon_name_from_fields(fields[0], fields[3], fields[4], fields[5])

	def _known_category_pairs(self):
		"""Páry (TSID, ONID) aktuálneho playlistu + zapamätané v SID mape."""
		pairs = list(self._category_pairs)
		try:
			for p in self._get_sid_map().category_pairs():
				if p not in pairs:
					pairs.append(p)
		except Exception:
			pass
		return pairs

	def wipe_picons(self):
		"""Zmaže všetky picony tohto doplnku (viď wipe_m3u_picons) pre
		kategórie aktuálneho playlistu aj kategórie zapamätané v SID mape.
		Vráti počet zmazaných."""
		picon_dir = self.s.get('picon_dir', DEFAULT_PICON_DIR)
		return wipe_m3u_picons(picon_dir, self._known_category_pairs(),
		                       log=self.log)

	def download_picons(self):
		"""Stiahne picony cez SRP-only (service reference) cestu.

		Skyjet PR #22 review #8: SNP cesta odstránená — picons sa ukladajú
		LEN ako `<service_ref>.png` (napr. `4097_0_1_174_B366_1_0_0_0_0.png`),
		nie ako `<channel_name_slug>.png` (napr. `beatv.png`). SNP cesta by
		prepisovala picons iných providerov.

		OpenATV/OpenPLI skiny default-uje hľadať picons SRP-first, SNP-fallback.
		Pre M3U bouquet generovaný týmto plugin-om sú SRP picons dostatočné.

		FIX 1.0.0:
		  - existujúci súbor sa preskočí LEN ak index zdrojov
		    (m3u_picon_sources.json) hovorí, že bol stiahnutý z tej istej
		    tvg-logo URL — zmena loga u providera = stiahnuť znova
		  - sťahovanie beží v poole _PICON_WORKERS vlákien
		  - auth: tvg-logo URL už má auth token doplnený providerom
		    (M3UProvider._auth_tvg_logos), rovnako ako doteraz
		"""
		if not bool(self.s.get('download_picons', True)):
			return
		picon_dir = self.s.get('picon_dir', DEFAULT_PICON_DIR)
		if not os.path.isdir(picon_dir):
			try:
				os.makedirs(picon_dir)
			except Exception as e:
				self.log('[M3U] Cannot create picon dir %s: %s' % (picon_dir, e))
				return

		sources = _picon_sources_load()
		jobs = []       # (srp_name, logo_url, dst_path, exists, channel_name)
		seen = set()
		skipped = 0
		for ch in self.provider.get_all_channels():
			ref = ch.get('_service_ref')
			logo = (ch.get('tvg_logo') or '').strip()
			if not ref or not logo:
				continue
			srp_name = self._picon_filename_from_ref(ref)
			if not srp_name or srp_name in seen:
				continue
			seen.add(srp_name)
			srp_path = os.path.join(picon_dir, srp_name)
			try:
				exists = os.path.isfile(srp_path) and os.path.getsize(srp_path) > 0
			except Exception:
				exists = False
			if exists and sources.get(srp_name) == logo:
				skipped += 1
				continue
			jobs.append((srp_name, logo, srp_path, exists, ch.get('name') or ''))

		stats = {'downloaded': 0, 'replaced': 0, 'failed': 0}
		lock = threading.Lock()

		def _fetch(job):
			srp_name, logo, srp_path, exists, ch_name = job
			try:
				req = Request(logo)
				req.add_header('User-Agent', _PICON_UA)
				resp = urlopen(req, timeout=15)
				try:
					data = resp.read()
				finally:
					try:
						resp.close()
					except Exception:
						pass
				if not data or len(data) < 100:
					raise ValueError('response too small (%d bytes)' % len(data or b''))
				tmp = srp_path + '.tmp'
				with open(tmp, 'wb') as f:
					f.write(data)
				if hasattr(os, 'replace'):
					os.replace(tmp, srp_path)
				else:
					if os.path.exists(srp_path):
						os.remove(srp_path)
					os.rename(tmp, srp_path)
				with lock:
					stats['downloaded'] += 1
					if exists:
						stats['replaced'] += 1
					sources[srp_name] = logo
			except Exception as e:
				with lock:
					stats['failed'] += 1
				self.log('[M3U] Picon download failed for %s: %s' % (ch_name, e))

		if jobs:
			cursor = {'i': 0}

			def _worker():
				while True:
					with lock:
						if cursor['i'] >= len(jobs):
							return
						job = jobs[cursor['i']]
						cursor['i'] += 1
					_fetch(job)

			threads = []
			for _ in range(min(_PICON_WORKERS, len(jobs))):
				t = threading.Thread(target=_worker, name='M3UPicon')
				t.daemon = True
				t.start()
				threads.append(t)
			for t in threads:
				t.join()

		# vyhoď z indexu mená, ktoré už v bouquete nie sú
		for k in list(sources.keys()):
			if k not in seen:
				sources.pop(k, None)
		_picon_sources_save(sources, log=self.log)

		self.log('[M3U] Picons (SRP-only): downloaded=%d replaced=%d skipped=%d '
		         'failed=%d total=%d'
		         % (stats['downloaded'], stats['replaced'], skipped,
		            stats['failed'], len(seen)))

	# ------------------ epgimport integration ------------------
	#
	# write_epgimport_files() bola odstránená (audit). m3u_manager.py
	# v `settings` natvrdo nastavoval `'write_epgimport': False` od 0.48g
	# (komentár: "Generation epgimport XML súborov je duplicitná s direct
	# EPG injection ktorý robí to isté efektívnejšie"). Direct injection
	# cez m3u_epg_injector + Enigma2 eEPGCache.importEvent() XMLTV programmy
	# do EPG cache bez nutnosti externého epgimport pluginu.
	#
	# Pre legacy inštalácie ktoré majú /etc/epgimport/<prefix>.channels.xml
	# alebo .sources.xml z minulých verzií zostáva `cleanup_m3u_bouquet()`
	# nižšie — vie ich zmazať pri "Remove M3U bouquet" akcii v UI.

	# ------------------ One-shot orchestration ------------------

	def run(self):
		"""Convenience: write bouquet + picons + reload Enigma2 in one call."""
		self.write_bouquet()
		# FIX 1.0.0: jednorazový plný reset piconov po vzniku SID mapy
		# (write_bouquet nastaví príznak len keď sa mapa úspešne uložila)
		if self._picon_wipe_pending:
			self._picon_wipe_pending = False
			if bool(self.s.get('download_picons', True)):
				try:
					self.wipe_picons()
				except Exception as e:
					self.log('[M3U] wipe_picons raised: %s' % e)
		try:
			self.download_picons()
		except Exception as e:
			self.log('[M3U] Picon stage failed: %s' % e)

		# Force Enigma2 to re-read bouquet files from disk so that the
		# new #NAME and channel list show up immediately without restart.
		self._reload_enigma_bouquets()

	def _reload_enigma_bouquets(self):
		"""
		Tell Enigma2 to re-read bouquet files from disk.

		Without this, Enigma2 keeps the cached bouquet list in memory
		(loaded at startup) and the new #NAME/channels we just wrote
		won't appear until next enigma restart.

		Tries multiple reload mechanisms (some images expose different APIs):
		  1. eDVBDB.reloadBouquets()  - primary, C++ API
		  2. eDVBDB.reloadServicelist() - secondary, refreshes service cache
		  3. OpenWebif /web/servicelistreload - HTTP fallback
		"""
		# Path 1: eDVBDB.reloadBouquets() — standard C++ API
		try:
			from enigma import eDVBDB
		except ImportError:
			# Not running inside Enigma2 (e.g. tests)
			return
		try:
			db = eDVBDB.getInstance()
			db.reloadBouquets()
			self.log('[M3U] eDVBDB.reloadBouquets() OK')
			# Bonus: also reload service list if available (some skins/images
			# cache the bouquet display names in serviceCenter)
			try:
				db.reloadServicelist()
				self.log('[M3U] eDVBDB.reloadServicelist() OK')
			except Exception:
				pass
		except Exception as e:
			self.log('[M3U] eDVBDB.reloadBouquets() failed: %s' % e)

		# Path 2: OpenWebif HTTP endpoint - works regardless of skin caching
		try:
			# mode=2 reloads bouquets and userbouquets
			resp = urlopen('http://127.0.0.1/web/servicelistreload?mode=2',
			               timeout=5)
			try:
				resp.read()
			finally:
				try:
					resp.close()
				except Exception:
					pass
			self.log('[M3U] OpenWebif servicelistreload OK')
		except Exception as e:
			# OpenWebif may not be running or on different port
			self.log('[M3U] OpenWebif servicelistreload skipped: %s' % e)


# -------------------------------------------------
# Cleanup helper (callable without an M3UBouquetWriter instance)
# -------------------------------------------------
def cleanup_m3u_bouquet(bouquet_prefix=None,
                        bouquet_dir=DEFAULT_BOUQUET_DIR,
                        epgimport_dir=DEFAULT_EPGIMPORT_DIR,
                        picon_dir=DEFAULT_PICON_DIR,
                        log=None):
	"""
	Remove a previously generated M3U bouquet from the system. Idempotent —
	safe to call even when nothing exists yet.

	FIX 0.48f: bouquet_prefix default je teraz None — fallne na konštantu
	M3U_BOUQUET_PREFIX. Caller môže poslať explicitný prefix ak chce
	vyčistiť legacy bouquet s iným prefixom.

	Steps:
	  1. Strip our #SERVICE + following #DESCRIPTION lines from bouquets.tv
	     AND bouquets.radio
	  2. Delete /etc/enigma2/userbouquet.<prefix>.tv and .radio
	  3. Delete /etc/epgimport/<prefix>.channels.xml and .sources.xml
	  4. FIX 1.0.0: delete this addon's picons (podľa (TSID, ONID) párov
	     kategórií zapamätaných v m3u_sids.json) + picon source index.
	     SID mapa sa NEMAŽE — po opätovnom zapnutí exportu dostanú kanály
	     tie isté refy (obľúbené/timery ostanú platné).
	  5. Reload Enigma2 bouquet/service list (best effort)

	Returns dict with stats {bouquets_tv_updated, bouquets_radio_updated,
	userbouquet_deleted, epgimport_deleted, picons_deleted, reloaded}.
	"""
	log = log or (lambda *a, **k: None)
	prefix = _safe_filename(bouquet_prefix or M3U_BOUQUET_PREFIX)
	tv_filename = 'userbouquet.{}.tv'.format(prefix)
	radio_filename = 'userbouquet.{}.radio'.format(prefix)
	stats = {
		'bouquets_tv_updated': False,
		'bouquets_radio_updated': False,
		'userbouquet_deleted': 0,
		'epgimport_deleted': 0,
		'picons_deleted': 0,
		'reloaded': False,
	}

	# ----- 1a. Strip from bouquets.tv -----
	def _strip_from_master(master_filename, our_filename, stats_key):
		bq_index = os.path.join(bouquet_dir, master_filename)
		try:
			with io.open(bq_index, 'r', encoding='utf-8') as f:
				content = f.read()
		except IOError:
			return
		if not content:
			return
		lines = content.rstrip().split('\n')
		out_lines = []
		i = 0
		removed = 0
		while i < len(lines):
			ln = lines[i]
			if ln.startswith('#SERVICE ') and our_filename in ln:
				removed += 1
				i += 1
				if i < len(lines) and lines[i].startswith('#DESCRIPTION'):
					i += 1
				continue
			out_lines.append(ln)
			i += 1
		if removed:
			try:
				_atomic_write(bq_index, '\n'.join(out_lines) + '\n')
				stats[stats_key] = True
				log('[M3U-cleanup] Removed %d entry/entries for %s from %s'
				    % (removed, our_filename, master_filename))
			except Exception as e:
				log('[M3U-cleanup] %s update failed: %s' % (master_filename, e))

	_strip_from_master('bouquets.tv', tv_filename, 'bouquets_tv_updated')
	_strip_from_master('bouquets.radio', radio_filename,
	                   'bouquets_radio_updated')

	# ----- 2. Delete userbouquet files (TV + Radio) -----
	for fname in (tv_filename, radio_filename):
		ub_path = os.path.join(bouquet_dir, fname)
		if os.path.isfile(ub_path):
			try:
				os.remove(ub_path)
				stats['userbouquet_deleted'] += 1
				log('[M3U-cleanup] Deleted %s' % ub_path)
			except Exception as e:
				log('[M3U-cleanup] Cannot delete %s: %s' % (ub_path, e))

	# ----- 3. Delete epgimport channels/sources -----
	for suffix in ('.channels.xml', '.sources.xml'):
		p = os.path.join(epgimport_dir, prefix + suffix)
		if os.path.isfile(p):
			try:
				os.remove(p)
				stats['epgimport_deleted'] += 1
				log('[M3U-cleanup] Deleted %s' % p)
			except Exception as e:
				log('[M3U-cleanup] Cannot delete %s: %s' % (p, e))

	# ----- 4. Delete our picons (FIX 1.0.0) -----
	try:
		stats['picons_deleted'] = wipe_m3u_picons(
			picon_dir, load_known_category_pairs(log=log), log=log)
	except Exception as e:
		log('[M3U-cleanup] picon wipe failed: %s' % e)

	# ----- 5. Reload Enigma2 (best effort) -----
	if (stats['bouquets_tv_updated'] or stats['bouquets_radio_updated']
	        or stats['userbouquet_deleted']):
		try:
			from enigma import eDVBDB
			db = eDVBDB.getInstance()
			db.reloadBouquets()
			try:
				db.reloadServicelist()
			except Exception:
				pass
			stats['reloaded'] = True
			log('[M3U-cleanup] Enigma2 bouquets reloaded')
		except ImportError:
			pass  # not running on Enigma2 (tests)
		except Exception as e:
			log('[M3U-cleanup] eDVBDB reload failed: %s' % e)

		# OpenWebif fallback
		try:
			resp = urlopen('http://127.0.0.1/web/servicelistreload?mode=2',
			               timeout=5)
			try:
				resp.read()
			finally:
				try:
					resp.close()
				except Exception:
					pass
			stats['reloaded'] = True
		except Exception:
			pass

	return stats


# -------------------------------------------------
# Standalone smoke test
# -------------------------------------------------
if __name__ == '__main__':
	import sys
	from m3u_provider import M3UProvider

	if len(sys.argv) < 2:
		print('Usage: m3u_bouquet.py <m3u_url> [<epg_url>]')
		sys.exit(1)

	m3u_url = sys.argv[1]
	epg_url = sys.argv[2] if len(sys.argv) > 2 else None

	p = M3UProvider(m3u_url=m3u_url, epg_url=epg_url, log=print)
	p.fetch_and_parse()

	settings = {
		'bouquet_prefix': 'm3u_iptv_test',
		'bouquet_display_name': 'IPTV M3U Test',
		'service_type': '1',
		'add_category_markers': True,
		'bouquet_dir': '/tmp/test_bouquet',
		'picon_dir': '/tmp/test_picons',
		'download_picons': False,  # set True to actually fetch logos
	}
	for d in (settings['bouquet_dir'], settings['picon_dir']):
		if not os.path.exists(d):
			os.makedirs(d)

	w = M3UBouquetWriter(p, settings, log=print)
	w.run()
	print('Done. Inspect /tmp/test_bouquet/userbouquet.m3u_iptv_test.tv')
