# -*- coding: utf-8 -*-
"""
m3u_sids.py — stabilné prideľovanie SID (service ID) kanálom v M3U userbouquete.

FIX 1.0.0 (Juraj): picony v Enigma2 sa hľadajú podľa service ref
(1_0_1_<SID>_<TSID>_<ONID>_0_0_0_0.png). Doteraz bol SID kanála jeho
PORADOVÉ ČÍSLO v playliste (globálny index cez všetky kategórie). Keď
provider pridal kanál doprostred playlistu, všetky kanály za ním dostali
nový SID — a s ním aj picon svojho predchodcu, lebo súbor s tým menom
už na disku bol a sťahovanie existujúce súbory preskakovalo. Zároveň sa
rozbili obľúbené, timery a EPG cache naviazané na starý ref.

Riešenie (rovnaký návrh ako _bouquet_sids.py v plugin.video.tvheadend):
SID sa pridelí kanálu RAZ a zapamätá sa v perzistentnej mape
(data_path('m3u_sids.json')) podľa identity kanála, nie podľa pozície:

  * primárna identita = stream URL (url_key): celá URL vrátane query,
    z ktorej sa odstránia LEN pominuteľné auth parametre (auth, token,
    ticket, password, pass, sig, expires) a userinfo `user:pass@`;
    fragment sa zahodí. Ostatné query parametre ostávajú — playlisty
    bez tvg-id často rozlišujú kanály len query-stringom
    (`/stream?id=1`, `?id=2`, …).
  * tvg-id je ALIAS: hľadanie SID pre kanál ide v poradí
      (a) 'url:'+url_key            — ak je v mape
      (b) 'id:'+tvg-id              — ak tvg-id existuje, nezdieľa ho
                                      v tomto playliste viac rôznych
                                      url_key (HD/SD variant) a je v mape
                                      (kanál zmenil URL)
      (c) pridelenie nového SID.
    Pri pridelení sa SID uloží VŽDY pod 'url:' a pod 'id:' len keď
    tvg-id nie je zdieľané. Kanál bez tvg-id a bez URL (nemal by
    existovať — bez URL sa do zoznamu nedostane) padne na 'name:'.
    Ten istý stream (rovnaký url_key) vo viacerých kategóriách zdieľa
    SID zámerne — refy sa líšia v TSID/ONID kategórie.
  * prvá inicializácia (mapa neexistuje): prevezmú sa AKTUÁLNE poradové
    čísla, aby existujúcim inštaláciám nezmenili service refy (obľúbené,
    timery). Jediná výnimka: ten istý stream uvedený v playliste
    dvakrát (dve kategórie) dostane pri druhom výskyte SID prvého
    výskytu, nie svoje poradové číslo. Volajúci dostane príznak
    `seeded` a spraví jednorazový plný reset piconov, lebo picony na
    disku už mohli byť posunuté.
  * nový kanál dostane SID z crc32(kľúč) v rozsahu 1..0xFFF0 a pri
    kolízii sa lineárne posúva na prvé voľné. Pridelené SID sa NIKDY
    nemenia, kým existujú v mape. Mapa je jedna pre všetky kategórie,
    takže SID je unikátny naprieč celým bouquetom.
  * spolu so SID sa pamätajú aj (TSID, ONID) páry kategórií, ktoré
    kedy boli v playliste — wipe_picons() podľa nich vie zmazať aj
    picony kategórií, ktoré už v playliste nie sú.

Mapa sa ukladá atomicky (tmp + fsync + rename). Záznamy kanálov, ktoré
už v playliste nie sú, sa ponechávajú (držia svoj SID) — sú malé a
zabránia tomu, aby po dočasnom zmiznutí kanála jeho SID dostal iný kanál.

Python 2.7 + Python 3.x kompatibilné.
"""
from __future__ import absolute_import, unicode_literals

import json
import os
import threading
import zlib

try:
	from ._paths import data_path
except (ValueError, ImportError):
	from _paths import data_path

try:
	from urllib.parse import urlsplit
except ImportError:
	from urlparse import urlsplit

_SIDS_FILE = 'm3u_sids.json'
_SID_MIN = 1
_SID_MAX = 0xFFF0          # vrátane; hex má max 4 znaky, rezerva pre markery
_SID_SPAN = _SID_MAX - _SID_MIN + 1


# query parametre, ktoré sa v identite streamu ignorujú (rotujú sa)
_VOLATILE_QUERY_KEYS = ('auth', 'token', 'ticket', 'password', 'pass',
                        'sig', 'expires')


def url_key(url):
	"""Identita streamu: celá URL bez pominuteľných auth parametrov,
	bez userinfo a bez fragmentu. Ostatné query parametre (aj poradie)
	ostávajú zachované."""
	u = (url or '').strip()
	if not u:
		return ''
	u = u.split('#', 1)[0]
	try:
		p = urlsplit(u)
	except Exception:
		return u
	if not (p.scheme and p.netloc):
		return u
	netloc = p.netloc
	if '@' in netloc:
		netloc = netloc.rsplit('@', 1)[1]
	kept = []
	for part in (p.query or '').split('&'):
		if not part:
			continue
		name = part.split('=', 1)[0].strip().lower()
		if name in _VOLATILE_QUERY_KEYS:
			continue
		kept.append(part)
	out = '{}://{}{}'.format(p.scheme.lower(), netloc, p.path)
	if kept:
		out += '?' + '&'.join(kept)
	return out


def channel_key(channel):
	"""Primárny kľúč kanála: 'url:'+url_key, pri chýbajúcej URL
	'name:'+názov (viď docstring modulu)."""
	uk = url_key(channel.get('url'))
	if uk:
		return 'url:' + uk
	name = (channel.get('name') or '').strip()
	if name:
		return 'name:' + name
	return ''


def shared_tvg_ids(channels):
	"""Vráti množinu tvg-id, ktoré v playliste používa viac RÔZNYCH
	url_key (HD/SD variant s rovnakým EPG id). Pre tie sa 'id:' alias
	nepoužije ani neuloží."""
	keys_by_id = {}
	for ch in channels:
		tvg = (ch.get('tvg_id') or '').strip()
		if not tvg:
			continue
		keys_by_id.setdefault(tvg, set()).add(url_key(ch.get('url')))
	return set(t for t, keys in keys_by_id.items() if len(keys) > 1)


def _hash_sid(key):
	return _SID_MIN + (zlib.crc32(key.encode('utf-8')) & 0xFFFFFFFF) % _SID_SPAN


class StableSidMap(object):
	"""Perzistentná mapa kľúč kanála -> SID (int) + zoznam (TSID, ONID)
	párov kategórií, ktoré kedy boli v playliste."""

	def __init__(self, path=None, log=None):
		self.path = path or data_path(_SIDS_FILE)
		self._log = log or (lambda m: None)
		self.map = {}
		self.used = set()
		self.categories = []     # list of (tsid_hex, onid_hex)
		self.seeded = False      # True = mapa vznikla práve teraz (nie je na disku)
		self.dirty = False
		# write_bouquet môže bežať z refresh vlákna aj z ručnej akcie —
		# get/save sú preto pod zámkom
		self._lock = threading.Lock()
		self._load()

	# ------------------------------------------------------------------
	def _load(self):
		if not os.path.isfile(self.path):
			self.seeded = True
			return
		try:
			with open(self.path, 'r') as f:
				data = json.load(f)
			raw = data.get('sids') if isinstance(data, dict) else None
			if not isinstance(raw, dict):
				raise ValueError('bad format')
			for k, v in raw.items():
				try:
					v = int(v)
				except Exception:
					continue
				# kľúč nechať ako text (na Py2 by str() padol na diakritike);
				# viac kľúčov môže ukazovať na ten istý SID ('url:' + 'id:' alias)
				if _SID_MIN <= v <= _SID_MAX:
					self.map[k] = v
					self.used.add(v)
			for pair in (data.get('categories') or []):
				try:
					t, o = str(pair[0]).upper(), str(pair[1]).upper()
				except Exception:
					continue
				if (t, o) not in self.categories:
					self.categories.append((t, o))
		except Exception as e:
			self._log('m3u_sids: cannot read %s (%s) — starting fresh' % (self.path, e))
			self.map = {}
			self.used = set()
			self.categories = []
			self.seeded = True

	def save(self):
		"""Atomický zápis (tmp + fsync + rename). Vráti True ak je mapa na
		disku (aj keď nebolo čo ukladať), False ak sa zápis nepodaril."""
		with self._lock:
			if not self.dirty:
				return True
			snapshot = dict(self.map)
			cats = [list(p) for p in self.categories]
			tmp = self.path + '.tmp'
			try:
				with open(tmp, 'w') as f:
					json.dump({'version': 2, 'sids': snapshot, 'categories': cats},
					          f, sort_keys=True)
					f.flush()
					os.fsync(f.fileno())
				os.rename(tmp, self.path)
				self.dirty = False
				# po prvom uložení už nie sme v "seed" režime — ďalšie nové
				# kanály dostávajú hash SID, nie svoje poradové číslo
				self.seeded = False
				return True
			except Exception as e:
				self._log('m3u_sids: cannot write %s: %s' % (self.path, e))
				try:
					os.remove(tmp)
				except Exception:
					pass
				return False

	# ------------------------------------------------------------------
	def _alloc(self, key):
		sid = _hash_sid(key)
		for _ in range(_SID_SPAN):
			if sid not in self.used:
				return sid
			sid = sid + 1 if sid < _SID_MAX else _SID_MIN
		raise RuntimeError('m3u_sids: SID space exhausted')

	def _alloc_or_seed(self, key, seed_id):
		"""Pridelí SID pre nový kľúč (pod zámkom): pri prvej inicializácii
		mapy (seeded) a voľnom seed_id sa prevezme seed_id (kontinuita
		s doterajšou schémou), inak hash."""
		if (self.seeded and seed_id is not None
				and _SID_MIN <= int(seed_id) <= _SID_MAX
				and int(seed_id) not in self.used):
			return int(seed_id)
		return self._alloc(key)

	def get(self, key, seed_id=None):
		"""Vráti stabilný SID pre jeden kľúč (bez alias logiky)."""
		if not key:
			return None
		with self._lock:
			sid = self.map.get(key)
			if sid is None:
				sid = self._alloc_or_seed(key, seed_id)
				self.map[key] = sid
				self.used.add(sid)
				self.dirty = True
			return sid

	def sid_for_channel(self, channel, shared_ids=(), seed_id=None):
		"""Vráti stabilný SID pre kanál podľa poradia (a) url (b) tvg-id
		alias (c) pridelenie — viď docstring modulu. `shared_ids` =
		výsledok shared_tvg_ids() pre aktuálny playlist."""
		primary = channel_key(channel)
		if not primary:
			return None
		tvg = (channel.get('tvg_id') or '').strip()
		id_key = ('id:' + tvg) if (tvg and tvg not in shared_ids) else None
		with self._lock:
			sid = self.map.get(primary)
			if sid is None and id_key is not None:
				# kanál zmenil URL — prevezmi SID cez tvg-id alias
				sid = self.map.get(id_key)
			if sid is None:
				sid = self._alloc_or_seed(primary, seed_id)
				self.used.add(sid)
			changed = False
			if self.map.get(primary) != sid:
				self.map[primary] = sid
				changed = True
			if id_key is not None and self.map.get(id_key) != sid:
				# alias len keď ho ešte nemá iný kanál (inak by sa
				# "ukradol" — tvg-id zdieľané naprieč behmi)
				if id_key not in self.map:
					self.map[id_key] = sid
					changed = True
			if changed:
				self.dirty = True
			return sid

	def remember_category(self, tsid_hex, onid_hex):
		"""Zapamätá (TSID, ONID) pár kategórie — pre neskorší wipe piconov
		aj kategórií, ktoré už v playliste nie sú."""
		pair = (str(tsid_hex).upper(), str(onid_hex).upper())
		with self._lock:
			if pair not in self.categories:
				self.categories.append(pair)
				self.dirty = True

	def category_pairs(self):
		with self._lock:
			return list(self.categories)

	def __len__(self):
		return len(self.map)


def load_known_category_pairs(path=None, log=None):
	"""Pomocník pre cleanup bez writer inštancie: vráti (TSID, ONID) páry
	zapamätané v m3u_sids.json (alebo [] ak mapa neexistuje)."""
	try:
		return StableSidMap(path=path, log=log).category_pairs()
	except Exception:
		return []
