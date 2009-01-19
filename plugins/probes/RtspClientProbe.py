# -*- coding: utf-8 -*-
##
# This file is part of Testerman, a test automation system.
# Copyright (c) 2008-2009 Sebastien Lefevre and other contributors
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
##

##
# Implements RTSP encoding/decoding (request and response) over tcp (for now)
# RFC 2326
# 
# Manages CSeq (generates them, matches them when waiting for a response)
# if not provided by the user.
##


import CodecManager
import ProbeImplementationManager

import threading
import socket
import select


class RtspClientProbe(ProbeImplementationManager.ProbeImplementation):
	"""
	type record RtspRequest
	{
		charstring method,
		charstring uri,
		charstring version optional, // default: 'RTSP/1.0', or as configured
		record { charstring <header name>* } headers,
		charstring body optional, // default: ''
	}

	type record RtspResponse
	{
		integer status,
		charstring reason,
		charstring protocol,
		record { charstring <header name>* } headers,
		charstring body,
	}

	type port RtspClientPortType message
	{
		in RtspRequest;
		out RtspResponse;
	}
	"""
	def __init__(self):
		ProbeImplementationManager.ProbeImplementation.__init__(self)
		self._mutex = threading.RLock()
		self._responseThread = None
		self._connection = None
		self._cseq = 0
		# Default test adapter properties
		self.setDefaultProperty('version', 'RTSP/1.0')
		self.setDefaultProperty('auto_connect', False)
		self.setDefaultProperty('maintain_connection', False)
		self.setDefaultProperty('host', 'localhost')
		self.setDefaultProperty('port', 554)
		self.setDefaultProperty('transport', 'tcp')
		self.setDefaultProperty('local_ip', '')
		self.setDefaultProperty('local_port', 0)
		self.setDefaultProperty('strict_mode', False)

	# LocalProbe reimplementation)
	def onTriMap(self):
		if self['auto_connect']:
			self.connect()
	
	def onTriUnmap(self):
		self.reset()
	
	def onTriExecuteTestCase(self):
		# No static connections
		pass

	def onTriSAReset(self):
		# No static connections
		pass
	
	def onTriSend(self, message, sutAddress):
		try:
			# FIXME:
			# Should go to a configured codec instance instead.
			# (since we modify the message here... should be a copy instead)
			if not message.has_key('version'):
				message['version'] = self['version']
			if not message.has_key('headers'):
				message['headers'] = {}
			
			cseq = None

			# Non-strict mode: CSeq management: we add one if none is found
			if not self['strict_mode']:
				# Look for a CSeq
				for k, v in message['headers'].items():
					if k.lower() == 'cseq':
						cseq = str(v)
				if cseq is None:
					# Generate and set a cseq
					message['headers']['CSeq'] = self.generateCSeq()
					cseq = str(message['headers']['CSeq'])

			try:
				encodedMessage = CodecManager.encode('rtsp.request', message)
			except Exception, e:
				raise ProbeImplementationManager.ProbeException('Invalid request message format: cannot encode RTSP request')
			
			# Connect if needed
			if not self.isConnected():
				self.connect()

			# Send our payload
			self._connection.send(encodedMessage)
			self.logSentPayload(encodedMessage.split('\r\n')[0], encodedMessage)
			# Now wait for a response asynchronously
			self.waitResponse(cseq = cseq)
		except Exception, e:
			raise ProbeImplementationManager.ProbeException('Unable to send RTSP request: %s' % str(e))
			
	# Specific methods
	def _lock(self):
		self._mutex.acquire()
	
	def _unlock(self):
		self._mutex.release()
	
	def generateCSeq(self):
		self._lock()
		self._cseq += 1
		cseq = self._cseq
		self._unlock()
		return self._cseq
	
	def connect(self):
		"""
		Tcp-connect to the host. Returns when we are ready to send something.
		"""
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
		sock.bind((self['local_ip'], self['local_port']))
		sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
		# Blocking or not ?
		sock.connect((self['host'], self['port']))
		self._connection = sock
	
	def isConnected(self):
		if self._connection:
			return True
		else:
			return False
	
	def disconnect(self):
		if self._connection:
			try:
				self._connection.close()
			except:
				pass
		self._connection = None
	
	def reset(self):
		if self._responseThread:
			self.getLogger().debug("Stopping response thread...")
			self._responseThread.stop()
		self.disconnect()
		self._responseThread = None
	
	def waitResponse(self, cseq):
		"""
		Creates a thread, wait for the response.
		@type  cseq: string, or None
		@param cseq: the expected CSeq in response. If None, let the userland checks it (strict mode)
		"""
		self._responseThread = ResponseThread(self, self._connection, cseq)
		self._responseThread.start()

class ResponseThread(threading.Thread):
	def __init__(self, probe, socket, cseq):
		threading.Thread.__init__(self)
		self._probe = probe
		self._socket = socket
		self._stopEvent = threading.Event()
		self._cseq = cseq
	
	def run(self):
		buf = ''
		while not self._stopEvent.isSet():
			try:
				r, w, e = select.select([self._socket], [], [], 0.1)
				if self._socket in r:
					read = self._socket.recv(1024*1024)
					buf += read

					# In RTSP/1.0, content-length is mandatory if there is a body.
					decodedMessage = None
					try:
						self._probe.getLogger().debug('data received (bytes %d), decoding attempt...' % len(buf))
						decodedMessage = CodecManager.decode('rtsp.response', buf, lower_case = (not self._probe['strict_mode']))
					except Exception, e:
						# Incomplete message. Wait for more data.
						self._probe.getLogger().debug('unable to decode: %s' % str(e))
						pass
						
					if decodedMessage:
						# System log, always
						self._probe.logReceivedPayload(buf.split('\r\n')[0], buf)
						
						# Should we check the cseq locally ?
						if self._cseq is None:
							# Let the user check the cseq
							self._probe.getLogger().info('message decoded, enqueuing without checking CSeq...')
							self._probe.triEnqueueMsg(decodedMessage)
							self._stopEvent.set()
						else:
							# Conditional enqueing - let's found the cseq
							cseq = None
							for k, v in decodedMessage['headers'].items():
								# Stop on the first cseq header found
								if k.lower() == 'cseq':
									cseq = v
									break
							if cseq == self._cseq:
								self._probe.getLogger().info('message decoded, CSeq matched, enqueuing...')
								self._probe.triEnqueueMsg(decodedMessage)
								self._stopEvent.set()
							else:
								self._probe.getLogger().warning('Invalid CSeq received. Not enqueuing, ignoring message')
								buf = ''
								decodedMessage = None
								# Wait for a possible next message...

					elif not read:
						# Message not decoded, nothing to read anymore.
						raise Exception('Incomplete message received, stream interrupted')
						# .. and we should disconnect, too...
						
			except Exception, e:
				self._probe.getLogger().error('Error while waiting for rtsp response: %s' % str(e))
				self._stopEvent.set()
		if not self._probe['maintain_connection']:
			# Well, maintain connection in RTSP ? 
			self._probe.disconnect()
	
	def stop(self):
		self._stopEvent.set()
		self.join()
					
					
ProbeImplementationManager.registerProbeImplementationClass('rtsp.client', RtspClientProbe)
		