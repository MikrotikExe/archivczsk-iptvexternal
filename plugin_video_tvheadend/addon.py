# -*- coding: utf-8 -*-
from tools_archivczsk.contentprovider.archivczsk_provider import ArchivCZSKContentProvider
from tools_archivczsk.http_handler.playlive import PlayliveTVHTTPRequestHandler

from .provider import TvheadendContentProvider

# 0.62.0: HTSP mód sa používa LEN pre dáta (kanály/EPG/DVR cez port 9982).
# Streamovanie ide PRIAMO cez TVH HTTP endpoint (stream/channelid/, dvrfile/)
# na porte 9981 — rovnako ako klasický HTTP mód. Preto stačí framework
# PlayliveTVHTTPRequestHandler (base64 decode channel key + redirect na
# stream URL + built-in 15-min LRU cache). Vlastný HTSP→MPEG-TS proxy
# (P_htsp endpoint) bol odstránený — exteplayer3 nemá demuxer API ako
# Kodi pvr.hts, takže natívne HTSP streamovanie na Enigme nie je možné.


def main(addon):
	return ArchivCZSKContentProvider(TvheadendContentProvider, addon,
	                                  http_cls=PlayliveTVHTTPRequestHandler)
