# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals

from tools_archivczsk.contentprovider.exception import AddonErrorException

try:
	from tools_archivczsk.six.moves.urllib.parse import urlparse, urlunparse, quote, urlencode
except Exception:
	try:
		from urllib.parse import urlparse, urlunparse, quote, urlencode
	except ImportError:
		from urlparse import urlparse, urlunparse
		from urllib import quote, urlencode


class TvhStreamUrlMixin(object):
	"""Stavba stream URL (live/DVR) pre triedu Tvheadend.
	Vynate z tvheadend.py v 0.90.0 (refaktor, bez zmeny spravania).
	Pouziva class-level konstanty STREAM_*_ENDPOINT/PREFER_CHANNEL_STREAM/
	USE_TITLE_PARAM, self._url (core) a self.is_htsp_mode (HTSP mixin) cez MRO."""

	def _url_with_creds(self, full_url):
		user = (self.cp.get_setting('username') or '').strip()
		pwd  = (self.cp.get_setting('password') or '')
		if not user:
			return full_url
		u = urlparse(full_url)
		netloc = '%s:%s@%s' % (quote(user, safe=''), quote(pwd, safe=''), u.netloc)
		return urlunparse((u.scheme, netloc, u.path, u.params, u.query, u.fragment))

	def _build_stream_url(self, endpoint_path, profile=None, channel_title=None):
		url = self._url(endpoint_path)
		params = {}
		if profile:
			params['profile'] = profile
		if self.USE_TITLE_PARAM and channel_title:
			try:
				ct = str(channel_title).strip()
				if ct:
					params['title'] = ct
			except Exception:
				pass
		if params:
			url = url + '?' + urlencode(params)
		return self._url_with_creds(url)

	def make_live_stream_url(self, channel_uuid=None, service_uuid=None, channel_title=None):
		profile = (self.cp.get_setting('profile') or 'pass').strip()

		# HTSP mód: stream ide PRIAMO cez TVH HTTP endpoint (ako 9981 mód) —
		# žiadny vlastný proxy/remux cez 18888 (ten player odmietal). HTSP
		# channel_uuid je číselné channelId, ktoré endpoint stream/channelid/
		# berie priamo. TVH pošle hotový TS (vrátane descramblingu).
		if self.is_htsp_mode() and channel_uuid:
			return self._build_stream_url(
				self.STREAM_CHID_ENDPOINT % channel_uuid,
				profile=profile, channel_title=channel_title
			)

		if self.PREFER_CHANNEL_STREAM and channel_uuid:
			return self._build_stream_url(
				self.STREAM_CH_ENDPOINT % channel_uuid,
				profile=profile, channel_title=channel_title
			)
		if service_uuid:
			return self._build_stream_url(
				self.STREAM_SVC_ENDPOINT % service_uuid,
				profile=profile, channel_title=channel_title
			)
		if channel_uuid:
			return self._build_stream_url(
				self.STREAM_CHID_ENDPOINT % channel_uuid,
				profile=profile, channel_title=channel_title
			)
		raise AddonErrorException(self._("Missing channel/service identifier for streaming."))

	def make_dvr_url(self, entry_url_field):
		if not entry_url_field:
			return None
		# HTSP mód: marker 'htsp_dvr:<idStr>' -> PRIAMA TVH HTTP URL
		# (dvrfile/<idStr>), rovnako ako 9981 mód. Žiadny proxy cez 18888.
		if isinstance(entry_url_field, str) and entry_url_field.startswith('htsp_dvr:'):
			id_str = entry_url_field.split(':', 1)[1]  # hex idStr
			return self._url_with_creds(self._url('dvrfile/%s' % id_str))
		return self._url_with_creds(self._url(entry_url_field))
