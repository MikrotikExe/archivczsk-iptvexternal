# -*- coding: utf-8 -*-


class BouquetTagsMixin(object):
	"""Tag/kategoria klasifikacia kanalov pre TvheadendBouquetXmlEpgGenerator:
	mapovanie tag uuid->nazov+poradie, detekcia radia podla tagov, kategorie
	kanala, poradie kategorii. Vynate z bouquet.py v 0.90.0 (refaktor).
	Zavisi na self._tagmap/_taguuid_to_order/_tagnorm_to_order (init v hlavnej
	triede) a na self._safe_int / self._normalize_tag_name (BouquetCommonMixin)."""

	def _get_tag_uuid_to_name(self):
		# ✅ if cache ready, return
		if self._tagmap is not None and self._taguuid_to_order is not None and self._tagnorm_to_order is not None:
			return self._tagmap

		self._tagmap = {}
		self._taguuid_to_order = {}
		self._tagnorm_to_order = {}

		try:
			tags = self.cp.tvh.get_tags() or []
		except Exception:
			tags = []

		# TVH returns "index" (important for ordering)
		for t in tags:
			u = (t.get('uuid') or '').strip()
			n = (t.get('name') or t.get('val') or '').strip()
			if not u or not n:
				continue

			self._tagmap[u] = n

			idx = self._safe_int(t.get('index', None), default=10**6)
			if idx == 10**6:
				idx = self._safe_int(t.get('order', None), default=10**6)

			if (u not in self._taguuid_to_order) or (idx < self._taguuid_to_order.get(u, 10**6)):
				self._taguuid_to_order[u] = idx

			nn = self._normalize_tag_name(n)
			if nn:
				if (nn not in self._tagnorm_to_order) or (idx < self._tagnorm_to_order.get(nn, 10**6)):
					self._tagnorm_to_order[nn] = idx

		return self._tagmap

	def _is_radio_by_tags(self, tag_uuids):
		radio_tokens = (
			"radio", "radia", "radia fm", "radio fm",
			"radiostanice", "radiostanica",
			"rádio", "rádia", "rádiá",
		)

		tagmap = self._get_tag_uuid_to_name()
		for tu in (tag_uuids or []):
			n = self._normalize_tag_name(tagmap.get(tu) or "")
			if not n:
				continue

			for tok in radio_tokens:
				tokn = self._normalize_tag_name(tok)
				if n == tokn or tokn in n:
					return True

		return False

	def _get_channel_categories(self, ch):
		out = []
		tagmap = self._get_tag_uuid_to_name()
		for tu in (ch.get("tags") or []):
			n = (tagmap.get(tu) or "").strip()
			if n:
				out.append(n)
		return out

	def _category_order(self, cat_name):
		"""
		Category order:
		- based on TVH "index" (api/channeltag/grid)
		- "Ostatné" always last
		- fallback: big number
		"""
		if not cat_name:
			return 10**6

		nn = self._normalize_tag_name(cat_name)
		if nn in ("ostatne", "ostatné"):
			return 10**9

		self._get_tag_uuid_to_name()
		if self._tagnorm_to_order and nn in self._tagnorm_to_order:
			return self._safe_int(self._tagnorm_to_order.get(nn), default=10**6)

		return 10**6

