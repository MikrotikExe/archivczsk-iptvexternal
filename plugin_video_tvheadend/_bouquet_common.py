# -*- coding: utf-8 -*-
import re
import os
import io as _io
from tools_archivczsk.six import string_types as basestring
from tools_archivczsk.string_utils import strip_accents


def _open_utf8(path, mode='r'):
	"""Otvorí súbor s UTF-8 kódovaním kompatibilne s Py2 aj Py3."""
	return _io.open(path, mode, encoding='utf-8', errors='ignore')


class BouquetCommonMixin(object):
	"""Spolocne pomocne metody pre TvheadendBouquetXmlEpgGenerator:
	logging, settings coercion, tag normalizacia, jazykovy pick.
	Vynate z bouquet.py v 0.90.0 (refaktor, bez zmeny spravania)."""

	def _log(self, msg):
		"""FIX 0.57.0: log cez framework cp.log_info() ktorý ide priamo
		do /tmp/archivCZSK.log. Predtým bol print() a potom
		logging.getLogger() — ani jeden nešiel do archivCZSK.log
		(archivCZSK framework zachytí len vlastný logger).
		Sleduj cez: `grep '\\[Tvheadend' /tmp/archivCZSK.log`
		"""
		try:
			self.cp.log_debug('[Tvheadend.bouquet] ' + str(msg))
		except Exception:
			pass

	def get_setting(self, name):
		# FIX 0.58.2 (skyjet PR #22 review #11 follow-up):
		# Odstránené hardcoded overrides `enable_xmlepg=False`,
		# `xmlepg_dir=''`, `xmlepg_days=0` (boli z 0.48e keď plugin
		# používal vlastnú custom EPG injection cestu a chcel
		# blokovať framework XML EPG export). Teraz framework
		# `EnigmaEpgGenerator` má naopak BEŽAŤ — settings sa čítajú
		# z user settings.xml ako bool/int.

		# FIX 0.57.0 (skyjet PR #22 review): odstránený priame čítanie
		# /etc/enigma2/settings cez vlastný parser — framework
		# self.cp.get_setting() vracia už sparsovanú a type-correct hodnotu
		# (bool/int/str podľa addon.xml settings.xml type=). Wrappujeme len
		# kvôli legacy bool/int coerce-ovaniu nad str výsledkom z framework-u
		# pre starý formát settingov.
		try:
			val = self.cp.get_setting(name)
		except Exception:
			val = None

		if name in (
			"enable_userbouquet", "enable_userbouquet_radio",
			"userbouquet_categories",
			"enable_picons",
			# FIX 0.58.2: enable_xmlepg bool toggle (framework default name)
			"enable_xmlepg",
		):
			# FIX 0.58.2: enable_xmlepg default=True (settings.xml má
			# default="true"), takže pre fresh install pred prvým save
			# settingov chceme vrátiť True ak setting nie je v configu.
			default = True if name == "enable_xmlepg" else False
			return self._to_bool(val, default=default)

		# FIX 0.58.2: framework default name `xmlepg_days` (predtým `enigmaepg_days`)
		if name == "xmlepg_days":
			return self._to_int(val, default=7)

		# text settings
		if name in ("xmlepg_dir", "player_name",
		            "userbouquet_custom_name_tv", "userbouquet_custom_name_radio"):
			return val if val is not None and val != "" else ""

		return val

	def _to_bool(self, v, default=False):
		if isinstance(v, bool):
			return v
		if v is None:
			return default
		try:
			s = str(v).strip().lower()
		except Exception:
			return default
		if s in ("1", "true", "yes", "on", "enabled"):
			return True
		if s in ("0", "false", "no", "off", "disabled"):
			return False
		return default

	def _to_int(self, v, default=0):
		try:
			return int(v)
		except Exception:
			return default

	def _safe_int(self, v, default=10**6):
		try:
			return int(v)
		except Exception:
			return default

	def _normalize_tag_name(self, s):
		s = (s or "").strip().lower()
		s = strip_accents(s) if s else ''   # rádio -> radio
		s = re.sub(r"\s+", " ", s)
		return s

	def _pick(self, val):
		if not val:
			return ""
		if isinstance(val, basestring):
			return val
		if isinstance(val, dict):
			for k in ('slk', 'slo', 'ces', 'cze', 'eng'):
				if k in val and val[k]:
					return val[k]
			for v in val.values():
				if v:
					return v
		return ""


	def _read_lines(self, path):
		try:
			if not os.path.isfile(path):
				return []
			with _open_utf8(path, "r") as f:
				return f.read().splitlines()
		except Exception:
			return []

	def _write_lines(self, path, lines):
		try:
			with _open_utf8(path, "w") as f:
				f.write("\n".join(lines) + "\n")
			return True
		except Exception:
			return False
