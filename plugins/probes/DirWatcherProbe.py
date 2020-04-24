# -*- coding: utf-8 -*-
##
# This file is part of Testerman, a test automation system.
# Copyright (c) 2009 Sebastien Lefevre and other contributors
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
# Directory watcher
# Monitors a particular directory, raise events whenever new objects
# are created or deleted (and matching a regexp pattern).
#
##


import ProbeImplementationManager

import os
import re
import threading

class DirWatcherProbe(ProbeImplementationManager.ProbeImplementation):
	"""
Identification and Properties
-----------------------------

Probe Type ID: ``watcher.dir``

Properties:

None.

Overview
--------

This probe watches one or more directories locally and sends notifications whenever a new entry
whose name matches a pattern is created or removed in one of them.

You should first send a startWatchingDirs command, specifying the directories to monitor (absolute paths, wildcards accepted)
and an optional list of regular expression patterns the interesting entry names should match.
These patterns may contain named group, such as in ``r'errorlog_(?P<number>[0-9+])\.log'``.
In this example, if a file (or directory or link) matching this pattern is created or removed 
in one of the watched dirs, you will receive a notification containing the watched dir, the
complete entry name that matched the pattern, and an additional ``matched_number`` string entry containing the matched group.

For instance, if you're watching ``['/tmp', '/var/lock']`` with the patterns ``[r'(?P<application>[a-z]+)_(?P<number>[0-9+])\.log', r'(?P<application>[a-z]+).lock']``,
as soon as the file ``'/tmp/testerman_1.log'`` is created, you should expect a Notification message such as:

.. code-block:: python

  ('added', {'dir': '/tmp', 'name': 'testerman_1.log', 'mached_application': 'testerman', 'matched_number': '1'})

When the file ``'/var/lock/testerman.lock'`` is removed, expect:

.. code-block:: python

  ('removed', {'dir': '/var/lock', 'name': 'testerman.lock', 'mached_application': 'testerman'})

On start watching, the probe checks for changes in the monitored dirss each second (by default). The interval
between two checks can be configured via the ``interval`` startWatchingFiles field. The probe is aware of reset/recreated
or new born dirs (when monitoring a dir that has not been created yet). Be aware that you may miss notifications
if some files are created/deleted faster than the interval allows to detect.

When you do not need to watch these dirs any more, send a stopWatchingDirs command. 

The probe automatically stops watching dirs on unmap and when the current test case is over. 

If you send a startWatchingDirs command while the probe is already in watching mode, the monitoring is restarted
with the new watching parameters.

The ``patterns`` startWatchingDirs field may contain several regular expression. Only the first one that matches new lines in watched files
is used to generate ``matched_*`` notification fields.

Possible use cases for this probe:

* Checking file rotation (the oldest should be removed, a new one should be added)
* Checking lock files (created on application start, deleted when stopped, etc)

Availability
~~~~~~~~~~~~

All platforms.

Dependencies
~~~~~~~~~~~~

None.

See Also
~~~~~~~~

* :doc:`ProbeFileWatcher`, a probe that watch a directory for new/removed files


TTCN-3 Types Equivalence
------------------------

The test system interface port bound to such a probe complies with the `DirWatcherPortType` port type as specified below:

::

  type union WatchingCommand
  {
    StartWatchingDir startWatchingDirs,
    anytype           stopWatchingDirs
  }
  
  type record StartWatchingDirs
  {
    record of charstring dirs,
    record of charstring patterns optional, // defaulted to [ '.*' ]
    float interval optional, // defaulted to 1.0, in second
  }
  
  type union Notification
  {
    DirNotification added, // entry added
    DirNotification removed, // entry removed
  }
  
  type record DirNotification
  {
    charstring dir,
    charstring name, // the matched added or removed entry in dir
    charstring matched_* optional, // matched groups, if defined in patterns
  }
  
  type port DirWatcherPortType message
  {
    in  WatchingCommand;
    out Notification;
  }

	"""
	def __init__(self):
		ProbeImplementationManager.ProbeImplementation.__init__(self)
		self._mutex = threading.RLock()
		self._watchingThread = None

	def _lock(self):
		self._mutex.acquire()
	
	def _unlock(self):
		self._mutex.release()
	
	def onTriMap(self):
		self.stopWatching()
	
	def onTriUnmap(self):
		self.stopWatching()
	
	def onTriExecuteTestCase(self):
		self.stopWatching()
	
	def onTriSAReset(self):
		self.stopWatching()

	def onTriSend(self, message, sutAddress):
		(cmd, args) = message
		if cmd == 'startWatchingDirs':
			self._checkArgs(args, [ ('dirs', None), ('interval', 1.0), ('patterns', [ r'.*' ])] )
			compiledPatterns = [ re.compile(x) for x in args['patterns']]
			self.startWatching(dirs = args['dirs'], interval = args['interval'], patterns = compiledPatterns)
		elif cmd == 'stopWatchingDirs':
			self.stopWatching()
		else:
			raise ProbeImplementationManager.ProbeException("Invalid message format (%s)" % cmd)
	
	def startWatching(self, dirs, interval, patterns):
		self.stopWatching()
		self._lock()
		self._watchingThread = WatchingThread(self, dirs, interval, patterns)
		self._watchingThread.start()
		self._unlock()
	
	def stopWatching(self):
		self._lock()
		t = self._watchingThread
		self._watchingThread = None
		self._unlock()
		if t:
			t.stop()

class WatchingThread(threading.Thread):
	def __init__(self, probe, dirs, interval, patterns):
		threading.Thread.__init__(self)
		self._probe = probe
		self._stopEvent = threading.Event()
		self._dirs = dirs
		self._interval = interval
		self._patterns = patterns
		#: last dir info indexed by absolute directory path
		self._watchedDirs = {}
	
	def run(self):
		self._probe.getLogger().debug("Starting watching dirs %s with %s every %ss" % (self._dirs, self._patterns, self._interval))
		self._watchedDirs = {}
		while not self._stopEvent.isSet():
			try:
				for directory in self._dirs:
					try:
						self._checkDir(directory)
					except Exception as e:
						self._probe.getLogger().debug("Unable to watch directory %s: %s" % (directory, str(e)))
			except Exception as e:
				self._probe.getLogger().debug("Error while watching directories: %s" % str(e))
			self._stopEvent.wait(self._interval)
	
	def stop(self):
		self._stopEvent.set()
		self.join()
		self._probe.getLogger().debug("Watching thread stopped")
	
	def _checkDir(self, directory):
		if not self._watchedDirs.has_key(directory):
			# First look at the dir. Take a snapshot as a first reference
			e = os.listdir(directory)
			e.sort()
			self._watchedDirs[directory] = e
		
		ref = self._watchedDirs[directory]
		current = os.listdir(directory)
		current.sort()
		self._watchedDirs[directory] = current
		
		# Let's compare current and ref list of entries
		# (NB: they are already ordered)
		(added, removed) = _compareLists(current, ref)
		
		for (label, l) in [ ('added', added), ('removed', removed) ]:
			for entryname in l:
				for pattern in self._patterns:
					m = pattern.match(entryname)
					if m:
						attr = { 'dir': directory, 'name': entryname }
						for k, v in m.groupdict().items():
							attr['matched_%s' % k] = v
						event = (label, attr)
						self._probe.triEnqueueMsg(event)			
						# A name can be matched only once.
						break
					# else no match

		

def _compareLists(current, ref):
	"""
	Compares two lists current and ref.
	Returns a couple (a, b) where a is the list
	of added elements (elements in current and not in ref)
	and b is the list of removed elements
	(elements not in current but in ref)

	current and ref are assumed to be ordered and contains unique elements.
	
	Complexity: n+m
	"""
	# current and ref index
	ci = 0
	ri = 0
	added = []
	removed = []
	while ci < len(current) and ri < len(ref):
		# current and ref element
		ce = current[ci]
		re = ref[ri]
		if ce == re:
			ci += 1
			ri += 1
		elif ce < re:
			# ce is new (unexpected)
			ci += 1
			added.append(ce)
		else:
			# re is missing (deleted)
			ri += 1
			removed.append(re)
		
	if ci < len(current):
		# We did not consume our list: all additional entries
		# are new
		for a in current[ci:]:
			added.append(a)
	elif ri < len(ref):
		# missing entries in current: removed entries
		for a in ref[ri:]:
			removed.append(a)

	return (added, removed)
	

def _compareLists2(current, ref):
	"""
	Naive implementation
	Complexity: 2n*m
	"""
	added = [x for x in current if x not in ref]
	removed = [x for x in ref if x not in current]
	return (added, removed)

def _compareLists3(current, ref):
	"""
	Naive implementation
	"""
	added = filter(lambda x: x not in ref, current)
	removed = filter(lambda x: x not in current, ref)
	return (added, removed)

if __name__ == '__main__':
	assert(_compareLists([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6]) == ([], []))
	assert(_compareLists([1, 2, 5, 6], [1, 2, 3, 4, 5, 6]) == ([], [3, 4]))
	assert(_compareLists([1, 2, 3, 4, 5, 6], [1, 2, 5, 6]) == ([3, 4], []))
	assert(_compareLists([1, 2, 3, 4, 5, 6], [2, 3, 5, 6]) == ([1, 4], []))
	assert(_compareLists([4, 5, 6], [1, 2, 3, 4, 5, 6]) == ([], [1, 2, 3]))
	assert(_compareLists([1, 3, 4, 5, 6], [1, 2, 3, 4, 7]) == ([5, 6], [2, 7]))

	assert(_compareLists2([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6]) == ([], []))
	assert(_compareLists2([1, 2, 5, 6], [1, 2, 3, 4, 5, 6]) == ([], [3, 4]))
	assert(_compareLists2([1, 2, 3, 4, 5, 6], [1, 2, 5, 6]) == ([3, 4], []))
	assert(_compareLists2([1, 2, 3, 4, 5, 6], [2, 3, 5, 6]) == ([1, 4], []))
	assert(_compareLists2([4, 5, 6], [1, 2, 3, 4, 5, 6]) == ([], [1, 2, 3]))
	assert(_compareLists2([1, 3, 4, 5, 6], [1, 2, 3, 4, 7]) == ([5, 6], [2, 7]))
	
	import random
	import time
	l = 10000
	l1 = [random.randrange(0, l * 10) for x in range(0, l)]
	l2 = [random.randrange(0, l * 10) for x in range(0, l)]
	l1.sort()
	l2.sort()
	
	for f in (_compareLists, _compareLists2, _compareLists3):
		start = time.time()
		f(l1, l2)
		stop = time.time()
		print "%s: %s" % (f, stop-start)
	
	
else: 
	ProbeImplementationManager.registerProbeImplementationClass("watcher.dir", DirWatcherProbe)
