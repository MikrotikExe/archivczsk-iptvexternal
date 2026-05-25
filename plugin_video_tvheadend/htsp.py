# -*- coding: utf-8 -*-
"""
htsp.py — HTSP klient pre Tvheadend (port 9982) s MPEG-TS muxom.

Súčasť plugin_video_tvheadend. Poskytuje:
  - HTSP handshake + SHA1 digest auth
  - HTSMSG binárna serializácia
  - načítanie kanálov, tagov, EPG, DVR cez HTSP
  - MPEG-TS muxer (H264 + AAC -> TS) z muxpkt
  - lokálny HTTP proxy na streamovanie (subscribe -> TS -> exteplayer3)

Overené: VU+ Uno4K SE, OpenATV, Python 3.13, HTSP v44.
Py2/Py3 kompatibilné.
"""
import socket
import struct
import hashlib
import time

HTSP_PROTO_VERSION = 35
HMF_MAP=1; HMF_S64=2; HMF_STR=3; HMF_BIN=4; HMF_LIST=5

# TS konštanty
PID_PAT = 0x0000
PID_PMT = 0x1000
PID_VIDEO = 0x0100
PID_AUDIO = 0x0101
STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_HEVC = 0x24
STREAM_TYPE_MPEG2VIDEO = 0x02
STREAM_TYPE_AAC = 0x0F  # ADTS AAC
STREAM_TYPE_MPEG2AUDIO = 0x03
STREAM_TYPE_AC3 = 0x81
STREAM_TYPE_EAC3 = 0x87
PCR_PID = PID_VIDEO

# AAC sampling frequency index tabuľka (ADTS)
AAC_FREQ_TABLE = [96000, 88200, 64000, 48000, 44100, 32000,
                  24000, 22050, 16000, 12000, 11025, 8000, 7350]


def _to_bytes(s):
	return s if isinstance(s, bytes) else s.encode('utf-8')


# ---- HTSMSG (rovnaké ako v diagnostike) ----
def _int_min(n):
	if n == 0: return b'\x00'
	out = bytearray()
	v = n
	while v:
		out.append(v & 0xff); v >>= 8
	return bytes(out)

def _ser_field(name, value):
	nb = _to_bytes(name)
	if isinstance(value, bool):
		typ, data = HMF_S64, _int_min(1 if value else 0)
	elif isinstance(value, int):
		typ, data = HMF_S64, _int_min(value)
	elif isinstance(value, bytes):
		typ, data = HMF_BIN, value
	elif isinstance(value, str):
		typ, data = HMF_STR, _to_bytes(value)
	elif isinstance(value, dict):
		typ, data = HMF_MAP, _ser_map(value)
	elif isinstance(value, (list, tuple)):
		typ, data = HMF_LIST, b''.join(_ser_field('', v) for v in value)
	else:
		typ, data = HMF_STR, _to_bytes(value)
	return struct.pack('>BBI', typ, len(nb), len(data)) + nb + data

def _ser_map(d):
	return b''.join(_ser_field(k, v) for k, v in d.items())

def serialize(msg):
	body = _ser_map(msg)
	return struct.pack('>I', len(body)) + body

def _bin2int(b):
	n = 0
	for i in range(len(b)-1, -1, -1):
		n = (n << 8) | (b[i] if isinstance(b[i], int) else ord(b[i]))
	return n

def _deser(data, is_list=False):
	res = [] if is_list else {}
	pos = 0; n = len(data)
	while pos + 6 <= n:
		typ = data[pos] if isinstance(data[pos], int) else ord(data[pos])
		nl = data[pos+1] if isinstance(data[pos+1], int) else ord(data[pos+1])
		dl = struct.unpack('>I', data[pos+2:pos+6])[0]
		pos += 6
		name = data[pos:pos+nl]; pos += nl
		pay = data[pos:pos+dl]; pos += dl
		if typ == HMF_STR: val = pay.decode('utf-8','replace')
		elif typ == HMF_BIN: val = pay
		elif typ == HMF_S64: val = _bin2int(pay)
		elif typ == HMF_MAP: val = _deser(pay, False)
		elif typ == HMF_LIST: val = _deser(pay, True)
		else: val = pay
		if is_list: res.append(val)
		else: res[name.decode('utf-8','replace')] = val
	return res

def deserialize(data):
	return _deser(data, False)



# ============================================================
# HTSP klient s metadata + auth
# ============================================================
class HTSPClient(object):
	"""HTSP klient pre Tvheadend. Drží spojenie, vie načítať
	kanály/EPG/DVR a subscribe na stream.

	cp = content provider doplnku (na logging cez cp.log_info);
	môže byť None pre samostatné použitie.
	"""

	def __init__(self, host, port=9982, user='', pwd='', cp=None, timeout=15):
		self.host = host
		self.port = int(port or 9982)
		self.user = user
		self.pwd = pwd
		self.cp = cp
		self.timeout = timeout
		self.sock = None
		self.challenge = None
		self._seq = 0
		self.server_name = None
		self.server_version = None

	def _log(self, msg):
		try:
			if self.cp is not None and hasattr(self.cp, 'log_info'):
				self.cp.log_info('[Tvheadend.htsp] ' + str(msg))
		except Exception:
			pass

	# ---- spojenie ----
	def connect(self):
		self.sock = socket.create_connection((self.host, self.port), self.timeout)
		self._hello()
		if not self._auth():
			raise IOError('HTSP autentifikácia zlyhala')
		self._log('pripojený k %s:%d (HTSP v%s, %s)' % (
			self.host, self.port, self.server_version, self.server_name))

	def close(self):
		if self.sock:
			try: self.sock.close()
			except Exception: pass
			self.sock = None

	def _send(self, method, args=None, with_seq=True):
		msg = dict(args or {})
		msg['method'] = method
		if with_seq:
			self._seq += 1
			msg['seq'] = self._seq
			self.sock.sendall(serialize(msg))
			return self._seq
		self.sock.sendall(serialize(msg))
		return None

	def _recv(self):
		hdr = self._recv_n(4)
		if len(hdr) < 4:
			raise IOError('HTSP spojenie zatvorené')
		length = struct.unpack('>I', hdr)[0]
		return deserialize(self._recv_n(length))

	def _recv_reply(self, seq, maxn=400):
		for _ in range(maxn):
			m = self._recv()
			if m.get('seq') == seq:
				return m
		raise IOError('HTSP: nedorazila odpoveď seq=%d' % seq)

	def _recv_n(self, n):
		buf = b''
		while len(buf) < n:
			c = self.sock.recv(n - len(buf))
			if not c:
				break
			buf += c
		return buf

	def _hello(self):
		s = self._send('hello', {
			'htspversion': HTSP_PROTO_VERSION,
			'clientname': 'archivczsk-tvheadend',
			'clientversion': '0.70.0',
		})
		r = self._recv_reply(s)
		self.server_version = r.get('htspversion')
		self.server_name = r.get('servername')
		self.challenge = r.get('challenge')
		return r

	def _auth(self):
		if self.pwd and self.challenge:
			digest = hashlib.sha1(_to_bytes(self.pwd) + self.challenge).digest()
			s = self._send('authenticate', {'username': self.user, 'digest': digest})
		else:
			s = self._send('authenticate', {'username': self.user})
		return not self._recv_reply(s).get('noaccess')

	def call(self, method, args=None):
		"""RPC volanie so seq (preskočí async správy)."""
		s = self._send(method, args)
		return self._recv_reply(s)

	# ---- metadata: kanály, tagy, EPG, DVR ----
	def fetch_metadata(self, with_epg=False, timeout=None, epg_max_days=2,
	                   channels_only=False):
		"""Načíta kanály, tagy, (voliteľne EPG) a DVR cez async metadata.
		Vráti dict: {'channels': [...], 'tags': [...], 'events': [...], 'dvr': [...]}

		POZOR: pri EPG nesmieme skončiť na initialSyncCompleted — EPG eventy
		(eventAdd) chodia AŽ ZA ním ako async prúd. Preto pri with_epg
		čítame ďalej kým chodia eventy (s krátkym idle timeoutom), inak
		dostaneme 0 alebo neúplný EPG.

		epg_max_days obmedzí EPG na N dní dopredu (server inak drží CELÝ
		EPG buffer v RAM). 0 = bez limitu.

		channels_only=True: rýchla cesta pre streamovanie — skonči hneď
		ako máme kanály+tagy (pred DVR dumpom). Použiť keď potrebujeme len
		zoznam kanálov (stream URL), nie archív/EPG.
		"""
		if timeout is None:
			# EPG fetch potrebuje viac času: server posiela najprv celý
			# DVR dump (8000+) a potom 16000+ EPG eventov. 45s nestačilo.
			timeout = 120 if with_epg else 45
		args = {'epg': 1 if with_epg else 0}
		if with_epg and epg_max_days:
			# epgMaxTime = absolútny čas (epoch) do ktorého chceme EPG
			args['epgMaxTime'] = int(time.time()) + epg_max_days * 86400
		self._send('enableAsyncMetadata', args, with_seq=False)
		channels = []; tags = []; events = []; dvr = []
		sync_done = False
		idle_after_sync = 6
		self.sock.settimeout(timeout)
		start = time.time()
		last_event = start
		try:
			while True:
				# globálny strop
				if time.time() - start > timeout:
					break
				# RÝCHLA CESTA: keď chceme len kanály (pre streamovanie),
				# skonči hneď ako máme kanály a začnú chodiť DVR záznamy —
				# HTSP posiela kanály+tagy PRED DVR dumpom (8000+ = 30s).
				if channels_only and channels and dvr:
					break
				# po sync a bez EPG končíme hneď; s EPG čakáme na idle
				if sync_done:
					if not with_epg:
						break
					if time.time() - last_event > idle_after_sync:
						break
				try:
					m = self._recv()
				except socket.timeout:
					break
				meth = m.get('method')
				if meth == 'channelAdd':
					channels.append(m)
				elif meth == 'tagAdd':
					tags.append(m)
				elif meth == 'eventAdd':
					events.append(m)
					last_event = time.time()
				elif meth == 'dvrEntryAdd':
					dvr.append(m)
				elif meth == 'initialSyncCompleted':
					sync_done = True
		finally:
			# DÔLEŽITÉ pre RAM servera: vypni async metadata aby server
			# prestal držať/posielať EPG buffer pre toto spojenie.
			try:
				self._send('disableAsyncMetadata', {}, with_seq=False)
			except Exception:
				pass
		self._log('metadata (epg=%s): %d kanálov, %d tagov, %d EPG, %d DVR%s' % (
			'ÁNO' if with_epg else 'nie',
			len(channels), len(tags), len(events), len(dvr),
			'' if sync_done else ' (NEDOKONČENÝ sync!)'))
		return {'channels': channels, 'tags': tags, 'events': events,
		        'dvr': dvr, '_sync_done': sync_done}

