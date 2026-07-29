# -*- coding: utf-8 -*-

from tools_archivczsk.http_handler.hls import HlsHTTPRequestHandler
from tools_archivczsk.http_handler.dash import DashHTTPRequestHandler

# #################################################################################################
# OKTAGON.tv beží na Tivio platforme (rovnako ako JOJ Play).
#
# Ak sú live/PPV streamy chránené Widevine DRM, treba zapnúť interné dešifrovanie:
#   self.dash_internal_decrypt = True
# a v oktagontv.py v get_video_source_url() žiadať {"encryption":"widevine"} + načítať
# licenčnú URL. DRM podporuje tools_cenc (wvl3 CDM). Zatiaľ predpokladáme čisté streamy.
# #################################################################################################

class OktagonTVHTTPRequestHandler(HlsHTTPRequestHandler, DashHTTPRequestHandler):
	def __init__(self, content_provider, addon):
		super(OktagonTVHTTPRequestHandler, self).__init__(content_provider, addon)
		self.hls_proxy_variants = False
		self.hls_proxy_segments = False
		self.dash_proxy_segments = False
		self.dash_internal_decrypt = False
