# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals


class TvhDataApiMixin(object):
	"""Datove accessory pre triedu Tvheadend: kanaly/tagy/EPG-now/DVR.
	Vynate z tvheadend.py v 0.90.0 (refaktor, bez zmeny spravania).
	Deleguje na api_get/api_get_all (core) a HTSP-mixin metody cez MRO;
	pouziva zdielany class-level cache self._channels_cache (def. v core)."""

	def get_tags(self):
		if self.is_htsp_mode():
			# HTSP: tagy z metadát (tagAdd). uuid = str(tagId), aby
			# get_channels_by_tag vedel filtrovať podľa channel.tags.
			# tagIndex = poradie na serveri (pre rovnaké zoradenie ako HTTP API).
			data = self._htsp_meta_best()
			out = []
			for t in data.get('tags', []):
				tid = t.get('tagId')
				if tid is None:
					continue
				out.append({
					'uuid': str(tid),
					'name': t.get('tagName') or str(tid),
					'index': t.get('tagIndex', 999999),
				})
			return out
		return self.api_get_all('api/channeltag/grid', {'start': 0}, page_limit=200)

	def get_channels(self, force=False):
		"""Vráti zoznam kanálov. Výsledok sa cachuje na 60 sekúnd."""
		if not force and self._channels_cache is not None:
			cached = self._channels_cache.get('channels')
			if cached is not None:
				return cached
		if self.is_htsp_mode():
			result = self._htsp_channels_mapped()
		else:
			result = self.api_get_all('api/channel/grid', {'start': 0}, page_limit=1000)
		if self._channels_cache is not None:
			self._channels_cache.put('channels', result)
		return result

	def invalidate_channels_cache(self):
		"""Zmaže cache kanálov."""
		if self._channels_cache is not None:
			self._channels_cache.invalidate('channels')

	def get_channels_by_tag(self, tag_uuid):
		channels = self.get_channels()
		if not tag_uuid:
			return channels
		return [ch for ch in channels if tag_uuid in (ch.get('tags') or [])]

	def get_dvr_finished(self):
		if self.is_htsp_mode():
			return self._htsp_dvr_mapped()
		return self.api_get_all('api/dvr/entry/grid_finished', {'start': 0}, page_limit=500)

	def get_epg_now(self, limit=5000):
		"""Vráti dict {channelUuid: event} pre práve bežiace programy."""
		if self.is_htsp_mode():
			# HTSP: nájdi pre každý kanál práve bežiaci event (start<=now<stop)
			import time as _t
			now = int(_t.time())
			try:
				data = self.htsp_fetch_metadata(with_epg=True) or {}
			except Exception:
				return {}
			out = {}
			for e in data.get('events', []):
				cid = e.get('channelId')
				if cid is None:
					continue
				start = int(e.get('start') or 0)
				stop = int(e.get('stop') or 0)
				if start <= now < stop:
					out[str(cid)] = {
						'channelUuid': str(cid),
						'start': start, 'stop': stop,
						'title': e.get('title') or '',
						'description': e.get('description') or e.get('summary') or '',
					}
			try:
				self._log('[Tvheadend.htsp] get_epg_now: events=%d, now-match=%d' % (
					len(data.get('events') or []), len(out)))
			except Exception:
				pass
			return out
		try:
			data = self.api_get("api/epg/events/grid", params={"mode": "now", "limit": int(limit)})
		except Exception:
			return {}
		out = {}
		for e in (data.get("entries") or []):
			ch = e.get("channelUuid")
			if ch:
				out[ch] = e
		return out

	def get_channel_name_by_service_uuid(self, service_uuid):
		if not service_uuid:
			return None
		try:
			for ch in self.get_channels():
				if service_uuid in (ch.get('services') or []):
					return ch.get('name') or None
		except Exception:
			pass
		return None
