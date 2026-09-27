# -*- coding: utf-8 -*-
import os
import re


class BouquetDvbMixin(object):
	"""DVB service-ref prepis bouquetov pre TvheadendBouquetXmlEpgGenerator.
	Vynate z bouquet.py v 0.90.0 (refaktor, bez zmeny spravania).
	FIX 1.0.2: picon remap presunuty do _bouquet_picons.py (BouquetPiconsMixin).
	Zavisi na _read_lines/_write_lines/_log (BouquetCommonMixin) a hooku
	get_bouquet_channels (hlavna trieda) cez MRO; pouziva self._key_to_url."""

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
