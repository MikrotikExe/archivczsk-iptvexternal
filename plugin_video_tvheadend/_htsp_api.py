# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals


class TvhHtspApiMixin(object):
	"""HTSP-rezim API pre triedu Tvheadend: detekcia rezimu, fetch metadat
	(kanaly/tagy/EPG/DVR) cez HTSP protokol a mapovanie na interne struktury.
	Vynate z tvheadend.py v 0.90.0 (refaktor, bez zmeny spravania).
	Vsetky importy su lokalne v metodach; zavisi na self._log (core Tvheadend)
	a self.cp cez MRO."""

	def is_htsp_mode(self):
		"""True ak je v nastaveniach zvolený HTSP mód pripojenia."""
		try:
			return (self.cp.get_setting('connection_mode') or 'http').strip() == 'htsp'
		except Exception:
			return False

	def _htsp_host(self):
		"""Hostname pre HTSP (bez http:// prefixu a portu)."""
		host = (self.cp.get_setting('host') or '').strip()
		if '://' in host:
			try:
				from urllib.parse import urlparse
			except ImportError:
				from urlparse import urlparse
			host = urlparse(host).hostname or host
		return host

	def _htsp_params(self):
		host = self._htsp_host()
		port = int(self.cp.get_setting('htsp_port') or 9982)
		user = (self.cp.get_setting('username') or '').strip()
		pwd = (self.cp.get_setting('password') or '')
		return host, port, user, pwd

	def htsp_fetch_metadata(self, with_epg=True, force=False, ttl=None,
	                        channels_only=False):
		"""Načíta HTSP metadata s cache. Rozlišuje cache pre with_epg vs bez,
		lebo fetch bez EPG vráti 0 eventov a nesmie 'zatieniť' EPG fetch.
		EPG fetch je drahý, tak má dlhší TTL. Vždy dotiahne celý DVR.
		channels_only=True: rýchla cesta — len kanály+tagy (pre stream URL),
		ukladá sa do non-EPG cache."""
		import time as _t
		now = _t.time()
		# dve cache podľa with_epg. Dlhé TTL = menej HTSP connectov
		# (predtým sa connectovalo pri každej operácii a server sa zahltil).
		if with_epg:
			cache_attr, ts_attr, def_ttl = '_htsp_meta_epg', '_htsp_meta_epg_ts', 600
		else:
			cache_attr, ts_attr, def_ttl = '_htsp_meta', '_htsp_meta_ts', 600
		if ttl is None:
			ttl = def_ttl
		cached = getattr(self, cache_attr, None)
		cached_ts = getattr(self, ts_attr, 0)
		if not force and cached is not None and (now - cached_ts) < ttl:
			return cached
		# Lock: ak už beží fetch (napr. prefetch na pozadí), počkáme naň
		# namiesto spustenia druhého paralelného fetchu (dva EPG buffery
		# = záťaž servera). Po získaní locku znova skontrolujeme cache —
		# medzitým ju mohol naplniť ten druhý fetch.
		lock = getattr(self, '_htsp_fetch_lock', None)
		if lock is not None:
			lock.acquire()
		try:
			now = _t.time()
			cached = getattr(self, cache_attr, None)
			cached_ts = getattr(self, ts_attr, 0)
			if not force and cached is not None and (now - cached_ts) < ttl:
				return cached
			from . import htsp as _htsp
			host, port, user, pwd = self._htsp_params()
			client = _htsp.HTSPClient(host, port, user, pwd, cp=self.cp)
			client.connect()
			try:
				# epg_max_days=2: obmedz EPG na ~2 dni (rýchlejší fetch + menej RAM)
				data = client.fetch_metadata(with_epg=with_epg, epg_max_days=2,
				                             channels_only=channels_only)
			finally:
				client.close()
			tagmap = {}
			for t in data.get('tags', []):
				tid = t.get('tagId')
				if tid is not None:
					tagmap[tid] = t.get('tagName') or str(tid)
			data['_tagmap'] = tagmap
			# EPG fetch: ak je neúplný (0 eventov, alebo sync nedobehol =
			# server nestihol poslať celý EPG), NEcachuj nadlho — necháme to
			# skúsiť znova. Inak by sa neúplný EPG zafixoval na celé TTL a
			# väčšina kanálov by nemala "teraz" program.
			incomplete_epg = with_epg and (
				not data.get('events') or not data.get('_sync_done'))
			if incomplete_epg:
				self._log('EPG fetch neúplný (events=%d, sync=%r) — necachujem nadlho' % (
					len(data.get('events') or []), data.get('_sync_done')))
				setattr(self, cache_attr, data)
				setattr(self, ts_attr, now - ttl + 15)  # platnosť len 15s
			elif channels_only:
				# rýchly fetch len kanálov (bez DVR) — kanály sú platné, ale
				# DVR chýba, tak cachuj len krátko (60s) nech plný fetch
				# (s DVR pre archív) môže čoskoro nabehnúť
				setattr(self, cache_attr, data)
				setattr(self, ts_attr, now - ttl + 60)
			else:
				setattr(self, cache_attr, data)
				setattr(self, ts_attr, now)
			return data
		finally:
			if lock is not None:
				try:
					lock.release()
				except Exception:
					pass

	def _htsp_meta_best(self):
		"""Vráti najlepšie dostupné metadata z JEDNÉHO zdieľaného fetchu:
		kanály+tagy+DVR+EPG prídu naraz v jednom HTSP spojení (server ich
		posiela v jednom async prúde). Ak je čerstvá EPG cache, použi ju;
		inak spusti plný fetch (with_epg=True) ktorý naplní všetko naraz.
		Tým sa DVR aj EPG načítajú spolu, nie dvoma oddelenými fetchmi."""
		import time as _t
		now = _t.time()
		epg = getattr(self, '_htsp_meta_epg', None)
		epg_ts = getattr(self, '_htsp_meta_epg_ts', 0)
		if epg is not None and (now - epg_ts) < 600 and epg.get('channels'):
			return epg
		# JEDEN fetch = DVR aj EPG naraz (server ich pošle v jednom prúde)
		return self.htsp_fetch_metadata(with_epg=True)

	def _htsp_channels_mapped(self):
		"""HTSP kanály v rovnakom formáte ako HTTP api/channel/grid.
		Pre streamovanie stačia kanály — ak nemáme plnú cache, použijeme
		RÝCHLY channels_only fetch (nečaká na DVR dump 30s). To rieši
		pomalé spustenie kanála po reboote GUI."""
		import time as _t
		now = _t.time()
		# 1) plná EPG cache (má kanály+tagy+DVR+EPG)
		epg = getattr(self, '_htsp_meta_epg', None)
		epg_ts = getattr(self, '_htsp_meta_epg_ts', 0)
		if epg is not None and (now - epg_ts) < 600 and epg.get('channels'):
			data = epg
		else:
			# 2) non-EPG cache (kanály+tagy, prípadne DVR)
			meta = getattr(self, '_htsp_meta', None)
			meta_ts = getattr(self, '_htsp_meta_ts', 0)
			if meta is not None and (now - meta_ts) < 600 and meta.get('channels'):
				data = meta
			else:
				# 3) nič v cache → RÝCHLY fetch len kanálov (bez DVR/EPG dumpu)
				data = self.htsp_fetch_metadata(with_epg=False, channels_only=True)
		out = []
		for ch in data.get('channels', []):
			cid = ch.get('channelId')
			if cid is None:
				continue
			tag_ids = ch.get('tags') or []
			# ulož tagId ako stringy (zhodné s get_tags uuid) — aby
			# get_channels_by_tag(cat_id) správne filtroval
			tag_uuids = [str(t) for t in tag_ids] if isinstance(tag_ids, list) else []
			out.append({
				'uuid': str(cid),
				'name': ch.get('channelName') or str(cid),
				'number': ch.get('channelNumber') or 0,
				'icon_public_url': ch.get('channelIcon') or '',
				'tags': tag_uuids,
				'services': ch.get('services') or [],
				'enabled': True,
			})
		return out

	def _htsp_dvr_mapped(self):
		"""HTSP dokončené DVR nahrávky v formáte ako HTTP grid_finished."""
		data = self._htsp_meta_best()
		raw = data.get('dvr') or []
		try:
			from collections import Counter
			states = Counter(x.get('state') for x in raw)
			self._log('[Tvheadend.htsp] DVR mapping: raw=%d, state=%r' % (
				len(raw), dict(states)))
		except Exception:
			pass
		# mapa channelId -> channelIcon (pre picon v archíve)
		chan_icon = {}
		for ch in data.get('channels', []):
			cid = ch.get('channelId')
			if cid is not None:
				chan_icon[cid] = ch.get('channelIcon') or ''
		out = []
		skipped_state = 0
		for d in data.get('dvr', []):
			# completed nahrávky: state 'completed' alebo má súbor+veľkosť
			st = d.get('state')
			if st != 'completed':
				# fallback: niektoré servery/verzie majú iný state string
				if not (d.get('dataSize') and d.get('idStr')):
					skipped_state += 1
					continue
			did = d.get('id')
			if did is None:
				continue
			# fileOpen /dvrfile/ chce hex idStr (nie číselné id!)
			id_str = d.get('idStr') or str(did)
			start = d.get('start') or 0
			stop = d.get('stop') or 0
			ch_id = d.get('channel')
			# picon: priamo z DVR alebo dohľadaj cez channelId kanála
			icon = d.get('channelIcon') or chan_icon.get(ch_id, '')
			out.append({
				'uuid': str(did),
				'disp_title': d.get('title') or '',
				'disp_subtitle': d.get('subtitle') or '',
				'disp_description': d.get('description') or d.get('summary') or '',
				'channelname': d.get('channelName') or '',
				'channel': str(ch_id or ''),
				'channel_icon': icon,
				'start': start,
				'stop': stop,
				'duration': max(0, int(stop) - int(start)) if (start and stop) else 0,
				'filesize': d.get('dataSize') or 0,
				'_htsp_dvr_id': did,
				'url': 'htsp_dvr:%s' % id_str,  # idStr (hex) pre fileOpen
			})
		# zoradiť od najnovších
		out.sort(key=lambda x: x.get('start', 0), reverse=True)
		try:
			self._log('[Tvheadend.htsp] DVR mapping: %d raw -> %d completed (skip_state=%d)' % (
				len(raw), len(out), skipped_state))
		except Exception:
			pass
		return out
