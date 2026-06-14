# -*- coding: utf-8 -*-
import os
import re


class BouquetRadioMixin(object):
	"""Radio-bouquet manazment pre TvheadendBouquetXmlEpgGenerator:
	zabezpecenie/cistenie radio bouquetov, oprava nazvov suborov, presun z TV.
	Vynate z bouquet.py v 0.90.0 (refaktor, bez zmeny spravania).
	Zavisi na _read_lines/_write_lines/get_setting/_log (BouquetCommonMixin, cez MRO)."""

	def _ensure_bouquets_radio_has(self, bouquet_filename):
		base = "/etc/enigma2"
		br = os.path.join(base, "bouquets.radio")

		line_need = '#SERVICE 1:7:2:0:0:0:0:0:0:0:FROM BOUQUET "%s" ORDER BY bouquet' % bouquet_filename

		lines = self._read_lines(br)
		if not lines:
			lines = [
				'#NAME Bouquets (Radio)',
				line_need,
			]
			if self._write_lines(br, lines):
				self._log("Created bouquets.radio + added %s" % bouquet_filename)
			return

		for ln in lines:
			if bouquet_filename in ln and "FROM BOUQUET" in ln:
				return

		out = []
		inserted = False
		for ln in lines:
			out.append(ln)
			if (not inserted) and ln.startswith("#NAME"):
				out.append(line_need)
				inserted = True
		if not inserted:
			out.append(line_need)

		if self._write_lines(br, out):
			self._log("Patched bouquets.radio: added %s" % bouquet_filename)

	def _remove_from_bouquets_tv(self, bouquet_filenames):
		base = "/etc/enigma2"
		bt = os.path.join(base, "bouquets.tv")

		lines = self._read_lines(bt)
		if not lines:
			return

		def _hit(line):
			for b in bouquet_filenames:
				if ('FROM BOUQUET "%s"' % b) in line:
					return True
			return False

		new_lines = [ln for ln in lines if not _hit(ln)]
		if new_lines != lines:
			if self._write_lines(bt, new_lines):
				self._log("Removed radio bouquet(s) from bouquets.tv: %s" % (", ".join(bouquet_filenames)))

	def _fix_radio_bouquet_filenames(self):
		# FIX 0.58.5 (audit, Juraj): pridaný entry/exit logging a count tracking
		# pre diagnostiku. Predtým funkcia bežala silently a keď zlyhala (napr.
		# rename race, IOError, alebo permission issue na /etc/enigma2) výnimka
		# bola zachytená vyššie v `_post()` cez `try/except: pass` čo viedlo
		# k tomu že userbouquet.tvheadend_radio.tv zostal v bouquets.tv namiesto
		# byť presunutý do bouquets.radio. Bez logu sa to nedalo identifikovať.
		if not self.get_setting("enable_userbouquet_radio"):
			self._log("_fix_radio_bouquet_filenames: skipped (enable_userbouquet_radio=False)")
			return

		self._log("_fix_radio_bouquet_filenames: started")

		base = "/etc/enigma2"
		try:
			files = os.listdir(base)
		except Exception as e:
			self._log("_fix_radio_bouquet_filenames: listdir(%s) failed: %s" % (base, e))
			return

		remove_from_tv = []

		renamed_to = []
		for fn in files:
			lfn = fn.lower()
			if not fn.startswith("userbouquet."):
				continue
			if not fn.endswith(".tv"):
				continue
			if not (("radio" in lfn) or ("radia" in lfn) or ("rádio" in lfn) or ("rádia" in lfn)):
				continue

			src = os.path.join(base, fn)

			dst_fn = fn[:-3] + ".radio"
			dst = os.path.join(base, dst_fn)

			remove_from_tv.append(fn)

			try:
				if os.path.isfile(dst):
					try:
						os.remove(src)
					except Exception as e:
						self._log("Radio bouquet: cannot remove duplicate %s: %s" % (src, e))
					renamed_to.append(dst_fn)
					continue

				os.rename(src, dst)
				renamed_to.append(dst_fn)
				self._log("Radio bouquet rename: %s -> %s" % (fn, dst_fn))
			except Exception as e:
				self._log("Radio bouquet rename FAILED: %s -> %s: %s" % (fn, dst_fn, e))
				continue

		br = os.path.join(base, "bouquets.radio")
		lines = self._read_lines(br)
		if lines:
			new_lines = []
			changed = False
			for line in lines:
				m = re.search(r'FROM BOUQUET \"(userbouquet\.[^\"]+)\.tv\"', line)
				if m:
					base_name = m.group(1)
					new_line = line.replace(base_name + ".tv", base_name + ".radio")
					if new_line != line:
						changed = True
						line = new_line
				new_lines.append(line)
			if changed and self._write_lines(br, new_lines):
				self._log("Patched bouquets.radio references (.tv -> .radio)")

		for fn in list(remove_from_tv):
			remove_from_tv.append(fn[:-3] + ".radio")
		self._remove_from_bouquets_tv(sorted(set(remove_from_tv)))

		target = "userbouquet.tvheadend_radio.radio"
		if os.path.isfile(os.path.join(base, target)):
			self._ensure_bouquets_radio_has(target)
			self._log("_fix_radio_bouquet_filenames: done (target %s already exists, renamed=%d)"
			          % (target, len(renamed_to)))
			return

		try:
			for fn in os.listdir(base):
				lfn = fn.lower()
				if not fn.startswith("userbouquet.") or not fn.endswith(".radio"):
					continue
				if "tvheadend" in lfn and ("radio" in lfn or "radia" in lfn or "rádio" in lfn or "rádia" in lfn):
					self._ensure_bouquets_radio_has(fn)
					self._log("_fix_radio_bouquet_filenames: done (renamed=%d, found existing radio bouquet=%s)"
					          % (len(renamed_to), fn))
					return
		except Exception as e:
			self._log("_fix_radio_bouquet_filenames: scan for existing .radio failed: %s" % e)

		if renamed_to:
			self._ensure_bouquets_radio_has(renamed_to[0])

		self._log("_fix_radio_bouquet_filenames: done (renamed=%d files, remove_from_tv=%d)"
		          % (len(renamed_to), len(remove_from_tv)))

	def _cleanup_orphaned_radio_bouquets(self):
		# FIX 0.58.6 (audit, Juraj): pri vypnutí enable_userbouquet framework
		# zmaže iba `userbouquet.<prefix>.tv` súbory (podľa `userbouquet_file_name`).
		# Tvheadend Radio userbouquet má extension `.radio` (po
		# _fix_radio_bouquet_filenames rename), takže framework ho nezachytí.
		# Táto metóda doupracuje: zmaže orphaned .radio súbory pre prefix
		# 'tvheadend' a odstráni ich referencie z bouquets.radio.
		base = "/etc/enigma2"
		try:
			files = os.listdir(base)
		except Exception as e:
			self._log("_cleanup_orphaned_radio_bouquets: listdir failed: %s" % e)
			return

		removed_files = []
		for fn in files:
			lfn = fn.lower()
			if not fn.startswith("userbouquet."):
				continue
			if not fn.endswith(".radio"):
				continue
			# Bezpečnostná kontrola: maž iba tvheadend bouquety, nie napr.
			# m3u_iptv.radio (z e2m3u2bouquet) alebo favourites.radio.
			if "tvheadend" not in lfn:
				continue

			path = os.path.join(base, fn)
			try:
				os.remove(path)
				removed_files.append(fn)
				self._log("_cleanup_orphaned_radio_bouquets: removed %s" % fn)
			except Exception as e:
				self._log("_cleanup_orphaned_radio_bouquets: cannot remove %s: %s" % (fn, e))

		# Odstrániť referencie z bouquets.radio
		if removed_files:
			br = os.path.join(base, "bouquets.radio")
			lines = self._read_lines(br)
			if lines:
				def _hit(line):
					for f in removed_files:
						if ('FROM BOUQUET "%s"' % f) in line:
							return True
					return False
				new_lines = [ln for ln in lines if not _hit(ln)]
				if new_lines != lines:
					if self._write_lines(br, new_lines):
						self._log("_cleanup_orphaned_radio_bouquets: patched bouquets.radio (removed %d refs)"
						          % (len(lines) - len(new_lines)))

		self._log("_cleanup_orphaned_radio_bouquets: done (removed %d files)" % len(removed_files))
