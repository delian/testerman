# -*- coding: utf-8 -*-
##
# This file is part of Testerman, a test automation system.
# Copyright (c) 2008,2009,2010 Sebastien Lefevre and other contributors
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
# Testerman default TTCN-3 Adapter.
# The main module that provides ATS with entry points to
# TTCN-3 logic.
#
#
# Usable, stable API for ATS:
# - only function names in lower_case
# - and not starting with a _
##

import TestermanSA
import TestermanPA
import TestermanCD
from TestermanTCI import *
import TestermanTCI

import binascii
import random
import re
import threading
import time
import os
import select

# 1.1:
# - added control:bind()
# 1.2:
# - added TestCase.stop_ats_on_testcase_failure(stop = True),
# - added control:stop_testcase_on_failure(stop = True)
# 1.3: 
# - added set_(*args)
API_VERSION = "1.3"

################################################################################
# Some general functions
# (non-TTCN3-related)
################################################################################

VERDICT_PASS = 'pass'
VERDICT_FAIL = 'fail'
VERDICT_ERROR = 'error'
VERDICT_INCONC = 'inconc'
VERDICT_NONE = 'none'

pass_ = VERDICT_PASS
fail_ = VERDICT_FAIL
error_ = VERDICT_ERROR
inconc_ = VERDICT_INCONC
none_ = VERDICT_NONE

# Public aliases for userland modules 
PASS = VERDICT_PASS
FAIL = VERDICT_FAIL
ERROR = VERDICT_ERROR
INCONC = VERDICT_INCONC
NONE = VERDICT_NONE

_GeneratorBaseId = 0
_GeneratorBaseIdMutex = threading.RLock()

def _getNewId():
	"""
	Generates a new unique ID.

	@rtype: int
	@returns: a new unique ID
	"""
	global _GeneratorBaseId
	_GeneratorBaseIdMutex.acquire()
	_GeneratorBaseId += 1
	ret = _GeneratorBaseId
	_GeneratorBaseIdMutex.release()
	return ret

# docstring trimmer - from PEP 257 sample code
def trim(docstring):
	if not docstring:
		return u''
	docstring = docstring.decode('utf-8')
	maxint = 2147483647
	# Convert tabs to spaces (following the normal Python rules)
	# and split into a list of lines:
	lines = docstring.expandtabs().splitlines()
	# Determine minimum indentation (first line doesn't count):
	indent = maxint
	for line in lines[1:]:
		stripped = line.lstrip()
		if stripped:
			indent = min(indent, len(line) - len(stripped))
	# Remove indentation (first line is special):
	trimmed = [lines[0].strip()]
	if indent < maxint:
		for line in lines[1:]:
			trimmed.append(line[indent:].rstrip())
	# Strip off trailing and leading blank lines:
	while trimmed and not trimmed[-1]:
		trimmed.pop()
	while trimmed and not trimmed[0]:
		trimmed.pop(0)
	# Return a single string:
	return u'\n'.join(trimmed)

	
# Contexts are general containers similar to TLS (Thread Local Storages).
# We don't use Python 2.4 TLS because they may evolve to something else than
# thread-based once node-based PTC execution is available.
_ContextMap = {} # a list of TLS, per thread ID
_ContextMapMutex = threading.RLock()

class TestermanContext:
	"""
	A Context store several info about the associated timers,
	test component, test case it belongs.
	Well, this is the local test component context at any time.
	
	TODO: this context may be distributed on any TestermanNode.
	"""
	def __init__(self):
		# Current timers
		self._timers = []
		# Current Test Component (a PTC or the MTC)
		self._tc = None
		# Current Test Case
		self._testcase = None
		# Current matched values for value()
		self._values = {}
		# Current matched senders for sender()
		self._senders = {}
		# Current activated default alternatives
		self._defaultAlternatives = []
		self._defaultAltsteps = {}
		# The pipe used for system queue notifications in alt()
		self._systemQueueNotifier = None
		self._systemQueueNotifierUserCount = 0 # smart counter, multiple nested alt() can use the same notifier
	
	def getValues(self):
		return self._values
	
	def getValue(self, name):
		return self._values.get(name, None)
	
	def setValue(self, name, value):
		self._values[name] = value
	
	def getSender(self, name):
		return self._senders.get(name, None)
	
	def setSender(self, name, sender):
		self._senders[name] = sender
	
	def getTc(self):
		return self._tc
	
	def setTc(self, tc):
		self._tc = tc
	
	def getTestCase(self):
		return self._testcase
	
	def setTestCase(self, testcase):
		self._testcase = testcase

	def addDefaultAltstep(self, altstep):
		altstepReference = "default_altstep_%s" % _getNewId()
		self._defaultAltsteps[altstepReference] = altstep
		# FIXME: need a real altstep-branch implementation.
		# For now, we fallback to alternatives, which can be OK
		# for activate(), but not sufficient for real altstep support in alt(),
		# since we may execute a code block after the altstep already executed
		# something it brings.
		for alternative in altstep:
			self._defaultAlternatives.append(alternative)
		TestermanTCI.logInternal("Activated default altstep %s" % altstepReference)
		return altstepReference

	def removeDefaultAltstep(self, ref):
		if not ref in self._defaultAltsteps:
			TestermanTCI.logInternal("Unable to deactivate altstep %s: not activated" % ref)
			return False
		altstep = self._defaultAltsteps[ref]
		for alternative in altstep:
			# This 'if' should be useless.
			if alternative in self._defaultAlternatives:
				self._defaultAlternatives.remove(alternative)
		TestermanTCI.logInternal("Default altstep %s deactivated" % ref)
		return True
	
	def getDefaultAlternatives(self):
		return self._defaultAlternatives
	
	def registerTimer(self, timer):
		self._timers.append(timer)
	
	def unregisterTimer(self, timer):
		if timer in self._timers:
			self._timers.remove(timer)
	
	def getSystemQueueNotifier(self):
		if not self._systemQueueNotifier:
			self._systemQueueNotifier = os.pipe()
			self._systemQueueNotifierUserCount = 0
		self._systemQueueNotifierUserCount += 1
		return self._systemQueueNotifier
	
	def cleanSystemQueueNotifier(self, force = False):
		self._systemQueueNotifierUserCount -= 1
		if (force or self._systemQueueNotifierUserCount <= 0) and self._systemQueueNotifier:
			try:
				os.close(self._systemQueueNotifier[0])
				os.close(self._systemQueueNotifier[1])
			except:
				pass
			self._systemQueueNotifierUserCount = 0
			self._systemQueueNotifier = None
			logInternal("tc %s does not use the system queue notifier any more - cleaned up" % self._tc)
	
def getLocalContext():
	"""
	Returns the current TC context.
	Creates a new one if it does not exist.
	Currently, "current" means "in the same thread", since
	we have one thread per TC. 
	Once TC are distributed over multiple TE nodes, the current
	context identification will be slightly more complex.
	
	But for now, this is basically a TLS.
	
	@rtype: TestermanContext object
	@returns: the current TC context.
	"""
	global _ContextMap, _ContextMapMutex
	_ContextMapMutex.acquire()
	if _ContextMap.has_key(threading.currentThread()):
		context = _ContextMap[threading.currentThread()]
	else:
		context = TestermanContext()
		_ContextMap[threading.currentThread()] = context
	_ContextMapMutex.release()
	return context

def _stopAllTimers():
	"""
	Stops all registered timers.
	(to call at the end of testcases)
	"""
	_ContextMapMutex.acquire()
	for thr, context in _ContextMap.items():
		for timer in context._timers:
			timer.stop()
	_ContextMapMutex.release()

def _clearLocalContexts():
	"""
	Clears the existing local contexts.
	"""
	_ContextMapMutex.acquire()
	for context in _ContextMap.values():
		context.cleanSystemQueueNotifier(force = True)
	_ContextMap.clear()
	_ContextMapMutex.release()


class _BranchCondition:
	"""
	This class represents a branch condition in an alternative.
	"""
	def __init__(self, port, template = None, value = None, sender = None, from_ = None):
		self.port = port
		self.template = template
		self.value = value
		self.sender = sender
		self.from_ = from_

################################################################################
# ATS Context: ATS-wide 
################################################################################

################################################################################
# Some tools: Variable & StateManager (to implement alt-based state machines)
################################################################################

class StateManager:
	"""
	This object is a convenience object to:
	- set a value from a lambda (within a alt()) that can be retrieve
	from outside
	- as a side effect, enables to build state machines using alt() only
	(or almost).
	
	Ex:
	s = StateManager('idle')
	
	alt([
		[ lambda: s.get() == 'idle':
			control.RECEIVE(templateNewCall()),
			lambda: s.set('ringing'),
		],
		...
	])
	"""
	def __init__(self, state = None):
		self._state = state
	def get(self):
		return self._state
	def set(self, state):
		self._state = state

class Variable(StateManager):
	"""
	Alias to render the StateManager a general-purpose
	variable that can be set in alt/lambda.
	"""
	pass


################################################################################
# Exceptions
################################################################################

# Implementation exception
class TestermanException(Exception): pass

# Control-oriented exceptions
class TestermanKillException(TestermanException): pass

class TestermanStopException(TestermanException):
	def __init__(self, retcode = None):
		"""
		Retcode is used as a return code for the ATS.
		"""
		self.retcode = retcode

class TestermanCancelException(TestermanException): pass

# Exception for TTCN-3 related problems
class TestermanTtcn3Exception(TestermanException): pass


################################################################################
# TTCN-3 Timers
################################################################################

class Timer:
	"""
	Almost straightforward implementation of TTCN-3 Timers.
	Same API.
	
	The actual timer low-level implementation lies in TestermanPA.
	"""
	def __init__(self, duration = None, name = None):
		self._name = name
		self._timerId = None
		self._defaultDuration = duration
		self._TIMEOUT_EVENT = { 'event': 'timeout', 'timer': self }
		if self._name is None:
			self._name = "timer_%d" % _getNewId()
		
		self.TIMEOUT = _BranchCondition(_getSystemQueue(), self._TIMEOUT_EVENT)
		getLocalContext().registerTimer(self)
		self._tc = getLocalContext().getTc()

		logInternal("%s created" % str(self))
	
	def __str__(self):
		return self._name
	
	def _onTimeout(self):
		self._timerId = None
		logTimerExpiry(tc = str(self._tc), id_ = str(self))
		# we post a message into the system component special port
		_postSystemEvent(self._TIMEOUT_EVENT, self)
		getLocalContext().unregisterTimer(self)

	# TTCN-3 compliant interface

	def start(self, duration = None):
		"""
		Starts the timer, with its default duration is not set here.
		@type  duration: float, or None
		@param duration: the timer duration, in s (if provided)
		"""
		if self._timerId:
			self.stop()
			
		if duration is None:
			duration = self._defaultDuration
		
		if duration is None:
			raise TestermanTtcn3Exception("No duration set for this timer")

		# We remove any TIMEOUT event that may be in the system queue for this timer,
		# so that a previous timer.TIMEOUT / timer.timeout() does not match after a restart()
		# In other words, the state "timeout" is no longer valid for this timer.
		_removeSystemEvent(self._TIMEOUT_EVENT, self)

		self._timerId = _getNewId()
		_registerTimer(self._timerId, self)
		TestermanPA.triStartTimer(self._timerId, duration)

		logTimerStarted(tc = str(self._tc), id_ = str(self), duration = duration)

	def stop(self):
		"""
		Stops the timer. Does nothing if it was not running.
		"""
		if self._timerId:
			TestermanPA.triStopTimer(self._timerId)
			_unregisterTimer(self._timerId)
			self._timerId = None
			logTimerStopped(tc = str(self._tc), id_ = str(self), runningTime = 0.0)

	def running(self):
		"""
		@rtype: bool
		@returns: True if the timer is running, False otherwise
		"""
		return TestermanPA.triTimerRunning(self._timerId)

	def timeout(self):
		"""
		Only returns when the timer expires.
		Immediately returns if the timer is not started.
		"""
		if self._timerId:
			alt([[self.TIMEOUT, lambda: RETURN]])
		else:
			return

	def read(self):
		"""
		Returns the number of s (decimal) since last start, and 0 if not started.
		@rtype: float
		@returns: running duration
		"""
		# Efficient way:
		#if self._timerId:
		#	return (time.time() - self.startTime)
		#return 0
		# TTCN3 compliant way
		return TestermanPA.triReadTimer(self._timerId)
	
# Internal Timer management and TRI associations
_TimersLock = threading.RLock()
_Timers = {}

def _registerTimer(timerId, timer):
	_TimersLock.acquire()
	_Timers[timerId] = timer
	_TimersLock.release()

def _unregisterTimer(timerId):
	_TimersLock.acquire()
	if _Timers.has_key(timerId):
		del _Timers[timerId]
	_TimersLock.release()

def triTimeout(timerId):
	timer = None
	_TimersLock.acquire()
	if _Timers.has_key(timerId):
		timer = _Timers[timerId]
		del _Timers[timerId]	
	_TimersLock.release()
	if timer:
		timer._onTimeout()


################################################################################
# TTCN-3 Test Component (TC)
################################################################################

class TestComponent:
	"""
	Implements most of the TestComponent TTCN-3 interface.
	"""
	# TC states
	STATE_INACTIVE = 0
	STATE_RUNNING = 1
	STATE_KILLED = 2
	STATE_STOPPED = 3

	# Static events - emitted conditionally on done, killed
	_ALL_DONE_EVENT = { 'event': 'all.c.done' }
	_ALL_KILLED_EVENT = { 'event': 'all.c.killed' }

	_ANY_DONE_EVENT = { 'event': 'any.c.done' }
	_ANY_KILLED_EVENT = { 'event': 'any.c.killed' }
	
	def __init__(self, name = None, alive = False):
		"""
		Creates a new Test Component (TC), that is theorically
		suitable for MTC or PTC.
		
		@type  name: string, or None
		@param name: the name of the component.
		@type  alive: bool
		@param alive: TTCN-3 alive parameter.
		"""
		self._name = name
		if not self._name:
			self._name = "tc_%d" % _getNewId()

		self._mutex = threading.RLock()

		# exposed Ports, indexed by their name (string)
		self._ports = {}

		# The parent testcase (also present in current local context(), so - useful ?)
		self._testcase = None

		# Taints this TC as MTC or not
		self._mtc = False

		# (P)TC state
		self._state = self.STATE_INACTIVE
		# (P)TC aliveness
		self._alive = alive
		# PTC local verdict (not used for MTC for now...)
		self._verdict = VERDICT_NONE
		
		# Special internal events used by stop() and kill()
		self._STOP_COMMAND = { 'event': 'stop_ptc', 'ptc': self }
		self._KILL_COMMAND = { 'event': 'kill_ptc', 'ptc': self }
		
		# The event fired (through the system queue) when the TC is done()
		self._DONE_EVENT = { 'event': 'done', 'ptc': self }
		# ... and when it's killed()
		self._KILLED_EVENT = { 'event': 'killed', 'ptc': self }
		# The associated branch conditions to use in alt
		self.DONE = _BranchCondition(_getSystemQueue(), self._DONE_EVENT)
		self.KILLED = _BranchCondition(_getSystemQueue(), self._KILLED_EVENT)
		
		# Filled when executing a behaviour
		# Enables map/unmap operations from a PTC
		self.system = None
		
		logTestComponentCreated(id_ = str(self))

	def _lock(self):
		self._mutex.acquire()
	
	def _unlock(self):
		self._mutex.release()
	
	def _setState(self, state):
		self._lock()
		self._state = state
		self._unlock()
		logInternal("%s switched its state to %s" % (str(self), str(state)))
	
	def _getState(self):
		self._lock()
		state = self._state
		self._unlock()
		return state

	def _finalize(self):
		"""
		Prepares the TC for discarding: purge all port queues.
		"""
		for port in self._ports.values():
			logInternal("Finalizing port %s" % port)
			port.stop()
			port._finalize()
	
	def _makeMtc(self, testcase):
		"""
		Flags this TC as being the MTC for the given testcase.
		To call when creating the MTC, i.e. when executing the testcase.
		"""
		self._mtc = True
		self._testcase = testcase
	
	def __str__(self):
		return self._name
	
	def __getitem__(self, name):
		"""
		Returns a port instance.
		@rtype: Port instance
		@returns: the port associated to portName.
		"""
		if not self._ports.has_key(name):
			port = Port(self, name)
			self._ports[name] = port
			port.start()
		return self._ports[name]

	# Behaviour thread management

	def _start(self, behaviour, **kwargs):
		"""
		This method is called within the new PTC thread.
		Additional startup code, etc.
		"""
		logTestComponentStarted(id_ = str(self), behaviour = behaviour._name)
		try:
			# Initialize the local context associated to this PTC
			getLocalContext().setTc(self)
			getLocalContext().setTestCase(self._testcase)
			self.system = self._testcase.system
			
			# Execute the user code
			behaviour._execute(**kwargs)

			self._doStop("PTC %s ended normally" % str(self)) # Non-alive components are automatically killed by a stop.
			
		except TestermanStopException:
			self._doStop("PTC %s stopped explicitly" % str(self))

		except TestermanKillException:
			# In this special case, we don't update the testcase verdict (violent death)
			self._doStop("PTC %s killed" % str(self), forward_verdict = False)
			self._doKill()

		except Exception:
			# Non-control exception.
			self._setverdict(VERDICT_ERROR)
			logUser(tc = str(self), message = "PTC %s stopped on error:\n%s" % (str(self), getBacktrace()))
			self._doStop("PTC %s stopped on error" % str(self))
			# Kill it
			self._doKill()

	def _setverdict(self, verdict):
		"""
		Updates the local verdict (may be the testcase verdict if the tc is the mtc)

		TTCN-3 overwriting rules:
		fail > [ pass, inconc, none ]
		pass > none
		inconc > pass, none
		
		to sum it up: fail > inconc > pass.
		
		'error' overwrites them all.
		
		@type  verdict: string in [ "none", "pass", "fail", "inconc", "error" ]
		@param verdict: the new verdict
		"""
		updated = False
		self._lock()
		
		if verdict == VERDICT_ERROR and self._verdict != VERDICT_ERROR:
			self._verdict = verdict
			updated = True
		elif verdict == VERDICT_FAIL and self._verdict in [VERDICT_NONE, VERDICT_PASS, VERDICT_INCONC]:
			self._verdict = verdict
			updated = True
		elif verdict == VERDICT_INCONC and self._verdict in [VERDICT_NONE, VERDICT_PASS]:
			self._verdict = verdict
			updated = True
		elif verdict == VERDICT_PASS and self._verdict in [VERDICT_NONE]:
			self._verdict = verdict
			updated = True
			
		self._unlock()

		# Should we log the setverdict event if not actually updated ?
		# if updated:
		logVerdictUpdated(tc = str(self), verdict = self._verdict)
		
		# Auto-stop management
		if self._mtc:
			if self._testcase._stopOnFailure and verdict in [VERDICT_FAIL]:
				logInternal("Stopping TestCase on failure (autostop is set)")
				stop()

	def _getverdict(self):
		"""
		Returns the current local verdict (may be the testcase verdict if the tc is mtc)
		@rtype: string in [ "none", "pass", "fail", "inconc", "error" ]
		@returns: the current verdict
		"""
		self._lock()
		ret = self._verdict
		self._unlock()
		return ret

	def _updateTestCaseVerdict(self):
		"""
		Pushes the local verdict to the MTC verdict.
		"""
		self._testcase._mtc._setverdict(self._verdict)

	def _doStop(self, message, forward_verdict = True):
		"""
		Sets to stopped state, emit signals, additional transitions to killed, etc - if needed
		
		If forward_verdict is set, updates the test case (mtc) verdict.
		"""
		logTestComponentStopped(id_ = str(self), verdict = self._verdict, message = message)
		if forward_verdict:
			self._updateTestCaseVerdict()
		
		if not self._alive:
			if not self._getState() == self.STATE_KILLED:
				# According to TTCN-3, a stopped non-alive component is a killed component.
				# Direct transition to KILLED. Just emit the DONE event in the process.
				logTestComponentKilled(id_ = str(self), message = "PTC %s, non-alive, automatically killed after stop" % str(self))
				self._setState(self.STATE_KILLED)
				self._finalize()
				self._emitDoneEvent()
				self._emitKilledEvent()
		else:
			if not self._getState() == self.STATE_STOPPED:
				# Alive components
				self._setState(self.STATE_STOPPED)
				self._emitDoneEvent()

	def _emitDoneEvent(self):
		_postSystemEvent(self._DONE_EVENT, self)
		# If we are the last DONE, emit a all_component._DONE_EVENT too
		for ptc in self._testcase._ptcs:
			if ptc.alive():
				return
		# OK, Last one.
		_postSystemEvent(self._ALL_DONE_EVENT, None)
	
	def _emitKilledEvent(self):
		_postSystemEvent(self._KILLED_EVENT, self)
		# If we are the last DONE, emit a all_component._KILLED_EVENT too
		for ptc in self._testcase._ptcs:
			if ptc.alive():
				return
		# OK, Last one.
		_postSystemEvent(self._ALL_KILLED_EVENT, None)
	
	def _doKill(self):
		"""
		Sets to killed state, emits killed signal, etc - if needed
		"""
		if self._getState() != self.STATE_KILLED:
			logTestComponentKilled(id_ = str(self), message = "killed")
			self._setState(self.STATE_KILLED)
			self._finalize()
			self._emitKilledEvent()

	def _raiseStopException(self):
		"""
		Wrapper function due to the fact that we cannot raise an exception in a lambda.
		"""
		raise TestermanStopException()
	
	def _raiseKillException(self):
		"""
		Wrapper function due to the fact that we cannot raise an exception in a lambda.
		"""
		raise TestermanKillException()

	def _getAltPrefix(self):
		"""
		Provides some additional messages to catch in a alt for internal reasons.
		In this case, this is to ensure that stop(), kill() generated events
		are correctly taken into account in any alt involving this TC,
		making sure that TC.alt()s are interruptible.
		"""
		return [
				[ _BranchCondition(_getSystemQueue(), self._STOP_COMMAND), self._raiseStopException ],
				[ _BranchCondition(_getSystemQueue(), self._KILL_COMMAND), self._raiseKillException ],
		]

	# TTCN-3 compliant interface

	def _log(self, msg):
		logUser(tc = unicode(self), message = unicode(msg))

	def alive(self):
		"""
		TTCN-3 alive():
		For alive TC:
		Returns True is the TC is inactive, running, or stopped, 
		or false if killed.
		For non-alive TC:
		Returns True if the TC is inactive or running, False otherwise.

		@rtype: bool
		@returns: True if the component is alive, False otherwise.
		"""
		if self._mtc:
			return True

		if self._alive:
			return not (self._getState() == self.STATE_KILLED)

		return (self._getState() in [self.STATE_INACTIVE, self.STATE_RUNNING])

	def running(self):
		"""
		TTCN-3 running()
		Returns true if the TC is executing a behaviour.

		@rtype: bool
		@returns: True if the TC is running, False otherwise.
		"""
		if self._mtc:
			return True
		return (self._getState() == self.STATE_RUNNING)

	def start(self, behaviour, **kwargs):
		"""
		TTCN-3 start()
		Binds and runs a behaviour to a TC. 
		
		Starts the TC with behaviour, whose parameters are keyword args
		(passed to the user-implemented body()).
		
		Implementation note:
		normally we should go through the Component Handler to execute the behaviour
		on a possibly distributed PTC. 
		For now, this is just a (local) thread.
		
		@type  behaviour: a Behaviour object
		@param behaviour: the behaviour to bind to the PTC
		"""
		if self._mtc:
			# ignore start() on MTC
			return

		if not self.alive():
			raise TestermanTtcn3Exception("Invalid operation: you cannot start a behaviour on a PTC which is not alive anymore.")

		if self._getState() == self.STATE_RUNNING:
			raise TestermanTtcn3Exception("Invalid operation: you cannot start a behaviour on a running PTC.")

		# We remove any DONE event that may be in the system queue for this PTC,
		# so that a previous ptc.DONE / ptc.done() does not match after a restart()
		# In other words, the state "done" is no longer valid for this PTC.
		_removeSystemEvent(self._DONE_EVENT, self)
		_removeSystemEvent(self._ALL_DONE_EVENT, None)

		logInternal("Starting %s..." % str(self))
		self._setState(self.STATE_RUNNING)
		# Attach the PTC to this behaviour
		behaviour._setPtc(self)
		
		behaviourThread = threading.Thread(target = self._start, args = (behaviour, ), kwargs = kwargs)
		behaviourThread.start()

	def stop(self):
		"""
		TTCN-3 stop()
		Stops the TC.

		On MTC, stops the testcase.
		On non-alive PTC, stops the PTC, which kills it.
		On alive PTC, stops the PTC. The PTC is then available for another start().
		"""
		if self._mtc:
			raise TestermanStopException()
		else:
			if self._getState() == self.STATE_RUNNING:
				logInternal("Stopping %s..." % str(self))
				# Let's post a system event to manage inter-thread communications
				_postSystemEvent(self._STOP_COMMAND, self)

	def kill(self):
		"""
		TTCN-3 kill()
		Kills a TC.
		
		Killing the MTC is equivalent to stop the testcase.
		Killing an alive-PTC makes it non-suitable for a new start().
		Supposed to free technical resources, but nothing to do in our implementation.
		"""
		if self._mtc:
			self._testcase.stop()
		else:
			if self._getState() == self.STATE_RUNNING:
				# Post our internal event to communicate with the PTC thread.
				_postSystemEvent(self._KILL_COMMAND, self)

	def done(self):
		"""
		TTCN-3 done()
		Waits for the TC termination (warning: no timeout).
		
		Only meaningful for PTC.
		
		NB: use self.DONE instead of self.done() in an alt statement.
		"""
		# Immediately returns if the TC is not running (i.e. already 'done')
		if not self._getState() == self.STATE_RUNNING:
			return
		alt([[self.DONE, lambda: RETURN]])

	def killed(self):
		"""
		TTCN-3 killed()
		Waits for the TC termination (warning: no timeout).
		
		Only meaningful for PTC.
		
		NB: use self.KILLED instead of self.killed() in an alt statement.
		"""
		# Immediately returns if the TC is not alive (i.e. already 'killed')
		if self._getState() == self.STATE_KILLED:
			return
		alt([[self.KILLED, lambda: RETURN]])


###############################################################################
# TTCN-3 Port
###############################################################################

class Port:
	"""
	TTCN-3 Port object.
	"""
	def __init__(self, tc, name = None):
		self._name = name
		if not self._name:
			self._name = "port_%d" % _getNewId()
		self._mutex = threading.RLock()

		# The internal port's message queue
		self._messageQueue = []

		# The port state. Automatically started() when accessed for the first type ( via tc[port])
		self._started = False
		# associated test component.
		self._tc = tc

		# Ports connected to this port.
		# Whenever we send a message through this port, we actually enqueue the
		# message to each connected port's internal queue
		self._connectedPorts = []
		# The test system interface we are mapped to, if any.
		# In this case, _connectedPorts shall be empty.
		self._mappedTsiPort = None
		
		# a pipe ((r, w) fds) to notify that the port has something new in it.
		# Enables to implement a poll/select on multiple ports in alt()
		self._notifier = None
	
	def getNotifierFd(self):
		"""
		Returns the (pipe) fd to watch to be notified
		of a new message on this port.
		"""
		return self._notifier[0]
	
	def _lock(self):
		self._mutex.acquire()
	
	def _unlock(self):
		self._mutex.release()

	def __str__(self):
		return "%s.%s" % (str(self._tc), self._name)
	
	def _finalize(self):
		# Disconnections, queue purge... useless since we won't use it anymore anyway.
		pass
	
	def _isMapped(self):
		if self._mappedTsiPort:
			return True
		else:
			return False
	
	def _isConnectedTo(self, port):
		return port in self._connectedPorts
	
	def _enqueue(self, message, from_):
#		logInternal("%s enqueuing message (started=%s)" % (str(self), str(self._started)))
		self._lock()
		if self._started:
			self._messageQueue.append((message, from_))
			try:
				os.write(self._notifier[1], 'r')
				logInternal("port %s: notifying a new message for reader on %s" % (self, self._notifier[0]))
			except Exception as e:
				logInternal("port %s: async notifier error %s" % (self, e))
				pass
		# else not started: not enqueueing anything.
		self._unlock()


	# TTCN-3 compliant operations
	def send(self, message, to = None):
		"""
		Sends a message to the connected ports or the mapped port.
		
		@type  message: any structure
		@param message: the message to send through this port
		@type  to: string, or component instance, or list of component instances
		@param to: an optional SUT address (string), the meaning is mapped-tsiPort-specific,
		           or a single component, or a list of target components.
		
		@rtype: bool
		@returns: True if the message has been sent (i.e. if the port has not been connected or mapped),
		          False if not (port stopped)
		"""
		logInternal("sending a message through %s" % str(self))
		if self._started:
			messageToLog = _expandTemplate(message)
			messageToSend = _encodeTemplate(message)

			# Mapped port first.
			if self._mappedTsiPort:
				logMessageSent(fromTc = str(self._tc), fromPort = self._name, toTc = "system", toPort = self._mappedTsiPort._name, message = messageToLog, address = to)
				self._mappedTsiPort.send(messageToSend, to)
			else:
				for port in self._connectedPorts:
					if not to or port._tc == to or (isinstance(to, list) and port._tc in to):
						logMessageSent(fromTc = str(self._tc), fromPort = self._name, toTc = str(port._tc), toPort = port._name, message = messageToLog, address = to)
						port._enqueue(messageToSend, self._tc)
			return True
		else:
			return False

	def receive(self, template = None, value = None, sender = None, from_ = None, timeout = None, on_timeout = None):
		"""
		Waits (blocking if timeout = None) for template to be received on this port. 
		If asValue is provided, store the received message to it.
		
		@type  template: any structure valid for a template
		@param template: the template to match
		@type  value: string
		@param value: the name of the value() variable to store the received message to.
		@type  sender: string
		@param sender: the name of the value() variable to store the sender of the message to.
		@type  from_: string
		@param from_: the SUT address to send the message shoud be received from
		@type  timeout: float
		@param timeout: if provided, the maximum time to wait for the message.
		@type  on_timeout: function
		@param on_timeout: the action to trigger on timeout, if timeout is provided.
		"""
		if self._started:
			if timeout:
				timer = Timer(timeout, name = 'implicit receive timer')
				timer.start()
				alt([
					[self.RECEIVE(template, value, sender, from_)],
					[timer.TIMEOUT,
					 lambda: on_timeout()
					]
				])
				timer.stop()
			else:
				alt([[self.RECEIVE(template, value, sender, from_)]])

	def start(self):
		"""
		Starts the port (after purging its queue)
		"""
		self._lock()
		if not self._started:
			self._messageQueue = []
			self._started = True
			try:
				self._notifier = os.pipe()
			except Exception as e:
				self._unlock()
				raise Exception("Unable to start port %s: %s" % (str(self), e))
		self._unlock()
		logInternal("%s started" % str(self))

	def stop(self):
		"""
		Stops the port, keeping it from receiving further messages.
		Current enqueue messages are kept.
		"""
		self._lock()
		if self._started:
			self._started = False
			try:
				os.close(self._notifier[0])
				os.close(self._notifier[1])
			except:
				pass
			self._notifier = None
		self._unlock()			
		logInternal("%s stopped" % str(self))

	def clear(self):
		"""
		Purges the internal queue, without stopping the port.
		"""
		self._lock()
		self._messageQueue = []
		self._unlock()
		logInternal("%s cleared" % str(self))

	def RECEIVE(self, template = None, value = None, sender = None, from_ = None):
		"""
		Returns the branch condition to use in alt()

		@type  template: any structure valid for a template
		@param template: the template to match
		@type  value: string
		@param value: the name of the value() variable to store the received message to.
		@type  sender: string
		@param sender: the name of the value() variable to store the sender of the message to.
		@type  from_: string
		@param from_: the SUT address to send the message shoud be received from
		"""
		# This is an internal branch condition representation.
		return _BranchCondition(self, template, value, sender, from_)


###############################################################################
# TTCN-3 Behaviour
###############################################################################

class Behaviour:
	"""
	The class to subclass and whose body(self, args) must be implemented
	to run as a behaviour within a PTC.
	"""
	def __init__(self):
		self._name = self.__class__.__name__
		#: associated PTC
		self._ptc = None
	
	def __str__(self):
		return self._name

	def _setPtc(self, ptc):
		self._ptc = ptc

	def __getitem__(self, name):
		"""
		Convenience/Diversion to the associated PTC ports.
		"""
		return self._ptc[name]
	
	def _log(self, msg):
		"""
		Diversion to the associated PTC log.
		"""
		self._ptc._log(msg)

	# body does not exist in the base class, but must be implemented in the user class.
	
	def _execute(self, **kwargs):
		"""
		Executes the body part.
		Or nothing if no body has been defined.
		"""
		body = getattr(self, 'body')
		if callable(body):
			body(**kwargs)
	
	def stop(self):
		"""
		TTCN-3: stop interface from within a PTC - equivalent to the stop statement. 
		"""
		stop()
		

################################################################################
# TTCN-3 Testcase
################################################################################

class TestCase:
	"""
	Main TestCase class, representing a TTCN-3 testcase.
	"""
	
	# The role the testcase is used for: preamble, postamble, or testcase/None,
	# i.e. as an actual testcase.
	_role = "testcase"
	
	def __init__(self, title = None, id_suffix = None):
		self._title = title
		if not self._title:
			self._title = ''
		self._idSuffix = id_suffix
		self._description = trim(self.__doc__)
		self._mutex = threading.RLock()
		self._name = self.__class__.__name__
		# This is a list of the ptc created by/within this testcase.
		self._ptcs = []
		self._stopOnFailure = False
		logTestcaseCreated(str(self), role = self._role)

		self._mtc = None
		self._system = None

		# Aliases, provided for convenience in user part.
		self.system = None
		self.mtc = None
		
	def __str__(self):
		"""
		Returns the testcase identifier.
		"""
		if self._idSuffix is not None:
			return '%s_%s' % (self._name, self._idSuffix)
		else:
			return self._name

	def _lock(self):
		self._mutex.acquire()
	
	def _unlock(self):
		self._mutex.release()

	def _createMtc(self):
		"""
		Creates the MTC component.
		"""
		tc = TestComponent(name = "mtc")
		tc._testcase = self
		tc._makeMtc(self)
		return tc
	
	def _finalize(self):
		"""
		Performs a test case finalization:
		- stop all PTCs
		- stop all timers
		- cleanup the system component (purge internal ports and TSI ports)
		- we should unmap all ports, though triSAReset will force it implicitly
		NB: triSAReset is called after this finalization, in execute()
		"""
		# Stops PTCs and wait for their completion.
		# 2 passes proven to be more efficient that ptc.stop() + done() in one pass
		for ptc in self._ptcs:
			ptc.stop()
		for ptc in self._ptcs:
			ptc.done()
		
		self._mtc._finalize()
			
		# Stop timers
		_stopAllTimers()

		self._system._finalize()

	def setverdict(self, verdict):
		"""
		Sets the testcase verdict.
		Provided for convenience - DEPRECATED.
		"""
		return self._mtc._setverdict(verdict)
	
	def getverdict(self):
		"""
		Returns the current testcase verdict.
		Provided for convenience - DEPRECATED.
		"""
		return self._mtc._getverdict()

	def set_description(self, description):
		"""
		Sets an extended, possibly dynamic description for the testcase.
		By default, the description is the testcase autodoc.
		@type  description: unicode/string
		@param description: the description
		"""
		self._description = description
	
	def create(self, name = None, alive = False):
		"""
		Creates and returns a (P)TC.
		The resulting TC will be associated to the testcase.
		
		@type  name: string
		@param name: the name of the PTC. If None, the name is automaticall generated as tc_%d.
		@type  alive: bool
		@param alive: TTCN-3 TC alive parameter.
		
		@rtypes: TestComponent
		@returns: a new TestComponent.
		"""
		tc = TestComponent(name, alive)
		tc._testcase = self
		self._ptcs.append(tc)
		# Remove state events that are no longuer relevant - "ALL_DONE_EVENT" is still, however.
		_removeSystemEvent(TestComponent._ALL_KILLED_EVENT, None)
		return tc
	
	# NB: No default implementation of body() since its signature would not be
	# matched by most testcases, anyway.
	# def body(self, ...)
	
	def execute(self, **kwargs):
		try:
			self._mtc = self._createMtc()
			self._system = SystemTestComponent()
			# Aliases kept for compatibility
			self.mtc = self._mtc
			self.system = self._system
			# Let's set global variables (system and mtc - only for convenience)
			# NB: won't work. Not in the same module as the TE...
#			globals()['mtc'] = self._mtc
#			globals()['system'] = self._system
			
			# Let's set the global context
			getLocalContext().setTc(self._mtc)
			getLocalContext().setTestCase(self)

			# Make sure no old system messages remain in queue
			_resetSystemQueue()
		
			logTestcaseStarted(str(self), title = self._title)

			# Install a default Test Adapter configuration if none is already set
			if not _getCurrentTestAdapterConfiguration():
				TestermanTCI.logInternal("Using default test adapter configuration")
				with_test_adapter_configuration(DEFAULT_TEST_ADAPTER_CONFIGURATION_NAME)

			# Initialize static connections
			# (and testerman bindings according to the system configuration)
			if _getCurrentTestAdapterConfiguration():
				tsiPortList = _getCurrentTestAdapterConfiguration()._getTsiPortList()
			else:
				tsiPortList = []
			TestermanSA.triExecuteTestCase(str(self), tsiPortList)

			# Call the body			
			body = getattr(self, 'body')
			if callable(body):
				body(**kwargs)
			
		except TestermanStopException:
			logInternal("Testcase explicitely stop()'d")

		except Exception:
			self._mtc._setverdict(VERDICT_ERROR)
			log("Testcase stopped on error:\n%s" % getBacktrace())
		
		try:
			self._finalize()
		except Exception:
			# Nothing particular to do in case of an error here...
			logInternal("Exception while finalizing testcase:\n%s" % getBacktrace())

		# Final static connection reset
		TestermanSA.triSAReset()

		# Reset the system queue (to do on start only ?)
		_resetSystemQueue()

		# Register the execution status in the ATS map
		_AtsResults.append(dict(testcase_id = str(self), verdict = self._mtc._verdict))		
		logTestcaseStopped(str(self), verdict = self._mtc._verdict, description = self._description)

		# Make sure we clean the local contexts
		_clearLocalContexts()

		# Now check if we can continue or stop here if the ATS has been cancelled
		if _isAtsCancelled():
			raise TestermanCancelException()

		verdict = self._mtc._getverdict()
		# Support for auto ATS stop on failure
		if _StopOnTestCaseFailure and verdict != PASS:
			logUser("Stopping ATS due to a testcase failure (autostop is set)")
			stop()
		else:
			return verdict

	def _log(self, message):
		"""
		Logging at testcase level is equivalent to logging at MTC level.
		We only support logging of simple messages.

		@type  message: unicode/string
		@param message: the message to log
		"""
		self._mtc._log(message)

	def stop(self):
		"""
		TTCN-3: the stop operation can be applied to the MTC too.
		"""
		stop()
	
	def stop_testcase_on_failure(self, stop = True):
		"""
		Set whether the TestCase should stop automatically as soon as its MTC verdict
		is set to ERROR or FAIL.
		"""
		self._stopOnFailure = stop
		


################################################################################
# Some tools: Preamble & Postamble
################################################################################

# For now, they are simple aliases to TestCase.
# However, their log and verdict interpretation will differ soon.
# (especially for result reporting: failing a preamble won't mean the testcase
# is failed)
# These aliases enables to create the correct logic in ATSes right now
# without using TestCase the way they are not meant to be used.
class Preamble(TestCase):
	_role = "preamble"

class Postamble(TestCase):
	_role = "postamble"


###############################################################################
# System TC
###############################################################################

class SystemTestComponent:
	"""
	This is a special object interfacing the TRI, via its test system interface
	ports, to the TTCN-3 userland.
	
	Basically exposes Port-compatible objects for mapping/unmapping.
	These ports should be bound to a test adapter using a TestAdaterConfiguratin.
	
	There is one and only one system TC per testcase, automatically created.
	In TTCN-3, it is created from a system type definition, defining valid port
	names and types.
	In this implementation, valid ports are the one that have been bound to a
	test adapters, but there is no formal typing, as usual.
	"""
	def __init__(self):
		self._name = "system"
		# TestSystemInterfacePorts, identified by their names (string)
		self._tsiPorts = {}
		logTestComponentCreated(id_ = str(self))
	
	def __str__(self):
		return self._name

	def __getitem__(self, name):
		"""
		Returns (and creates, if needed) a tsi port instance.
		"""
		if not self._tsiPorts.has_key(name):
			self._tsiPorts[name] = TestSystemInterfacePort(name)
		return self._tsiPorts[name]

	def _finalize(self):
		"""
		Unmap all mapped tsi ports.
		"""
		for (name, tsiPort) in self._tsiPorts.items():
			for port in [p for p in tsiPort._mappedPorts]: # copy the list, as it will be updated during unmap
				port_unmap(port, tsiPort)
	
################################################################################
# Test System Interface Port
################################################################################

class TestSystemInterfacePort:
	"""
	This Port "specialization" (although this is not a Port subclass)
	interfaces the TRI with the userland.
	"""
	def __init__(self, name):
		self._name = name
		self._mappedPorts = []

	def __str__(self):
		return "system.%s" % self._name

	def _enqueue(self, message, sutAddress):
		"""
		Forwards an incoming message (from TRI) to the ports mapped to this tsi port.
		Called by triEnqueueMsg.
		"""
		for port in self._mappedPorts:
			logMessageSent(fromTc = "system", fromPort = self._name, toTc = str(port._tc), toPort = port._name, message = message, address = sutAddress)
			port._enqueue(message, sutAddress)

	def send(self, message, sutAddress):
		"""
		Specialized reimplementation.
		Calls the tri interface.
		
		The returned status is ignored for now.
		"""
		return TestermanSA.triSend(None, self._name, sutAddress, message)


################################################################################
# TTCN-3 primitives for Testcase and Control part
################################################################################

def stop(retcode = 0):
	"""
	Stops the current testcase or ATS, depending on where the instruction appears.
	
	When executed from a testcase/behaviour, stops the testcase/behaviour
	with the last known verdict. 'code' is ignored in this case.
	
	When used from the control part, stops the ATS, setting its result code to
	code, if provided.
	
	Implementation note: implemented as an exception, leading to the expected
	behaviour depending on the context.
	
	@type  retcode: integer
	@param retcode: the return code, only valid for Control part.
	
	TODO: 
	"""
	raise TestermanStopException(retcode)

def log(msg):
	"""
	Logs a user message.
	
	SUGGESTION: should we force a log at TC level only ? (using getLocalContext()
	it is easy to do so, but it can be convenient to have 2 log 'levels':
	- one attached a a component
	- an other one (this one) attached to a testcase (or... even to the control part).
	
	@type  msg: unicode or string
	@param msg: the message to log
	"""
	tc = getLocalContext().getTc()
	if tc:
		# Logging while a test component is executing (either mtc or ptc)
		tc._log(msg)
	else:
		# control part logging
		logUser(msg)

def setverdict(verdict):
	"""
	Sets the local verdict.
	"local" means: the currently running PTC (or MTC)

	TTCN-3 overwriting rules:
	fail > [ pass, inconc, none ]
	pass > none
	inconc > pass, none

	to sum it up: fail > inconc > pass.

	'error' overwrites them all.

	@type  verdict: string in [ "none", "pass", "fail", "inconc", "error" ]
	@param verdict: the new verdict
	"""
	tc = getLocalContext().getTc()
	if tc:
		# Setting a verdict while a TC is running (good)
		tc._setverdict(verdict)
	else:
		# Control part: not goog
		raise TestermanTtcn3Exception("Setting a verdict in control part - not applicable")

def getverdict():
	"""
	Gets the local verdict.
	"local" means: the currently running PTC (or MTC)

	Returns the current local verdict (may be the testcase verdict if the tc is mtc)

	@rtype: string in [ "none", "pass", "fail", "inconc", "error" ]
	@returns: the current verdict
	"""
	tc = getLocalContext().getTc()
	if tc:
		# Setting a verdict while a TC is running (good)
		return tc._getverdict()
	else:
		# Control part: not goog
		raise TestermanTtcn3Exception("Getting a verdict in control part - not applicable")

def connect(portA, portB):
	"""
	Connects portA and portB (symmetrical connection).
	Verifies basic TTCN-3 restrictions/constraints (but none regarding typing and allowed
	messages since we don't define any typing).
	
	Connections are bi-directional.
	
	@type  portA: Port
	@param portA: one of the port to connect.
	@type  portB: Port
	@param portB: the other port to connect.
	"""
	# Does not reconnect connected ports:
	if portA._isConnectedTo(portB): # The reciprocity should be True, too (normally)
		logInternal("Multiple connection attempts between %s and %s. Discarding." % (str(portA), str(portB)))
		return

	# TTCN-3 restriction: "A port that is mapped shall not be connected"
	if portA._isMapped() or portB._isMapped():
		raise TestermanTtcn3Exception("Cannot connect %s and %s: at least one of these ports is already mapped." % (str(portA), str(portB)))

	# TTCN-3 restriction: "A port owned by a component A shall not be connected with 2 or more ports owned by the same component"
	# TTCN-3 restriction: "A port owned by a component A shall not be connected with 2 or more ports owned by a component B"
	for port in portA._connectedPorts:
		if port._tc == portB._tc:
			raise TestermanTtcn3Exception("Cannot connect %s and %s: %s is already connected to %s" % (str(portA), str(portB), str(portA), str(port)))
	for port in portB._connectedPorts:
		if port._tc == portA._tc:
			raise TestermanTtcn3Exception("Cannot connect %s and %s: %s is already connected to %s" % (str(portA), str(portB), str(portB), str(port)))

	# OK, we can connect
	portA._connectedPorts.append(portB)
	portB._connectedPorts.append(portA)

def disconnect(portA, portB):
	"""
	Disconnects portA and portB.
	Does nothing if they are not connected.
	"""
	if portA in portB._connectedPorts:
		portB._connectedPorts.remove(portA)
	if portB in portA._connectedPorts:
		portA._connectedPorts.remove(portB)

# Map[tsiPort._name] = tsiPort 
# A system-wide/global view on the current mappings. A local view is also available on each tsiPort and on each Port.
# As for timers, enables triEnqueueMsg to retrieve a TestSystemInterfacePort instance based on an id used for tri.
_TsiPorts = {}
_TsiPortsLock = threading.RLock()

def port_map(port, tsiPort):
	"""
	TTCN-3 map()
	Maps a port to a test system interface port, verifying basic
	TTCN-3 restrictions.
	
	Interfaces to the triMap TRI operation.
	
	@type  port: Port
	@param port: the port to map
	@type  tsiPort: TestSystemInterfacePort
	@param tsiPort: the test system interface to map the port to
	"""
	# TTCN-3 restriction: "A port owned by a component A can only have a one-to-one connection with the test system"
	# TTCN-3 restriction: "A port that is connected shall not be mapped"
	if port._isMapped():
		raise TestermanTtcn3Exception("Cannot map %s to %s: %s is already mapped" % (str(port), str(tsiPort), str(port)))

	# Should we use a status or directly an exception ?...
	status = TestermanSA.triMap(port, tsiPort._name)
	if status == TestermanSA.TR_Error:
		raise TestermanTtcn3Exception("Cannot map %s to %s: triMap returned TR_Error, probably a missing binding" % (str(port), str(tsiPort._name)))
	
	# TRI local association, so that triEnqueueMsg can retrieve the associated tsiPort based on its name
	# (used as a tri id)
	_TsiPortsLock.acquire()
	_TsiPorts[tsiPort._name] = tsiPort
	_TsiPortsLock.release()
	# "Local" mapping
	port._mappedTsiPort = tsiPort
	tsiPort._mappedPorts.append(port)

def port_unmap(port, tsiPort):
	"""
	TTCN-3 unmap()
	Unmaps a mapped port.
	
	Does nothing if the port was not mapped to this tsiPort.

	@type  port: Port
	@param port: the port to unmap from the tsiPort
	@type  tsiPort: TestSystemInterfacePort
	@param tsiPort: the tsi port to unmap from the tsiPort
	"""
	# TRI call
	TestermanSA.triUnmap(port, tsiPort._name)
	# System-wide de-association
	_TsiPortsLock.acquire()
	if _TsiPorts.has_key(tsiPort._name):
		# We only remove the tsiport from the tri local mapping
		# if the tsi port is not used by any other port anymore.
		# Let's check this.
		# Perform our "local" deassociation
		port._mappedTsiPort = None
		if port in tsiPort._mappedPorts: 
			tsiPort._mappedPorts.remove(port)
		if tsiPort._mappedPorts == []:
			# OK, the tsiPort is not used anymore. We can remove it.
			del _TsiPorts[tsiPort._name]
	_TsiPortsLock.release()

################################################################################
# Some built-in functions (both TTCN-3 and for convenience)
################################################################################

def octetstring(s):
	"""
	Enables to get a human-readable representation for TTCN-3 octetstring:
	'aabb00'O
	-> octetstring('aabb00')
	
	we may alias it to O('aabb00') too ?
	"""
	return binascii.unhexlify(s)

################################################################################
# alt() management
################################################################################

def _setValue(name, message):
	"""
	Called by alt() to store a message to a variable.
	"""
	getLocalContext().setValue(name, message)

def value(name):
	"""
	Gets a saved value after a match.

	In TTCN-3, this is:
		port.receive(myTemplate) -> value myValue
		log("value" & myValue)

	Testerman equivalent:
		port.receive(myTemplate, 'myValue')
		log("value" + value('myValue'))
	
	@type  name: string
	@param name: the name of the value to retrieve
	
	@rtype: object, or None
	@returns: the matched value object, if any, or None if the value was not set.
	"""
	return getLocalContext().getValue(name)

def _setSender(name, sender):
	"""
	Called by alt() to store a message to a variable.
	"""
	getLocalContext().setSender(name, sender)

def sender(name):
	"""
	Gets a saved sender after a match.
	
	In TTCN-3, this is:
		port.receive(myTemplate) -> value myValue sender mySender
		log("value" & myValue)
		log("sender" & mySender)

	Testerman equivalent:
		port.receive(myTemplate, 'myValue', 'mySender')
		log("value" + value('myValue'))
		log("sender" + value('mySender'))
	
	@type  name: string
	@param name: the name of the sender to retrieve
	
	@rtype: string, or None
	@returns: the matched value object, if any, or None if the sender was not set.
	"""
	return getLocalContext().getSender(name)

def alt(alternatives):
	"""
	TTCN-3 alt(),
	with some minor modifications to make it more convenient (supposely).
	
	TTCN-3 syntax:
	alt {
		[] port.receive(template) {
			action1;
			action2;
			...
			}
		[ x > 0 ] port.receive {
			...
			}
		[] timer.timeout {
			...
			}
	}
	
	Testerman syntax:
	alt([
		[ port.RECEIVE(template),
			lambda: action1,
			lamdda: action2,
			...
			REPEAT, # or lambda: REPEAT
		],
		[ lambda: x > 0, port.RECEIVE(),
			...
		],
		[ timer.TIMEOUT,
			...
		]
	])
	
	i.e. alt is implemented as a function whose arguments are in undefined number (can be computed at runtime
	as a list and passed as the *args), called "alternatives",
	where is alternative is a list made of:
	- an optional guard. If present, detected because the first element of the alternative is callable().
	  In TTCN-3, the guard is mandatory, even if empty (and is actually not part of the alternative itself,
	  but just precedes it (and separate them).
	- the branch condition, which is implemented by a _BranchCondition instance but
	  MUST be declared in userland through port.RECEIVE(template, ...) or timer.TIMEOUT,
	  tc.DONE, tc.KILLED, etc (since the internal representation may change)
	- the associated branch actions, as the remaining list of elements in the alternative list.
	  They must be lambda or callable() to be executed only if the branch is selected.
	
	This implementation is not TTCN-3 compliant because:
	- it does not take a snapshot at each iteration, meaning that messages may arrive on port2 just
	  before analyzing it, while something arrives on port1 just after, leading to incorrect alt branching
	  in the case of port1.RECEIVE() is before port2.RECEIVE() (in "order of appearance")
	- altstep-branches are not implemented. Only timeout-, receiving-, killed-, done- branches are.
	- there is no mechanism to trigger an exception if the alt is completely blocked.
	  As a consequence, the user must carefully design his/her alt() (especially with watchdog timers)
	  or may risk a blocking call.
	- 'else' guard is not implemented
	- 'any' is not implemented
	
	
	Additional notes:
	- alt() is also used, in this implementation, to handle internal control messages
	  posted using the system queue. This is used to match timeout-, killed-, done- branches conditions,
		and to manage basic inter-tc control communications (inter thread posting) for tc.stop() and tc.killed().
	
	
	@type  alternatives: list of [ callable, (Port, any, string), callable, callable, ... ]
	@param alternatives: a list of alternatives, containing an optional callable as a guard,
	                     then a branch condition as _BranchCondition (port, template, value, sender, from),
	                     then 0..N callable as actions to perform if the branch is selected.

	The guard is detected if the first object in the list is callable. If it is, this is a guard. If not, no guard available.
	"""
	# Algorithm:
	# 1. First, we group alternatives per port (ordered).
	# 2. Then, we look messages port by port:
	#  2.1 Pop the first message in its queue (if any)
	#  2.2 Compare it to the templates contained in its associated alternative's conditions, once we checked that the guard was satisfied
	#  2.3 If we have a template match (the first one for the list of alternatives)
	#      - select the branch: execute the associated actions. If an action evaluates to RETURN, stop executing further actions,
	#        and leave the alt. If one evaluates to REPEAT, stop executing further actions, and repeat the alt() from start.
	#        If we have no other actions to execute, leave the alt.
	#      If this is a mismatch, do nothing, just compare to the next alternative's conditions.
	#  2.4 in any case (even if we leave or repeat the alt, match or mismatch), the current popped message is consumed.
	# 3. Once we looped once without a match, repeat from 2 until we have a match.
	# 
	# The system queue is handled differently:
	# - unmatched messages are not consumed, but kept in the queue. This is not the case for "userland ports".

	# Gets some basic things to intercept whenever we enter an alt, such as STOP_COMMAND and KILL_COMMAND
	# through the system queue.	
	additionalInternalAlternatives = getLocalContext().getTc()._getAltPrefix()
	for a in additionalInternalAlternatives:
		alternatives.insert(0, a)
	
	# Now add default alternatives from altstep activations
	for a in getLocalContext().getDefaultAlternatives():
		alternatives.append(a)

	logInternal("Number of alternatives for this alt: %s" % str(len(alternatives)))
	
#	logInternal("Entering alt():\n%s" % alternatives)
	
	# Step 1. Preparation.
	# Alternatives per port
	portAlternatives = {}
	# And prepare a list of watched fds (pipes) to be notified as soon as a
	# port has something new in it.
	watchedPortsFds = []
	
	systemQueueWatched = False
		
	for alternative in alternatives:
		# Optional guard. Its presence is detected if the first element of the clause is callable.
		guard = None
		if callable(alternative[0]):
			guard = alternative[0]
			condition = alternative[1]
			actions = alternative[2:]
		else:
			guard = None # lambda: True ?
			condition = alternative[0]
			actions = alternative[1:]
		
 		if not portAlternatives.has_key(condition.port) and condition.port._started:
			portAlternatives[condition.port] = []
			if condition.port is _getSystemQueue():
				# Register ourselves as a listener on the system port
				condition.port._registerListener()
				systemQueueWatched = True
			watchedPortsFds.append(condition.port.getNotifierFd())
		portAlternatives[condition.port].append((guard, condition, actions))
	
	logInternal("alt: tc %s is watching the following fds: %s - watching the system queue: %s" % (getLocalContext().getTc(), watchedPortsFds, systemQueueWatched))

	# Step 2.
	matchedInfo = None # tuple (guard, template, asValue, actions, message, decodedMessage)
	repeat = False
	try:
		while (not matchedInfo) or repeat:
			# Reset info in case of a repeat
			matchedInfo = None
			repeat = False

			for (port, alternatives) in portAlternatives.items():

				# Special handling for system queue: messages are NOT popped if not matching anything.
				# Instead, they are kept in the queue for other consumers (other TCs, or in a next alt()
				# in the current TC).
				if port is _getSystemQueue():
					port._lock()
					try:
						# Make sure that we don't loop forever here because we did not remove our notification
						# from the notification pipe.
						port._acknowledgeNotification()
						for (message, from_) in port._messageQueue:
							# We ignore the 'from' in systemQueue
							for (guard, condition, actions) in alternatives:
								# Guard is ignored for internal messages (we shouldn't have one, anyway)
	
								# Special message matches (NB: we're suppose to have only dict messages in the system queue)
								if isinstance(message, dict) and condition.template['event'].startswith('any.'):
									# "Wildcard"-based match: we do not expect this exact event in the queue.
									# Instead, we match any 'ressembling' event.
									if condition.template['event'] == 'any.c.done':
										# We match is we have any 'done' in our queue
										if message.get('event') == 'done':
											match = True
									elif condition.template['event'] == 'any.c.killed':
										# We match is we have any 'killed' in our queue
										if message.get('event') == 'killed':
											match = True
	
									if match:
										matchedInfo = (guard, condition, actions, message, None) # None: decodedMessage
										# In this case, we do NOT consume the message: left for
										# other ptc.KILLED, or other any component killed, ...
										break

								# Standard system message matches - consumed if matched
								else:
									# Ignore the decoded message: must be the same as encoded for internal events.
									(match, _, _) = templateMatch(message, condition.template)
									if match:
										matchedInfo = (guard, condition, actions, message, None) # None: decodedMessage
										# Consume the message
										port._remove(message, from_)
										# Exit the port alternative loop, with matchedInfo
										break
	
							if matchedInfo:
								# Exit the loop on messages directly.
								break
					except Exception as e:
						port._unlock()
						logInternal("Exception while analyzing system events: %s" % str(e))
						raise
					port._unlock()
					if matchedInfo:
						(guard, condition, actions, message, _) = matchedInfo
						# OK, we have some actions to trigger (outside the critical section)
						# According to the event type we matched, log it (or not)
						# system queue events are always formatted as a dict { 'event': string } and 'ptc' or 'timer' dependending on the event.
						branch = condition.template['event']
						if branch == 'timeout':
							# timeout-branch selected
							logTimeoutBranchSelected(id_ = str(condition.template['timer']))
						elif branch == 'done':
							# done-branch selected
							logDoneBranchSelected(id_ = str(condition.template['ptc']))
						elif branch == 'killed':
							# killed-branch selected
							logKilledBranchSelected(id_ = str(condition.template['ptc']))
						elif branch == 'all.c.done':
							# all component-done branch selected
							logDoneBranchSelected(id_ = 'all')
						elif branch == 'all.c.killed':
							# all component-killed branch selected
							logKilledBranchSelected(id_ = 'all')
						elif branch == 'any.c.done':
							# any component-done branch selected
							logDoneBranchSelected(id_ = 'any')
						elif branch == 'any.c.killed':
							# all component-killed branch selected
							logKilledBranchSelected(id_ = 'any')
						else:
							# Other system messages are for internal purpose only and does not have TTCN-3 branch equivalent
							logInternal('system event received in system queue: %s' % repr(condition.template))

						for action in actions:
							# Minimal command management for internal messages
							if callable(action):
								action = action()
							if action == REPEAT:
								repeat = True
								break
							elif action == RETURN:
								return
						if repeat:
							# Break the loop on system messages
							break

					else:
						# no match, nothing to do.
						pass

				else:
					# This is a normal port. We always consume the popped message, 
					# support for RETURN and REPEAT "keywords" in actions, etc.
					message = None
					port._lock()
					try:
						if port._messageQueue:
							# 2.1 Let's pop the first message in the queue (will be consumed whatever happens since not kept in queue)
							# FIXME: flawn implementation: we shoud not consume only one message per port per pass.
							# We should really take a "snapshot" (ie freezing ports) and considering timestamped messages...
							(message, from_) = port._messageQueue[0]
							port._messageQueue = port._messageQueue[1:]
							# FIXME - must be hidden in the port class
							try:
								os.read(port._notifier[0], 1)
							except:
								pass
					except Exception as e:
						port._unlock()
						logInternal("Exception while consuming standard port message: %s" % str(e))
						raise e
					port._unlock()
					if message is not None: # And what is we want to send "None" ? should be considered as a non-message, ie a non-send ?
						# 2.2: For each existing satisfied conditions for this port (x[0] is the guard)
						for (guard, condition, actions) in filter(lambda x: (x[0] and x[0]()) or (x[0] is None), alternatives):
							# Only try to match messages from the expected sender
							if condition.from_ and condition.from_ != from_:
								logInternal("not matching condition: not received from the expected address (expected: %s, got: %s)" % (condition.from_, from_))
								match = False
								# In this case, we don't even attempt to decode the message. So we assign a default decoded one for logging purpose
								decodedMessage = message
							else:
								(match, decodedMessage, mismatchedPath) = templateMatch(message, condition.template)
							# Now handle the matching result
							if not match:
								# 2.3 - Mismatch, we should log it.
								logTemplateMismatch(tc = port._tc, port = port._name, message = decodedMessage, template = _expandTemplate(condition.template), encodedMessage = message, mismatchedPath = mismatchedPath)
							else:
								# 2.3 - Match
								matchedInfo = (guard, condition, actions, message, decodedMessage)
								logTemplateMatch(tc = port._tc, port = port._name, message = decodedMessage, template = _expandTemplate(condition.template), encodedMessage = message)
								# Store the message as value, if needed
								if condition.value:
									_setValue(condition.value, decodedMessage)
								if condition.sender:
									_setSender(condition.sender, from_)
								# Then execute actions
								for action in actions:
									if callable(action):
										action = action()
									if action == REPEAT:
										repeat = True
										break
									elif action == RETURN:
										return
								# Break the loop on guard-validated alternatives for this port
								break
						if matchedInfo:
							# We left the loop on guard-validated alternatives for this port because of a match,
							# Let's break the main loop on ports
							break
						else:
							# No match for this port: nothing to do
							pass
					else:
						# no message for this port: nothing to do
						pass

			# Now wait until another message arrives on one of our watched ports (if we have to wait)
			if (not matchedInfo) or repeat:
				try:
					logInternal("alt: tc %s is renewing its subscription on the following fds: %s" % (getLocalContext().getTc(), watchedPortsFds))
					r, w, e = select.select(watchedPortsFds, [], [], 1)
				except select.error, e:
					if e.args[0] == 4:
						# Interrupted system call -> SIGINT, stop() the TC
						stop()
					else:
						raise
					
	#			if r: logInternal("activity detected on port(s) %s" % r)
	except Exception as e:
		logInternal("exception in alt(): %s (%s)" % (str(e), repr(e)))
		if systemQueueWatched:
			_getSystemQueue()._unregisterListener()
		raise e

	if systemQueueWatched:
		_getSystemQueue()._unregisterListener()

# Control "Keywords" for alt().
# May be used as is directly, in a lambda, or returned from an altstep or a function called
# from a lambda.
def REPEAT():
	return REPEAT

def RETURN():
	return RETURN


################################################################################
# System/internal queue
################################################################################

# The system/internal queue is implemented as 
# a special internal communication port, called the "system port",
# used as an internal messaging queue for implementation control, i.e.:
# - timeout management (timeout events are not associated to a particular port in TTCN3)
# - inter-TC control events (kill-, stop-, related operations)
#
# We use the messaging mechanism developed to manage TTCN-3 messages (alt, ports,
# etc) to implement, provision and read this message queue as well.
# System messages are handled in alt(), as if it were any other TTCN-3 message.

class SystemQueue(Port):
	"""
	This is basically a standard port, but with
	modified low-level functions due to specific ways
	system messages are handled in alt(), in particular with regards
	to new message notifications.
	
	each alt() that are watching the system queue should register
	a dedicated notifier (calling getNotifierFd(), the queue assign
	a new notifier automatically per TLS),
	and whenever a new message arrives in the system queue, all
	registered notifiers are notified.
	"""
	def __init__(self):
		Port.__init__(self, tc = None, name = '__system_queue__')
		self._pipes = []

	def _registerListener(self):
		pipe = getLocalContext().getSystemQueueNotifier()
		self._lock()
		if not pipe in self._pipes:
			self._pipes.append(pipe)
		self._unlock()
		logInternal("system queue: tc %s registered as a listener (fd %s)" % (getLocalContext().getTc(), pipe[0]))
		return pipe
	
	def getNotifierFd(self):
		pipe = getLocalContext().getSystemQueueNotifier()
		return pipe[0]
	
	def _unregisterListener(self):
		pipe = getLocalContext().getSystemQueueNotifier()
		self._lock()
		try:
			self._pipes.remove(pipe)
		except:
			pass
		self._unlock()
		getLocalContext().cleanSystemQueueNotifier()
		logInternal("system queue: tc %s unregistered as a listener (fd %s)" % (getLocalContext().getTc(), pipe[0]))
	
	def _notifyListeners(self):
		"""
		Notify current system queue listeners (writing something in their notification pipes).
		"""
		for p in self._pipes:
			try:
				os.write(p[1], 'r')
				logInternal("system queue: notifying a new message for reader on %s" % (p[0]))
			except Exception as e:
				logInternal("system queue: async notifier error %s" % (e))
				pass

	def _enqueue(self, message, from_):
		"""
		The system queue implementation for enqueue is to enqueue the message,
		then send a notification through the notifier pipe only if
		"""
		logInternal("system queue: enqueuing message from %s" % (str(from_)))
		self._lock()
		self._messageQueue.append((message, from_))
		self._notifyListeners()
		self._unlock()

	def _acknowledgeNotification(self):
		"""
		To be called by a listener when it acknowledges that
		it is aware that new messages where received in the system queue.
		Technically, this purges its notification pipe to avoid an overflow
		in the long run.
		"""
		pipe = getLocalContext().getSystemQueueNotifier()
		try:
			f = pipe[0]
			r, w, e = select.select([f], [], [], 0)
			if f in r:
				os.read(f, 1000)
				logInternal("system queue: tc %s acknowledged new message notification on fd %s" % (getLocalContext().getTc(), f))
		except:
			pass
	
	def _remove(self, message, from_):
		"""
		Consumes a particular message from the system queue.
		Only used to remove messages that relates to system states that
		are no longer valid (for instance timer.TIMEOUT, ptc.DONE
		when a timer or a PTC has been restarted, etc).
		Typically, a system queue message is NOT consumed, even if matched,
		as it is more a collection of states (that could be read by next alt())
		instead of actual triggers.
		"""
		self._lock()
		try:
			self._messageQueue.remove((message, from_))
		except ValueError:
			# Not in queue
			pass
		self._unlock()
	

_SystemQueue = SystemQueue()

def _getSystemQueue():
	return _SystemQueue

def _resetSystemQueue():
	"""
	Resets the system queue by restarting the implementing Port.
	"""
	_getSystemQueue().stop()
	_getSystemQueue().start()

def _postSystemEvent(event, from_):
	"""
	Posts an event into the system bus.
	"""
	_getSystemQueue()._enqueue(event, from_)

def _removeSystemEvent(event, from_):
	"""
	Removes an event from the system bus.
	Called to remove states that are no longer valid
	(timer.TIMEOUT, ptc.DONE, ...) due to object restart.
	"""
	_getSystemQueue()._remove(event, from_)

################################################################################
# Test Adapter Configuration management - System Bindings management
################################################################################

# Actually, this is not a part of TTCN-3.
# Pure testerman "concept" to configure Test Adapters, i.e. probes to use.

_CurrentTestAdapterConfiguration = None
_AvailableTestAdapterConfigurations = {}

class TestAdapterConfiguration:
	def __init__(self, name):
		self._name = name
		self._tac = TestermanSA.TestAdapterConfiguration(name = name)
		_registerAvailableTestAdapterConfiguration(name, self)
	
	def bindByUri(self, tsiPort, uri, type_, **kwargs):
		return self._tac.bindByUri(tsiPort, uri, type_, **kwargs)

	def bind(self, tsiPort, uri, type_, **kwargs):
		"""
		The "default" binding method: by URI
		"""
		return self._tac.bindByUri(tsiPort, uri, type_, **kwargs)
	
	def _getTsiPortList(self):
		return self._tac.getTsiPortList()

def with_test_adapter_configuration(name):
	"""
	Testerman API function.
	
	Activates a Test Adapter Configuration:
	this installs the bindings (and desinstalls the previous ones, if any).
	"""
	return useTestAdapterConfiguration(name)

def useTestAdapterConfiguration(name):
	"""
	Activates a Test Adapter Configuration:
	this installs the bindings (and desinstalls the previous ones, if any).
	"""
	global _CurrentTestAdapterConfiguration
	if _CurrentTestAdapterConfiguration:
		_CurrentTestAdapterConfiguration._tac._uninstall()
	if _AvailableTestAdapterConfigurations.has_key(name):
		tac = _AvailableTestAdapterConfigurations[name]
		tac._tac._install()
		_CurrentTestAdapterConfiguration = tac
	else:
		raise TestermanException("Unknown Test Adapter Configuration %s" % name)

def _registerAvailableTestAdapterConfiguration(name, tac):
	_AvailableTestAdapterConfigurations[name] = tac

def _getCurrentTestAdapterConfiguration():
	return _CurrentTestAdapterConfiguration	

# Support for a default, built-in test adapter configuration.
# Simply use 'bind' in the control part, no need to create a configuration explicitely,
# or use the with_test_adapter_configuration() anymore.	
DEFAULT_TEST_ADAPTER_CONFIGURATION_NAME = '__default__'
_DefaultTestAdapterConfiguration = TestAdapterConfiguration(DEFAULT_TEST_ADAPTER_CONFIGURATION_NAME)

def bind(tsiPort, uri, type_, **kwargs):
	log("TSI port '%s' bound to %s [%s]" % (tsiPort, uri, type_))
	return _DefaultTestAdapterConfiguration.bind(tsiPort, uri, type_, **kwargs)
	
################################################################################
# Template Conditions, i.e. Matching Mechanisms
################################################################################

class ConditionTemplate:
	"""
	This is a template proxy, like a CodecTemplate.
	
	Some conditions can work on other conditions (wildcards/conditions templates),
	some other ones are terminal and only works with fixed values
	(fully defined templates - no wildcards, no conditions)
	
	For instance, contains() can nest a condition on scalar (lower_than, ...),
	same for length(), (length(between(1, 3)), length(lower_than(2)), ...)
	but not things like between, lower_than, regexp, etc.
	"""
	def match(self, message, path = ''):
		return True
	def value(self):
		"""
		Called when encoding a template; enables to use
		matching mechanisms in sent messages, too.
		However, may not be supported for all mechanisms.
		"""
		raise TestermanException("Matching mechanism %s cannot be valuated and used in a sent message." % self)

##
# Terminal conditions
##

# Scalar conditions
class greater_than(ConditionTemplate):
	def __init__(self, value):
		self._value = value
	def match(self, message, path = ''):
		try:
			return float(message) >= float(self._value)
		except:
			return False
	def __repr__(self):
		return "(>= %s)" % str(self._value)
	def value(self):
		return self._value

class lower_than(ConditionTemplate):
	def __init__(self, value):
		self._value = value
	def match(self, message, path = ''):
		try:
			return float(message) <= float(self._value)
		except:
			return False
	def __repr__(self):
		return "(<= %s)" % str(self._value)
	def value(self):
		return self._value

class between(ConditionTemplate):
	def __init__(self, a, b):
		if a < b:
			self._a = a
			self._b = b
		else:
			self._a = b
			self._b = a
	def match(self, message, path = ''):
		try:
			return float(message) <= float(self._b) and float(message) >= float(self._a)
		except:
			return False
	def __repr__(self):
		return "(between %s and %s)" % (str(self._a), str(self._b))
	def value(self):
		return random.randint(self._a, self._b)

# Any
class any(ConditionTemplate):
	"""
	Following the TTCN-3 standard, equivalent to ?.
	- must be present
	- contains at least one element for dict/list/string
	"""
	def __init__(self):
		pass
	def match(self, message, path = ''):
		if isinstance(message, (list, dict, basestring)):
			if message:
				return True
			else:
				return False
		# Primitives/non-constructed types (and tuple)
		return True
	def __repr__(self):
		return "(?)"
	def value(self):
		return None

class any_or_none(ConditionTemplate):
	"""
	Equivalent to * in TTCN-3.
	Provided for convenience. Equivalent to 'None' in Testerman.
	- in a dict: any value if present
	- in a list: match any number of elements
	- as a value: any value
	"""
	def __init__(self):
		pass
	def match(self, message, path = ''):
		return True
	def __repr__(self):
		return "(*)"
	def value(self):
		return None

# Empty (list, string, dict)
class empty(ConditionTemplate):
	"""
	Applies to lists, strings, dict
	"""
	def __init__(self):
		pass
	def match(self, message, path = ''):
		try:
			return len(message) == 0
		except:
			return False
	def __repr__(self):
		return "(empty)"
	def value(self):
		return None

# String: regexp
class pattern(ConditionTemplate):
	def __init__(self, pattern):
		self._pattern = pattern
	def match(self, message, path = ''):
		if re.search(self._pattern, message):
			return True
		return False
	def __repr__(self):
		return "(pattern %s)" % str(self._pattern)

class omit(ConditionTemplate):
	"""
	Special template.
	Use it when you want to be sure you did not receive
	an entry in a dict (a field in a record).
	Specially handled in _templateMatch since in case of matching,
	we are not supposed to call it.match().
	"""
	def __init__(self):
		pass
	def match(self, message, path = ''):
		return False
	def __repr__(self):
		return "(omitted)"
	def value(self):
		return None

class equals_to(ConditionTemplate):
	"""
	Case-insensitive equality.
	To remove ?
	"""
	def __init__(self, value):
		self._value = value
	def match(self, message, path = ''):
		return unicode(message).lower() == unicode(self._value).lower()
	def __repr__(self):
		return "(=== %s)" % unicode(self._value)

##
# Non-terminal conditions
##
# Matching negation
class not_(ConditionTemplate):
	def __init__(self, template):
		self._template = template
	def match(self, message, path = ''):
		(m, _, _) = templateMatch(message, self._template, path)
		return not m
	def toMessage(self):
		return ('(not)', self._template)
	def __repr__(self):
		return "(not %s)" % str(self._template) # a recursive str(template) is needed - here it will work only for simple types/comparators.

class ifpresent(ConditionTemplate):
	def __init__(self, template):
		self._template = template
	def match(self, message, path = ''):
		(m, _, _) = templateMatch(message, self._template, path)
		return m
	def __repr__(self):
		return "(%s, if present)" % unicode(self._template)
	def toMessage(self):
		return ('(ifpresent)', self._template)

# Length attribute
class length(ConditionTemplate):
	def __init__(self, template):
		self._template = template
	def match(self, message, path = ''):
		(m, _, _) = templateMatch(len(message), self._template, path)
		return m
	def __repr__(self):
		return "(length %s)" % unicode(self._template)

class superset(ConditionTemplate):
	"""
	contains at least each element of the value (1 or more times)
	(list only)
	"""
	def __init__(self, *templates):
		self._templates = list(templates)
	def match(self, message, path = ''):
		if not isinstance(message, list):
			return False
		for tmplt in self._templates:
			ret = False
			for e in message:
				(ret, _, _) = templateMatch(e, tmplt, path)
				if ret: 
					# ok, tmplt is in message. Next template?
					break
			# sorry, tmplt is not in the message. This is not a superset.
			if not ret:
				return False
		# All tmplt in the message, at least once (the actual count is not computed)
		return True
	def __repr__(self):
		return "(superset of [%s])" % ', '.join([unicode(x) for x in self._templates])
	def toMessage(self):
		return ('(superset)', self._templates)
	def value(self):
		return self._templates

class subset(ConditionTemplate):
	"""
	contains only elements from the template (1 or more times each)
	(list only)
	"""
	def __init__(self, *templates):
		self._templates = list(templates)
	def match(self, message, path = ''):
		if not isinstance(message, list):
			return False
		for e in message:
			ret = False
			for tmplt in self._templates:
				(ret, _, _) = templateMatch(e, tmplt, path)
				if ret: 
					break
			if not ret:
				return False
		return True
	def __repr__(self):
		return "(subset of [%s])" % ', '.join([unicode(x) for x in self._templates])
	def toMessage(self):
		return ('(subset)', self._templates)
	def value(self):
		return self._templates

class set_(ConditionTemplate):
	"""
	Contains the elements from the template, exactly one time each, in any order.
	
	This first implementation does not play nice with wildcards or even
	conditional templates. Static template elements are preferred.
	"""
	def __init__(self, *templates):
		self._templates = list(templates)
	def match(self, message, path = ''):
		if not isinstance(message, list):
			return False
			
		# The current implementation does not necessarily associate a matching template with
		# a message in "both way":
		# a message M can be matched by a template T, while T may have matched another message too.
		# This behavior needs fixing.

		matchedElementIndexes = []		
		# Check that each template have a single (and different)
		# corresponding value in the message
		for t in self._templates:
			satisfied = False
			for i in range(len(message)):
				if not i in matchedElementIndexes:
					(ret, _, _) = templateMatch(message[i], t, path)
					if ret:
						# t is satisfied with element i, which was not used to match another template yet
						matchedElementIndexes.append(i)
						satisfied = True
						break
			if not satisfied:
				# One template element is not matched
				return False

		satisfiedElementIndexes = []		
		# Now check that each message element have a single (and different)
		# matching element in the template
		for e in message:
			matched = False
			for i in range(len(self._templates)):
				if not i in satisfiedElementIndexes:
					(ret, _, _) = templateMatch(e, self._templates[i], path)
					if ret:
						# t is satisfied with element i, which was not used to match another template yet
						satisfiedElementIndexes.append(i)
						matched = True
						break
			if not matched:
				# One message element is not matched
				return False

		return True
	def __repr__(self):
		return "(set of [%s])" % ', '.join([unicode(x) for x in self._templates])
	def toMessage(self):
		return ('(set)', self._templates)
	def value(self):
		return self._templates


class contains(ConditionTemplate):
	"""
	Dict/list/string content check
	As a consequence, a bit wider than superset(template)
	"""
	def __init__(self, template):
		self._template = template
	def match(self, message, path = ''):
		if isinstance(message, basestring) and isinstance(self._template, basestring):
			return message in self._template
		if not isinstance(message, list):
			return False
		# At least one match
		for element in message:
			(m, _, _) = templateMatch(element, self._template, path)
			if m:
				return True
		return False
	def toMessage(self):
		return ('(contains)', self._template)
	def __str__(self):
		return "(contains %s)" % unicode(self._template)

class in_(ConditionTemplate):
	"""
	'included in' a set/list of other templates
	
	This is a subset() for a single element. (mergeable with subset ?)
	"""
	def __init__(self, *template):
		# template is a list of templates (wildcards accepted)
		self._template = template
	def match(self, message, path = ''):
		for element in self._template:
			(m, _, _) = templateMatch(message, element, path)
			if m:
				return True
		return False
	def __repr__(self):
		return "(in %s)" % unicode(self._template)

class complement(ConditionTemplate):
	"""
	'not included in a list'.
	The TTCN-3 equivalent is 'complement'.
	Equivalent to not_(in_)
	"""
	def __init__(self, *templates):
		# template is a list of templates (wildcards accepted)
		self._templates = templates
	def match(self, message, path = ''):
		for element in self._templates:
			(m, _, _) = templateMatch(message, element, path)
			if m:
				return False
		return True
	def __repr__(self):
		return "(complements %s)" % unicode(self._templates)

class and_(ConditionTemplate):
	"""
	And condition operator.
	"""
	def __init__(self, templateA, templateB):
		# template is a list of templates (wildcards accepted)
		self._templateA = templateA
		self._templateB = templateB
	def match(self, message, path = ''):
		(m, _, _) = templateMatch(message, self._templateA, path)
		if m:
			return templateMatch(message, self._templateB, path)[0]
		return False
	def __repr__(self):
		return "(%s and %s)" % (unicode(self._templateA), unicode(self._templateB))
		
class or_(ConditionTemplate):
	"""
	Or condition operator.
	"""
	def __init__(self, templateA, templateB):
		# template is a list of templates (wildcards accepted)
		self._templateA = templateA
		self._templateB = templateB
	def match(self, message, path = ''):
		(m, _, _) = templateMatch(message, self._templateA, path)
		if not m:
			return templateMatch(message, self._templateB, path)[0]
		else:
			return True
	def __repr__(self):
		return "(%s or %s)" % (unicode(self._templateA), unicode(self._templateB))

################################################################################
# Extractor 
################################################################################

class extract(ConditionTemplate):
	"""
	Partial value extractor.
	Use value(name) to retrieve the associated value if matched.
	"""
	def __init__(self, template, value):
		self._template = template
		self._name = value
	def match(self, message, path):
		(matched, decodedMessage, _) = templateMatch(message, self._template, path)
		if matched:
			_setValue(self._name, decodedMessage)
			return True
		else:
			return False
	def __repr__(self):
		return "%s -> %s" % (str(self._template), self._name)
	def value(self):
		if hasattr(self._template, "value"):
			return self._template.value()
		else:
			return self._template
	
################################################################################
# "Global" Variables Management
################################################################################

# Session variables are external variables: can be set from outside,
# returned outside for ATS chaining.
# By convention, session variable names starts with 'PX_'
_SessionVariables = {}
# Ats variables are global variables for the ATS only.
# They are not exposed or provisioned to/from the outside world.
_AtsVariables = {}

# Contains a list of dict (testcase_id, verdict) to get an ATS-level summary
_AtsResults = []

# Enable to stop the ATS automatically as soon as a testcase is not passed
_StopOnTestCaseFailure = False

_VariableMutex = threading.RLock()

def get_variable(name, default_value = None):
	# Fallback to defaultValue for invalid variable names ?
	ret = default_value
	_VariableMutex.acquire()
	if name.startswith('PX_'):
		ret = _SessionVariables.get(name, default_value)
	elif name.startswith('P_') :
		ret = _AtsVariables.get(name, default_value)
	_VariableMutex.release()
	return ret

def set_variable(name, value):
	_VariableMutex.acquire()
	if name.startswith('PX_'):
		_SessionVariables[name] = value
	elif name.startswith('P_'):
		_AtsVariables[name] = value
	_VariableMutex.release()

def _get_all_session_variables():
	# Not protected, not a deep copy - should
	# ony used internally, not part of the Testerman TE API.
	return _SessionVariables

def stop_ats_on_testcase_failure(stop = True):
	global _StopOnTestCaseFailure
	_StopOnTestCaseFailure = stop


################################################################################
# codec management: 'with' support
################################################################################

# with_ behaviour:
# when used in a template to match, first decode the received payload, then
# "returns" the decoded message struct.
#
# when used in a template to send, first encode the struct, then "returns" the
# encoded message.

def _encodeTemplate(template):
	"""
	Valuates a template:
	- calls encoders,
	- valuates matching mechanisms, if possible
	- if it's a function, call it (0-arity)
	"""
	if callable(template):
		template = template()
	
	if isinstance(template, CodecTemplate):
		return template.encode()

	if isinstance(template, list):
		ret = []
		for e in template:
			ret.append(_encodeTemplate(e))
		return ret
		
	if isinstance(template, dict):
		ret = {}
		for k, v in template.items():
			ret[k] = _encodeTemplate(v)
		return ret
	
	if isinstance(template, tuple):
		return (template[0], _encodeTemplate(template[1]))
	
	try:
		# if the template is a matching mechanism, it may provide a value
		return template.value()
	except:
		pass
	return template
	

def _expandTemplate(template):
	"""
	Expands a template to its Testerman structure, 
	evaluating inner functions and skipping encoding/decoding.
	"""
	if callable(template):
		template = template()

	if isinstance(template, CodecTemplate):
		return template.getTemplate()

	if isinstance(template, list):
		ret = []
		for e in template:
			ret.append(_expandTemplate(e))
		return ret
		
	if isinstance(template, dict):
		ret = {}
		for k, v in template.items():
			ret[k] = _expandTemplate(v)
		return ret
	
	if isinstance(template, tuple):
		return (template[0], _expandTemplate(template[1]))
	
	return template


class CodecTemplate:
	"""
	This is a proxy template class.
	"""
	def __init__(self, codec, template):
		"""
		codec can be a string identifying an actual codec to use,
		but can also be a callable. In this case, the callable is used unconditionally to encode/decode.
		Could be convenient to apply local, simple transformations before encoding or decoding a template.
		"""
		self._codec = codec
		self._template = template
	
	def encode(self):
		if callable(self._codec):
			return self._codec(self._template)
		else:
			# Standard codec.
			# Recursive encoding:
			# first encode what should be encoded within the template,
			# then encode it
			try:
				(encodedMessage, summary) = TestermanCD.encode(self._codec, _encodeTemplate(self._template))
			except Exception:
				# This includes a CodecNotFound exception
				raise TestermanException('Encoding error: could not encode message with codec %s:\n%s' % (self._codec, getBacktrace()))
			else:
				# Summary is FFU.
				return encodedMessage
	
	def getTemplate(self):
		# recursive expansion
		return _expandTemplate(self._template)
		
	def decode(self, encodedMessage):
		if callable(self._codec):
			return self._codec(encodedMessage)
		else:
			# NB: no recursive decoding: let the calling templateMatch calls decode when needed
			# This way, the codec only decodes what it knows how to decode, and has no need
			# to know what other codecs are available.
			try:
				# In the TE context, 
				(decodedMessage, summary) = TestermanCD.decode(self._codec, encodedMessage)
			except TestermanCD.CodecNotFoundException:
				raise TestermanException('Decoding error: codec %s not found' % self._codec)
			except Exception:
				logInternal('Decoding error: could not decode message with codec %s:\n%s' % (self._codec, getBacktrace()))
				# Unable to decode: leave the buffer as is - it will lead to a match error probably.
				# Leaving it as is enables to convey the payload all along the flow for further analysis.
				return encodedMessage
			if decodedMessage is None:
				logInternal('Decoding error: could not decode message with codec %s' % self._codec)
				return encodedMessage
			else:
				# Summary if FFU.
				return decodedMessage
			
# The main alias - as defined in Testerman API 1.0
class with_(CodecTemplate):
	pass

################################################################################
# Template matching
################################################################################

def templateMatch(message, template, initialPath = u'template'):
	"""
	A simple wrapper over _templateMatch to catch possible internal exceptions.

	@type  message: any object
	@param message: the encoded message, as received (may be structured, too)
	@type  template: any object suitable for a template
	@param template: the template to match. may contain references to coders and conditions. They are evaluated on time.
		
	@rtype: (bool, object)
	@returns: (a, b) where a is True if match, False otherwise, b is the decoded message (partially decoded in case of decoding error ?)
	"""
	mismatchedPath = initialPath
	try:
		(ret, decodedMessage, mismatchedPath) = _templateMatch(message, template, initialPath)
	except Exception:
		# Actually, this is for debug purposes
		logUser("Exception while trying to match a template:\n%s" % getBacktrace())
		return (False, message, mismatchedPath)
	return (ret, decodedMessage, mismatchedPath)

def match(message, template):
	"""
	TTCN-3 match function.
	"""
	ret, decodedMessage, mismatchedPath = templateMatch(message, template)
	if not ret:
		logTemplateMismatch(tc = getLocalContext().getTc(), port = "", message = decodedMessage, template = _expandTemplate(template), encodedMessage = message, mismatchedPath = mismatchedPath)
	else:
		logTemplateMatch(tc = getLocalContext().getTc(), port = "", message = decodedMessage, template = _expandTemplate(template), encodedMessage = message)
	return ret

def _templateMatch(message, template, path):
	"""
	Returns True if the message matches the template.
	Includes calls to decoders and comparators if present in template.
	
	@type  message: any python object, valid for a Testerman fully qualified message
	@param message: the message to match
	@type  template: any python object, valid for a Testerman template: may contains template proxies such as CodecTemplates and ConditionTemplates.
	@param template: the template to verify against the message
	@type  path: string
	@param path: this is a human readable path of the object in the template that we try to match.
	This parameter greatly helps in understanding why a complex template mismatched.
	
	@rtype: tuple (bool, object, string)
	@returns: (a, b, path) where a is the matching status (True/False),
	          b the decoded message (same type as @param message),
	          path is the last attempted template path before a mismatch. Undetermined if a == True.
	"""
	# Support for dynamic templates
	if callable(template):
		template = template()

	# Match all
	if template is None:
		return (True, message, path)
	
	# CodecTemplate proxy template
	if isinstance(template, CodecTemplate):
		# Let's see if we can first decode the message
		try:
			decodedMessage = template.decode(message)
		except Exception as e:
			logInternal("mismatch: unable to decode message part with codec %s: %s" % (template._codec, str(e) + getBacktrace()))
			return (False, message, path)
		# TODO: handle decoding error here ?
		logInternal("_templateMatch: message part %s decoded with codec %s: %s" % (path, template._codec, repr(decodedMessage)))
		# Now match the decoded message against the proxied template (not expanded, because it should contain other proxies, if any)
		return _templateMatch(decodedMessage, template._template, path)
	
	# Structured type: dict
	# all entries in template dict must match ; extra message entries are ignored (but kept in "decoded dict")
	if isinstance(template, dict):
		if not isinstance(message, dict):
			logInternal("mismatch: %s: expected a dict << %s >>, got << %s >>" % (path, repr(template), repr(message)))
			return (False, message, path)
		# Existing entries in template dict must be matched (excepting 'omit' entries, which must not be present...)
		decodedDict = {}
		result = True
		mismatchedPath = None
		for key, tmplt in template.items():
			# any value or none, ie '*'
			if tmplt is None:
				continue
			if message.has_key(key):
				(ret, decodedField, p) = _templateMatch(message[key], tmplt, u"%s.{%s}" % (path, unicode(key)))
				decodedDict[key] = decodedField
				if not ret:
					logInternal("mismatch: %s: mismatched dict entry %s" % (path, unicode(key)))
					result = False
					mismatchedPath = p
					# continue to traverse the dict to perform "maximum" message decoding
			elif isinstance(tmplt, (omit, any_or_none, ifpresent)) or (isinstance(tmplt, extract) and isinstance(tmplt._template, (omit, any_or_none, ifpresent))):
				# if the missing keys are omit(), that's ok.
				logInternal("omit: %s: omitted value %s not found, or optional value not found. OK." % (path, repr(key)))
				continue
			else:
				# if it's something else, missing key, so no match.
				logInternal("mismatch: %s: missing dict entry %s" % (path, repr(key)))
				result = False
				mismatchedPath = path
		# Now, add message keys that were not in template to the decoded dict
		for key, m in message.items():
			if not key in template:
				decodedDict[key] = m
		return (result, decodedDict, mismatchedPath)
	
	# Structured type: tuple (choice, value)
	# Must be the same choice name (ie tupe[0]) and matching value
	if isinstance(template, tuple):
		if not isinstance(message, tuple):
			logInternal("mismatch: %s: expected a tuple << %s >>, got << %s >>" % (path, repr(template), repr(message)))
			return (False, message, path)
		# Check choice
		if not message[0] == template[0]:
			logInternal("mismatch: %s: tuple choices differ (message: %s, template %s)" % (path, repr(message[0]), repr(template[0])))
			return (False, message, path)
		# Check value
		(ret, decoded, path) = _templateMatch(message[1], template[1], u"%s.(%s)" % (path, unicode(message[0])))
		return (ret, (message[0], decoded), path)

	# Structured type: list
	# This is a one-to-one exact match, ordered.
	# as a consequence, the same number of elements in template and message are expected,
	# unless we have some * in template.
	if isinstance(template, list):
		if not isinstance(message, list):
			logInternal("mismatch: %s: expected a list" % path)
			return (False, message, path)
		
		# Wildcard (*) support:
		# match(message, *|template) =
		#  matched = False
		#  i = 0
		#  while not matched and message[i:]:
		#   matched = match(message[i:], template)
		(result, decodedList, path) = _templateMatch_list(message, template, path)
		return (result, decodedList, path)

	# conditions: proxied templates	
	if isinstance(template, ConditionTemplate):
		# TODO: ConditionTemplate.match() should returns a decoded message, too
		return (template.match(message, path), message, path)
	
	# Simple types
	return (message == template, message, path)

def _is_any_or_none(template):
	"""
	Returns True if the template is a any_or_none behind a extract, codec template, etc
	"""
	if isinstance(template, any_or_none):
		return True
	elif isinstance(template, extract):
		return _is_any_or_none(template._template)
	elif isinstance(template, CodecTemplate):
		return _is_any_or_none(template.getTemplate())
	else:
		return False

def _templateMatch_list(message, template, path):
	"""
	both message and template are lists.
	
	Semi-recursive implementation.
	De-recursived on wildcard * only.
	"""
	logInternal("Trying to match %s with %s" % (repr(message), repr(template)))
	# match(message, *|template) =
	#  matched = False
	#  i = 0
	#  while not matched and message[i:]:
	#   matched = match(message[i:], template)
	
	# An empty template can only match an empty message
	if not template:
		if not message:
			return (True, [], path)
		else:
			return (False, [], path)

	# The contrary is false. A non-empty template
	# may match an empty message (wilcards, ifpresent elements, etc)

	# template header|trail	
	th, tt = (template[0], template[1:])
	
	if not message:
		if _is_any_or_none(th):
			# matched
			logInternal("_templateMatch_list matched: [] against [*]")
			return (True, [], path)
		elif isinstance(th, ifpresent):
			# discard the optional element, check with the others
			ret, decoded, path = _templateMatch_list(message, tt, path)
			return (ret, decoded, path)
		else:
			# Other templates: no match, missing mandatory elements to match
			return (False, [], path)
	
	# message header|trail
	mh, mt = (message[0], message[1:])

	if _is_any_or_none(th):
		if not tt:
			logInternal("_templateMatch_list matched: %s against %s ([*])" % (repr(message), repr(template)))
			return (True, message, path)
		matched = False
		decodedList = []
		trailingDecodedList = []
		i = 0
		mismatchedPath = path
		while not matched and message[i:]:
			(matched, trailingDecodedList, p) = _templateMatch_list(message[i:], tt, path)
			if not matched:
				mismatchedPath = p
				decodedList.append(message[i])
			i += 1
		logInternal("_templateMatch_list res %s: %s against %s ([*])" % (matched, repr(message), repr(template)))
		# decodedList += trailingDecodedList
		for e in trailingDecodedList:
			decodedList.append(e)
		return (matched, decodedList, mismatchedPath)
	else:
		# Recursive approach:
		# we match the same element first element, and the trailing list should match, too
		decodedList = []
		result = True
		(ret, decodedAttemptedElement, mismatchedPath) = _templateMatch(mh, th, u'%s.*' % path)

		if not ret and not isinstance(th, ifpresent):
			# mismatch on non-optional/if present element
			logInternal("_templateMatch_list mismatched on first element: %s against %s " % (repr(message), repr(template)))
			result = False
			# Display why we didn't match our element
			decodedList.append(decodedAttemptedElement)
			# Complete with undecoded message
			decodedList += mt
		elif not ret:
			# not matching, but it was an optional/ifpresent element.
			# We just bypass this template element and try to match the
			# trailing template only
			
			# Display why we didn't match our element
			# This may cause duplicated list elements in the 'decoded message',
			# in particular in the cases where multiple optional matches are 
			# attempted in a row. Actually, the same element will be matched
			# against each optional template elements, making it appear multiple
			# time in the final 'decoded' message used for template matching.
			
			# This basically leads to "expand" the message so that it contains
			# a number of elements that can be mapped with the optional/ifpresent
			# template elements.
			decodedList.append(decodedAttemptedElement)

			logInternal("_templateMatch_list mismatched on first optional element: %s against %s " % (repr(message), repr(template)))
			(ret, decoded, mismatchedPath) = _templateMatch_list(message, tt, path)
			result = ret
			decodedList += decoded
		else:
			# Display why we didn't match our element
			decodedList.append(decodedAttemptedElement)
			logInternal("_templateMatch_list matched on first element: %s against %s " % (repr(message), repr(template)))
			(ret, decoded, mismatchedPath) = _templateMatch_list(mt, tt, path)
			result = ret
			decodedList += decoded

#		logInternal("_templateMatch_list res %s: %s against %s ([*])" % (result, repr(message), repr(template)))
		return (result, decodedList, mismatchedPath)


################################################################################
# TRI interface - TE provided
################################################################################

def triEnqueueMsg(tsiPortId, sutAddress = None, componentId = None, message = None):
	"""
	TRI interface: TE provided.
	Enqueues a TRI-received message to the userland (i.e. the TE world).
	
	We retrieve the corresponding tsiPort and its mapped ports thanks to the tsiPortId,
	used as a key.
	
	@type  tsiPortId: string
	@param tsiPortId: the name of the test system interface port.
	@type  sutAddress: string
	@param sutAddress: the sutAddress the message comes from
	@type  componentId: string
	@param componentId: the name of ?? (dest test component ?) - not used.
	@type  message: any valid testerman message
	@param message: the received message to enqueue to userland
	"""
	tsiPort = None
	_TsiPortsLock.acquire()
	if _TsiPorts.has_key(tsiPortId):
		tsiPort = _TsiPorts[tsiPortId]
	_TsiPortsLock.release()
	
	if tsiPort:
		logInternal("triEnqueueMsg: received a message for tsiPort %s from %s. Enqueing it." % (str(tsiPort), str(sutAddress)))
		tsiPort._enqueue(message, sutAddress)
	else:
		# Late message ? just discard it.
		logInternal("triEnqueueMsg: received a message for unmapped tsiPortId %s. Not delivering to userland, discarding." % str(tsiPortId))

TestermanSA.registerTriEnqueueMsgFunction(triEnqueueMsg)

################################################################################
# ATS Cancellation management
################################################################################

_AtsCancelled = False

def _cancel():
	"""
	Called when receiving a SIGINT by the TE.
	Flags a global variable so that the test case stops once the current testcase
	is over.
	"""
	global _AtsCancelled
	_AtsCancelled = True
	
def _isAtsCancelled():
	"""
	Returns True if the ATS is cancelled: we should not execute new testcases,
	but raise a TestermanCancelException instead.
	"""
	return _AtsCancelled

################################################################################
# action() management
################################################################################

def action(message, timeout = 5.0):
	"""
	Prompts the user to perform an external action.
	If the user did not confirm he/she performed it within timeout,
	we assume it was done and continue.
	
	Only returns when the user confirmed the action, or on timeout.
	
	Implementation note:
	implemented as a "signal" sent through the logs to the log watchers,
	the log analyzer interprets the log event and display a dialog box to the
	user
	when the user confirms the action, a signal "action_performed" is sent to the
	job, leading to a SIGUSR1 for the TE process.
	
	
	Only one action can be waited at the same time (other action() calls
	are blocked until the previous one is complete).
	
	@type  message: unicode string
	@param message: a message describing the action. Will be presented to the
	                end-user
	@type  timeout: float
	@param timeout: the timeout before assuming the action has been performed,
	                in s. This parameter does not exist in TTCN-3.
	"""
	TestermanSA.triSUTactionInformal(message, timeout, tc = getLocalContext().getTc())

def _actionPerformedByUser():
	TestermanSA._actionPerformedByUser()

################################################################################
# default alternative
################################################################################

def activate(altstep):
	"""
	Activate an altstep (which is a list of alternatives)
	Returns a reference to the activated altstep so that it can be
	deactivated later.
	
	Activation is valid for the whole TC.
	@type  altstep: a list of alternatives
	@param altstep: the default altstep to activate
	
	@rtype: object
	@param: an internal representation of the altstep, suitable for a use in deactivate()
	"""
	return getLocalContext().addDefaultAltstep(altstep)

def deactivate(id_):
	"""
	@type  id_: object
	@param id_: the internal representation of an activated altstep, as returned by activate()
	
	@rtype: bool
	@returns: True if deactivated, False otherwise (not activated before)
	"""
	return getLocalContext().removeDefaultAltstep(id_)

################################################################################
# all component / all timer / ...
################################################################################

class all_component:
	"""
	Defined as a static class.

	FIXME: the exception when the testcase is None are not correct:
	- the testcase is assumed to be always available in any context,
	- we need to implement an explicit way to know if we're in the MTC thread
	  or not
	"""

	DONE = _BranchCondition(_getSystemQueue(), TestComponent._ALL_DONE_EVENT)
	KILLED = _BranchCondition(_getSystemQueue(), TestComponent._ALL_KILLED_EVENT)
	
	@staticmethod
	def stop():
		testcase = getLocalContext().getTestCase()
		if not testcase:
			raise TestermanTtcn3Exception("'all component.stop' can only be called from the MTC")
		for ptc in testcase._ptcs:
			ptc.stop()

	@staticmethod
	def kill():
		testcase = getLocalContext().getTestCase()
		if not testcase:
			raise TestermanTtcn3Exception("'all component.kill' can only be called from the MTC")
		for ptc in testcase._ptcs:
			ptc.kill()

	@classmethod
	def done(cls):
		"""
		Equivalent to:
		alt([[cls.DONE]])
		
		FIXME: according to TTCN-3, all component.done matches also when no component
		has been created. This is not the case here for now.
		"""
		testcase = getLocalContext().getTestCase()
		if not testcase:
			raise TestermanTtcn3Exception("'all component.done' can only be called from the MTC")
		alt([[cls.DONE]])

	@classmethod
	def killed(cls):
		testcase = getLocalContext().getTestCase()
		if not testcase:
			raise TestermanTtcn3Exception("'all component.killed' can only be called from the MTC")
		alt([[cls.KILLED]])

	@staticmethod
	def running()	:
		testcase = getLocalContext().getTestCase()
		if not testcase:
			raise TestermanTtcn3Exception("'all component.running' can only be called from the MTC")
		# No PTC -> none running
		if not testcase._ptcs:
			return False
		for ptc in testcase._ptcs:
			if not ptc.running():
				return False
		return True

	@staticmethod
	def alive():
		testcase = getLocalContext().getTestCase()
		if not testcase:
			raise TestermanTtcn3Exception("'all component.alive' can only be called from the MTC")
		# No PTC -> none alive.
		if not testcase._ptcs:
			return False
		for ptc in testcase._ptcs:
			if not ptc.alive():
				return False
		return True
			

################################################################################
# any component / all timer / ...
################################################################################

class any_component:
	"""
	Defined as a static class.
	
	FIXME: the exception when the testcase is None are not correct:
	- the testcase is assumed to be always available in any context,
	- we need to implement an explicit way to know if we're in the MTC thread
	  or not
	"""

	DONE = _BranchCondition(_getSystemQueue(), TestComponent._ANY_DONE_EVENT)
	KILLED = _BranchCondition(_getSystemQueue(), TestComponent._ANY_KILLED_EVENT)
	
	@classmethod
	def done(cls):
		"""
		Equivalent to:
		alt([[cls.DONE]])
		"""
		testcase = getLocalContext().getTestCase()
		if not testcase:
			raise TestermanTtcn3Exception("'any component.done' can only be called from the MTC")
		alt([[cls.DONE]])

	@classmethod
	def killed(cls):
		testcase = getLocalContext().getTestCase()
		if not testcase:
			raise TestermanTtcn3Exception("'any component.killed' can only be called from the MTC")
		alt([[cls.KILLED]])

	@staticmethod
	def running()	:
		testcase = getLocalContext().getTestCase()
		if not testcase:
			raise TestermanTtcn3Exception("'any component.running' can only be called from the MTC")
		for ptc in testcase._ptcs:
			if ptc.running():
				return True
		return False

	@staticmethod
	def alive():
		testcase = getLocalContext().getTestCase()
		if not testcase:
			raise TestermanTtcn3Exception("'any component.alive' can only be called from the MTC")
		for ptc in testcase._ptcs:
			if ptc.alive():
				return True
		return False


################################################################################
# Convenience functions: log level management
################################################################################

def enable_debug_logs():
	TestermanTCI.enableDebugLogs()

def disable_debug_logs():
	TestermanTCI.enableLogs()

def enable_log_levels(*levels):
	for level in levels:
		TestermanTCI.enableLogLevel(level)

def disable_log_levels(*levels):
	for level in levels:
		TestermanTCI.disableLogLevel(level)

def disable_logs():
	TestermanTCI.disableLogs()

def enable_logs():
	TestermanTCI.enableLogs()

################################################################################
# Convenience functions: TTCN-3 "extensions"
################################################################################

def wait(duration, name = "wait"):
	timer = Timer(duration, name)
	timer.start()
	timer.timeout()
	

################################################################################
# Control part functions
################################################################################

def define_codec_alias(name, codec, **kwargs):
	TestermanCD.alias(name, codec, **kwargs)


################################################################################
# Additional init/finalization fonctions
################################################################################

def _finalize():
	if _CurrentTestAdapterConfiguration:
		_CurrentTestAdapterConfiguration._tac._uninstall()

def _initialize():
	random.seed()

