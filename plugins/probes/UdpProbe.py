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
# A UDP basic probe.
#
##

import ProbeImplementationManager

import select
import socket
import sys
import threading
import time

class Connection:
	def __init__(self):
		self.localAddress = None
		self.peerAddress = None
		self.buffer = ''

class UdpProbe(ProbeImplementationManager.ProbeImplementation):
	"""
Identification and Properties
-----------------------------

Probe Type ID: ``udp``

Properties:

.. csv-table::
   :header: "Name","Type","Default value","Description"

   "``local_ip``","string","(empty - system assigned)","Local IP address to use when sending packets"
   "``local_port``","integer","0 (system assigned)","Local port to use when sending packets"
   "``listen_on_send``","boolean","True","Once something has been sent (from ``local_ip``:``local_port``), keep listening for a possible response on this address. Only stops listening on unmapping. When set to False, immediately closes the sending socket once the packet has been sent."
   "``listening_ip``","string","0.0.0.0","Listening IP address, if listening mode is activated (see below)"
   "``listening_port``","integer","0","Set it to a non-zero port to start listening on mapping. May be the same ip/port as ``local_ip``:``local_port``. In this case, ``listen_on_send`` is meaningless."
   "``default_sut_address``","string (ip:port)","``None``","If set, used as a default SUT address if none provided by the user"

Overview
--------

This probe is a very simple adapter that enables to send and receive UDP packets on the network.

It can work as a listening probe, raising an event/message each time a new UDP packet is received on its ``listening_ip``:``listening_port`` address, or send an UDP packet to either ``default_sut_address``
or to an explicit sutAddress if one was provided in the ``send()`` port command.

When sending a packet, the UDP source address is set to ``local_ip``:``local_port``.

Listening and sending modes can work in parallel on the same probe.

Availability
~~~~~~~~~~~~

All platforms.

Dependencies
~~~~~~~~~~~~

None.

See Also
~~~~~~~~

Other transport-oriented probes:

* :doc:`ProbeSctp`
* :doc:`ProbeTcp`


TTCN-3 Types Equivalence
------------------------

The test system interface port bound to such a probe complies with the ``TransportProbePortType`` port type as specified below:

::

  type TransportProbePortType
  {
    in, out octetstring;
  }
	"""
	def __init__(self):
		ProbeImplementationManager.ProbeImplementation.__init__(self)
		self._mutex = threading.RLock()

		self._listeningSocket = None
		self._localSocket = None # The socket we send messages from (if not listeningSocket)
		self._connections = {} # Connections() indexed by peer address (ip, port)
		self._pollingThread = None
		self.setDefaultProperty('local_ip', '')
		self.setDefaultProperty('local_port', 0)
		self.setDefaultProperty('listen_on_send', True)
		self.setDefaultProperty('listening_port', 0) # 0 means: not listening
		self.setDefaultProperty('listening_ip', '')
		self.setDefaultProperty('default_sut_address', None)

		# For future use (only datagram mode is supported for now - no context kept per peer address):
		# || `size` || integer || `0` || Fixed-size packet strategy: if set to non-zero, only raises messages when `size` bytes have been received. All raised messages will hage this constant size. ||
		# || `separator` || string || `None` || Separator-based packet strategy: if set no a character or a string, only raises messages when `separator` has been encountered; this separator is assumed to be a packet separator, and is not included in the raised message. May be useful for, for instance, \\x00-based packet protocols. ||
		self.setDefaultProperty('size', 0)
		self.setDefaultProperty('separator', None)
		self.setDefaultProperty('timeout', 0) # Not supported for now - this packetization criterium would be "raise a packet after N ms of inactivity on the socket"

	# ProbeImplementation reimplementation
	def onTriMap(self):
		self._reset()
		# Should we start listening here ??
		port = self['listening_port']
		if port:
			self._startListening()
		self._startPollingThread()
	
	def onTriUnmap(self):
		self._reset()
	
	def onTriSAReset(self):
		self._reset()
	
	def onTriExecuteTestCase(self):
		self._reset()

	# Specific implementation
	def _reset(self):	
		self._stopPollingThread()
		self._stopListening()
		self._lock()
		self._connections = {}
		if self._localSocket:
			try:
				self._localSocket.close()
			except Exception as e:
				pass
			self._localSocket = None
		self._unlock()

	def onTriSend(self, message, sutAddress):
		# First implementation level: no notification/connection explicit management.

		# We send a message from local_ip/local_port to sutAddress.
		# If listen_on_send, register the socket for future listening.

		# First fallback if the user did not provide a SUT address:
		# default SUT address (useful for outgoing connections)
		if not sutAddress:
			sutAddress = self['default_sut_address']

		try:
			# Split a ip:port to a (ip, port)
			t = sutAddress.split(':')
			addr = (t[0], int(t[1]))
		except:
			raise Exception("Invalid or missing SUT Address when sending a message")

		# First, get the local socket to use
		sock = self._getLocalSocket()
		# Now we can send our payload
		self._send(sock, message, addr)
		# And keep the socket open (or not)
		self._conditionallyCloseSocket(sock)
	
	def _lock(self):
		self._mutex.acquire()
	
	def _unlock(self):
		self._mutex.release()
	
	def _getLocalSocket(self):
		"""
		Check if you can reused an existing socket, or recreate a new one.
		"""
		sock = None
		self._lock()
		if self['listening_port'] and self['listening_port'] == self['local_port']:
			# We are listening. Should we reuse the listening socket ?
			if not self['listening_ip'] or not self['local_ip'] or (self['listening_ip'] == self['local_ip']):
				# listening on any, or on the same IP -> reuse
				sock = self._listeningSocket			

		# In all other cases, let's create a local socket.
		if not sock:
			try:
				if not self._localSocket:			
					sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
					sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
					sock.bind((self['local_ip'], self['local_port']))
					self._localSocket = sock
				else:
					# Reuse the local, not listening socket ??
					sock = self._localSocket
			except Exception as e:
				self._unlock()
				raise e
		
		self._unlock()
		return sock
	
	def _conditionallyCloseSocket(self, sock):
		"""
		Keeps open listening or local socket only is listen_on_send is True.
		"""
		self._lock()	
		if sock == self._listeningSocket:
			pass
		elif self['listen_on_send']:
			pass
		else:
			try:
				assert(sock == self._localSocket)
				sock.close()
				self._localSocket = None
			except:
				pass
		self._unlock()
	
	def _getConnection(self, localAddress, peerAddress):
		self._lock()
		key = (localAddress, peerAddress)
		if self._connections.has_key(key):
			conn = self._connections[key]
		else:
			conn = Connection()
			conn.peerAddress = peerAddress
			conn.localAddress = localAddress
			conn.buffer = ''
			self._connections[key] = conn

		self._unlock()
		return conn
	
	def _send(self, sock, data, addr):
		self.logSentPayload("UDP data", data, "%s:%s" % addr)
		self.getLogger().info("Sending data from %s to %s" % (str(sock.getsockname()), str(addr)))
		sock.sendto(data, addr)

	def _startListening(self):
		addr = (self['listening_ip'], self['listening_port'])
		self.getLogger().info("Starting listening on %s" % (str(addr)))
		
		# Should be mutex-protected
		self._lock()
		try:
			self._listeningSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
			self._listeningSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
			self._listeningSocket.bind(addr)
		except Exception as e:
			self._unlock()
			raise e
		self._unlock()
	
	def _stopListening(self):
		# Should be mutex-protected
		self._lock()
		if self._listeningSocket:
			self.getLogger().info("Stopping listening...")
			try:
				self._listeningSocket.close()
			except:
				pass
			self._listeningSocket = None
			self.getLogger().info("Stopped listening")
		self._unlock()
	
	def _startPollingThread(self):
		if not self._pollingThread:
			self._pollingThread = PollingThread(self)
			self._pollingThread.start()

	def _stopPollingThread(self):
		if self._pollingThread:
			self._pollingThread.stop()
			self._pollingThread = None

	def _getListeningSockets(self):
		sockets = []
		self._lock()
		if self._listeningSocket:
			sockets.append(self._listeningSocket)
		self._unlock()
		return sockets
	
	def _getActiveSockets(self):
		sockets = []
		self._lock()
		if self._localSocket:
			sockets.append(self._localSocket)
		self._unlock()
		return sockets
		
	def _feedData(self, localaddr, addr, data):
		conn = self._getConnection(localaddr, addr)
		if not conn:
			self.getLogger().warning("Received data from %s, which is not a known connection" % str(addr))
		else:
			# We are suppose to check for packetization criteria here
			# (size, timeout, separator)
			conn.buffer += data
			msg = None
			
			size = self['size']
			separator = self['separator']
			if size:
				while len(conn.buffer) >= size:
					msg = conn.buffer[:size]
					conn.buffer = conn.buffer[size+1:]
					self.logReceivedPayload("UDP data", msg, "%s:%s" % addr)
					self.triEnqueueMsg(msg, "%s:%s" % addr)
			elif separator is not None:
				msgs = conn.buffer.split(separator)
				for msg in msgs[:-1]:
					self.logReceivedPayload("UDP data", msg, "%s:%s" % addr)
					self.triEnqueueMsg(msg, "%s:%s" % addr)
				conn.buffer = msgs[-1]
			else:
				msg = conn.buffer
				conn.buffer = ''
				# No separator or size criteria -> send to userland what we received according to the udp stack
				self.logReceivedPayload("UDP data", msg, "%s:%s" % addr)
				self.triEnqueueMsg(msg, "%s:%s" % addr)


class PollingThread(threading.Thread):
	"""
	This is a worker thread that pools all existing
	connections, based on their sockets.
	It also waits for incoming connections on listening sockets.
	
	These sockets are extracted from the probe when needed, that's why
	the probe implements the following interface:
		_getListeningSockets()
		_getActiveSockets()
		_disconnect(addr)
		_registerIncomingConnection(sock, addr)
	"""
	def __init__(self, probe):
		threading.Thread.__init__(self)
		self._probe = probe
		self._stopEvent = threading.Event()
	
	def stop(self):
		self._stopEvent.set()
		self.join()
	
	def run(self):
		# Main poll loop
		while not self._stopEvent.isSet():
			listening = self._probe._getListeningSockets()
			active = self._probe._getActiveSockets()
			rset = listening + active
			
			try:
				r, w, e = select.select(rset, [], [], 0.001)
				for s in r:
					try:
						localaddr = s.getsockname()
						(data, addr) = s.recvfrom(65535)
						self._probe.getLogger().debug("New data to read from %s" % str(addr))
						# New received message.
						self._probe._feedData(localaddr, addr, data)

					except Exception as e:
						self._probe.getLogger().warning("exception while polling active/listening sockets: %s" % str(e))
			
			except Exception as e:
				self._probe.getLogger().warning("exception while polling active/listening sockets: %s" % str(e))
				# Avoid 100% CPU usage when select() raised an error
				time.sleep(0.01)	


ProbeImplementationManager.registerProbeImplementationClass('udp', UdpProbe)
