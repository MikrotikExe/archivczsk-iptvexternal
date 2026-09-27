# -*- coding: utf-8 -*-
"""
_bouquet_picons.py — picony pre userbouquet (/usr/share/enigma2/picon).

FIX 1.0.2 (Juraj): vynate z _bouquet_dvb.py a prerobene:

  * JEDINA cesta stahovania piconov pre bouquet je _remap_picons_to_bouquet.
    Framework BouquetGeneratorTemplate.download_picons (patchnuty v
    bouquet.py) je no-op — predtym bezal paralelne s remapom, stahoval tie
    iste subory druhykrat a robil vlastnu auth sondu.
  * Respektuje nastavenie "Automatically download picons" (enable_picons).
    Predtym remap stahoval vzdy, aj ked bolo vypnute.
  * Index zdrojov (data_path('picon_sources.json')): picon meno -> URL ikony
    v Tvheadende. Ked sa kanalu zmeni logo (iny imagecache ID), picon sa
    stiahne znova aj ked subor uz existuje. Predtym "existuje = preskoc".
  * _wipe_tvh_picons: zmaze VSETKY picony patriace tomuto doplnku podla
    namespace v service ref (TID/ONID/NS su pre doplnok unikatne), nie len
    tie z aktualneho bouquetu. Pouziva sa pri akcii "plny refresh" a
    jednorazovo po zavedeni stabilnych SID (_bouquet_sids.py), ked mozu byt
    picony na disku posunute voci kanalom.
  * stahovanie bezi v 4 vlaknach (predtym sekvencne).
"""
from __future__ import absolute_import, unicode_literals

import json
import os
import re
import threading

from ._paths import data_path

_PICON_DIRS = ('/usr/share/enigma2/picon', '/media/hdd/picon', '/media/usb/picon')
_PICON_SOURCES_FILE = 'picon_sources.json'
_PICON_WORKERS = 4


class BouquetPiconsMixin(object):
	"""Picon cast TvheadendBouquetXmlEpgGenerator. Zavisi na _log a get_setting
	(BouquetCommonMixin), get_bouquet_channels/load_channel_list (hlavna
	trieda), self._channels, self.cp.tvh a self.tid/onid/namespace (framework
	BouquetXmlEpgGenerator.__init__) cez MRO."""

	# ------------------------------------------------------------------
	# index zdrojov
	# ------------------------------------------------------------------
	def _picon_sources_path(self):
		return data_path(_PICON_SOURCES_FILE)

	def _picon_sources_load(self):
		try:
			with open(self._picon_sources_path(), 'r') as f:
				d = json.load(f)
			return d if isinstance(d, dict) else {}
		except Exception:
			return {}

	def _picon_sources_save(self, d):
		p = self._picon_sources_path()
		tmp = p + '.tmp'
		try:
			with open(tmp, 'w') as f:
				json.dump(d, f, sort_keys=True)
				f.flush()
				os.fsync(f.fileno())
			os.rename(tmp, p)
		except Exception as e:
			self._log("picon_sources: cannot write %s: %s" % (p, e))
			try:
				os.remove(tmp)
			except Exception:
				pass

	# ------------------------------------------------------------------
	# mazanie
	# ------------------------------------------------------------------
	def _tvh_picon_name_re(self):
		"""Regex na picon subory tohto doplnku: <typ>_0_<stype>_<SID>_<TID>_<ONID>_<NS>_0_0_0.png
		(typ moze byt 1 aj legacy 4097/5001/5002 z verzii pred normalizaciou)."""
		try:
			tail = '_%X_%X_%X_0_0_0' % (int(self.tid), int(self.onid), int(self.namespace))
		except Exception:
			return None
		return re.compile(r'^\d+_0_[0-9a-f]+_[0-9a-f]+' + re.escape(tail) + r'\.png$', re.I)

	def _wipe_tvh_picons(self):
		"""Zmaze vsetky picony patriace tomuto doplnku (podla TID/ONID/NS
		v mene suboru) zo vsetkych picon adresarov + index zdrojov.
		Picony inych doplnkov/satelitne ostavaju. Vrati pocet zmazanych."""
		rx = self._tvh_picon_name_re()
		if rx is None:
			self._log("_wipe_tvh_picons: namespace unknown, nothing deleted")
			return 0
		deleted = 0
		for pdir in _PICON_DIRS:
			if not os.path.isdir(pdir):
				continue
			try:
				names = os.listdir(pdir)
			except Exception as e:
				self._log("_wipe_tvh_picons: cannot list %s: %s" % (pdir, e))
				continue
			for fn in names:
				if not rx.match(fn):
					continue
				try:
					os.remove(os.path.join(pdir, fn))
					deleted += 1
				except Exception:
					pass
		try:
			os.remove(self._picon_sources_path())
		except Exception:
			pass
		self._log("_wipe_tvh_picons: deleted %d TVH picon files (pattern %s)" % (deleted, rx.pattern))
		return deleted

	# ------------------------------------------------------------------
	# remap / download
	# ------------------------------------------------------------------
	def _remap_picons_to_bouquet(self):
		"""Stiahne/ulozi picony pod menom ktore PRESNE zodpoveda service
		ref v userbouquete.

		Pre kazdy bouquet (TV + radio) paralelne prejdeme:
		  - #SERVICE riadky zo suboru -> cielove picon mena (service type
		    normalizovany na 1, tak ich hlada Enigma2)
		  - get_bouquet_channels(ctype) -> icon_public_url kanalov
		Obe su v identickom poradi (bouquet bol z get_bouquet_channels
		vygenerovany), takze i-ty ne-separatorovy riadok zodpoveda i-temu
		kanalu — aj pri multi-tag kategoriach, kde sa kanal opakuje.

		Existujuci subor sa preskoci LEN ak index zdrojov hovori, ze bol
		stiahnuty z tej istej ikony. Zmena loga v Tvheadende (iny imagecache
		ID) alebo subor bez zaznamu v indexe = stiahnut znova.
		"""
		if not self.get_setting('enable_picons'):
			self._log("_remap_picons_to_bouquet: enable_picons=off, skipping")
			return

		picon_dir = _PICON_DIRS[0]
		if not os.path.isdir(picon_dir):
			try:
				os.makedirs(picon_dir)
			except Exception as e:
				self._log("_remap_picons_to_bouquet: cannot create picon dir: %s" % e)
				return

		if not self._channels:
			try:
				self.load_channel_list()
			except Exception:
				pass

		# Len refy tohto doplnku (polia 5-10 = TID_ONID_NS_0_0_0). Pri
		# vypnutom XML EPG framework pise pre kanaly najdene v lamedb REALNE
		# satelitne refy — tie sa nesmu premapovat, inak by sa TVH logo
		# zapisalo cez pravy satelitny picon uzivatela.
		try:
			own_tail = '_%X_%X_%X_0_0_0' % (int(self.tid), int(self.onid), int(self.namespace))
		except Exception:
			own_tail = None

		icon_by_key = {}
		for ch in self._channels:
			icon = (ch.get('icon_public_url') or '').strip()
			for k in (ch.get('key'), ch.get('uuid')):
				if k:
					icon_by_key[k] = icon

		# Paruj bouquet service refs s kanalmi pre TV aj radio.
		mapping = []  # (picon_name, icon_public_url)
		base = "/etc/enigma2"
		bouquet_files = [
			("userbouquet.tvheadend_tv.tv", "tv"),
			("userbouquet.tvheadend_radio.radio", "radio"),
			("userbouquet.tvheadend_radio.tv", "radio"),
		]

		for fn, ctype in bouquet_files:
			path = os.path.join(base, fn)
			if not os.path.isfile(path):
				continue

			# 1) service refs z bouquet suboru (ne-separatory, v poradi)
			refs = []
			try:
				with open(path, 'r') as f:
					for line in f:
						if not line.startswith('#SERVICE'):
							continue
						parts = line.split(':')
						if len(parts) < 11:
							continue
						if parts[1] == '64' or 'FROM BOUQUET' in line:
							continue
						stype = parts[0].replace('#SERVICE', '').strip()
						ref10 = [stype] + parts[1:10]
						ref10[0] = '1'
						name = '_'.join(p.strip() for p in ref10)
						if own_tail is not None and not name.upper().endswith(own_tail.upper()):
							name = None   # cudzi (lamedb) ref — drz poziciu, nestahuj
						refs.append(name)
			except Exception as e:
				self._log("_remap_picons_to_bouquet: read %s failed: %s" % (fn, e))
				continue

			# 2) icon_public_url z get_bouquet_channels (ne-separatory, v poradi)
			icons = []
			try:
				for item in self.get_bouquet_channels(ctype):
					if item.get('is_separator'):
						continue
					icons.append(icon_by_key.get(item.get('key'), ''))
			except Exception as e:
				self._log("_remap_picons_to_bouquet: get_bouquet_channels(%s) failed: %s" % (ctype, e))
				continue

			# 3) Paruj i-ty ref s i-tym icon (identicke poradie)
			if len(refs) != len(icons):
				self._log("_remap_picons_to_bouquet: %s ref/icon mismatch (refs=%d, icons=%d)" % (fn, len(refs), len(icons)))
			paired = 0
			foreign = 0
			for i in range(min(len(refs), len(icons))):
				if refs[i] is None:
					foreign += 1
					continue
				if icons[i]:
					mapping.append((refs[i], icons[i]))
					paired += 1
			if foreign:
				self._log("_remap_picons_to_bouquet: %s skipped %d lamedb/foreign refs" % (fn, foreign))
			self._log("_remap_picons_to_bouquet: %s paired %d channels" % (fn, paired))

		if not mapping:
			self._log("_remap_picons_to_bouquet: nothing to map")
			return

		try:
			import requests as _req
		except ImportError:
			self._log("_remap_picons_to_bouquet: requests missing")
			return

		tvh = self.cp.tvh

		def http_url_fn(icon):
			# URL BEZ inline credentials — auth ide cez session (nizsie),
			# rovnako ako vsetky ostatne requesty doplnku (respektuje
			# http_auth_mode basic/digest/none aj SHA-256 digest). Inline
			# user:heslo v URL requests ignoruje a percent-encoding hesla
			# by rozbil digest.
			if not icon:
				return None
			if icon.startswith(('http://', 'https://')):
				return icon
			if icon.startswith(('file://', 'picon://')):
				return None
			return tvh._url(icon)

		sess = _req.Session()
		try:
			tvh._apply_auth_to_session(sess)
		except Exception as e:
			self._log("_remap_picons_to_bouquet: auth setup failed: %s" % e)

		sources = self._picon_sources_load()
		jobs = []
		skipped = 0
		seen = set()
		for ref_name, icon in mapping:
			if ref_name in seen:
				continue
			seen.add(ref_name)
			dst = os.path.join(picon_dir, ref_name + '.png')
			try:
				exists = os.path.isfile(dst) and os.path.getsize(dst) > 0
			except Exception:
				exists = False
			if exists and sources.get(ref_name) == icon:
				skipped += 1
				continue
			try:
				http_url = http_url_fn(icon)
			except Exception:
				http_url = None
			if not http_url:
				continue
			jobs.append((ref_name, icon, http_url, dst, exists))

		stats = {'written': 0, 'failed': 0, 'replaced': 0}
		lock = threading.Lock()

		def _fetch(job):
			ref_name, icon, http_url, dst, exists = job
			try:
				r = sess.get(http_url, timeout=10)
				if r.status_code == 200 and r.content and len(r.content) > 100:
					tmp = dst + '.tmp'
					with open(tmp, 'wb') as f:
						f.write(r.content)
					os.rename(tmp, dst)
					with lock:
						stats['written'] += 1
						if exists:
							stats['replaced'] += 1
						sources[ref_name] = icon
					return
			except Exception:
				pass
			with lock:
				stats['failed'] += 1

		if jobs:
			idx = {'i': 0}

			def _worker():
				while True:
					with lock:
						if idx['i'] >= len(jobs):
							return
						job = jobs[idx['i']]
						idx['i'] += 1
					_fetch(job)

			threads = []
			for _ in range(min(_PICON_WORKERS, len(jobs))):
				t = threading.Thread(target=_worker)
				t.daemon = True
				t.start()
				threads.append(t)
			for t in threads:
				t.join()

		# vyhod z indexu mena, ktore uz v bouquete nie su
		for k in list(sources.keys()):
			if k not in seen:
				sources.pop(k, None)
		self._picon_sources_save(sources)

		self._log("_remap_picons_to_bouquet: done (written=%d, replaced=%d, skipped=%d, "
		          "failed=%d, total_mapped=%d)"
		          % (stats['written'], stats['replaced'], skipped, stats['failed'], len(seen)))
