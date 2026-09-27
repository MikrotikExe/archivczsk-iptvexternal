# -*- coding: utf-8 -*-
"""
_bouquet_sids.py — stabilne pridelovanie SID (service ID) kanalom v userbouquete.

FIX 1.0.2 (Juraj): picony v Enigma2 sa hladaju podla service ref
(1_0_1_<SID>_<TID>_<ONID>_<NS>_0_0_0.png). Framework pocita SID ako
sid_start + channel['id'] a doplnok davad ako 'id' CISLO KANALA z Tvheadendu
(alebo poradove cislo pri kanaloch bez cisla). Ked sa v Tvheadende pridal
kanal doprostred zoznamu a ostatne sa precislovali (alebo kanaly nemali
cisla vobec), vsetky kanaly za nim dostali novy SID. Picon subory na disku
ale ostali pomenovane podla STARYCH SID — a kedze stahovanie preskakuje
existujuce subory, kanal X zobrazoval logo kanala, ktory mal jeho SID
predtym. "Obnova piconov" to neopravila, lebo nic nezmazala.

Riesenie: SID sa prideli kanalu RAZ a zapamata sa v perzistentnej mape
(data_path('bouquet_sids.json')) podla identity kanala v Tvheadende, nie
podla jeho pozicie/cisla:

  * kluc = kanonicke ID kanala: v HTTP rezime je uuid 32-znakovy hex,
    v HTSP rezime je 'uuid' = channelId (short uuid = prve 4 bajty uuid
    ako little-endian uint32 & 0x7FFFFFFF, viď idnode_get_short_uuid v
    tvheadend/src/idnode.c). Obe formy prepocitame na to iste cislo,
    takze prepnutie HTTP <-> HTSP nezmeni SID.
  * prva inicializacia (mapa neexistuje): prevezmu sa AKTUALNE id
    (cisla kanalov), aby existujucim instalaciam nezmenili service refs
    (oblubene, timery, blacklist). Volajuci dostane priznak `seeded`
    a spravi jednorazovy plny refresh piconov, lebo picony na disku mohli
    byt uz posunute.
  * novy kanal dostane SID z crc32(kluc) v rozsahu 1..0xFFF0 a pri
    kolizii sa linearne posuva na prve volne. Pridelene SID sa NIKDY
    nemenia, kym existuju v mape.
  * rozsah je obmedzeny na 0xFFF0, lebo framework pouziva pre bouquet
    (sid_start + id) % 0xFFFF, ale pre EPG (enigmaepg.py, xmlepg.py)
    sid_start + id BEZ modula — vacsie id by rozbilo parovanie EPG.

Mapa sa uklada atomicky (tmp + rename). Zaznamy kanalov, ktore uz na
serveri nie su, sa ponechaju (drzia svoj SID) — su male a zabranuju tomu,
aby po docasnom zmiznuti kanala jeho SID dostal iny kanal.
"""
from __future__ import absolute_import, unicode_literals

import json
import os
import struct
import threading
import zlib

from ._paths import data_path

_SIDS_FILE = 'bouquet_sids.json'
_SID_MIN = 1
_SID_MAX = 0xFFF0          # vratane; viď docstring (EPG bez modula)
_SID_SPAN = _SID_MAX - _SID_MIN + 1


def canonical_channel_key(uuid):
	"""Prevedie uuid kanala (HTTP hex alebo HTSP channelId) na kanonicky
	retazec pouzity ako kluc mapy. Nezname formaty vrati ako su."""
	s = (uuid or '').strip()
	if not s:
		return ''
	if s.isdigit():
		return s
	hex_part = s.replace('-', '')
	if len(hex_part) == 32:
		try:
			raw = bytes(bytearray.fromhex(hex_part[:8]))
			short = struct.unpack('<I', raw)[0] & 0x7FFFFFFF
			return str(short)
		except Exception:
			pass
	return s


def _hash_sid(key):
	return _SID_MIN + (zlib.crc32(key.encode('utf-8')) & 0xFFFFFFFF) % _SID_SPAN


class StableSidMap(object):
	"""Perzistentna mapa kanonicky kluc -> SID id (int)."""

	def __init__(self, path=None, log=None):
		self.path = path or data_path(_SIDS_FILE)
		self._log = log or (lambda m: None)
		self.map = {}
		self.used = set()
		self.seeded = False      # True = mapa vznikla prave teraz
		self.dirty = False
		# load_channel_list bezi aj z dvoch vlakien naraz (framework loop +
		# rucna akcia) — get/save su preto pod zamkom
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
				if _SID_MIN <= v <= _SID_MAX and v not in self.used:
					self.map[str(k)] = v
					self.used.add(v)
		except Exception as e:
			self._log('bouquet_sids: cannot read %s (%s) — starting fresh' % (self.path, e))
			self.map = {}
			self.used = set()
			self.seeded = True

	def save(self):
		"""Atomicky zapis (tmp + fsync + rename). Vrati True ak je mapa na
		disku (aj ked nebolo co ukladat)."""
		with self._lock:
			if not self.dirty:
				return True
			snapshot = dict(self.map)
			tmp = self.path + '.tmp'
			try:
				with open(tmp, 'w') as f:
					json.dump({'version': 1, 'sids': snapshot}, f, sort_keys=True)
					f.flush()
					os.fsync(f.fileno())
				os.rename(tmp, self.path)
				self.dirty = False
				# po prvom ulozeni uz nie sme v "seed" rezime — dalsie nove
				# kanaly dostavaju hash SID, nie svoje cislo
				self.seeded = False
				return True
			except Exception as e:
				self._log('bouquet_sids: cannot write %s: %s' % (self.path, e))
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
		raise RuntimeError('bouquet_sids: SID space exhausted')

	def get(self, uuid, seed_id=None):
		"""Vrati stabilny SID id pre kanal. Ak kanal este nema zaznam:
		pri prvej inicializacii mapy (seeded) a volnom seed_id sa prevezme
		seed_id (kontinuita s doterajsim schemou), inak sa prideli hash."""
		key = canonical_channel_key(uuid)
		if not key:
			return None
		with self._lock:
			sid = self.map.get(key)
			if sid is not None:
				return sid
			if (self.seeded and seed_id is not None
					and _SID_MIN <= int(seed_id) <= _SID_MAX
					and int(seed_id) not in self.used):
				sid = int(seed_id)
			else:
				sid = self._alloc(key)
			self.map[key] = sid
			self.used.add(sid)
			self.dirty = True
			return sid

	def __len__(self):
		return len(self.map)
