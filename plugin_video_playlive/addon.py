# -*- coding: utf-8 -*-
from tools_archivczsk.contentprovider.archivczsk_provider import ArchivCZSKContentProvider
from .provider import PlayLiveContentProvider
from .http_handler import PlayLiveHTTPRequestHandler

# #################################################################################################

def main(addon):
	return ArchivCZSKContentProvider(PlayLiveContentProvider, addon, http_cls=PlayLiveHTTPRequestHandler)
