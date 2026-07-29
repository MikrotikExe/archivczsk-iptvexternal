# -*- coding: utf-8 -*-
#
# Katalógové API OKTAGON.tv (api.oktagonmma.com) - overené z HAR.
# Je verejné (bez prihlásenia). Vracia položky typu:
#   STREAM = živý prenos / event, VIDEO = záznam/VOD, BUNDLE / PASS = balíčky/predplatné.
# Každá položka nesie videoSourceType="TIVIO" a videoSource=<Tivio video id>, ktoré sa
# potom prehráva cez Tivio getSourceUrl (viď oktagontv.py).
#
# POZOR: v HAR bola odchytená len domovská stránka -> endpoint /banners. Pre plnú navigáciu
# (Turnaje / Zápasy / Pořady, detail eventu, zoznam zápasov) treba odchytiť ďalšie endpointy
# (miesta označené TODO(oktagon-nav)).

LANGS = ['sk', 'cs', 'en']


class OktagonApi(object):
	BASE = "https://api.oktagonmma.com/v1"

	def __init__(self, content_provider):
		self.cp = content_provider
		self.req_session = self.cp.get_requests_session()

	# ##################################################################################################################

	def _get(self, path, params=None):
		resp = self.req_session.get(self.BASE + path, params=params, headers={'Referer': 'https://oktagon.tv/'})
		resp.raise_for_status()
		return resp.json()

	# ##################################################################################################################

	@staticmethod
	def lang_label(item):
		if isinstance(item, dict):
			for l in LANGS:
				if item.get(l):
					return item[l]
			# fallback - prvá dostupná hodnota
			for v in item.values():
				if v:
					return v
			return ""
		return item or ""

	# ##################################################################################################################

	def _img(self, item):
		# preferuj detail/thumbnail/banner obrázok; url je dict podľa jazyka
		for key in ('detailImage', 'image', 'thumbnailImage', 'mobileImage'):
			node = item.get(key)
			if node and isinstance(node.get('url'), dict):
				return self.lang_label(node['url'])
		return None

	# ##################################################################################################################

	def _parse_item(self, it):
		return {
			'id': it.get('id'),
			'slug': it.get('slug'),
			'event_id': it.get('eventId'),
			'event_tag_id': it.get('eventTagId'),
			'type': it.get('type'),                       # STREAM | VIDEO | BUNDLE | PASS
			'video_source_type': it.get('videoSourceType'),  # 'TIVIO'
			'video_source': it.get('videoSource'),        # Tivio video id -> getSourceUrl
			'start_date': it.get('startDate'),
			'title': self.lang_label(it.get('title', {})),
			'subtitle': self.lang_label(it.get('subTitle', {})),
			'plot': self.lang_label(it.get('descriptionShort') or it.get('descriptionLong') or {}),
			'img': self._img(it),
		}

	# ##################################################################################################################

	def get_banners(self, types=None, promoted=None, lang='sk'):
		# types: zoznam z ['STREAM','VIDEO','BUNDLE']; promoted: True/False/None
		params = []
		for i, t in enumerate(types or []):
			params.append(('types[%d]' % i, t))
		if promoted is not None:
			params.append(('promoted', 'true' if promoted else 'false'))
		params.append(('onlyLanguages[0]', lang))
		params.append(('sort[0][key]', 'startDate'))
		params.append(('sort[0][direction]', 'desc'))

		data = self._get('/banners', params=params)
		return [self._parse_item(x) for x in data]

	# ##################################################################################################################

	def get_banner(self, slug):
		return self._parse_item(self._get('/banners/' + slug))

	# ##################################################################################################################
	# TODO(oktagon-nav): doplniť endpointy pre plnú navigáciu po odchytení druhého HAR:
	#   - zoznam všetkých turnajov/eventov (Turnaje)
	#   - zoznam zápasov v evente (Zápasy) - pravdepodobne cez eventId / eventTagId
	#   - relácie/pořady (Pořady)
	#   - vyhľadávanie
