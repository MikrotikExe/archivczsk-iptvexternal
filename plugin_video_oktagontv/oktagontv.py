# -*- coding: utf-8 -*-
#
# OKTAGON.tv klient pre archivczsk.
#
# OKTAGON.tv beží na platforme Tivio / Streamonline - rovnako ako doplnok JOJ Play
# (plugin_video_jojplay), z ktorého tento kód vychádza. Logika komunikácie s Tivio
# backendom (Firebase auth + Firestore + Tivio cloud functions + Algolia search) je
# spoločná; líšia sa len identifikátory organizácie/aplikácie a niektoré kľúče.
#
# Všetky identifikátory (API kľúče, organization/tenant ID) sú overené z odchytenej
# komunikácie webu oktagon.tv (HAR záznamy z 28.7. a 1.8.2026 - viď komentáre pri konštantách).
#
# Kód je písaný tak, aby bežal na Pythone 2 aj 3 (staršie Enigma2 image).

from tools_archivczsk.contentprovider.exception import LoginException, AddonErrorException
from tools_archivczsk.debug.http import dump_json_request
from tools_archivczsk.string_utils import int_to_roman
from time import time
import json
import os
import sys

DUMP_API_REQUESTS = False

# ##################################################################################################################

def to_text(msg):
	"""
	Prevedie hodnotu na textový reťazec bezpečne pre Python 2 aj 3.
	Na py2 sa bytes (napr. preložený text z _()) dekódujú z utf-8, aby sa dali
	spájať s unicode reťazcami bez UnicodeDecodeError.
	"""
	if sys.version_info[0] == 2:
		try:
			if isinstance(msg, str):
				return msg.decode('utf-8', 'ignore')
			return unicode(msg)  # noqa: F821 - existuje len na py2
		except Exception:
			return u''

	try:
		if isinstance(msg, bytes):
			return msg.decode('utf-8', 'ignore')
		return str(msg)
	except Exception:
		return ''

# ##################################################################################################################

def error_text(e):
	"""
	Vráti text výnimky bezpečne pre Python 2 aj 3.
	Na py2 by str() nad hláškou s diakritikou (napr. preloženou) spadol na UnicodeEncodeError.
	"""
	msg = None
	try:
		if getattr(e, 'args', None):
			msg = e.args[0]
	except Exception:
		msg = None

	if msg is None:
		msg = e

	return to_text(msg)

# ##################################################################################################################

def safe_url(url):
	"""
	Vráti URL bez query stringu - do logu nepatrí sessionId ani pairingId.
	"""
	if not url:
		return url

	try:
		return url.split('?', 1)[0]
	except Exception:
		return url

# ##################################################################################################################

def norm_text(text):
	"""
	Normalizuje text pre vyhľadávanie - malé písmená bez diakritiky.
	Funguje na Pythone 2 aj 3 (na py2 treba pracovať s unicode, nie s bytes).
	"""
	if not text:
		return ''

	if sys.version_info[0] == 2:
		string_type = unicode  # noqa: F821 - existuje len na py2
	else:
		string_type = str

	if not isinstance(text, string_type):
		try:
			text = text.decode('utf-8')
		except Exception:
			try:
				return text.lower()
			except Exception:
				return ''

	try:
		import unicodedata
		text = unicodedata.normalize('NFKD', text)
		text = u''.join([c for c in text if not unicodedata.combining(c)])
	except Exception:
		pass

	return text.lower()

# ##################################################################################################################
# Pomocník na rozbalenie Firestore REST odpovede (typované hodnoty) do bežného dictu.
# Toto je čisto formát Firestore - je zhodné naprieč všetkými Tivio projektami, nemeň.
# ##################################################################################################################

class FirestoreJsonProcessor(object):
	def __init__(self, documents):
		self.documents = documents

	def parse_value(self, value):
		if type(value) == list:
			ret = []
			for i in value:
				ret.append(self.parse_value(i))
			return ret

		value_type = list(value.keys())[0]

		if value_type == 'geoPointValue':
			return (value['geoPointValue']['latitude'], value['geoPointValue']['longitude'],)
		elif value_type == 'arrayValue':
			if value['arrayValue'].get('values') == None:
				return []
			else:
				return self.parse_value(value['arrayValue']['values'])
		elif value_type == 'mapValue':
			if value['mapValue'].get('fields') == None:
				return {}
			else:
				return self.parse_fields(value['mapValue']['fields'])
		elif value_type == 'integerValue':
			return int(value['integerValue'])
		elif value_type == 'doubleValue':
			return float(value['doubleValue'])
		else:
			return value[value_type]

	def parse_fields(self, fields):
		res = {}
		for key, value in fields.items():
			res[key] = self.parse_value(value)
		return res

	def run(self):
		unpack = False
		if not isinstance(self.documents, list):
			self.documents = [self.documents]
			unpack = True

		ret = []
		for x in self.documents:
			if 'fields' not in x and 'document' in x:
				x = x['document']

			if 'fields' in x:
				d = self.parse_fields(x['fields'])
				d['__name'] = x.get('name')
				ret.append(d)

		return ret[0] if unpack else ret

# ##################################################################################################################
# Nízkoúrovňový klient - komunikácia s backendom, vracia čiastočne spracované dáta.
# ##################################################################################################################

class OktagonTVClient(object):
	# ---- Overené z HAR záznamu (28.7.2026) ----
	# OKTAGON má vlastný Firebase projekt "oktagonprod" (prihlásenie účtu). Cez Tivio cloud
	# funkciu signInWithTenant sa účet premostí do Tivio (projekt "tivio-production"), odkiaľ
	# získame idToken používaný pre Firestore aj getSourceUrl (prehrávanie).

	# Firebase Web API key OKTAGON účtu (projekt oktagonprod):
	OKTAGON_APP_KEY = "AIzaSyDTDAMftECKq34nQn0F_6fGWIXui-SSl24"
	# Firebase Web API key Tivio (projekt tivio-production):
	TIVIO_APP_KEY = "AIzaSyB02udgMkNLADkLJ_w5YNBMR2VR1WHfusI"

	# Tivio organization ID OKTAGONu (z Firestore ciest organizations/<ID>):
	ORGANIZATION_ID = "ZA6ZOtuDD90uHsRkXNyj"

	# Tivio Firebase tenant (informatívne; premostenie rieši signInWithTenant serverovo):
	TENANT_ID = "XA6ZOtuDD90uHsRkXNyj-e2xvz"

	# Odvodené cesty (nemeň - postavené z ORGANIZATION_ID)
	ORG_PATH = "/organizations/" + ORGANIZATION_ID
	DOCUMENTS_ROOT = 'projects/tivio-production/databases/(default)/documents'
	ORG_ROOT = DOCUMENTS_ROOT + ORG_PATH
	TAGS_ROOT = ORG_ROOT + '/tags/'
	TAG_TYPES_ROOT = ORG_ROOT + '/tagTypes/'
	FIRESTORE_REST_URL = 'https://firestore.googleapis.com/v1/' + DOCUMENTS_ROOT

	# ---- Overené z HAR (28.7.2026, prechod Turnaje -> Zápasy -> Pořady -> Domov) ----
	# Typ tagu, ktorý web používa pre zoznam turnajov (OKTAGON 92, 93, ...):
	#   tags where tagTypeRef == organizations/<org>/tagTypes/<ID>, orderBy created DESC, limit 21
	EVENT_TAG_TYPE_ID = "JW171vftakPSYzzVl7LW"

	# Obrazovky webu (pole screenId v kolekcii organizations/<org>/screens):
	SCREEN_HOME = "screen-himhHzoaJZS4tEPP-5Fh-"     # https://oktagon.tv/sk/
	SCREEN_FIGHTS = "screen-UUal-pc3NL1T9fwTiupn9"   # https://oktagon.tv/sk/fights/ (Zápasy)

	# Riadok, z ktorého web skladá stránku Pořady (/sk/shows/):
	ROW_SHOWS = "row-CBNuWIfjpCKTMflELreLN"

	REFERER = "https://oktagon.tv/"

	# Jazyk, ktorý sa posiela do getSourceUrl (vyberá jazykovú mutáciu živého prenosu).
	LANGUAGE = "cs"

	# Tivio cloud funkcia na premostenie OKTAGON účtu do Tivio (vracia Firebase custom token):
	SIGN_IN_WITH_TENANT_URL = "https://europe-west3-tivio-production.cloudfunctions.net/signInWithTenant"

	def __init__(self, content_provider):
		self.cp = content_provider
		self.req_session = self.cp.get_requests_session()
		self.login_data = {}
		self.purchases = []
		self.user_info = {}
		self.favourites = {'video': {}, 'tag': {}}
		self.watch_positions = {}
		self.load_login_data()

		if DUMP_API_REQUESTS:
			self.req_session.request_orig = self.req_session.request
			def request_and_dump(*args, **kwargs):
				response = self.req_session.request_orig(*args, **kwargs)
				dump_json_request(response)
				return response
			self.req_session.request = request_and_dump

	# ##################################################################################################################

	def load_login_data(self):
		self.login_data = self.cp.load_cached_data('login')

	def save_login_data(self):
		self.cp.save_cached_data('login', self.login_data)

	# ##################################################################################################################

	def login(self):
		# 3-krokový login OKTAGON -> Tivio (overené z HAR):
		#  1) prihlásenie OKTAGON účtu (Firebase projekt oktagonprod) -> oktagon idToken + userId
		#  2) signInWithTenant (Tivio cloud funkcia) -> Firebase custom token pre Tivio
		#  3) verifyCustomToken (Firebase projekt tivio-production) -> Tivio idToken + refresh token
		self.cp.log_debug("Starting login procedure (OKTAGON -> Tivio)")

		username = self.cp.get_setting('username')
		password = self.cp.get_setting('password')

		if not username or not password:
			raise LoginException(self.cp._("No username or password provided"))

		headers = {'Referer': self.REFERER}

		# --- krok 1: OKTAGON účet (oktagonprod) ---
		response = self.req_session.post(
			"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
			params={'key': self.OKTAGON_APP_KEY},
			json={'email': username, 'password': password, 'returnSecureToken': True, 'clientType': 'CLIENT_TYPE_WEB'},
			headers=headers
		)
		try:
			resp_json = response.json()
		except:
			resp_json = {}
		try:
			response.raise_for_status()
		except:
			self.cp.log_error("OKTAGON login failed: %s" % resp_json.get('error', {}).get('message'))
			raise LoginException(self.cp._("Login failed. Probably wrong username/password combination."))

		oktagon_id_token = resp_json['idToken']
		user_id = resp_json['localId']

		# --- krok 2: premostenie do Tivio -> custom token ---
		response = self.req_session.post(
			self.SIGN_IN_WITH_TENANT_URL,
			json={'data': {'userId': user_id, 'token': oktagon_id_token, 'tenantId': self.TENANT_ID}},
			headers=headers
		)
		try:
			response.raise_for_status()
			custom_token = response.json()['result']
		except:
			self.cp.log_error("signInWithTenant failed: %s" % response.text[:200])
			raise LoginException(self.cp._("Login failed (Tivio bridge)."))

		# --- krok 3: custom token -> Tivio idToken ---
		response = self.req_session.post(
			"https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyCustomToken",
			params={'key': self.TIVIO_APP_KEY},
			json={'token': custom_token, 'returnSecureToken': True, 'tenantId': self.TENANT_ID},
			headers=headers
		)
		try:
			resp_json = response.json()
			response.raise_for_status()
		except:
			self.cp.log_error("verifyCustomToken failed: %s" % str(resp_json))
			raise LoginException(self.cp._("Login failed (Tivio token)."))

		self.login_data['id_token'] = resp_json['idToken']
		self.login_data['refresh_token'] = resp_json['refreshToken']
		self.login_data['valid_to'] = int(time()) + int(resp_json['expiresIn'])
		self.login_data['local_id'] = user_id
		self.login_data['checksum'] = self.cp.get_settings_checksum(('username', 'password',))
		self.save_login_data()

	# ##################################################################################################################

	def refresh_id_token(self):
		# Obnovenie Tivio idTokenu cez securetoken (projekt tivio-production)
		self.cp.log_debug("Refreshing ID token")
		params = {
			'key': self.TIVIO_APP_KEY
		}
		data = {
			'grant_type': 'refresh_token',
			'refresh_token': self.login_data['refresh_token']
		}
		headers = {
			'Referer': self.REFERER
		}

		response = self.req_session.post("https://securetoken.googleapis.com/v1/token", params=params, json=data, headers=headers)

		try:
			resp_json = response.json()
		except:
			resp_json = {}

		try:
			response.raise_for_status()
		except:
			self.cp.log_error("Login refresh failed: %s" % str(resp_json))
			raise LoginException(self.cp._("Login refresh failed. Refresh token is not valid anymore."))

		self.login_data['id_token'] = resp_json['id_token']
		self.login_data['refresh_token'] = resp_json['refresh_token']
		self.login_data['valid_to'] = int(time()) + int(resp_json['expires_in'])
		self.save_login_data()

	# ##################################################################################################################

	def refresh_user_data(self):
		params = {
			'key': self.TIVIO_APP_KEY
		}
		data = {
			'idToken': self.login_data['id_token']
		}
		headers = {
			'Referer': self.REFERER
		}

		response = self.req_session.post("https://www.googleapis.com/identitytoolkit/v3/relyingparty/getAccountInfo", params=params, json=data, headers=headers)

		try:
			resp_json = response.json()
		except:
			resp_json = {}

		try:
			response.raise_for_status()
		except:
			self.user_info = {}
			self.cp.log_error("Login failed: %s" % resp_json.get('message'))
			raise LoginException(self.cp._("Failed to get user informations"))

		# tivioUserId je uložené v customAttributes používateľa (Firebase custom claims)
		user_id = json.loads(resp_json['users'][0]['customAttributes'])['tivioUserId']

		purchases = self.load_purchases(user_id)
		self.purchases = [p['monetizationRef'].split('/')[-1] for p in purchases]
		self.dump_json('purchases', self.purchases)
		self.user_info = self.load_document('/users/' + user_id)
		self.dump_json('user-info', self.user_info)

		# obľúbené
		self.favourites = {'video': {}, 'tag': {}}
		for f in self.user_info.get('favorites', []):
			if f.get('profileId') == self.login_data.get('profile_id'):
				item_type = f.get('contentRef', '/').split('/')[-2]
				if item_type == 'videos':
					self.favourites['video'][f['contentRef'].split('/')[-1]] = True
				elif item_type == 'tags':
					self.favourites['tag'][f['contentRef'].split('/')[-1]] = True

		# pozície prehrávania
		self.watch_positions = {}
		for witem in self.user_info.get('watchHistory', []):
			if witem.get('videoRef') and witem.get('profileId') == self.login_data.get('profile_id'):
				position = witem.get('position', 0)
				if position > 0 and position < witem.get('videoDuration', 0):
					self.watch_positions[witem['videoRef'].split('/')[-1]] = position

	# ##################################################################################################################

	def reset_login_data(self):
		# zahodí tokeny, ale device_id ostáva - web ho posiela do getSourceUrl a má byť stabilné
		device_id = self.login_data.get('device_id')
		self.login_data = {}
		if device_id:
			self.login_data['device_id'] = device_id

	def refresh_login(self):
		# 60 s rezerva, aby token nevypršal medzi kontrolou a samotnou požiadavkou
		if self.login_data.get('valid_to', 0) - 60 < int(time()) and self.login_data.get('refresh_token'):
			self.cp.log_debug("Login refresh is needed")
			try:
				self.refresh_id_token()
			except:
				self.cp.log_debug("Failed to refresh ID token")
				self.reset_login_data()

		if self.cp.get_settings_checksum(('username', 'password',)) != self.login_data.get('checksum'):
			self.cp.log_debug("Login data changed - starting fresh login using name/password")
			self.reset_login_data()
			self.login()
			self.user_info = {}

		if not self.user_info:
			self.cp.log_debug("Refreshing user info")
			self.user_info = {'x': True}  # zabránenie rekurzii
			self.refresh_user_data()

			profiles = [x['id'] for x in self.user_info.get('profiles', [])]
			if len(profiles) > 0:
				if self.login_data.get('profile_id') not in profiles:
					self.login_data['profile_id'] = profiles[0]
					self.save_login_data()

	# ##################################################################################################################

	def call_firestore_api(self, query=None, path='', org_root=False):
		self.refresh_login()

		headers = {
			'Authorization': 'Bearer ' + self.login_data['id_token']
		}
		data = {
			"structuredQuery": query,
		}
		if org_root:
			path = self.ORG_PATH + path

		if query:
			response = self.req_session.post(self.FIRESTORE_REST_URL + path + ":runQuery", json=data, headers=headers)
		else:
			response = self.req_session.get(self.FIRESTORE_REST_URL + path, headers=headers)

		response.raise_for_status()

		self.dump_json('last-firestore-response', response.json())
		return FirestoreJsonProcessor(response.json()).run()

	# ##################################################################################################################

	def call_tivio_api(self, endpoint, data):
		self.refresh_login()

		headers = {
			'Authorization': 'Bearer ' + self.login_data['id_token']
		}

		response = self.req_session.post('https://europe-west3-tivio-production.cloudfunctions.net/' + endpoint, json={'data': data}, headers=headers)
		try:
			response.raise_for_status()
		except:
			err_msg = None
			try:
				err_msg = response.json().get('error', {}).get('details', {}).get('reason')
				if err_msg == 'MONETIZATION':
					err_msg = self.cp._("With your subscription you don't have access to this content.")
				else:
					err_msg = response.json().get('error', {}).get('message')
			except:
				pass

			if err_msg:
				raise AddonErrorException(err_msg)
			else:
				raise

		return response.json().get('result')

	# ##################################################################################################################

	def call_firestore_api_paged(self, query, org_root=False, max_pages=20):
		# Firestore runQuery vráti najviac "limit" dokumentov. Aby sa nestratili položky
		# nad limit (dlhý zoznam turnajov, zápasov v evente), stránkuje sa cez "offset"
		# nad tým istým orderBy, kým stránka nevráti menej ako "limit" dokumentov.
		page_size = int(query.get('limit') or 100)
		ret = []

		for page in range(max_pages):
			page_query = dict(query)
			page_query['limit'] = page_size
			page_query['offset'] = page * page_size

			items = self.call_firestore_api(page_query, org_root=org_root)
			ret.extend(items)

			if len(items) < page_size:
				break
		else:
			self.cp.log_debug("Firestore query reached max pages (%d x %d) - result may be truncated" % (max_pages, page_size))

		return ret

	# ##################################################################################################################

	def get_screen_rows(self, screen_id, offset=0, limit=30):
		data = {
			"organizationId": self.ORGANIZATION_ID,
			"screenId": screen_id,
			"offset": offset,
			"limit": limit,
			"initialTilesCount": 1,
			"isLockedApplicationOnStargazeHosting": False,
			"anonymousUserId": None
		}
		ret = self.call_tivio_api('getRowsInScreen3', data)
		self.dump_json('getRowsInScreen3-' + screen_id, ret)
		return ret

	# ##################################################################################################################

	def get_row_tiles(self, row_id, offset=0, limit=30):
		data = {
			'limit': limit,
			'offset': offset,
			"organizationId": self.ORGANIZATION_ID,
			'rowId': row_id
		}
		ret = self.call_tivio_api('getTilesInRow', data)
		self.dump_json('getTilesInRow-' + row_id, ret)
		return ret

	# ##################################################################################################################

	def load_videos_by_tag(self, tag_id, limit=100):
		# Zápasy v turnaji / epizódy relácie.
		# 1. pokus = presne ten dopyt, ktorý posiela web (overené z HAR /sk/fights):
		#    organizationRef + transcodingStatus + publishedStatus + tags ARRAY_CONTAINS_ANY,
		#    orderBy created DESC. Tento tvar má v Firestore zaručene vytvorený index.
		# Záložný dopyt sa použije LEN keď prvý zlyhá (chýbajúci index -> HTTP 400),
		# nie keď legitímne nič nevráti - prázdny turnaj je prázdny.
		tag_ref = self.TAGS_ROOT + tag_id

		web_query = {
			"from": [{"collectionId": "videos"}],
			"where": {
				"compositeFilter": {
					"op": "AND",
					"filters": [
						{"fieldFilter": {"field": {"fieldPath": "organizationRef"}, "op": "EQUAL", "value": {"referenceValue": self.ORG_ROOT}}},
						{"fieldFilter": {"field": {"fieldPath": "transcodingStatus"}, "op": "EQUAL", "value": {"stringValue": "ENCODING_DONE"}}},
						{"fieldFilter": {"field": {"fieldPath": "publishedStatus"}, "op": "EQUAL", "value": {"stringValue": "PUBLISHED"}}},
						{"fieldFilter": {"field": {"fieldPath": "tags"}, "op": "ARRAY_CONTAINS_ANY", "value": {"arrayValue": {"values": [{"referenceValue": tag_ref}]}}}}
					]
				}
			},
			"orderBy": [
				{"field": {"fieldPath": "created"}, "direction": "DESCENDING"},
				{"field": {"fieldPath": "__name__"}, "direction": "DESCENDING"}
			],
			"limit": limit
		}

		try:
			ret = self.call_firestore_api_paged(web_query)
			self.dump_json('videos-by-tag-' + tag_id, ret)
			return ret
		except LoginException:
			raise
		except Exception as e:
			self.cp.log_debug("load_videos_by_tag (web query) failed: %s" % error_text(e))

		# 2. pokus (fallback): jednoduchší dopyt bez orderBy created, ale stále len publikované videá
		fallback_query = {
			"from": [{"collectionId": "videos"}],
			"where": {
				"compositeFilter": {
					"op": "AND",
					"filters": [
						{"fieldFilter": {"field": {"fieldPath": "tags"}, "op": "ARRAY_CONTAINS", "value": {"referenceValue": tag_ref}}},
						{"fieldFilter": {"field": {"fieldPath": "publishedStatus"}, "op": "EQUAL", "value": {"stringValue": "PUBLISHED"}}}
					]
				}
			},
			"orderBy": [{"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"}],
			"limit": limit
		}

		try:
			ret = self.call_firestore_api_paged(fallback_query)
			self.dump_json('videos-by-tag-' + tag_id, ret)
			return ret
		except LoginException:
			raise
		except Exception as e:
			self.cp.log_error("load_videos_by_tag failed: %s" % error_text(e))
			return []

	# ##################################################################################################################

	def load_tags_by_tagtype(self, tag_type_id, limit=100):
		# Presne ten dopyt, ktorý posiela web na stránke /sk/tournaments (overené z HAR).
		query = {
			"from": [{"collectionId": "tags"}],
			"where": {
				"fieldFilter": {
					"field": {"fieldPath": "tagTypeRef"},
					"op": "EQUAL",
					"value": {"referenceValue": self.TAG_TYPES_ROOT + tag_type_id}
				}
			},
			"orderBy": [
				{"field": {"fieldPath": "created"}, "direction": "DESCENDING"},
				{"field": {"fieldPath": "__name__"}, "direction": "DESCENDING"}
			],
			"limit": limit
		}
		ret = self.call_firestore_api_paged(query, org_root=True)
		self.dump_json('tags-by-tagtype-' + tag_type_id, ret)
		return ret

	# ##################################################################################################################

	def load_latest_videos(self, limit=60):
		# Najnovšie publikované videá organizácie (zápasy, zostrihy, epizódy) - rovnaké
		# filtre ako pri dopyte webu, len bez obmedzenia na konkrétny tag.
		query = {
			"from": [{"collectionId": "videos"}],
			"where": {
				"compositeFilter": {
					"op": "AND",
					"filters": [
						{"fieldFilter": {"field": {"fieldPath": "organizationRef"}, "op": "EQUAL", "value": {"referenceValue": self.ORG_ROOT}}},
						{"fieldFilter": {"field": {"fieldPath": "transcodingStatus"}, "op": "EQUAL", "value": {"stringValue": "ENCODING_DONE"}}},
						{"fieldFilter": {"field": {"fieldPath": "publishedStatus"}, "op": "EQUAL", "value": {"stringValue": "PUBLISHED"}}}
					]
				}
			},
			"orderBy": [
				{"field": {"fieldPath": "created"}, "direction": "DESCENDING"},
				{"field": {"fieldPath": "__name__"}, "direction": "DESCENDING"}
			],
			"limit": limit
		}
		try:
			ret = self.call_firestore_api(query)
			self.dump_json('latest-videos', ret)
			return ret
		except LoginException:
			raise
		except Exception as e:
			# ak Firestore nemá pre túto kombináciu index, vráti 400 - nie je to fatálne
			self.cp.log_error("load_latest_videos failed: %s" % error_text(e))
			return []

	# ##################################################################################################################

	def load_org_tags(self, tag_type=None, limit=300):
		# Zoznam tagov organizácie. Typy zistené z reálnych dát OKTAGONu:
		#   show_tag (relácie), event_tag (turnaje), genre_tag, video_type_tag,
		#   organization_tag + veľa tagov bez typu (bojovníci a pod.)
		query = {
			"from": [{"collectionId": "tags"}],
			"orderBy": [{"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"}],
			"limit": limit
		}

		if tag_type:
			query["where"] = {
				"fieldFilter": {
					"field": {"fieldPath": "type"},
					"op": "EQUAL",
					"value": {"stringValue": tag_type}
				}
			}

		ret = self.call_firestore_api(query, org_root=True)
		self.dump_json('org-tags-' + (tag_type or 'all'), ret)
		return ret

	# ##################################################################################################################

	def load_document(self, document_path, org_root=False):
		ret = self.call_firestore_api(path=document_path, org_root=org_root)
		self.dump_json('document', ret)
		return ret

	# ##################################################################################################################

	def load_purchases(self, user_id):
		query = {
			"from": [{"collectionId": "purchases"}],
			"where": {"fieldFilter": {"field": {"fieldPath": "status"}, "op": "IN", "value": {"arrayValue": {"values": [{"stringValue": "PAID"}, {"stringValue": "CANCELLING"}]}}}},
			"orderBy": [{"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"}]
		}
		return self.call_firestore_api(query, '/users/' + user_id)

	# ##################################################################################################################

	def get_device_id(self):
		# Stabilný identifikátor zariadenia - web ho posiela do getSourceUrl.
		device_id = self.login_data.get('device_id')

		if not device_id:
			import uuid
			device_id = str(uuid.uuid4())
			self.login_data['device_id'] = device_id
			self.save_login_data()

		return device_id

	# poradie schopností rozhoduje, aký protokol Tivio vráti - prvý vyhovujúci vyhráva
	CAPABILITIES = [
		{"codec": "h264", "protocol": "dash", "encryption": "none"},
		{"codec": "h264", "protocol": "dash", "encryption": "widevine"},
		{"codec": "h264", "protocol": "dash", "encryption": "playready"},
		{"codec": "h264", "protocol": "hls", "encryption": "none"},
	]

	def get_capabilities(self, protocol=None):
		if not protocol:
			return list(self.CAPABILITIES)

		# požadovaný protokol dopredu, zvyšok necháme ako záložné možnosti
		# (užší zoznam vracal iný zdroj, preto sa nič nevyhadzuje)
		return [c for c in self.CAPABILITIES if c['protocol'] == protocol] + \
		       [c for c in self.CAPABILITIES if c['protocol'] != protocol]

	def get_video_source_info(self, video_id, video_type='video', protocol=None):
		# Telo požiadavky je 1:1 to, čo posiela web (overené z HAR-u 1.8.2026).
		#
		# KĽÚČOVÉ je pole "language": OKTAGON vysiela event paralelne vo viacerých
		# jazykoch (CZ/SK, DE, PL) a bez neho Tivio vracalo náhodné encodery -
		# na prijímači tak hral nemecký komentár. So správnym jazykom vráti zdroj,
		# ktorého ID sa zhoduje s video_source z katalógu (česká verzia).
		#
		# Odpoveď pri živom evente:
		#   url             - manifest so sessionId, ?start=<začiatok eventu> a pairingId
		#   sourceHistory   - ten istý stream bez session (živá hrana)
		#   sessionType     - "interactive", activeContentId - práve vysielaný obsah
		data = {
			"id": video_id,
			"documentType": video_type,
			"language": self.LANGUAGE,
			"platform": "WEB",
			"deviceId": self.get_device_id(),
			"capabilities": self.get_capabilities(protocol)
		}
		result = self.call_tivio_api('getSourceUrl', data) or {}

		# diagnostika: pri živých eventoch Tivio vracia rôzne zdroje, nech je v logu vidieť celú odpoveď
		# (bez query stringu v URL - sessionId/pairingId do logu nepatria)
		try:
			self.cp.log_debug("getSourceUrl(%s, %s, %s) -> %s" % (video_id, video_type, protocol or 'default', json.dumps(self._safe_source_info(result))[:800]))
		except Exception:
			pass

		return result

	@staticmethod
	def _safe_source_info(result):
		# Kópia odpovede getSourceUrl vhodná do logu: z KAŽDEJ URL (aj vnorenej)
		# sa odstráni query string a hodnoty kľúčov so session/pairing/token
		# v názve sa zamaskujú. Predtým sa čistili len 'url' a 'sourceHistory',
		# ale odpoveď nesie sessionId aj v ďalších poliach (test na prijímači).
		def _clean(key, val):
			if isinstance(val, dict):
				return dict((k, _clean(k, v)) for k, v in val.items())
			if isinstance(val, (list, tuple)):
				return [_clean(key, v) for v in val]
			k = (key or '').lower()
			if any(x in k for x in ('session', 'pairing', 'token', 'signature')):
				return '***'
			if isinstance(val, (str, type(u''))) and val.startswith(('http://', 'https://')):
				return safe_url(val)
			return val

		return _clean('', result)

	# ##################################################################################################################

	def dump_json(self, name, data, force=False):
		if DUMP_API_REQUESTS or force:
			file_name = os.path.join(self.cp.tmp_dir, name + '.json')
			with open(file_name, 'w') as f:
				json.dump(data, f)

# ##################################################################################################################
# Vysokoúrovňový klient - spracuje dáta z OktagonTVClient do podoby pre frontend (provider).
# ##################################################################################################################

class OktagonTV(object):
	def __init__(self, content_provider):
		self.page_limit = 30
		self.langs = ['sk', 'cs', 'en']
		self.cp = content_provider
		self.client = OktagonTVClient(content_provider)

	# ##################################################################################################################

	def login(self):
		self.client.refresh_login()

	# ##################################################################################################################

	def get_lang_label(self, item):
		if isinstance(item, dict):
			for l in self.langs:
				if item.get(l):
					return item[l]
			else:
				return ""
		else:
			return item

	# ##################################################################################################################

	def check_playability(self, item):
		# 1 = voľné, 0 = treba predplatné/PPV, 2 = kúpené (má nárok)
		ret = 1
		for m in item.get('monetizations', []):
			mon_type = m.get('type')
			if mon_type not in ('transaction', 'subscription'):
				continue

			# kúpené PPV (transaction) aj aktívne predplatné (subscription) sú v purchases
			mon_id = m.get('id') or m.get('monetizationRef', '').split('/')[-1]
			if mon_id and mon_id in self.client.purchases:
				return 2

			ret = 0
		return ret

	# ##################################################################################################################

	def get_img(self, item):
		if 'itemSpecificData' in item:
			item = item['itemSpecificData']
		item = item.get('assets')
		if not item:
			return None

		img = None
		for k in ('portrait', 'tag_portrait_cover', 'cover', 'logo', 'tag_landscape_cover', 'tag_detial_cover'):
			img = (item.get(k) or {}).get('@1', {}).get('background')
			if img:
				return img

		for k in item.keys():
			img = item.get(k, {}).get('@1', {}).get('background')
			if img:
				return img
		return img

	# ##################################################################################################################

	def _add_video_item(self, item, series_tag_id=None):
		duration = None
		try:
			d = item.get('duration')
			if d:
				duration = int(float(d))
		except Exception:
			duration = None

		year = None
		try:
			created = item.get('created') or item.get('publishedAt')
			if created and not isinstance(created, dict) and len(created) >= 4 and created[:4].isdigit():
				year = int(created[:4])
		except Exception:
			year = None

		return {
			'title': self.get_lang_label(item.get('name', {})),
			'plot': self.get_lang_label(item.get('description', {})),
			'img': self.get_img(item),
			'type': 'video',
			'id': item['__name'].split('/')[-1],
			'playable': self.check_playability(item),
			'parent_tag_id': series_tag_id,
			'duration': duration,
			'year': year,
		}

	# ##################################################################################################################

	def _add_banner_item(self, item):
		if item.get('itemType') == 'VIDEO':
			item_type = 'video'
		elif item.get('itemType') == 'TAG':
			item_type = 'tag'
		else:
			self.cp.log_error("Unsupported banner item type: %s" % item.get('itemType'))
			return None

		return {
			'title': self.get_lang_label(item.get('name', {})),
			'plot': self.get_lang_label(item.get('itemSpecificData', {}).get('description', {})),
			'img': self.get_img(item),
			'type': item_type,
			'id': item['id'],
			'playable': self.check_playability(item.get('itemSpecificData', {}))
		}

	# ##################################################################################################################

	def _add_row(self, item):
		return {'title': self.get_lang_label(item.get('name', {})), 'type': 'row', 'id': item['rowId']}

	def _add_banner(self, item):
		ret = []
		for tile_item in item['tiles']['items']:
			x = self._add_banner_item(tile_item)
			if x:
				ret.append(x)
		return ret

	# ##################################################################################################################

	def get_screen_items(self, screen_id, page=0, ref=False):
		if ref:
			screen_id = self.get_document('/screens/' + screen_id, True)['screenId']

		screen_data = self.client.get_screen_rows(screen_id, page * self.page_limit, self.page_limit)

		ret = []
		for item in (screen_data or {}).get('items') or []:
			row_type = item.get('rowComponent')
			if row_type == 'ROW':
				subtype = item.get('type')
				if subtype in ('favourites', 'continueToWatch'):
					# obľúbené / pokračovať v sledovaní doplnok nepodporuje - riadok sa preskočí
					continue
				elif item.get('itemComponent') != 'ROW_ITEM_HIGHLIGHTED':
					ret.append(self._add_row(item))
			elif row_type == 'BANNER':
				ret.extend(self._add_banner(item))
			else:
				self.cp.log_error("Unsupported ROW type: %s" % row_type)

		if (screen_data or {}).get('nextPageParams'):
			ret.append({'type': 'next'})

		if not ret:
			# diagnostika (napr. prázdna obrazovka Domov) - čo vlastne Tivio vrátilo
			self.cp.log_info("OKTAGON screen %s returned no items, response keys: %s" % (
				screen_id, list((screen_data or {}).keys())))
		else:
			self.cp.log_info("OKTAGON screen %s: %d items" % (screen_id, len(ret)))

		return ret

	# ##################################################################################################################

	def _add_row_video(self, item):
		title = self.get_lang_label(item.get('name', {}))
		item_data = item.get('itemSpecificData', {})
		if 'episodeNumber' in item_data:
			title += ' {} ({})'.format(int_to_roman(item_data.get('seasonNumber', 0)), item_data['episodeNumber'])
		return {
			'title': title,
			'img': self.get_img(item),
			'type': 'video',
			'id': item['id'],
			'playable': self.check_playability(item_data)
		}

	def _add_row_tag(self, item):
		item_data = item.get('itemSpecificData', {})
		seasons = []
		for x in item_data.get('metadata', []):
			if x.get("key") == 'availableSeasons':
				seasons = [s['seasonNumber'] for s in x['value']]
		return {
			'title': self.get_lang_label(item.get('name', {})),
			'plot': self.get_lang_label(item_data.get('description', {})),
			'img': self.get_img(item),
			'type': 'tag',
			'id': item['id'],
			'seasons': seasons
		}

	def _add_row_tvchannel(self, item):
		item_data = item.get('itemSpecificData', {})
		return {
			'title': self.get_lang_label(item.get('name', {})),
			'img': self.get_img(item),
			'type': 'tvChannel',
			'id': item['id'],
			'playable': self.check_playability(item_data),
			'virtual': item_data.get('type') == 'VIRTUAL'
		}

	# ##################################################################################################################

	def get_row_items(self, row_id, page=0):
		row_data = self.client.get_row_tiles(row_id, page * self.page_limit, self.page_limit)

		ret = []
		for item in (row_data or {}).get('items') or []:
			item_type = item.get('itemType')
			if item_type == 'VIDEO':
				ret.append(self._add_row_video(item))
			elif item_type == 'TAG':
				ret.append(self._add_row_tag(item))
			elif item_type == 'TV_CHANNEL':
				ret.append(self._add_row_tvchannel(item))
			else:
				self.cp.log_error("Unsupported ROW item type: %s, path: %s" % (item_type, item.get('path')))

		if (row_data or {}).get('nextPageParams'):
			ret.append({'type': 'next'})
		return ret

	# ##################################################################################################################

	def get_videos_by_tag(self, tag_id):
		# zápasy v turnaji / epizódy relácie
		ret = []
		for item in self.client.load_videos_by_tag(tag_id):
			ret.append(self._add_video_item(item, tag_id))
		return ret

	# ##################################################################################################################

	def _tags_to_items(self, tags):
		ret = []
		for item in tags:
			title = self.get_lang_label(item.get('name', {}))
			if not title:
				continue

			# nepomenované tagy v Tiviu majú default názov "Tag" - do menu nepatria
			if title.strip().lower() == 'tag':
				continue

			plot = self.get_lang_label(item.get('description', {})) or ''
			if plot.strip().lower() == 'tag description':
				# default popis nepomenovaných tagov v Tiviu - nemá zmysel ho zobrazovať
				plot = ''

			ret.append({
				'title': title,
				'plot': plot,
				'img': self.get_img(item),
				'type': 'tag',
				'id': item['__name'].split('/')[-1],
			})
		return ret

	# ##################################################################################################################

	def get_latest_videos(self, limit=60):
		ret = []
		for item in self.client.load_latest_videos(limit):
			ret.append(self._add_video_item(item))
		self.cp.log_info("OKTAGON latest videos loaded: %d" % len(ret))
		return ret

	# ##################################################################################################################

	def get_tournaments(self, limit=100):
		# Turnaje (OKTAGON 92, 93, ...) = tagy typu EVENT_TAG_TYPE_ID.
		# Web ich takto číta na /sk/tournaments (overené z HAR 28.7.2026).
		ret = self._tags_to_items(self.client.load_tags_by_tagtype(self.client.EVENT_TAG_TYPE_ID, limit))
		self.cp.log_info("OKTAGON tournaments loaded: %d" % len(ret))
		return ret

	# ##################################################################################################################

	def get_shows(self):
		# Relácie (Pořady). Web ich skladá z riadku ROW_SHOWS (overené z HAR /sk/shows),
		# ktorý obsahuje dlaždice typu TAG. Ak sa riadok nepodarí načítať (napr. Tivio cloud
		# funkcia getTilesInRow nie je pre túto organizáciu dostupná), použije sa záloha:
		# tagy organizácie s typom "show_tag".
		ret = []
		try:
			for item in self.get_row_items(self.client.ROW_SHOWS):
				if item.get('type') == 'tag':
					ret.append(item)
			self.cp.log_info("OKTAGON shows loaded from row %s: %d" % (self.client.ROW_SHOWS, len(ret)))
		except Exception as e:
			self.cp.log_debug("Loading shows from row failed: %s" % error_text(e))

		if not ret:
			ret = self._tags_to_items(self.client.load_org_tags(tag_type='show_tag'))
			self.cp.log_info("OKTAGON shows loaded (fallback show_tag): %d" % len(ret))

		return ret

	# ##################################################################################################################

	def get_video_source_info(self, video_id, video_type='video', protocol=None):
		return self.client.get_video_source_info(video_id, video_type, protocol)

	def get_document(self, path, org_root=False):
		ret = self.client.load_document(path, org_root)
		self.client.dump_json('document-' + path.replace('/', '_'), ret)
		return ret

	# ##################################################################################################################

	def search(self, keyword, video_limit=300, tag_limit=500):
		# OKTAGON nemá odchytený (ani verejný) vyhľadávací endpoint a Firestore REST
		# nevie hľadať podčasť reťazca. Preto načítame katalóg (tagy = turnaje, relácie,
		# bojovníci + najnovšie videá) a filtrujeme na strane doplnku.
		kw = norm_text(keyword)
		if not kw:
			return {'tags': [], 'videos': []}

		tags = []
		seen = set()

		def add_tags(items):
			for it in items or []:
				if not it or not it.get('title') or not it.get('id'):
					continue
				if it['id'] in seen:
					continue
				if kw not in norm_text(it['title']):
					continue
				seen.add(it['id'])
				tags.append(it)

		# turnaje (OKTAGON 92, 91, ...)
		try:
			add_tags(self.get_tournaments())
		except LoginException:
			raise
		except Exception as e:
			self.cp.log_error("search tournaments failed: %s" % error_text(e))

		# relácie / pořady
		try:
			add_tags(self.get_shows())
		except LoginException:
			raise
		except Exception as e:
			self.cp.log_error("search shows failed: %s" % error_text(e))

		# ostatné tagy organizácie - hlavne mená bojovníkov
		try:
			add_tags(self._tags_to_items(self.client.load_org_tags(limit=tag_limit)))
		except LoginException:
			raise
		except Exception as e:
			self.cp.log_error("search tags failed: %s" % error_text(e))

		# a najnovšie videá podľa názvu
		videos = []
		try:
			for it in self.get_latest_videos(video_limit):
				if it.get('title') and kw in norm_text(it['title']):
					videos.append(it)
		except LoginException:
			raise
		except Exception as e:
			self.cp.log_error("search videos failed: %s" % error_text(e))

		self.cp.log_info("OKTAGON search '%s': %d tags, %d videos" % (keyword, len(tags), len(videos)))
		return {'tags': tags, 'videos': videos}
