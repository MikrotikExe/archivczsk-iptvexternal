# -*- coding: utf-8 -*-
"""
ActionsMixin — pouzivatelske akcie a servisne triggery Tvheadend pluginu.

Vyclenene z provider.py (refaktor 0.80.0, krok 5). Obsahuje rucne akcie
z menu Nastavenia (obnova bouquetov, picony, invalidacia cache, cistenie
404 cache picon) a interne triggery (_maybe_auto_refresh_bouquet,
_maybe_trigger_exports) volane pri login-e.

Stav (_*_STAMP) su read-only cesty k stamp suborom z _common; metody ich
iba citaju / zapisuju na disk (os.path.getmtime / open / os.utime), nerebinduju.
Background vlakna (_bg_*) a picon 404 helpery su nested/lokalne importy
a cestuju s metodami.

Bez zmeny spravania — iba presun metod.

Kompatibilita: Python 2.7 + Python 3.x
"""

from __future__ import absolute_import, unicode_literals, print_function

import os
import time

from ._common import _BOUQUET_REFRESH_STAMP, _EXPORT_TRIGGER_STAMP, _EXPORT_TRIGGER_TTL_SEC
from .classifier import _invalidate_classify_cache

# Mirror provider: bouquet generator je volitelny (graceful degradacia
# ak sa .bouquet modul nepodari naimportovat). Akcne metody straz cez
# `... and TvheadendBouquetXmlEpgGenerator is not None`.
try:
	from .bouquet import TvheadendBouquetXmlEpgGenerator
except Exception:
	TvheadendBouquetXmlEpgGenerator = None


class ActionsMixin(object):
	def _maybe_auto_refresh_bouquet(self):
		"""Automaticky refreshne bouquet ak uplynul nastavený interval.

		FIX 0.48: stamp sa zapíše AŽ po úspešnom spustení refreshu.
		Predtým sa zapísal pred volaním, takže pri zlyhaní (TVH dočasne
		nedostupný) sa nasledujúci pokus odložil o celý interval —
		bouquet ostal "navždy" zastaraný. Teraz pri zlyhaní zapíšeme
		"retry stamp" s krátkym intervalom (5 minút), takže auto-retry
		zbehne čoskoro keď sa TVH vráti.
		"""
		if self._bouquet_gen is None:
			return
		try:
			interval = int(self.get_setting('bouquet_refresh_interval') or 0)
			if interval <= 0:
				return
			now = int(time.time())
			last = 0
			try:
				last = int(os.path.getmtime(_BOUQUET_REFRESH_STAMP))
			except Exception:
				pass
			# Pri retry stamp-e (mtime v budúcnosti vďaka touch trickom by sme
			# museli komplikovať — držíme to jednoducho: posledný mtime + interval).
			if last and (now - last) < interval:
				return

			# Skontroluj TVH PRED volaním refreshu — keď je TVH down,
			# zmazaj len logically a skús o 5 minút.
			if not self._check_tvh_silent():
				# nastav stamp tak, aby ďalší pokus bol o 5 min
				retry_at = now - interval + 300
				try:
					os.utime(_BOUQUET_REFRESH_STAMP, (retry_at, retry_at))
				except Exception:
					try:
						with open(_BOUQUET_REFRESH_STAMP, 'w') as f:
							f.write(str(retry_at))
					except Exception:
						pass
				return

			# Spusti refresh — task beží na pozadí
			# FIX 0.70.2 (Juraj): bouquet_settings_changed reťazí refresh_bouquet
			# → refresh_xmlepg, takže cyklický auto-refresh teraz obnoví aj EPG
			# (nielen kanály). Predtým volaný refresh_userbouquet_start()
			# neexistuje v base triede (padal ticho) a EPG nereťazil.
			try:
				self._bouquet_gen.bouquet_settings_changed('interval_trigger', None)
				# úspešne naplánované → stamp je teraz
				try:
					with open(_BOUQUET_REFRESH_STAMP, 'w') as f:
						f.write(str(now))
				except Exception:
					pass
			except Exception as e:
				try:
					self.log_info('[plugin.tvheadend]auto-refresh bouquet failed: %s' % e)
				except Exception:
					pass
				# retry za 5 min
				retry_at = now - interval + 300
				try:
					with open(_BOUQUET_REFRESH_STAMP, 'w') as f:
						f.write(str(retry_at))
					os.utime(_BOUQUET_REFRESH_STAMP, (retry_at, retry_at))
				except Exception:
					pass
		except Exception:
			pass

	# FIX 0.58.2: `_maybe_auto_inject_epg` method (~85 LoC) odstránená.
	# Framework `BouquetXmlEpgGenerator` automaticky volá `refresh_xmlepg()`
	# každé 4 hodiny v internom bgservice loope + pri každom settings
	# change. Custom debouncing cez `_EPG_INJECT_STAMP` už nepotrebujeme.


	def _maybe_trigger_exports(self, silent=False):
		"""
		Spustí refresh bouquet + EPG na pozadí.
		Pri silent login-e (HTTP handler) sa spustí max raz za _EXPORT_TRIGGER_TTL_SEC.
		"""
		if self._bouquet_gen is None:
			return

		if silent:
			try:
				now  = int(time.time())
				last = 0
				try:
					last = int(os.path.getmtime(_EXPORT_TRIGGER_STAMP))
				except Exception:
					pass
				if last and (now - last) < int(_EXPORT_TRIGGER_TTL_SEC):
					return
				# Zapíš stamp pred štartom – ochrana proti burst requestom
				try:
					with open(_EXPORT_TRIGGER_STAMP, 'w') as f:
						f.write(str(now))
				except Exception:
					return
			except Exception:
				return

		# *_start() len naplánujú tasky – neblokujú GUI
		# FIX 0.70.1 (Juraj): predtým sa volalo refresh_userbouquet_start()
		# (tá metóda v base BouquetXmlEpgGenerator NEEXISTUJE → padala ticho
		# do except) + refresh_xmlepg_start() samostatne. Výsledok: EPG sa
		# často nenaplánoval (zdieľaný bgservice názov tasku + bouquet_refresh_running
		# flag). Správna framework cesta je bouquet_settings_changed(), ktorá
		# spoľahlivo reťazí refresh_bouquet → (callback) → refresh_xmlepg.
		try:
			self._bouquet_gen.bouquet_settings_changed('manual_trigger', None)
		except Exception:
			# fallback na pôvodné volania ak by sa API frameworku zmenilo
			try:
				self._bouquet_gen.refresh_userbouquet_start()
			except Exception:
				pass
			try:
				self._bouquet_gen.refresh_xmlepg_start(force=True)
			except Exception:
				pass

	# ------------------------------------------------------------------
	# Pomocné
	# ------------------------------------------------------------------


	def action_tvh_bouquet_refresh(self):
		"""Manuálne spustí PLNÝ TVH bouquet + XML EPG refresh.

		FIX 0.59.1 (audit, Juraj): volá override-nutý `refresh_bouquet()`
		namiesto `refresh_userbouquet_start()` + invaliduje bouquet cache.

		FIX 0.59.3 (audit, Juraj): plný refresh ako u piconov — pred
		generovaním zmaže channel cache + staré userbouquet súbory, aby sa
		zoznam kanálov natiahol čerstvý zo servera a bouquet sa vygeneroval
		od nuly (nie len prepísal). Predtým keď sa kanály na TVH serveri
		zmenili (pribudol/ubudol kanál, zmena poradia), refresh mohol nechať
		staré dáta kvôli channel cache alebo checksum heuristike. Teraz:
		  1. invalidate channel cache (fresh fetch zo servera)
		  2. zmaž existujúce TVH userbouquet súbory (.tv + .radio)
		  3. vyčisti bouquet cache + reset _channels v generatori
		  4. vygeneruj nanovo cez refresh_bouquet (vrátane download_picons)
		Beží na pozadí aby UI nezamrzlo.
		"""
		if not self._check_tvh_silent():
			self.add_dir(self._("✗ TVH login failed - check settings"),
			             cmd=self.settings_menu)
			return
		if self._bouquet_gen is None:
			self.add_dir(self._("✗ Bouquet generator not initialised"),
			             cmd=self.settings_menu)
			return
		try:
			import threading as _threading

			def _bg_full_refresh():
				try:
					# 1) Zmaž TVH channel cache — fresh fetch zo servera
					try:
						self.tvh.invalidate_channels_cache()
					except Exception:
						pass
					try:
						_invalidate_classify_cache()
					except Exception:
						pass

					# 2) Zmaž existujúce TVH userbouquet súbory aby sa
					#    vygenerovali od nuly (nie prepísali). Framework
					#    si ich vytvorí znova v refresh_bouquet.
					base = "/etc/enigma2"
					for fn in ("userbouquet.tvheadend_tv.tv",
					           "userbouquet.tvheadend_radio.radio",
					           "userbouquet.tvheadend_radio.tv"):
						p = os.path.join(base, fn)
						try:
							if os.path.isfile(p):
								os.remove(p)
								self.log_info('[Tvheadend.bouquet] full_refresh: '
								              'removed %s' % fn)
						except Exception as _e:
							self.log_error('[Tvheadend.bouquet] full_refresh: '
							               'cannot remove %s: %s' % (fn, _e))

					# 3) Vyčisti bouquet cache + stamp + reset _channels
					try:
						if os.path.exists(_BOUQUET_REFRESH_STAMP):
							os.remove(_BOUQUET_REFRESH_STAMP)
					except Exception:
						pass
					try:
						self.save_cached_data('bouquet', {})
					except Exception:
						pass
					# Force fresh načítanie kanálov zo servera. load_channel_list
					# je teraz atomické (FIX 0.59.7) — buduje lokálne a priradí
					# naraz, takže nehrozí race s prázdnym _channels.
					try:
						self._bouquet_gen.load_channel_list()
					except Exception:
						pass

					# 4) Vygeneruj nanovo (vrátane download_picons)
					self._bouquet_gen.refresh_bouquet()

					# 5) FIX 0.70.1 (Juraj): manuálny full refresh predtým NEvolal
					# EPG injekciu — generoval len kanály/picony, takže userbouquet
					# zostal bez EPG. Automatický framework reťazí refresh_xmlepg
					# po refresh_bouquet cez callback, ale táto ručná cesta ho
					# obchádza. Doplnené explicitné volanie (force=True, lebo
					# checksum sa nemusel zmeniť).
					try:
						self._bouquet_gen.refresh_xmlepg(force=True)
						self.log_info('[Tvheadend.bouquet] full_refresh: XML EPG injekcia dokončená')
					except Exception as _epg_e:
						self.log_error('[Tvheadend.bouquet] full_refresh: EPG injekcia zlyhala: %s' % _epg_e)

					# Zapíš nový stamp
					try:
						with open(_BOUQUET_REFRESH_STAMP, 'w') as f:
							f.write(str(int(time.time())))
					except Exception:
						pass

					self.log_info('[Tvheadend.bouquet] full_refresh: done')
				except Exception as _e:
					try:
						self.log_error('[Tvheadend.bouquet] manual full_refresh failed: %s' % _e)
					except Exception:
						pass

			_t = _threading.Thread(target=_bg_full_refresh, name='TVHBouquetFullRefresh')
			_t.daemon = True
			_t.start()

			self.add_dir(self._("✓ Full TVH bouquet refresh started in background "
			                    "(fresh channels + regenerate)"),
			             cmd=self.settings_menu)
			self.add_dir(self._("Progress: see /tmp/archivCZSK.log "
			                    "(filter '[Tvheadend.bouquet')"),
			             cmd=self.settings_menu)
		except Exception as e:
			self.add_dir(self._("✗ Error: ") + str(e),
			             cmd=self.settings_menu)
		self.add_dir(self._("« Back"), cmd=self.settings_menu)


	def action_tvh_picons(self):
		"""Manuálne spustí stiahnutie TVH piconov.

		FIX 0.57.0 (skyjet PR #22 review #11-#14): pre 0.57.0 picon flow
		má dva paralelné kanály:
		  1) /tmp/ cache pre internal menu rendering cez make_icon_url()
		     — sťahuje TVH imagecache do /tmp/ cez init_picons_async().
		  2) /usr/share/enigma2/picon/ pre E2 skin-y cez framework
		     BouquetGeneratorTemplate.download_picons() — volá sa
		     z BouquetXmlEpgGenerator.run() pri bouquet refresh-e.

		Manuálna akcia spustí oboje — (1) priamo cez init_picons_async,
		(2) force bouquet refresh ktorý framework picon download triggerne
		v background thread.

		FIX 0.59.1 (audit, Juraj): kanál (2) predtým volal
		`refresh_userbouquet_start()` + `save_cached_data('bouquet', {})`,
		ale to nespoľahlivo spúšťalo framework `download_picons`. Ak boli
		picon súbory zmazané z /usr/share/enigma2/picon/ ale channel
		checksum sa nezmenil, framework `refresh_bouquet` preskočil
		generovanie (a teda aj download_picons), takže Enigma2 picon dir
		zostal prázdny. (Reprodukované: zmazanie /usr/share/enigma2/picon/
		→ akcia "Stiahnuť TVH picony" → "Nothing to download" → picony
		sa neobjavili, kým sa nespravil VYP/ZAP auto-generovania.)
		Oprava: invaliduje sa cache + volá sa priamo override-nutý
		`refresh_bouquet()` (nie `refresh_userbouquet_start`), ktorý
		spoľahlivo prejde generovaním vrátane download_picons stage.
		Tým sa zachová správne SRP-based pomenovanie piconov (rovnaký
		kód ako pri auto-generovaní), takže picony vždy sedia s userbouquet.
		"""
		if not self._check_tvh_silent():
			self.add_dir(self._("✗ TVH login failed - check settings"),
			             cmd=self.settings_menu)
			return
		try:
			# (1) /tmp/ cache update pre menu rendering
			self.tvh.init_picons_async()

			# (2) Framework picon download — len ak enable_picons=true
			if self._bouquet_gen is None and TvheadendBouquetXmlEpgGenerator is not None:
				try:
					self._bouquet_gen = TvheadendBouquetXmlEpgGenerator(self)
				except Exception:
					self._bouquet_gen = None
			if (self._bouquet_gen is not None
			    and self._bouquet_gen.get_setting('enable_picons')):
				try:
					# Invaliduj bouquet cache aby framework refresh_bouquet
					# vždy reálne prebehol (vrátane download_picons stage),
					# aj keď sa channel checksum nezmenil. Bez tohto framework
					# SKIP-ne generovanie ak cks.get(channel_type) == checksum.
					try:
						self.log_debug('[Tvheadend.debug] invalidating bouquet cache to force regenerate')
						self.save_cached_data('bouquet', {})
					except Exception as _e:
						self.log_debug('[Tvheadend.debug] cache invalidation failed: %s' % _e)

					# FIX 0.59.1: volaj priamo refresh_bouquet() (náš override),
					# nie refresh_userbouquet_start(). refresh_bouquet je tá
					# framework metóda ktorá generuje userbouquet + spúšťa
					# download_picons keď enable_picons=True. Spustíme ju v
					# background thread aby UI nezamrzlo.
					import threading as _threading

					def _bg_refresh():
						try:
							self._bouquet_gen.refresh_bouquet()
						except Exception as _e:
							try:
								self.log_error('[Tvheadend.picons] manual refresh_bouquet failed: %s' % _e)
							except Exception:
								pass

					_t = _threading.Thread(target=_bg_refresh)
					_t.daemon = True
					_t.start()

					self.add_dir(self._("✓ Bouquet refresh + TVH picon download started in background"),
					             cmd=self.settings_menu)
				except Exception as e:
					self.add_dir(self._("✓ TVH picon /tmp cache update started "
					                    "(framework download skipped: %s)") % str(e),
					             cmd=self.settings_menu)
			else:
				self.add_dir(self._("✓ TVH picon /tmp cache update started "
				                    "(framework download disabled — enable 'Automatically "
				                    "download picons' in Userbouquet settings)"),
				             cmd=self.settings_menu)
			# FIX 0.48j: logy idú cez print() do /tmp/archivCZSK.log,
			# nie do vlastného súboru
			self.add_dir(self._("Progress: see /tmp/archivCZSK.log "
			                    "(filter '[plugin.tvheadend')"),
			             cmd=self.settings_menu)
		except Exception as e:
			self.add_dir(self._("✗ Error: ") + str(e),
			             cmd=self.settings_menu)
		self.add_dir(self._("« Back"), cmd=self.settings_menu)

	# FIX 0.58.2: `action_tvh_inject_epg` (manual EPG injection) odstránená
	# — framework `BouquetXmlEpgGenerator` triggeruje EPG inject po každom
	# bouquet refresh, takže `action_tvh_bouquet_refresh` robí oboje.


	def action_tvh_invalidate_cache(self):
		"""Zmaže TVH channel cache - užitočné po pridaní/zmene kanálov v TVH."""
		try:
			self.tvh.invalidate_channels_cache()
			# FIX 0.49: zruš aj DVR klasifikačnú cache (jej obsah by inak
			# 60s ostal stale aj keď kanály sa zmenili)
			try:
				_invalidate_classify_cache()
			except Exception:
				pass
			self.add_dir(self._("✓ TVH channel cache cleared"),
			             cmd=self.settings_menu)
			self.add_dir(self._("Next channel listing will fetch fresh data"),
			             cmd=self.settings_menu)
		except Exception as e:
			self.add_dir(self._("✗ Error: ") + str(e),
			             cmd=self.settings_menu)
		self.add_dir(self._("« Back"), cmd=self.settings_menu)


	def action_clear_picon_404_cache(self):
		"""FIX 0.48b: Vyčistí 404 negatívnu cache pre picony.

		Použiteľné keď užívateľ v TVH webUI opraví broken kanál icony
		a chce ich teraz znova stiahnuť bez čakania na 1h auto-expire.
		"""
		try:
			from .tvheadend import _picon_404_clear, _picon_404_count
			before = _picon_404_count()
			_picon_404_clear()
			self.add_dir(self._("✓ 404 picon cache cleared (was: %d entries)")
			             % before, cmd=self.settings_menu)
			self.add_dir(self._("Next picon refresh will retry all broken icons"),
			             cmd=self.settings_menu)
			# Hneď spusti retry na pozadí
			try:
				self.tvh.init_picons_async()
				self.add_dir(self._("✓ Picon download retry triggered in background"),
				             cmd=self.settings_menu)
			except Exception:
				pass
		except Exception as e:
			self.add_dir(self._("✗ Error: ") + str(e),
			             cmd=self.settings_menu)
		self.add_dir(self._("« Back"), cmd=self.settings_menu)


	def action_tvh_picons_full_refresh(self):
		"""FIX 0.59.2 (audit, Juraj): Plný refresh piconov — zmaže IBA picony
		patriace TVH kanálom (podľa service refs z TVH userbouquetu) a stiahne
		ich nanovo.

		Rozdiel oproti "Stiahnuť TVH picony": tá akcia skip-uje existujúce
		súbory, takže keď sa picon na serveri zmení (nové logo kanálu),
		nestiahne novú verziu. Tento full refresh najprv zmaže staré súbory,
		čím donúti stiahnuť aktuálne verzie zo servera.

		BEZPEČNOSŤ: maže iba picony ktorých service ref je v TVH userbouquete
		(userbouquet.tvheadend_tv.tv + .radio). Picony iných pluginov (M3U,
		satelitné, atď.) v /usr/share/enigma2/picon/ ostávajú nedotknuté.
		"""
		if not self._check_tvh_silent():
			self.add_dir(self._("✗ TVH login failed - check settings"),
			             cmd=self.settings_menu)
			return
		try:
			import threading as _threading

			def _bg_full_refresh():
				deleted = 0
				try:
					# 1) Pozbieraj service refs z TVH userbouquetov
					srefs = set()
					base = "/etc/enigma2"
					for fn in ("userbouquet.tvheadend_tv.tv",
					           "userbouquet.tvheadend_radio.radio",
					           "userbouquet.tvheadend_radio.tv"):
						path = os.path.join(base, fn)
						if not os.path.isfile(path):
							continue
						try:
							with open(path, 'r') as f:
								for line in f:
									# #SERVICE 1:0:1:...:0:0:0:URL:NAME
									if not line.startswith('#SERVICE'):
										continue
									parts = line.split(':')
									if len(parts) < 11:
										continue
									# service ref = prvých 10 polí, picon meno
									# je tých 10 polí spojených '_' (bez trailing)
									ref10 = parts[1:11]
									# Enigma2 picon meno: polia 1-10 spojené '_',
									# uppercase hex, s trailing '_0_0_0' formátom.
									# FIX 0.59.2: pridaj DVE varianty do mazacieho setu:
									#  a) normalizovaný type=1 (nové správne meno)
									#  b) pôvodný player_id type (staré zlé meno
									#     5002_/4097_/... z verzií pred normalizáciou)
									# Tým full refresh zmaže aj legacy nesprávne
									# pomenované picony.
									ref10 = [p.strip() for p in ref10]
									orig = '_'.join(ref10)
									srefs.add(orig.upper())
									if ref10 and ref10[0] != '1':
										norm = ref10[:]
										norm[0] = '1'
										srefs.add('_'.join(norm).upper())
						except Exception as _e:
							self.log_error('[Tvheadend.picons] full_refresh: '
							               'cannot read %s: %s' % (fn, _e))

					# 2) Zmaž zodpovedajúce picon súbory z Enigma2 picon dir
					picon_dirs = ['/usr/share/enigma2/picon',
					              '/media/hdd/picon',
					              '/media/usb/picon']
					for pdir in picon_dirs:
						if not os.path.isdir(pdir):
							continue
						try:
							for pf in os.listdir(pdir):
								if not pf.lower().endswith('.png'):
									continue
								# picon meno bez .png, uppercase
								base_name = pf[:-4].upper()
								if base_name in srefs:
									try:
										os.remove(os.path.join(pdir, pf))
										deleted += 1
									except Exception:
										pass
						except Exception as _e:
							self.log_error('[Tvheadend.picons] full_refresh: '
							               'cannot scan %s: %s' % (pdir, _e))

					# 3) Zmaž internú /tmp cache (imagecache_*)
					try:
						cache_dir = self.tvh._img_cache_dir
						if os.path.isdir(cache_dir):
							for cf in os.listdir(cache_dir):
								if cf.startswith('imagecache_'):
									try:
										os.remove(os.path.join(cache_dir, cf))
									except Exception:
										pass
					except Exception:
						pass

					# 4) Vyčisti 404 cache (nech sa skúsia stiahnuť aj predtým
					#    zlyhané)
					try:
						from .tvheadend import _picon_404_clear
						_picon_404_clear()
					except Exception:
						pass

					self.log_info('[Tvheadend.picons] full_refresh: deleted %d '
					              'TVH picon files, cleared caches — re-downloading'
					              % deleted)

					# 5) Force bouquet refresh → framework download_picons
					#    stiahne všetko nanovo (skip-exists teraz nič nepreskočí
					#    lebo súbory sú zmazané)
					try:
						self.save_cached_data('bouquet', {})
					except Exception:
						pass
					if self._bouquet_gen is None and TvheadendBouquetXmlEpgGenerator is not None:
						try:
							self._bouquet_gen = TvheadendBouquetXmlEpgGenerator(self)
						except Exception:
							self._bouquet_gen = None
					if self._bouquet_gen is not None:
						self._bouquet_gen.refresh_bouquet()
					# Plus /tmp cache pre menu rendering
					try:
						self.tvh.init_picons_async()
					except Exception:
						pass

					self.log_info('[Tvheadend.picons] full_refresh: done')
				except Exception as _e:
					try:
						self.log_error('[Tvheadend.picons] full_refresh failed: %s' % _e)
					except Exception:
						pass

			_t = _threading.Thread(target=_bg_full_refresh, name='TVHPiconFullRefresh')
			_t.daemon = True
			_t.start()

			self.add_dir(self._("✓ Full picon refresh started in background "
			                    "(deleting old TVH picons + re-downloading)"),
			             cmd=self.settings_menu)
			self.add_dir(self._("Progress: see /tmp/archivCZSK.log "
			                    "(filter '[Tvheadend.picons')"),
			             cmd=self.settings_menu)
		except Exception as e:
			self.add_dir(self._("✗ Error: ") + str(e),
			             cmd=self.settings_menu)
		self.add_dir(self._("« Back"), cmd=self.settings_menu)
