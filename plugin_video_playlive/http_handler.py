# -*- coding: utf-8 -*-

from tools_archivczsk.http_handler.hls import HlsHTTPRequestHandler
from tools_archivczsk.http_handler.dash import DashHTTPRequestHandler

# #################################################################################################
# HTTP handler pre PlayLive - HLS pre upútavky, DASH+DRM pre platené prenosy.
# DRM: BuyDRM/KeyOS (Widevine) cez proxy api.playlive.net/drmlaproxy/getLicense
# Licenčné požiadavky vyžadujú privacy mode (najprv service certificate, potom šifrovaný request).
# #################################################################################################


class PlayLiveHTTPRequestHandler(HlsHTTPRequestHandler, DashHTTPRequestHandler):
	def __init__(self, content_provider, addon):
		super(PlayLiveHTTPRequestHandler, self).__init__(content_provider, addon)
		self.hls_proxy_variants = False
		self.hls_proxy_segments = False
		self.dash_proxy_segments = False
		self.dash_internal_decrypt = True

	# #################################################################################################

	def get_drm_license(self, drm_info, lic_request):
		"""Override pre PlayLive DRM proxy - pridáva _sessid do URL a správne hlavičky."""
		session = self.cp.get_requests_session()

		license_url = drm_info.get('license_url', '')

		# PlayLive license proxy vyžaduje sessid v URL
		sessid = drm_info.get('sessid', '')
		if sessid and '_sessid=' not in license_url:
			license_url = license_url + ('&' if '?' in license_url else '?') + '_sessid=' + sessid

		headers = dict(drm_info.get('headers', {}))
		# BuyDRM/KeyOS proxy vyžaduje tieto hlavičky
		headers.setdefault('Content-Type', 'application/octet-stream')
		headers.setdefault('Origin', 'https://playlive.net')
		headers.setdefault('Referer', 'https://playlive.net/')

		content = None
		try:
			response = session.post(license_url, data=lic_request, headers=headers, timeout=20)
			if response.status_code == 200:
				content = response.content
			else:
				# zámerne bez URL - obsahuje _sessid a logy sa posielajú pri hlásení chýb
				self.cp.log_error("Failed to get DRM license from PlayLive: HTTP %d" % response.status_code)
		except Exception as e:
			self.cp.log_error("Failed to get DRM license from PlayLive: %s" % type(e).__name__)

		session.close()
		return content
