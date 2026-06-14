# -*- coding: utf-8 -*-
import os
import re


class BouquetDvbMixin(object):
	"""DVB service-ref prepis bouquetov + picon remap pre TvheadendBouquetXmlEpgGenerator.
	Vynate z bouquet.py v 0.90.0 (refaktor, bez zmeny spravania).
	Zavisi na _read_lines/_write_lines/_log (BouquetCommonMixin) a hookoch
	get_bouquet_channels/load_channel_list (hlavna trieda) cez MRO; pouziva
	instance state self._key_to_url a self._channels."""

	def _rewrite_bouquets_to_dvb(self):
		"""
		0.72.0: Prepíše vygenerované userbouquet súbory na natívny DVB player.

		Framework zapíše každý kanál ako:
		  #SERVICE 5002:0:1:SID:TSID:ONID:NS:0:0:0:<Playlive proxy URL>:NÁZOV
		Native DVB potrebuje:
		  #SERVICE 1:0:1:SID:TSID:ONID:NS:0:0:0:<priama TVH URL>:NÁZOV

		Menia sa LEN dve veci: typ (pole 0 -> '1') a URL (pole 10 -> priama
		TVH URL, profil pass, ':' escapnuté na '%3a'). Polia 1-9 (SID/TSID/NS)
		a názov ostávajú netknuté -> picon aj EPG párovanie sa zachová.

		Mapovanie kanál->URL je POZIČNÉ: poradie ne-separátorových riadkov v
		súbore zodpovedá poradiu get_bouquet_channels(channel_type). Tým sa
		vyhneme dekódovaniu frameworkového Playlive kľúča.

		POZOR: native DVB http zdroj robí BASIC auth. Server musí mať povolený
		plain/basic ("Both plain and digest"), inak DVB chain neoverí.
		"""
		base = "/etc/enigma2"
		try:
			files = [f for f in os.listdir(base)
			         if f.startswith('userbouquet.tvheadend_')
			         and (f.endswith('.tv') or f.endswith('.radio'))]
		except Exception as e:
			self._log("_rewrite_bouquets_to_dvb: cannot list %s: %s" % (base, e))
			return

		self._log("_rewrite_bouquets_to_dvb: BASIC auth required on TVH server "
		          "(Authentication type = Both/Plain), files=%r" % files)

		total = 0
		for fn in files:
			channel_type = 'radio' if 'radio' in fn else 'tv'
			path = os.path.join(base, fn)

			# Priame URL v poradí (ne-separátorové kanály), profil pass.
			urls = []
			try:
				for ch in self.get_bouquet_channels(channel_type):
					if ch.get('is_separator'):
						continue
					urls.append(self._dvb_url_for_key(ch.get('key')))
			except Exception as e:
				self._log("_rewrite_bouquets_to_dvb: get_bouquet_channels(%s) "
				          "failed: %s" % (channel_type, e))
				continue

			lines = self._read_lines(path)
			if not lines:
				continue

			out = []
			idx = 0
			rewritten = 0
			for line in lines:
				if not line.startswith('#SERVICE '):
					out.append(line)
					continue
				ref = line[len('#SERVICE '):]
				parts = ref.split(':')
				# marker (1:64:...) alebo FROM BOUQUET -> nechaj tak
				if 'FROM BOUQUET' in line or (len(parts) > 1 and parts[1] == '64'):
					out.append(line)
					continue
				# kanálový riadok
				url_enc = urls[idx] if idx < len(urls) else ''
				idx += 1
				if not url_enc or len(parts) < 11:
					out.append(line)   # bez URL nechaj pôvodný (typ + proxy)
					continue
				parts[0] = '1'          # eServiceFactoryDVB
				parts[10] = url_enc     # priama TVH URL
				out.append('#SERVICE ' + ':'.join(parts))
				rewritten += 1

			if idx != len(urls):
				self._log("_rewrite_bouquets_to_dvb: %s count mismatch "
				          "(lines=%d, channels=%d) — niektoré kanály neprepísané"
				          % (fn, idx, len(urls)))

			if rewritten and self._write_lines(path, out):
				total += rewritten
				self._log("_rewrite_bouquets_to_dvb: %s -> %d DVB refs" % (fn, rewritten))

		self._log("_rewrite_bouquets_to_dvb: hotovo, spolu %d refs" % total)

	def _dvb_url_for_key(self, channel_key):
		"""Priama TVH URL pre kanál: profil vynútený na pass, ':' -> '%3a'."""
		url = self._key_to_url.get(channel_key, '') if channel_key else ''
		if not url:
			return ''
		# Vynúť profil pass (verifikovaný pre native demux; transcode profily
		# môžu na HW demuxe robiť problém s PIDmi/timingom).
		if 'profile=' in url:
			url = re.sub(r'profile=[^&]*', 'profile=pass', url)
		else:
			url = url + ('&' if '?' in url else '?') + 'profile=pass'
		return url.replace(':', '%3a')

	def _remap_picons_to_bouquet(self):
		"""Stiahne/uloží picony pod menom ktoré PRESNE zodpovedá service
		ref v userbouquete.

		Problém ktorý rieši: framework download_picons ukladá picon súbory
		s menom odvodeným z interného SID páringu (pozorované 5002_0_1_100_
		B366_1_7070000), ale userbouquet má service refs 1_0_1_2_B366_1_
		7070000 (iný service type AJ iný SID). Enigma2 pri zobrazení hľadá
		picon podľa service ref z bouquetu (s normalizovaným type=1), takže
		framework-om uložené picony nikdy nenašla.

		Riešenie: pre každý bouquet (TV + radio) paralelne prejdeme:
		  - #SERVICE riadky zo súboru → presné cieľové picon mená
		    (service type normalizovaný na 1)
		  - get_bouquet_channels(ctype) → icon_public_url kanálov
		Obe sú v identickom poradí (bouquet bol z get_bouquet_channels
		vygenerovaný), takže i-tý ne-separátor riadok zodpovedá i-tému
		kanálu — aj pri multi-tag kategóriách kde sa kanál opakuje. Picon
		stiahneme/uložíme pod presným bouquet menom.
		"""
		import os as _os

		picon_dir = '/usr/share/enigma2/picon'
		if not _os.path.isdir(picon_dir):
			try:
				_os.makedirs(picon_dir)
			except Exception as e:
				self._log("_remap_picons_to_bouquet: cannot create picon dir: %s" % e)
				return

		if not self._channels:
			try:
				self.load_channel_list()
			except Exception:
				pass

		# Páruj bouquet service refs s kanálmi pre TV aj radio.
		mapping = []  # zoznam (service_ref_picon_name, icon_public_url)

		base = "/etc/enigma2"
		bouquet_files = [
			("userbouquet.tvheadend_tv.tv", "tv"),
			("userbouquet.tvheadend_radio.radio", "radio"),
			("userbouquet.tvheadend_radio.tv", "radio"),
		]

		for fn, ctype in bouquet_files:
			path = _os.path.join(base, fn)
			if not _os.path.isfile(path):
				continue

			# FIX 0.59.5 (audit, Juraj): paralelná iterácia. get_bouquet_channels
			# yielduje kanály v PRESNE rovnakom poradí (vrátane duplikátov v
			# kategóriách + separátorov) ako boli zapísané do bouquet súboru,
			# lebo bouquet bol z tejto funkcie vygenerovaný. Takže i-tý
			# ne-separátor kanál zodpovedá i-tému #SERVICE ne-separátor
			# riadku. Tým sa eliminuje mismatch z multi-tag kategórií.

			# 1) service refs z bouquet súboru (ne-separátory, v poradí)
			refs = []
			try:
				with open(path, 'r') as f:
					for line in f:
						if not line.startswith('#SERVICE'):
							continue
						parts = line.split(':')
						if len(parts) < 11:
							continue
						if parts[0] == '#SERVICE 1' and parts[1] == '64':
							continue
						stype = parts[0].replace('#SERVICE', '').strip()
						ref10 = [stype] + parts[1:10]
						if ref10[0] != '1':
							ref10[0] = '1'
						refs.append('_'.join(p.strip() for p in ref10))
			except Exception as e:
				self._log("_remap_picons_to_bouquet: read %s failed: %s" % (fn, e))
				continue

			# 2) icon_public_url z get_bouquet_channels (ne-separátory, v poradí)
			icons = []
			try:
				for item in self.get_bouquet_channels(ctype):
					if item.get('is_separator'):
						continue
					key = item.get('key')
					icon = ''
					for ch in self._channels:
						if (ch.get('key') == key) or (ch.get('uuid') == key):
							icon = (ch.get('icon_public_url') or '').strip()
							break
					icons.append(icon)
			except Exception as e:
				self._log("_remap_picons_to_bouquet: get_bouquet_channels(%s) failed: %s" % (ctype, e))
				continue

			# 3) Páruj i-tý ref s i-tým icon (identické poradie)
			if len(refs) != len(icons):
				self._log("_remap_picons_to_bouquet: %s ref/icon mismatch (refs=%d, icons=%d)" % (fn, len(refs), len(icons)))
			paired = 0
			for i in range(min(len(refs), len(icons))):
				if icons[i]:
					mapping.append((refs[i], icons[i]))
					paired += 1
			self._log("_remap_picons_to_bouquet: %s paired %d channels" % (fn, paired))

		if not mapping:
			self._log("_remap_picons_to_bouquet: nothing to map")
			return

		# Stiahni/ulož picony pod správnym menom (ak ešte neexistujú)
		try:
			http_url_fn = self.cp.tvh.make_icon_http_url
		except Exception:
			http_url_fn = None

		written = 0
		skipped = 0
		failed = 0
		try:
			import requests as _req
			from requests.auth import HTTPDigestAuth as _DigestAuth
		except ImportError:
			self._log("_remap_picons_to_bouquet: requests missing")
			return

		sess = _req.Session()
		# Auth: vytiahni z prvého http URL credentials (rovnako ako _patched_dp)
		auth = None
		try:
			from urllib.parse import urlparse as _up
			if http_url_fn and mapping:
				probe = http_url_fn(mapping[0][1])
				if probe:
					pp = _up(probe)
					if pp.username:
						# skús digest (TVH default)
						auth = _DigestAuth(pp.username, pp.password or '')
		except Exception:
			pass

		for ref_name, icon_public_url in mapping:
			dst = _os.path.join(picon_dir, ref_name + '.png')
			if _os.path.isfile(dst) and _os.path.getsize(dst) > 0:
				skipped += 1
				continue
			http_url = None
			try:
				http_url = http_url_fn(icon_public_url) if http_url_fn else None
			except Exception:
				http_url = None
			if not http_url:
				continue
			try:
				r = sess.get(http_url, auth=auth, timeout=10)
				if r.status_code == 200 and r.content and len(r.content) > 100:
					with open(dst, 'wb') as f:
						f.write(r.content)
					written += 1
				else:
					failed += 1
			except Exception:
				failed += 1

		self._log("_remap_picons_to_bouquet: done (written=%d, skipped=%d, "
		          "failed=%d, total_mapped=%d)"
		          % (written, skipped, failed, len(mapping)))
