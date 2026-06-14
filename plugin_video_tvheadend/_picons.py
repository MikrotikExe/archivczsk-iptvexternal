# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals

import os
import io
import re
import time
import threading

from ._paths import data_path
from tools_archivczsk.contentprovider.exception import AddonErrorException

# PIL je voliteľný – používa sa len na konverziu ikon
try:
	from PIL import Image as _PIL_Image
	_PIL_OK = True
except Exception:
	_PIL_OK = False

try:
	import queue as _queue_mod
except ImportError:
	try:
		import Queue as _queue_mod
	except ImportError:
		_queue_mod = None


# --------------------------------------------------------------------------
_PICON_TTL_DAYS = 7
# FIX 0.48j: stamp v persistent data dir-u, nie v /tmp
_PICON_STAMP = data_path("tvh_picon.stamp")
_PICON_MAX_WORKERS = 6
# FIX 0.71.1: EARLY-ABORT proti OOM na slabých boxoch.
# Ak server nemá ŽIADNE imagecache picony (napr. zle nakonfigurovaný TVH —
# vracia 404 na všetko), starší kód preženie cez sieť všetkých N kanálov
# (568+) hoci ani jeden neexistuje. Na boxe s ~300 MB RAM (Formuler F4 turbo)
# to spolu s načítaním kanálov/EPG vyčerpá pamäť → kernel zabije enigma2
# (SIGKILL, bez crash logu, tichý reboot GUI). A keďže reboot vyprázdni
# in-memory 404 cache, cyklus sa opakuje pri každom uložení údajov.
# Riešenie: keď príde _PICON_EARLY_ABORT_404 ×404 a 0 úspešných sťahovaní,
# je jasné že server picony nemá → zrušíme zvyšok a označíme do 404 cache.
_PICON_EARLY_ABORT_404 = 30
_picon_worker_lock = threading.Lock()

# threading.Event – signalizuje že picon worker dobehol
# bouquet._post() čaká na tento event namiesto sleep slučky
_picon_ready_event = threading.Event()

# FIX 0.48b: NEGATÍVNA CACHE pre 404 picony.
# Problém: niektoré channels v TVH majú icon_public_url='imagecache/NNNN'
# kde TVH samotný vracia 404 (broken upstream icon, lazy-load failed,
# kanál nebol nedávno "dotknutý" v TVH webUI atď.). Plugin to skúšal
# znova a znova pri každom login()/refresh-i a generoval log spam +
# zbytočnú záťaž na TVH server.
# Riešenie: zapamätáme si URL ktoré dali 404 a 1 hodinu ich nezačneme
# znova ťahať. Po hodine sa skúsi raz (kanál sa medzitým mohol opraviť),
# ak opäť 404 → ďalšia hodina tichá. Cache je modul-level, žije počas
# behu pluginu (resp. do reštartu E2).
_PICON_404_CACHE = {}     # url -> timestamp prvého 404
_PICON_404_LOCK = threading.Lock()
_PICON_404_TTL = 3600     # 1 hodina

def _picon_url_in_404_cache(url):
	"""Je toto URL v negatívnej cache a ešte platné?"""
	if not url:
		return False
	with _PICON_404_LOCK:
		ts = _PICON_404_CACHE.get(url)
		if ts is None:
			return False
		now = int(time.time())
		if (now - ts) >= _PICON_404_TTL:
			# expirované — vymaž a daj možnosť znova skúsiť
			_PICON_404_CACHE.pop(url, None)
			return False
		return True

def _picon_mark_404(url):
	"""Zaznamenaj že URL vrátilo 404."""
	if not url:
		return
	with _PICON_404_LOCK:
		_PICON_404_CACHE[url] = int(time.time())

def _picon_404_count():
	"""Počet aktívne-cached 404 URL (pre status / diagnostiku)."""
	with _PICON_404_LOCK:
		return len(_PICON_404_CACHE)

def _picon_404_clear():
	"""Manuálne vyčisti 404 cache (užívateľské Settings menu)."""
	with _PICON_404_LOCK:
		_PICON_404_CACHE.clear()


class TvhPiconMixin(object):
	"""Picon/obrazky pre triedu Tvheadend: imagecache ID, lokalne cesty,
	async sťahovanie do /tmp cache (worker + 404 negat. cache + early-abort),
	stavba icon URL a download/konverzia obrazkov (PIL volitelne).
	Vynate z tvheadend.py v 0.90.0 (refaktor, bez zmeny spravania).
	Deleguje na _apply_auth_to_session/_timeout/_url (core), _url_with_creds
	(STREAM mixin), get_channels (DATA mixin) cez MRO."""

	def _sanitize_filename(self, s):
		s = s or ''
		s = re.sub(r'[^a-zA-Z0-9_.-]+', '_', s)
		return s[:80]

	def _imagecache_id(self, icon_public_url):
		"""Vráti čistý ID z imagecache/ID (bez prípony), alebo None."""
		ipu = (icon_public_url or '').strip().lstrip('/')
		if not ipu.startswith('imagecache/'):
			return None
		idpart = ipu.split('/', 1)[1].split('?', 1)[0].strip()
		if not idpart:
			return None
		for e in ('.png', '.jpg', '.jpeg'):
			if idpart.lower().endswith(e):
				idpart = idpart[:-len(e)]
				break
		return self._sanitize_filename(idpart) or None

	def _picon_local_path(self, icon_public_url):
		"""
		Vráti lokálnu cestu pre danú ikonku.
		Pre imagecache/* hľadá existujúci súbor v .png aj .jpg variante.
		"""
		cid = self._imagecache_id(icon_public_url)
		if cid:
			# preferuj existujúci súbor (bez ohľadu na príponu)
			for ext in ('.png', '.jpg'):
				p = os.path.join(self._img_cache_dir, 'imagecache_%s%s' % (cid, ext))
				try:
					if os.path.isfile(p) and os.path.getsize(p) > 0:
						return p
				except Exception:
					pass
			# default – PNG (bude konvertované ak PIL dostupný, inak uložené as-is)
			return os.path.join(self._img_cache_dir, 'imagecache_%s.png' % cid)
		key = self._sanitize_filename((icon_public_url or '').replace('/', '_'))
		return os.path.join(self._img_cache_dir, '%s.png' % (key or 'img'))

	def init_picons_async(self):
		"""
		Spustí sťahovanie ikoniek na pozadí (daemon thread).
		Volá sa z login() – nie z __init__ – aby neblokovala GUI pri štarte.
		"""
		t = threading.Thread(target=self._init_picons_worker)
		t.daemon = True
		t.start()

	def _log_picon(self, msg):
		"""FIX 0.57.0: log cez framework self.cp.log_info() ktorý ide do
		/tmp/archivCZSK.log. Predtým bol print() ktorý nešiel (E2 stdout
		nie je redirected na archivCZSK.log pre plugin code).
		Sleduj cez: grep '\\[Tvheadend.picons\\]' /tmp/archivCZSK.log
		"""
		try:
			cp = getattr(self, 'cp', None)
			if cp is not None and hasattr(cp, 'log_info'):
				cp.log_debug('[Tvheadend.picons] ' + str(msg))
		except Exception:
			pass

	def _init_picons_worker(self):
		# Zabráň paralelným behom – len jeden worker naraz
		if not _picon_worker_lock.acquire(False):
			return
		# FIX 0.48c: clear() event PRED behom workera, set() až na konci.
		# Predtým bol event set forever po prvom behu — _post() v bouquet.py
		# pri ďalšom refreshi nečakal a picon copy bežal PRED tým ako sa
		# nové ikony stiahli (race condition pri pridaní nových kanálov v TVH).
		_picon_ready_event.clear()
		try:
			self._init_picons_worker_inner()
		finally:
			# Set sa volá aj z _init_picons_worker_inner pri early-return
			# cestách. Tu len garancia že sa to NEZABUDNE pri exception.
			_picon_ready_event.set()
			_picon_worker_lock.release()

	def _init_picons_worker_inner(self):
		try:
			now = int(time.time())
			ttl = int(_PICON_TTL_DAYS) * 24 * 3600

			cache_has_files = False
			cached_imagecache_count = 0
			try:
				if os.path.isdir(self._img_cache_dir):
					for f in os.listdir(self._img_cache_dir):
						if f.startswith('imagecache_') and not f.endswith('.tmp'):
							cached_imagecache_count += 1
					cache_has_files = cached_imagecache_count > 0
			except Exception:
				pass

			last = 0
			if cache_has_files:
				try:
					last = int(os.path.getmtime(_PICON_STAMP))
				except Exception:
					pass

			# Stamp je neplatný ak je picon adresár prázdny – napr. po reinstalácii
			picon_dir_empty = True
			for pd in ('/usr/share/enigma2/picon', '/media/hdd/picon', '/media/usb/picon'):
				try:
					if os.path.isdir(pd):
						if len([f for f in os.listdir(pd) if f.endswith('.png')]) > 0:
							picon_dir_empty = False
							break
				except Exception:
					pass

			# Stamp pre kopírovanie – zmaž ak je picon adresár prázdny
			# FIX 0.48j: persistent data dir
			_PICON_COPY_STAMP = data_path('tvh_picon_copy.stamp')
			if picon_dir_empty:
				try:
					if os.path.isfile(_PICON_COPY_STAMP):
						os.remove(_PICON_COPY_STAMP)
						self._log_picon('Picon dir empty – removed copy stamp')
				except Exception:
					pass

			# FIX 0.48: porovnaj počet cached súborov s počtom kanálov ktoré majú
			# imagecache ikonu. Ak je expected významne väčšie ako have, znamená to:
			#  a) TVH pridal nové kanály od posledného download-u
			#  b) cached súbory boli manuálne zmazané z /tmp
			# V oboch prípadoch ignoruj TTL stamp a spusti download — tým sa
			# rieši "po pridaní nového kanála v TVH treba reštart pluginu".
			channels_preview = None
			try:
				channels_preview = self.get_channels()
			except Exception:
				channels_preview = None

			expected_count = 0
			expected_404_known = 0  # FIX 0.48b: koľko z "expected" je v 404 cache
			if channels_preview is not None:
				try:
					for ch in channels_preview:
						ipu = (ch.get('icon_public_url') or '').lstrip('/')
						if ipu.startswith('imagecache/'):
							expected_count += 1
							if _picon_url_in_404_cache(ch.get('icon_public_url') or ''):
								expected_404_known += 1
				except Exception:
					expected_count = 0
					expected_404_known = 0

			# FIX 0.48b: dosažiteľné expected = bez tých čo sú permanente 404.
			# Bez tohto pri 116 broken-iconoch by sa "force download" triggroval
			# pri každom login()-e do nekonečna.
			effective_expected = expected_count - expected_404_known

			# Pomer "máme dosť" — 5% tolerancia na racing
			significantly_missing = (
				effective_expected > 0 and
				cached_imagecache_count < int(effective_expected * 0.95)
			)
			if significantly_missing:
				self._log_picon(
					'Cache incomplete: expected=%d (known-404=%d, effective=%d) '
					'have=%d — forcing download (ignoring TTL stamp)' %
					(expected_count, expected_404_known, effective_expected,
					 cached_imagecache_count))
				# zmaž stamp → nasledujúca časť detekuje fresh required
				last = 0
			elif expected_404_known > 0:
				self._log_picon('Note: %d channels have known-broken icons '
				                '(in 404 cache) — will retry after 1h' %
				                expected_404_known)

			if last and (now - last) < ttl and cache_has_files and not picon_dir_empty and not significantly_missing:
				self._log_picon('Picon cache is fresh (last=%d, ttl=%d), skipping' % (last, ttl))
				_picon_ready_event.set()
				return

			self._log_picon('Starting picon download (cache_has_files=%s, last=%d, '
			                'expected=%d, effective=%d, have=%d)' %
			                (cache_has_files, last, expected_count,
			                 effective_expected, cached_imagecache_count))

			try:
				if not os.path.isdir(self._img_cache_dir):
					os.makedirs(self._img_cache_dir)
			except Exception:
				pass

			# FIX 0.48: reuse channels už načítané vyššie (cache to aj tak vyrieši,
			# ale lepšie sa to číta)
			channels = channels_preview
			if channels is None:
				try:
					channels = self.get_channels()
				except Exception as e:
					self._log_picon('get_channels failed: %s' % e)
					return

			jobs = []
			skipped = 0
			no_icon = 0
			pre_404_skipped = 0  # FIX 0.48b: kanály ktoré skipujeme vďaka 404 cache
			for ch in channels:
				icon = ch.get('icon_public_url') or ''
				if not icon:
					no_icon += 1
					continue
				if not icon.lstrip('/').startswith('imagecache/'):
					continue
				dst = self._picon_local_path(icon)
				if not dst:
					continue
				try:
					if os.path.isfile(dst) and os.path.getsize(dst) > 0:
						skipped += 1
						continue
				except Exception:
					pass
				# FIX 0.48b: ak je v 404 cache, ani ho neskúšaj
				if _picon_url_in_404_cache(icon):
					pre_404_skipped += 1
					continue
				jobs.append((icon, dst))

			self._log_picon('Channels: %d, no_icon: %d, cached: %d, '
			                'skipped_404_cache: %d, to_download: %d' % (
				len(channels), no_icon, skipped, pre_404_skipped, len(jobs)))

			if not jobs:
				self._write_stamp(_PICON_STAMP, now)
				self._log_picon('Nothing to download, stamp updated')
				_picon_ready_event.set()
				return

			ok_count = [0]
			err_count = [0]
			err_404_count = [0]  # FIX 0.48b: tracknúť 404 zvlášť
			# FIX 0.71.1: early-abort stav
			aborted_count = [0]            # koľko jobov sme preskočili po abort-e
			abort_event = threading.Event()  # set() => server nemá picony, končíme
			# Per-thread logujeme len prvých 5 FAIL-ov, zvyšok len count
			_LOG_FAIL_LIMIT = 5
			err_log_lock = threading.Lock()

			def _maybe_trigger_abort():
				# Volá sa POD err_log_lock-om.
				# Podmienka: žiadne úspešné stiahnutie + dosť 404 => server
				# zjavne nemá imagecache picony, nemá zmysel skúšať zvyšok.
				if (not abort_event.is_set()
						and ok_count[0] == 0
						and err_404_count[0] >= _PICON_EARLY_ABORT_404):
					abort_event.set()
					self._log_picon(
						'EARLY-ABORT: %d×404 a 0 úspešných — server nemá '
						'imagecache picony, ruším zvyšok sťahovania '
						'(ochrana pred preťažením/OOM na slabých boxoch)'
						% err_404_count[0])

			def _record_fail(icon, exc):
				err_count[0] += 1
				msg = str(exc) or ''
				# negative-cache miss kvôli 404 vyzerá takto:
				if '404' in msg or 'in 404 negative cache' in msg:
					err_404_count[0] += 1
					# zaznamenaj do negatívnej cache aj zvonku (poistka,
					# _download_image to už urobí ale pri 'in 404 negative cache'
					# exception sa do _download_image vôbec nedostane)
					_picon_mark_404(icon)
				with err_log_lock:
					if err_count[0] <= _LOG_FAIL_LIMIT:
						self._log_picon('FAIL %s: %s' % (icon, exc))
					elif err_count[0] == _LOG_FAIL_LIMIT + 1:
						self._log_picon('... (suppressing further per-file '
						                'FAIL logs; summary at end)')
					# FIX 0.71.1: vyhodnoť či netreba prerušiť celý batch
					_maybe_trigger_abort()

			if _queue_mod is None:
				for icon, dst in jobs:
					# FIX 0.71.1: po abort-e zvyšok len označíme 404 a preskočíme
					if abort_event.is_set():
						_picon_mark_404(icon)
						aborted_count[0] += 1
						continue
					try:
						self._download_image(icon, dst)
						ok_count[0] += 1
					except Exception as e:
						_record_fail(icon, e)
			else:
				q = _queue_mod.Queue()
				for item in jobs:
					q.put(item)

				workers = max(1, min(_PICON_MAX_WORKERS, len(jobs), 12))

				def _worker():
					sess = self.cp.get_requests_session()
					self._apply_auth_to_session(sess)
					while True:
						# FIX 0.71.1: ak padol early-abort, zvyšok fronty len
						# vyprázdnime (mark 404 + task_done) bez sťahovania,
						# inak by q.join() zamrzol.
						if abort_event.is_set():
							while True:
								try:
									icon, dst = q.get_nowait()
								except Exception:
									return
								try:
									_picon_mark_404(icon)
									aborted_count[0] += 1
								finally:
									try:
										q.task_done()
									except Exception:
										pass
						try:
							icon, dst = q.get_nowait()
						except Exception:
							return
						try:
							self._download_image(icon, dst, session=sess)
							ok_count[0] += 1
						except Exception as e:
							_record_fail(icon, e)
						finally:
							try:
								q.task_done()
							except Exception:
								pass

				for _ in range(workers):
					t = threading.Thread(target=_worker)
					t.daemon = True
					t.start()
				try:
					q.join()
				except Exception:
					pass

			self._write_stamp(_PICON_STAMP, now)
			# FIX 0.48b: sumár s rozdelením 404 vs ostatné
			# FIX 0.71.1: + aborted (preskočené po early-abort-e)
			other_err = err_count[0] - err_404_count[0]
			self._log_picon('Done: ok=%d, err=%d (404=%d, other=%d), aborted=%d. '
			                '404 cache size: %d' %
			                (ok_count[0], err_count[0], err_404_count[0],
			                 other_err, aborted_count[0], _picon_404_count()))
		except Exception as e:
			self._log_picon('Worker exception: %s' % e)
		finally:
			# Vždy signalizuj – aj pri chybe – aby _post() nečakal zbytočne
			_picon_ready_event.set()

	def _write_stamp(path, now):
		try:
			with open(path, 'w') as f:
				f.write(str(now))
		except Exception:
			pass

	def make_icon_url(self, icon_public_url):
		"""
		Vráti lokálnu cestu k ikonke (z /tmp cache) alebo HTTP URL.

		NIKDY neblokuje na sieťovom stiahnutí – to by zamrzlo GUI pri renderovaní
		zoznamu kanálov. Ak súbor nie je v cache, vráti priamu HTTP URL s credentials
		(ArchivCZSK ju vie zobraziť). Sťahovanie do cache prebieha async cez init_picons_async().
		"""
		if not icon_public_url:
			return None
		if icon_public_url.startswith('file://'):
			return icon_public_url.replace('file://', '')
		if icon_public_url.startswith(('http://', 'https://', 'picon://')):
			return icon_public_url

		# Skontroluj lokálnu cache (ak async worker už stiahol)
		dst = self._picon_local_path(icon_public_url)
		try:
			if dst and os.path.isfile(dst) and os.path.getsize(dst) > 0:
				return dst
		except Exception:
			pass

		# Fallback: priama HTTP URL s credentials – žiadny blocking download
		return self._url_with_creds(self._url(icon_public_url))

	def make_icon_http_url(self, icon_public_url):
		"""Vráti absolútny HTTP URL na icon_public_url (pre EPG/bouquet export)."""
		if not icon_public_url:
			return None
		if icon_public_url.startswith('file://'):
			return None
		if icon_public_url.startswith(('http://', 'https://', 'picon://')):
			return self._url_with_creds(icon_public_url)
		return self._url_with_creds(self._url(icon_public_url))

	def _candidate_image_paths(self, icon_public_url):
		"""
		Vráti zoznam možných relatívnych ciest pre imagecache.
		TVH imagecache funguje BEZ prípony: imagecache/1644 vracia PNG.
		Prípony .png/.jpg skúšame len ako fallback.
		"""
		ipu = (icon_public_url or '').strip().lstrip('/')
		if not ipu:
			return []

		# Odstrán query string pre porovnanie
		ipu_clean = ipu.split('?', 1)[0]

		cands = []

		if ipu_clean.startswith('imagecache/'):
			idpart = ipu_clean.split('/', 1)[1].strip()
			# Zisti či má už príponu
			has_ext = any(idpart.lower().endswith(e) for e in ('.png', '.jpg', '.jpeg'))

			if has_ext:
				# Má príponu – skús s príponou aj bez
				cands.append(ipu_clean)
				base_id = idpart
				for e in ('.png', '.jpg', '.jpeg'):
					if base_id.lower().endswith(e):
						base_id = base_id[:-len(e)]
						break
				cands.append('imagecache/%s' % base_id)
			else:
				# Bez prípony – skús priamo (TVH to zvládne), potom s príponami
				cands.append(ipu_clean)          # imagecache/1644  ← funguje na TVH
				cands.append('%s.png' % ipu_clean)  # imagecache/1644.png
				# .jpg netreba – TVH vždy vracia PNG z imagecache
		else:
			cands.append(ipu_clean)

		# Ak mal query string, pridaj aj bez neho
		if '?' in ipu and ipu_clean not in cands:
			cands.append(ipu_clean)

		# Deduplikácia
		seen = set()
		out = []
		for p in cands:
			if p and p not in seen:
				seen.add(p)
				out.append(p)
		return out

	def _ctype_to_ext(ctype):
		"""Content-Type -> prípona súboru."""
		ctype = (ctype or '').lower().split(';')[0].strip()
		if 'jpeg' in ctype or 'jpg' in ctype:
			return '.jpg'
		if 'png' in ctype:
			return '.png'
		if 'gif' in ctype:
			return '.gif'
		if 'svg' in ctype:
			return '.svg'
		if 'webp' in ctype:
			return '.webp'
		return '.png'  # default

	def _sniff_ext(data):
		"""Zistí formát z magic bytes (prvých 16 bajtov)."""
		if data[:8] == b'\x89PNG\r\n\x1a\n':
			return '.png'
		if data[:3] == b'\xff\xd8\xff':
			return '.jpg'
		if data[:6] in (b'GIF87a', b'GIF89a'):
			return '.gif'
		if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
			return '.webp'
		if data[:5] in (b'<?xml', b'<svg ') or b'<svg' in data[:64]:
			return '.svg'
		return None  # neznámy

	def _download_image(self, icon_public_url, dst_path, session=None):
		"""
		Stiahne obrázok a uloží do dst_path.

		Kľúčová logika:
		1. FIX 0.48b: skontroluj negatívnu cache — ak sme nedávno (< 1h) dostali
		   404 pre toto URL, ani sa nepokúšaj a vyhoď okamžite (chráni TVH server
		   pred zbytočnými requestmi + odstraňuje log spam pre permanente
		   chýbajúce imagecache záznamy).
		2. Zistí skutočný formát z Content-Type + magic bytes
		3. Ak je PIL dostupný → konvertuje na RGBA PNG (transparentnosť!)
		4. Ak PIL nie je → uloží so správnou príponou (NIKDY JPEG ako .png)
		5. dst_path sa môže zmeniť (ak skutočná prípona != požadovaná)
		   → vracia skutočnú cestu uloženého súboru
		"""
		if not icon_public_url or not dst_path:
			raise AddonErrorException("Missing icon_public_url/dst_path")

		# FIX 0.48b: krátka cesta cez negatívnu cache
		if _picon_url_in_404_cache(icon_public_url):
			raise AddonErrorException("Image %s in 404 negative cache (skipped)"
			                           % icon_public_url)

		sess = session if session is not None else self.req
		if session is None:
			self._apply_auth_to_session()

		dst_dir = os.path.dirname(dst_path)
		if dst_dir:
			try:
				if not os.path.isdir(dst_dir):
					os.makedirs(dst_dir)
			except Exception:
				pass

		last_err = None
		got_404_on_all_candidates = True  # FIX 0.48b: budeme tracknúť či VŠETKY varianty zlyhali na 404
		for rel in self._candidate_image_paths(icon_public_url):
			url = self._url(rel)
			# Retry pri dočasných chybách (5xx, timeout) – max 3 pokusy
			r = None
			for attempt in range(3):
				try:
					r = sess.get(url, timeout=self._timeout(), stream=True)
					break  # úspech
				except Exception as e:
					last_err = e
					if attempt < 2:
						time.sleep(0.5 * (attempt + 1))
						continue
			if r is None:
				got_404_on_all_candidates = False  # network failure, nie 404
				continue
			if r.status_code == 404:
				# 404 = obrázok neexistuje, nema zmysel opakovať
				last_err = Exception("HTTP 404 for %s" % url)
				continue
			# Tu sme dostali non-404 odpoveď (či už 200, 500, čokoľvek)
			got_404_on_all_candidates = False
			if r.status_code >= 500:
				# 5xx = dočasná chyba servera, skús znova
				for attempt in range(2):
					time.sleep(1.0 * (attempt + 1))
					try:
						r = sess.get(url, timeout=self._timeout(), stream=True)
						if r.status_code == 200:
							break
					except Exception as e:
						last_err = e
			if r.status_code != 200:
				last_err = Exception("HTTP %s for %s" % (r.status_code, url))
				continue
			ctype = (r.headers.get('Content-Type') or '').lower()
			if ctype and not ctype.startswith('image/'):
				last_err = Exception("Not an image: %s" % ctype)
				continue

			# Načítaj celý obsah do pamäte (ikonky sú malé, typicky <50KB)
			try:
				raw = r.content
			except Exception as e:
				last_err = e
				continue

			if not raw:
				last_err = Exception("Empty response for %s" % url)
				continue

			# Zisti skutočný formát
			real_ext = self._sniff_ext(raw[:16])
			if real_ext is None:
				real_ext = self._ctype_to_ext(ctype)

			# --- PIL dostupný: konvertuj na RGBA PNG → transparentnosť v Enigma2 ---
			if _PIL_OK:
				try:
					img = _PIL_Image.open(io.BytesIO(raw))
					# Zachovaj transparentnosť: RGBA alebo P (palette s transparentnosťou)
					if img.mode == 'P' and 'transparency' in img.info:
						img = img.convert('RGBA')
					elif img.mode not in ('RGBA', 'LA'):
						img = img.convert('RGBA')
					# Vždy ukladaj ako PNG (transparentnosť!)
					final_path = dst_path if dst_path.lower().endswith('.png') else (
						os.path.splitext(dst_path)[0] + '.png'
					)
					tmp = final_path + '.tmp'
					img.save(tmp, format='PNG', optimize=False)
					try:
						if os.path.exists(final_path):
							os.remove(final_path)
					except Exception:
						pass
					os.rename(tmp, final_path)
					return final_path
				except Exception as e:
					last_err = e
					# PIL zlyhalo → fallback na raw uloženie

			# --- Bez PIL: uloži raw bytes so SPRÁVNOU príponou ---
			# NIKDY neukladaj JPEG obsah do .png súboru!
			final_path = dst_path
			dst_base = os.path.splitext(dst_path)[0]
			if real_ext and not dst_path.lower().endswith(real_ext):
				final_path = dst_base + real_ext
				# Ak existuje starý .png so zlým obsahom, zmaž ho
				if final_path != dst_path:
					try:
						if os.path.exists(dst_path):
							os.remove(dst_path)
					except Exception:
						pass

			tmp = final_path + '.tmp'
			try:
				with open(tmp, 'wb') as f:
					f.write(raw)
				try:
					if os.path.exists(final_path):
						os.remove(final_path)
				except Exception:
					pass
				os.rename(tmp, final_path)
				return final_path
			except Exception as e:
				last_err = e
				try:
					os.remove(tmp)
				except Exception:
					pass
				continue

		# FIX 0.48b: ak všetky varianty vrátili 404, zapíš do negatívnej cache
		# aby sme to ďalšiu hodinu neopakovali. Tým sa rieši log spam pri
		# kanáloch ktorých icon_public_url je trvalo broken na TVH strane.
		if got_404_on_all_candidates:
			_picon_mark_404(icon_public_url)

		raise AddonErrorException("Image download failed: %s" % (str(last_err) if last_err else "unknown"))
