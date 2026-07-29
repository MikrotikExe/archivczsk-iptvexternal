# -*- coding: utf-8 -*-
from tools_archivczsk.contentprovider.archivczsk_provider import ArchivCZSKContentProvider
from .provider import OktagonTVContentProvider
from .http_handler import OktagonTVHTTPRequestHandler

# #################################################################################################

def main(addon):
	return ArchivCZSKContentProvider(OktagonTVContentProvider, addon, http_cls=OktagonTVHTTPRequestHandler)
