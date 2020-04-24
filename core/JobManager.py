# -*- coding: utf-8 -*-
##
# This file is part of Testerman, a test automation system.
# Copyright (c) 2008-2013 Sebastien Lefevre and other contributors
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
# Job Manager:
# a job scheduler able to execute scheduling jobs on time.
#
# Jobs may be of different types:
# - AtsJob, the most used job type. Contructs a TE based on a source ATS,
#   then executes it.
# - CampaignJob, a job representing a tree of jobs.
# - PauseJob, a special job for campaign management.
#
##

import ConfigManager
import DependencyResolver
import EventManager
import FileSystemManager
import TestermanMessages as Messages
import TEFactory
import Tools
import Versions

import base64
import compiler
import cPickle as pickle
import copy_reg
import fcntl
import logging
import os
import os.path
import parser
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import zipfile

cm = ConfigManager.instance()

################################################################################
# Logging
################################################################################

def getLogger():
	return logging.getLogger('TS.JobManager')

################################################################################
# Exceptions
################################################################################

class PrepareException(Exception):
	"""
	This exception can be raised from Job.prepare() in case of
	an application-level error (i.e. not an Internal error)
	"""
	pass

################################################################################
# Tools/Convenience functions
################################################################################

_GeneratorBaseId = 0
_GeneratorBaseIdMutex = threading.RLock()

def getNewId():
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

def createJobEventNotification(uri, jobDict):
	"""
	Creates a Testerman Xc JOB-EVENT notification for a job.
	
	@type  uri: string
	@param uri: the uri of the JOB-EVENT
	@type  jobDict: dict
	@param jobDict: a dict providing job-related info, as returned by job.toDict()
	
	@rtype: TestermanMessages.Notification
	@returns: the notification ready to be dispatched by the Event Manager.
	"""
	notification = Messages.Notification("JOB-EVENT", uri, "Xc", Versions.getXcVersion())
	notification.setApplicationBody(jobDict)
	return notification

def parseParameters(parameters):
	"""
	Parses an inline string containing session parameters:
	key=value[,key=value] with support for ',' in a value.
	
	Such a string is suitable for session parameters mapping
	in a campaign.
	
	@type  parameters: utf-8 string
	@param parameters: the line to parse
	
	@rtype: dict[unicode] of unicode
	@returns: a dict of read parameters
	"""
	values = {}
	
	# We support a ',' as a value
	# (for instance: a=b,c=d,e,f=g)
	if parameters:
		splitParameters = parameters.split(',')
		parameters = []
		i = 0
		try:
			while i < len(splitParameters):
				if '=' in splitParameters[i]:
					parameters.append(splitParameters[i])
				else:
					parameters[-1] = parameters[-1] + ',' + splitParameters[i]
				i += 1

			for key, value in map(lambda x: x.split('=', 1), parameters):
				values[key.decode('utf-8')] = value.decode('utf-8')
		except Exception as e:
			raise Exception('Invalid parameters format (%s)' % str(e))
	
	return values 

def mergeSessionParameters(initial, signature, mapping, mode = "loose"):
	"""
	Computes the session parameter values to pass to a job from multiple sources:
	- initial: the user's or parent job's suggested, initial parameters.
	- signature: the job's signature (extracted from its metadata)
	- mapping: the contextual parameters mapping to apply, assigning some
	  parameter values (or a constant) to a parameter. Parameters values
	  are expressed as a ${my_variable} token. 
	
	Loose mode:
	parameters that are not defined in the job's signature (i.e.
	defined in the default dict) are also merged and returned.
	
	Strict mode:
	only parameters that are defined in the job's signature are
	returned.
	
	@type  initial: dict[unicode] of objects
	@type  signature: dict[unicode] of (name: unicode, defaultValue: unicode, type: unicode)
	@type  mapping: dict[unicode] of unicode (may contain ${references})
	
	@rtype: dict[unicode] of objects
	@returns: the merged parameters with their values to pass for a
	particular run, but left uncasted (not in the signature target's format/type)
	"""
	
	def substituteVariables(s, values):
		"""
		Replaces ${name} with values[name] in s.
		If name is not found, leave the token unchanged.
		"""
		def _subst(match, local_vars = None):
			name = match.group(1)
			return values.get(name, '${%s}' % name)
		return re.sub(r'\$\{([a-zA-Z_0-9-]+)\}', _subst, s)

	merged = {}

	if mode == "strict":
		# The initial parameters override the default ones
		for name, value in signature.items():
			format = value['type']
			if initial.has_key(name):
				merged[name] = initial[name]
			else:
				merged[name] = value['defaultValue']
		
		# Now apply the mapping on existing parameters
		for name, value in merged.items():
			if name in mapping:
				merged[name] = substituteVariables(mapping[name], merged)

	elif mode == "loose":
		# We take the full initial parameters
		for name, value in initial.items():
			merged[name] = value
		# And we complete with default parameters
		for name, value in signature.items():
			if not name in merged:
				merged[name] =value['defaultValue']

		# Now apply the mapping, creating new parameters if needed
		for name, value in mapping.items():
			merged[name] = substituteVariables(value, merged)

	else:
		raise Exception("Invalid session parameter merge mode (%s)" % mode)
	
	return merged


################################################################################
# Base Job
################################################################################

class Job(object):
	"""
	Main job base class.
	Jobs are organized in trees, based on _childBranches and _parent (Job instances).
	The tree is a 2-branch tree:
	each job may have a list of job to execute on success (success branch)
	or to execute in case of an error (error branch)
	"""

	# Job-acceptable signals
	SIGNAL_PAUSE = "pause"
	SIGNAL_CANCEL = "cancel"
	SIGNAL_KILL = "kill"
	SIGNAL_RESUME = "resume"
	SIGNAL_ACTION_PERFORMED = "action_performed"
	SIGNALS = [ SIGNAL_KILL, SIGNAL_PAUSE, SIGNAL_RESUME, SIGNAL_CANCEL, SIGNAL_ACTION_PERFORMED ]

	# Job states.
	# Basic state machine: initializing -> waiting -> running -> complete
	STATE_INITIALIZING = "initializing" # (i.e. preparing)
	STATE_WAITING = "waiting"
	STATE_RUNNING = "running"
	STATE_KILLING = "killing"
	STATE_CANCELLING = "cancelling"
	STATE_PAUSED = "paused"
	# Final states (ie indicating the Job is over)
	STATE_COMPLETE = "complete"
	STATE_CANCELLED = "cancelled"
	STATE_KILLED = "killed"
	STATE_ERROR = "error"
	STATE_CRASHED = "crashed" # unable to complete due to a server crash
	STARTED_STATES = [ STATE_RUNNING, STATE_KILLING, STATE_PAUSED ]
	FINAL_STATES = [ STATE_COMPLETE, STATE_CANCELLED, STATE_KILLED, STATE_ERROR, STATE_CRASHED ]
	STATES = [ STATE_INITIALIZING, STATE_WAITING, STATE_RUNNING, STATE_KILLING, STATE_CANCELLING, STATE_PAUSED, STATE_COMPLETE, STATE_CANCELLED, STATE_KILLED, STATE_ERROR ]

	BRANCH_SUCCESS = 0 # actually, this is more a "default" or "normal completion"
	BRANCH_ERROR = 1 # actualy, this is more a "abnormal termination"
	BRANCH_UNCONDITIONAL = 2 # actually, this is the list of root jobs, not depending on a previous execution status
	BRANCHES = [ BRANCH_ERROR, BRANCH_SUCCESS, BRANCH_UNCONDITIONAL ]
	
	##
	# To reimplement in your own subclasses
	##
	_type = "undefined"

	def __init__(self, name):
		"""
		@type  name: string
		@param name: a friendly name for this job
		"""
		# id is int
		self._id = getNewId()
		self._parent = None
		# Children Job instances are referenced here, according to their branch
		self._childBranches = {}
		for branch in self.BRANCHES: self._childBranches[branch] = []
		self._name = name
		self._state = self.STATE_INITIALIZING
		self._scheduledStartTime = time.time() # By default, immediate execution
		# the initial, input session variables.
		# passed as actual input session to execute() by the scheduler,
		# overriden by previous output sessions in case of campaign execution.
		self._scheduledSession = {}
		
		self._startTime = None
		self._stopTime = None
		
		# the final job result status (int, 0 = OK)
		self._result = None
		# the output, updated session variables after a complete execution
		self._outputSession = {}
		
		# the associated log filename with this job (within the docroot - not an absolute path)
		self._logFilename = None
		
		self._username = None
		# The complete docroot path to the source file of this job (including filename)
		# client-based source and non-source based jobs set it to None
		self._path = None
		
		# This mapping may override the injected initial session parameters on run.
		# The final initial session parameters are computed on run with it.
		self._sessionParametersMapping = {}

		self._mutex = threading.RLock()
	
	def setScheduledStartTime(self, timestamp):
		now = time.time()
		if timestamp is None:
			# No timestamp provided, we consider an "instant" run.
			# The 1s delay allow the client to register for logs.
			timestamp = now + 1.0; 
		elif timestamp < now:
			timestamp = now
		self._scheduledStartTime = timestamp
	
	def getScheduledStartTime(self):
		return self._scheduledStartTime

	def setScheduledSession(self, session):
		self._scheduledSession = session
	
	def getScheduledSession(self):
		return self._scheduledSession

	def getOutputSession(self):
		return self._outputSession
	
	def getUsername(self):
		return self._username
	
	def setUsername(self, username):
		self._username = username
	
	def setSessionParametersMapping(self, mapping):
		self._sessionParametersMapping = mapping
	
	def getId(self):
		return self._id
	
	def getParent(self):
		return self._parent
	
	def getName(self):
		return self._name

	def addChild(self, job, branch):
		"""
		Adds a job as a child to this job, in the success or error branch.
		
		@type  job: a Job instance
		@param job: the job to add as a child
		@type  branch: integer in self.BRANCHES
		@param branch: the child branch
		"""
		self._childBranches[branch].append(job)
		job._parent = self
	
	def setResult(self, result):
		self._result = result

	def getResult(self):
		"""
		Returns the job return code/result.
		Its value is job-type dependent.
		However, the following classification applies:
		0: complete
		1: cancelled
		2: killed
		3: runtime low-level error (process segfault, ...)
		4-9: other low-level errors 
		10-19: reserved
		20-29: preparation errors (not executed)
		30-49: reserved
		50-99: reserved for client-side retcode
		100+: userland set retcode
		
		@rtype: integer
		@returns: the job return code
		"""
		return self._result
	
	def getLogFilename(self):
		"""
		Returns a docroot-path to the job's log filename.
		
		@rtype: string
		@returns: the docroot-path to the job's log filename
		"""
		return self._logFilename

	def _lock(self):
		self._mutex.acquire()
	
	def _unlock(self):
		self._mutex.release()
	
	def getState(self):
		self._lock()
		ret = self._state
		self._unlock()
		return ret
	
	def isFinished(self):
		return self.getState() in self.FINAL_STATES
	
	def isStarted(self):
		return self.getState() in self.STARTED_STATES

	def setState(self, state):
		"""
		Automatically sends notifications according to the state.
		Also handles start/stop time assignments.
		"""
		self._lock()
		if state == self._state:
			# No change
			self._unlock()
			return
		
		self._state = state
		self._unlock()
		getLogger().info("%s changed state to %s" % (str(self), state))
		
		if state == self.STATE_RUNNING and not self._startTime:
			self._startTime = time.time()
		elif state in self.FINAL_STATES:
			if self._startTime and not self._stopTime:
				self._stopTime = time.time()
				getLogger().info("%s stopped, running time: %f" % (str(self), self._stopTime - self._startTime))
			else:
				# Never started. Keep start/stop and running time to None.
				getLogger().info("%s aborted" % (str(self)))
				# Always set a stop time, including for failed jobs - this is their failure time
				if not self._stopTime:
					self._stopTime = time.time()
			self.postRun()
			self.cleanup()
		
		self.notifyStateChange()

	def notifyStateChange(self):
		"""
		Dispatches JOB-EVENT
		notifications through the Event Manager
		"""
		jobDict = self.toDict()
		EventManager.instance().dispatchNotification(createJobEventNotification(self.getUri(), jobDict))
		EventManager.instance().dispatchNotification(createJobEventNotification('system:jobs', jobDict))

	def toDict(self, detailed = False):
		"""
		Returns the job info as a dict
		"""
		runningTime = None		
		if self._stopTime and self._startTime:
			runningTime = self._stopTime - self._startTime
	
		if self._parent:
			parentId = self._parent._id
		else:
			parentId = 0
	
		ret = { 'id': self._id, 'name': self._name, 
		'start-time': self._startTime, 'stop-time': self._stopTime,
		'running-time': runningTime, 'parent-id': parentId,
		'state': self.getState(), 'result': self._result, 'type': self._type,
		'username': self._username, 'scheduled-at': self._scheduledStartTime,
		'path': self._path, 'log-filename': self._logFilename }
		return ret

	def __str__(self):
		return "%s:%s (%s)" % (self._type, self._name, self.getUri())
	
	def getUri(self):
		"""
		Returns the job URI. Format: job:<id>

		@rtype: string
		@returns: the job URI.
		"""
		return "job:%s" % self._id
	
	def getType(self):
		return self._type
	
	def reschedule(self, at):
		"""
		Reschedule the job.
		Only possible if the job has not started yet.
		
		@type  at: timestamp
		@param at: the new recheduling
		
		@rtype: bool
		@returns: True if the rescheduling was ok, False otherwise
		"""

		self._lock()		
		if self._scheduledStartTime > time.time():
			self.setScheduledStartTime(at)
			self._unlock()
			self.notifyStateChange()
			return True
		else:
			self._unlock()
			return False
		
	##
	# Methods to implement in sub classes
	##	
	def handleSignal(self, sig):
		"""
		Called when a signal is sent to this job.
		The implementation for the default job does nothing.
		
		@type  sig: string (in self.SIGNALS)
		@param sig: the signal sent to this job.
		"""	
		getLogger().warning("%s received signal %s, no handler implemented" % (str(self), sig))

	def prepare(self):
		"""
		Prepares a job for a run.
		Called in state INITIALIZING. At the end, if OK, should be switched to WAITING
		or ERROR in case of a preparation error.
		
		@raises PrepareException: in case of any preparatin error.
		@rtype: None
		"""
		pass
		
	def preRun(self):
		"""
		Called by the scheduler when just about to call run() in a dedicated thread.
		
		Prepares the files that will be used for execution.
		In particular, enables to fill what is needed to provide a getLogFilename().
		"""
		pass
		
	def run(self, inputSession = {}):
		"""
		Called by the scheduler to run the job.
		Will be called in a specific thread, so your execution may take a while if needed.
		Just be sure to be able to stop it when receiving a SIGNAL_KILLED at least.
		
		@type  inputSession: dict of unicode of any object
		@param inputSession: the initial session parameters to pass to the job. May be empty (but not None),
		                     overriding the default parameter definitions as contained in job's metadata (if any).

		@rtype: integer
		@returns: the job _result
		"""
		self.setState(self.STATE_RUNNING)
		getLogger().warning("%s executed, but no execution handler implemented" % (str(self)))
		self.setState(self.STATE_COMPLETE)

	def getLog(self):
		"""
		Returns the current job's logs. 
		The result should be XML compliant. No prologue is required, however.
		
		@rtype: string (utf-8)
		@returns: the current job's logs, as an XML string. Returns None
		          if no log is available.
		"""		
		return None

	def postRun(self):
		"""
		Called when the job is complete, regardless of its status.
		Used to perform post-run actions, such as sending a notification, etc.
		
		The job cleanup code should be delegated to cleanup() instead.
		"""
		pass

	def cleanup(self):
		"""
		Called when the job is complete, regardless of its status.
		"""
		pass

################################################################################
# Job subclass: ATS
################################################################################

class AtsJob(Job):
	"""
	ATS Job.
	When ran, creates a TE from the ATS as a stand-alone Python scripts,
	the executes it through forking.
	
	Job signals are, for most of them, translated to Unix process signals.
	"""
	_type = "ats"

	def __init__(self, name, source, path):
		"""
		@type  name: string
		@type  source: string (utf-8)
		@param source: the complete ats file (containing metadata)
		
		@type  path: string (docroot path, for server-based source, or None (client-based source))
		"""
		Job.__init__(self, name)
		# string (as utf-8)
		self._source = source
		# The PID of the forked TE executable
		self._tePid = None

		self._path = path
		if self._path is None:
			# Fallback method for compatibility with old clients (Ws API < 1.2),
			# for which no path is provided by the client, even for server-based source jobs.
			#
			# So here we try to detect a repository path in its id/label/name:
			# We consider the atsPath to be /repository/ + the path indicated in
			# the ATS name. So the name (constructed by the client) should follow some rules 
			# to make it work correctly.

			self._path = '/repository/%s' % (self.getName())
		# Basic normalization
		if not self._path.startswith('/'):
			self._path = '/%s' % self._path
		
		# Some internal variables persisted to 
		# transit from prepare/preRun/Run
		self._tePreparedPackageDirectory = None
		self._baseDocRootDirectory = None
		self._baseName = None
		self._baseDirectory = None
		self._tePackageDirectory = None
		
		self._selectedGroups = None
		
		# For detailed info
		self._teInputSession = None
		self._teCommandLine = None
		self._teFilename = None

	def __repr__(self):
		return """ATS:
Job: %s
path: %s
tePreparedPackageDirectory: %s
baseDocRootDirectory: %s
baseName: %s
baseDirectory: %s
tePackageDirectory: %s
teCommandLine: %s
teFilename: %s
""" % (str(self), self._path, self._tePreparedPackageDirectory, self._baseDocRootDirectory, self._baseName,
self._baseDirectory, self._tePackageDirectory, self._teCommandLine, self._teFilename)

	def toDict(self, detailed = False):
		"""
		Returns the job info as a dict
		"""
		ret = Job.toDict(self, detailed)
		
		if detailed:
			# Add ATS-specific stuff here
			ret['source'] = base64.encodestring(self._source)
			ret['te-command-line'] = self._teCommandLine
			ret['te-filename'] = self._teFilename
			ret['te-input-parameters'] = self._teInputSession
			
		return ret

	def setSelectedGroups(self, selectedGroups):
		self._selectedGroups = selectedGroups
	
	def getSelectedGroups(self):
		return self._selectedGroups
	
	def handleSignal(self, sig):
		getLogger().info("%s received signal %s" % (str(self), sig))
		
		state = self.getState()
		try:
			if sig == self.SIGNAL_KILL and state != self.STATE_KILLED and self._tePid:
				# Violent sigkill sent to the TE and all its processes (some probes may implement things that
				# lead to a fork with another sid or process group, hence not receiving their parent's signal)
				self.setState(self.STATE_KILLING)
				for pid in Tools.getChildrenPids(self._tePid):
					getLogger().info("Killing child process %s..." % pid)
					try:
						os.kill(pid, signal.SIGKILL)
					except Exception as e:
						getLogger().error("Unable to kill %d: %s" % (pid, str(e)))

			elif sig == self.SIGNAL_CANCEL and self._tePid:
				if state == self.STATE_PAUSED:
					self.setState(self.STATE_CANCELLING)
					# Need to send a SIGCONT before the SIGINT to take the sig int into account
					os.kill(self._tePid, signal.SIGCONT)
					os.kill(self._tePid, signal.SIGINT)
				elif state == self.STATE_RUNNING:
					self.setState(self.STATE_CANCELLING)
					os.kill(self._tePid, signal.SIGINT)
			elif sig == self.SIGNAL_CANCEL and state == self.STATE_WAITING:
				self.setState(self.STATE_CANCELLED)
				
			elif sig == self.SIGNAL_PAUSE and state == self.STATE_RUNNING and self._tePid:
				os.kill(self._tePid, signal.SIGSTOP)
				self.setState(self.STATE_PAUSED)

			elif sig == self.SIGNAL_RESUME and state == self.STATE_PAUSED and self._tePid:
				os.kill(self._tePid, signal.SIGCONT)
				self.setState(self.STATE_RUNNING)
			
			# action() implementation: the user performed the requested action
			elif sig == self.SIGNAL_ACTION_PERFORMED and state == self.STATE_RUNNING and self._tePid:
				os.kill(self._tePid, signal.SIGUSR1)
			
		except Exception as e:
			getLogger().error("%s: unable to handle signal %s: %s" % (str(self), sig, str(e)))

	def cleanup(self):
		getLogger().info("%s: cleaning up..." % (str(self)))
		# Delete the prepared TE, if any
		if self._tePreparedPackageDirectory:
			try:
				shutil.rmtree(self._tePreparedPackageDirectory, ignore_errors = True)
			except Exception as e:
				getLogger().warning("%s: unable to remove temporary TE package directory '%s': %s" % (str(self), self._tePreparedPackageDirectory, str(e)))

	def prepare(self):
		"""
		Prepare a job for a run
		Called in state INITIALIZING. At the end, if OK, should be switched to WAITING
		or ERROR in case of a preparation error.
		
		For an ATS, this:
		- verifies that the dependencies are found.
		- build the TE and its dependencies into a temporary TE directory tree, as a Python egg
		  ${tePreparedPackageDirectory}/src: contains the egg tree:
			 - __main__.py: the TE main file (generated by TEFactory)
			 - *.py: 'system' dependencies for the TE
			 - repository/: userland dependencies for the TE
			${tePreparedPackageDirectory}/ats.egg: the egg once created. 
		- this temporary TE directory tree will be moved to $docroot/archives/ upon run().
		
		This avoids the user change any source code after submitting the job.
		
		@raises PrepareException: in case of any preparation error.
		
		@rtype: None
		"""
		
		def handleError(code, desc):
			getLogger().error("%s: %s" % (str(self), desc))
			self.setResult(code)
			self.setState(self.STATE_ERROR)
			raise PrepareException(desc)
		
		getLogger().debug("%s: preparing job %s" % (str(self), repr(self)))

		# Check the metadata and the language API
		metadata = TEFactory.getMetadata(self._source)
		if metadata.api:
			adapterModuleName = cm.get("testerman.te.python.module.api.%s" % metadata.api)
			if not adapterModuleName:
				getLogger().debug("Exception while creating the TE: unsupported language API %s" % metadata.api)
				return handleError(26, "Unsupported language API %s" % metadata.api)
			adapterDependencies = cm.get("testerman.te.python.dependencies.api.%s" % metadata.api)
			if not adapterDependencies:
				adapterDependencies = []
			else:
				adapterDependencies = [x.strip() for x in adapterDependencies.split(',')]
		else:
			adapterModuleName = cm.get("testerman.te.python.ttcn3module")


		# Build the TE, as a standalone, runnable Python egg
		
		getLogger().info("%s: resolving dependencies for source filename %s..." % (str(self), self._path))
		try:
			# Check if we should constraint our dependencies search to a package directory
			packagePath = FileSystemManager.instance().getPackageFor(self._path)
			if not packagePath:
				moduleRootDir = '/repository'
			else:
				moduleRootDir = packagePath + '/src'
			userlandDependencies = DependencyResolver.python_getDependencyFilenames(
				source = self._source, 
				recursive = True,
				sourceFilename = self._path,
				moduleRootDir = moduleRootDir)
		except Exception as e:
			desc = "unable to resolve dependencies: %s" % str(e)
			return handleError(25, desc)

		getLogger().info("%s: resolved deps:\n%s" % (str(self), userlandDependencies))

		getLogger().info("%s: creating TE..." % str(self))

		# For ATS from anonymous packages, the atsPath (self._path) is a local path, not
		# a relative path to the package:src/ folder.
		# We need to take this into account and do some substitution
		atsDirInTePackage = os.path.split(self._path)[0]
		if packagePath:
			atsDirInTePackage = os.path.normpath('/repository/%s' % atsDirInTePackage[len(packagePath+'/src/'):])
		if atsDirInTePackage.startswith('/'):
			atsDirInTePackage = atsDirInTePackage[1:]

		try:
			te = TEFactory.createTestExecutable(self.getName(), self._source, atsDirInTePackage = atsDirInTePackage)
		except Exception as e:
			getLogger().debug("Exception while creating the TE: %s\n%s" % (str(e), Tools.getBacktrace()))
			return handleError(26, str(e))
		
		# TODO: delegate TE check to the TE factory (so that if e need to use another builder that
		# build something else than a Python script, it contains its own checker)
		getLogger().info("%s: verifying TE..." % str(self))
		try:
			parser.suite(te).compile()
			compiler.parse(te)
		except SyntaxError, e:
			t = te.split('\n')
			line = t[e.lineno]
			context = '\n'.join([ "%s: %s" % (x, t[x]) for x in range(e.lineno-5, e.lineno+5)])
			desc = "syntax/parse error: %s:\n%s\ncontext:\n%s" % (str(e), line, context)
			return handleError(21, desc)
		except Exception as e:
			desc = "unable to check TE: %s" % (str(e))
			return handleError(22, desc)

		getLogger().info("%s: preparing TE files..." % str(self))
		# Now create a TE temporary package directory containing the prepared TE and all its dependencies.
		# Will be moved to archives/ upon run()
		self._tePreparedPackageDirectory = tempfile.mkdtemp()
		# We will now create a python egg file containing everything needed to run the TE independently from the server
		
		# The egg root dir is self._tePreparedPackageDirectory
		# From here:
		# /ats: contains the userland code, and the core Testerman dependencies
		# __main__.py: contains what is needed to bootstrap the egg for a python file.egg simple run
		# /EGG-INFO: contains the egg metadata
		
		tePackageDirectory = self._tePreparedPackageDirectory
		eggInfoDirectory = self._tePreparedPackageDirectory + '/src/EGG-INFO'
		try:
#			os.mkdir(tePackageDirectory)
			os.mkdir(tePackageDirectory + '/src')
			os.mkdir(eggInfoDirectory)
		except Exception as e:
			desc = 'unable to create TE package: %s' % (str(e))
			return handleError(20, desc)

		# The TE code is in __main__.py
		teFilename = "%s/src/__main__.py" % (tePackageDirectory)
		try:
			f = open(teFilename, 'w')
			f.write(te)
			f.close()
		except Exception as e:
			desc = 'unable to write TE to "%s": %s' % (teFilename, str(e))
			return handleError(20, desc)

		# This is the list of source files to include in our egg
		# Contains relative paths to ${tePreparedPackageDirectory}/src
		sources = []

		# Copy dependencies to the TE base dir
		getLogger().info("%s: preparing userland dependencies..." % (str(self)))
		adjustedUserlandDependencies = []
		try:
			for filename in userlandDependencies:
				# filename is a docroot-path

				depContent = FileSystemManager.instance().read(filename)
				# Alter the content (additional includes, etc)
				depContent = TEFactory.createDependency(depContent)

				# Target, local, absolute filename for the dep
				# If we are in a package, we need to strip the package dir (until src)
				# specific part.
				if packagePath:
					filename = 'repository/%s' % filename[len(packagePath+'/src/'):]
				adjustedUserlandDependencies.append(filename)

				targetFilename = '%s/src/%s' % (tePackageDirectory, filename)

				# Create required directory structure, with __init__.py file, if needed
				currentdir = tePackageDirectory + '/src'
				for d in filter(lambda x: x, filename.split('/')[:-1]):
					localdir = '%s/%s' % (currentdir, d)
					currentdir = localdir
					try:
						os.mkdir(localdir)
					except: 
						pass
					# Touch a __init__.py file
					getLogger().debug("Creating __init__.py in %s..." % localdir)
					
					initFilename = '%s/__init__.py' % localdir
					f = open(initFilename, 'w')
					f.close()
					# Register it as being part of our source file in package (relative to the package dir)
					initRelativeFilename = ('%s/__init__.py' % localdir)[len(tePackageDirectory+'/src'):]
					if not initRelativeFilename in sources:
						sources.append(initRelativeFilename)

				# Now we can dump the module
				f = open(targetFilename, 'w')
				f.write(depContent)
				f.close()
		except Exception as e:
			desc = 'unable unable to create dependency %s to "%s": %s' % (filename, targetFilename, str(e))
			return handleError(20, desc)
		

		getLogger().info("%s: preparing core dependencies..." % (str(self)))
		# So, in ${tePackageDirectory}/src, we have:
		# - the main test executable (__main__.py)
		# - all its userland dependencies
		# Now copy the core dependencies
		# These dependencies depend on the selected language api / adapter module
		coreDependencies = adapterDependencies
		
		for coreDep in coreDependencies:
			# Let's copy the dependencies
			try:
				shutil.copyfile("%s/%s" % (cm.get_transient('ts.server_root'), coreDep), "%s/src/%s" % (tePackageDirectory, coreDep))
			except Exception as e:
				desc = 'unable unable to copy core dependency %s: %s' % (coreDep, str(e))
				return handleError(20, desc)
			

		getLogger().info("%s: preparing egg packaging..." % (str(self)))
		# Now completing the directory tree to create a Python egg
		sources.append('__main__.py')

		for dep in adjustedUserlandDependencies:
			sources.append('%s' % dep)
		for dep in coreDependencies:
			sources.append('%s' % dep)

		pkgInfo = """Metadata-Version: 1.0
Name: testerman-te
Version: 1.0.0
Summary: Testerman TE
Author: TBD
Author-email: TBD
License: N/A
Description: 
              Testerman Test Executable.
              
Keywords: testerman
Platform: UNKNOWN
"""
		try:
			# The Python egg metadata are in EGG-INFO
			f = open("%s/src/EGG-INFO/dependency_links.txt" % (self._tePreparedPackageDirectory), 'w')
			f.write("\n")
			f.close()
			f = open("%s/src/EGG-INFO/top_level" % (self._tePreparedPackageDirectory), 'w')
			f.write("ats\n")
			f.close()
			f = open("%s/src/EGG-INFO/PKG-INFO" % (self._tePreparedPackageDirectory), 'w')
			f.write(pkgInfo)
			f.close()
			f = open("%s/src/EGG-INFO/zip-safe" % (self._tePreparedPackageDirectory), 'w')
			f.write("\n")
			f.close()
			f = open("%s/src/EGG-INFO/SOURCES.txt" % (self._tePreparedPackageDirectory), 'w')
			f.write('\n'.join(sources))
			f.close()
		except Exception as e:
			desc = 'unable to write Python egg metadata: %s' % (str(e))
			return handleError(20, desc)
		
		# So now self._tePreparedPackageDirectory/src
		# contains everything needed to create an independent, runnable egg.
		# However, plugins (probes & codecs) are not included in the egg yet.
		
		# Create the egg
		getLogger().info("%s: creating python egg..." % (str(self)))
		egg = zipfile.ZipFile("%s/ats.egg" % self._tePreparedPackageDirectory, 'w', zipfile.ZIP_DEFLATED)
		sources.append('EGG-INFO/dependency_links.txt')
		sources.append('EGG-INFO/top_level')
		sources.append('EGG-INFO/PKG-INFO')
		sources.append('EGG-INFO/zip-safe')
		sources.append('EGG-INFO/SOURCES.txt')
		for s in sources:
			getLogger().debug("%s: adding relative file %s (abs %s) to egg..." % (str(self), s, "%s/src/%s" % (self._tePreparedPackageDirectory, s)))
			egg.write("%s/src/%s" % (self._tePreparedPackageDirectory, s), s)
		egg.close()
		
		getLogger().info("%s: cleaning up temporary files..." % (str(self)))
		try:
			shutil.rmtree("%s/src" % self._tePreparedPackageDirectory, ignore_errors = True)
		except Exception as e:
			getLogger().warning("%s: unable to clean up temporary files after creating egg: %s" % (str(self), str(e)))
		
		# OK, we're ready. The egg is waiting as ${self._tePreparedPackagedDirectory}/ats.egg.
		self.setState(self.STATE_WAITING)

	def preRun(self):
		"""
		Called by the scheduler when just about to call run() in a dedicated thread.
		
		Prepares the files that will be used for execution.
		In particular, enables to fill what is needed to provide a getLogFilename().
		"""
		# Create some paths related to the final TE tree in the docroot

		# docroot-path for all TE packages for this ATS
		self._baseDocRootDirectory = os.path.normpath("/%s/%s" % (cm.get_transient("constants.archives"), self.getName()))
		# Base name for execution log and TE package dir
		# The basename is unique per execution: datetime+ms+jobid, formatted so that the jobid looks to be part of the timestamp ms (and more) precision.
		# This enables old QTesterman clients to parse the log filename to display a visually meaningful date for it.
		timestamp = time.time()
		datetimems = time.strftime("%Y%m%d-%H%M%S", time.localtime(timestamp))  + "-%3.3d" % int((timestamp * 1000) % 1000)
		self._basename = "%s%s_%s" % (datetimems, self.getId(), self.getUsername())
		# Corresponding absolute local path
		self._baseDirectory = os.path.normpath("%s%s" % (cm.get("testerman.document_root"), self._baseDocRootDirectory))
		# final TE package dir (absolute local path)
		self._tePackageDirectory = "%s/%s" % (self._baseDirectory, self._basename)
		# self._logFilename is a docroot-path for a retrieval via Ws
		self._logFilename = "%s/%s.log" % (self._baseDocRootDirectory, self._basename)

		try:
			os.makedirs(self._baseDirectory)
		except: 
			pass

	def run(self, inputSession = {}):
		"""
		Prepares the TE, Starts a prepared TE, and only returns when it's over.
		
		inputSession contains parameters values that overrides default ones.
		The default ones are extracted (during the TE preparation) from the
		metadata embedded within the ATS source.
		
		The TE execution tree is this one (prepared by a call to self.prepare())
		execution root:
		%(docroot)/%(archives)/%(ats_name)/
		contains:
			%Y%m%d-%H%M%S_%(user).log : the execution logs
			%Y%m%d-%H%M%S_%(user) : directory containing the TE package:
				te_mtc.py : the main TE
				repository/... : the (userland) modules the TE depends on
				This directory is planned to be packaged to be executed on
				any Testerman environment. (it may still evolve until so)
		
		The TE execution is performed from the directory containing the TE package.
		The module search path is set to:
		- first the path of the ATS (for local ATSes, defaulted to 'repository') as a docroot-path,
		- then 'repository'
		- then the Testerman system include paths
		
		@type  inputSession: dict[unicode] of unicode
		@param inputSession: the session parameters for this run.
		
		@rtype: int
		@returns: the TE return code
		"""
		baseDocRootDirectory = self._baseDocRootDirectory
		baseDirectory = self._baseDirectory
		tePackageDirectory = self._tePackageDirectory
		basename = self._basename
		
		# Move the prepared, temporary TE folder tree to its final location in archives
		try:
			shutil.move(self._tePreparedPackageDirectory, tePackageDirectory)
			self._tePreparedPackageDirectory = None
		except Exception as e:
			getLogger().error('%s: unable to move prepared TE and its dependencies to their final locations: %s' % (str(self), str(e)))
			self.setResult(24)
			self.setState(self.STATE_ERROR)
			return self.getResult()

		# Prepare input/output session files
		inputSessionFilename = "%s/%s.input.session" % (tePackageDirectory, basename)
		outputSessionFilename = "%s/%s.output.session"  % (tePackageDirectory, basename)
		# Create the actual input session:
		# the default session, from metadata, overriden with user input session values.
		# FIXME: should we accept user input parameters that are not defined in default parameters, i.e.
		# in ATS signature ?
		# default session
		try:
			scriptSignature = TEFactory.getMetadata(self._source).getSignature()
			if scriptSignature is None:
				getLogger().warning('%s: unable to extract script signature from ATS. Missing metadata ?' % (str(self)))
				scriptSignature = {}
		except Exception as e:
			getLogger().error('%s: unable to extract ATS signature from metadata: %s' % (str(self), str(e)))
			self.setResult(23)
			self.setState(self.STATE_ERROR)
			return self.getResult()
		
		# The merged input session
		mergedInputSession = mergeSessionParameters(inputSession, scriptSignature, self._sessionParametersMapping)
		self._teInputSession = mergedInputSession
		
		getLogger().info('%s: using merged input session parameters:\n%s' % (str(self), '\n'.join([ '%s = %s (%s)' % (x, y, repr(y)) for x, y in mergedInputSession.items()])))
		
		# Dumps input session to the corresponding file
		try:
			dumpedInputSession = TEFactory.dumpSession(mergedInputSession)
			f = open(inputSessionFilename, 'w')
			f.write(dumpedInputSession)
			f.close()
		except Exception as e:
			getLogger().error("%s: unable to create input session file: %s" % (str(self), str(e)))
			self.setResult(24)
			self.setState(self.STATE_ERROR)
			return self.getResult()
		
		getLogger().info("%s: building TE command line..." % str(self))
		# teLogFilename is an absolute local path
		teLogFilename = "%s/%s.log" % (baseDirectory, basename)
		teFilename = "%s/ats.egg" % (tePackageDirectory)
		# Get the TE command line options
		cmd = TEFactory.createCommandLine(
			jobId = self.getId(), 
			teFilename = teFilename, 
			logFilename = teLogFilename, 
			inputSessionFilename = inputSessionFilename, 
			outputSessionFilename = outputSessionFilename, 
			selectedGroups = self.getSelectedGroups())
		executable = cmd['executable']
		args = cmd['args']
		env = cmd['env']
		
		# Show a human readable command line for debug purposes
		cmdLine = '%s %s' % ( ' '.join(['%s=%s' % (x, y) for x, y in env.items()]), ' '.join(args))
		self._teCommandLine = cmdLine
		self._teFilename = teFilename[len(cm.get('testerman.document_root')):] # fill a teFilename that is relative to the docroot
		getLogger().info("%s: executing TE using:\n%s\nEnvironment variables:%s" % (str(self), cmdLine, '\n'.join(['%s=%s' % x for x in env.items()])))

		# Fork and run it
		try:
			pid = os.fork()
			if pid:
				# Wait for the child to finish
				self._tePid = pid
				self.setState(self.STATE_RUNNING)
				# actual retcode (< 256), killing signal, if any
				getLogger().info("%s: Waiting for TE to complete (pid %s)..." % (str(self), pid))
				res = os.waitpid(pid, 0)
				(retcode, sig) = divmod(res[1], 256)
				self._tePid = None
			else:
				# forked child: exec with the TE once moved to the correct dir
				os.chdir(tePackageDirectory)
				os.execve(executable, args, env)
				# Done with the child.
		except Exception as e:
			getLogger().error("%s: unable to execute TE: %s" % (str(self), str(e)))
			self._tePid = None
			self.setResult(23)
			self.setState(self.STATE_ERROR)
			# Clean input session filename
			try:
				os.unlink(inputSessionFilename)
			except Exception as e:
				getLogger().warning("%s: unable to delete input session file: %s" % (str(self), str(e)))
			return self.getResult()

		# Normal continuation, once the child has returned.
		getLogger().info("%s: TE completed" % str(self))
		if sig > 0:
			getLogger().info("%s: TE terminated with signal %d" % (str(self), sig))
			# In case of a kill, make sure we never return a "OK" retcode
			# For other signals, the retcode is already controlled by the TE that ensures specific retcodes.
			if sig == signal.SIGKILL:
				self.setResult(2)
				self.setState(self.STATE_KILLED)
			else:
				# Other signals (segfault, ...)
				self.setResult(3)
				self.setState(self.STATE_ERROR)
		else:
			getLogger().info("%s: TE returned, retcode %d" % (str(self), retcode))
			self.setResult(retcode)
			# Maps standard retcode to states
			if retcode == 0 or retcode == 4: # RETURN_CODE_OK || RETURN_CODE_OK_WITH_FAILED_TC
				self.setState(self.STATE_COMPLETE)
			elif retcode == 1: # RETURN_CODE_CANCELLED
				self.setState(self.STATE_CANCELLED)
			else: # Other errors
				self.setState(self.STATE_ERROR)
		
		# Read output session
		try:
			f = open(outputSessionFilename, 'r')
			self._outputSession = TEFactory.loadSession(f.read())
			f.close()
			getLogger().info('%s: output session parameters:\n%s' % (str(self), '\n'.join([ '%s = %s (%s)' % (x, y, repr(y)) for x, y in self._outputSession.items()])))
		except Exception as e:
			getLogger().warning("%s: unable to read output session file: %s" % (str(self), str(e)))

		# Clean input & output session filename
		try:
			os.unlink(inputSessionFilename)
		except Exception as e:
			getLogger().warning("%s: unable to delete input session file: %s" % (str(self), str(e)))
		try:
			os.unlink(outputSessionFilename)
		except Exception as e:
			getLogger().warning("%s: unable to delete output session file: %s" % (str(self), str(e)))
		
		return self.getResult()

	def getLog(self):
		"""
		Returns the current known log.
		"""
		if self._logFilename:
			try:
				# Logs are locally generated, so no need to access them through the FileSystemManager.
				absoluteLogFilename = os.path.normpath("%s%s" % (cm.get("testerman.document_root"), self._logFilename))
				f = open(absoluteLogFilename, 'r')
				fcntl.flock(f.fileno(), fcntl.LOCK_EX)
				res = '<?xml version="1.0" encoding="utf-8" ?>\n<ats>\n%s</ats>' % f.read()
				f.close()
				return res
			except Exception as e:
				if self.isFinished():
					# The log was deleted. So raise the exception - we should have been able to read it.
					raise e
				else:
					# The log file may have not been created yet.
					# Just return an empty log.
					return '<?xml version="1.0" encoding="utf-8" ?>\n<ats>\n</ats>'
		else:
			# The log file has not been initialized yet.
			return '<?xml version="1.0" encoding="utf-8" ?>\n<ats>\n</ats>'


################################################################################
# Job subclass: Campaign Group for parallel execution
################################################################################

class GroupJob(Job):
	"""
	This is a pseudo-job: 
	it is not registered into our job queue, cannot be monitored, cannot
	receive signals,
	this is just a container that references the jobs to be executed
	in a dedicated thread.
	"""
	_type = "group"

	def __init__(self, name):
		"""
		@type  name: string
		"""
		Job.__init__(self, "<<group:%s>>" % name)

	def addChild(self, job, branch):
		"""
		Adds a job as a child to this group job, in the unconditional branch, always.
		
		The parent of the added job will be the first ancestor that is NOT a parallel group.
		
		@type  job: a Job instance
		@param job: the job to add as a child
		@type  branch: integer in self.BRANCHES
		@param branch: the child branch
		"""
		self._childBranches[Job.BRANCH_UNCONDITIONAL].append(job)
		ancestor = self
		while isinstance(ancestor, GroupJob): 
			ancestor = ancestor._parent
		job._parent = ancestor

################################################################################
# Job subclass: Campaign
################################################################################

class GroupThreads:
	def __init__(self):
		self._list = []
		self._mutex = threading.RLock()
	
	def append(self, groupThread):
		self._mutex.acquire()
		self._list.append(groupThread)
		self._mutex.release()
	
	def getList(self):
		self._mutex.acquire()
		ret = [ x for x in self._list ]
		self._mutex.release()
		return ret
	
	def __getstate__(self):
		return None
	
	def __setstate__(self, state):
		self._mutex = threading.RLock()

class CampaignJob(Job):
	"""
	Campaign Job.
	
	Job signals are, for most of them, forwarded to the current child job.
	
	Campaign state machine:
	to discuss. Should we map the campaign state with the current child job's ?
	"""
	_type = "campaign"

	def __init__(self, name, source, path):
		"""
		@type  name: string
		@type  source: string (utf-8)
		@param source: the complete campaign source file (containing metadata)
		"""
		Job.__init__(self, name)
		# string (as utf-8)
		self._source = source
		# The PID of the forked TE executable
		self._tePid = None

		self._path = path
		if self._path is None:
			# Fallback method for compatibility with old clients (Ws API < 1.2),
			# for which no path is provided by the client, even for server-based source jobs.
			#
			# So here we try to detect a repository path in its id/label/name:
			# We consider the atsPath to be /repository/ + the path indicated in
			# the ATS name. So the name (constructed by the client) should follow some rules 
			# to make it work correctly.
			self._path = '/%s/%s' % (cm.get_transient('constants.repository'), '/'.join(self.getName().split('/')[:-1]))
		# Basic normalization
		if not self._path.startswith('/'):
			self._path = '/%s' % self._path
		
		self._absoluteLogFilename = None
		
		# The list of parallel groups that this campaign started - we have to wait for all of them to complete
		# to complete the campaign
		# Must be mutex protected
		self._groupThreads = GroupThreads()
	
	def handleSignal(self, sig):
		"""
		So, what should we do ?
		"""
		getLogger().info("%s received signal %s" % (str(self), sig))
		
		state = self.getState()
		try:
			if sig == self.SIGNAL_CANCEL:
				if state == self.STATE_WAITING:
					self.setState(self.STATE_CANCELLED)
				else:
					self.setState(self.STATE_CANCELLING)
			else:
				getLogger().warning("%s: received unhandled signal %s" % (str(self), sig))
			
		except Exception as e:
			getLogger().error("%s: unable to handle signal %s: %s" % (str(self), sig, str(e)))

	def prepare(self):
		"""
		Prepares the campaign, verifying the availability of each included job.

		During the campaign preparation, we just check that all child job sources
		are present within the directory.
		WARNING: we do not snapshot all ATSes and campaigns nor prepare them prior to 
		executing the campaign. 
		In particular, if an child ATS is changed after the campaign has been started
		or scheduled, before the child ATS is started, the updated ATS will be taken
		into account.
		"""
		# First step, parse
		getLogger().info("%s: parsing..." % str(self))
		try:
			self._parse()
		except Exception as e:
			desc = "%s: unable to prepare the campaign: %s" % (str(self), str(e))
			getLogger().error(desc)
			self.setResult(25) # Consider a dependency error ?
			self.setState(self.STATE_ERROR)
			raise PrepareException(e)
		
		getLogger().info("%s: parsed OK" % str(self))
		self.setState(self.STATE_WAITING)

	def preRun(self):
		"""
		Prepares the files that will be used for execution.
		In particular, enables to fill what is needed to provide a getLogFilename().
		"""
		# docroot-path for all files related to this job
		baseDocRootDirectory = os.path.normpath("/%s/%s" % (cm.get_transient("constants.archives"), self.getName()))
		# Corresponding absolute local path
		baseDirectory = os.path.normpath("%s%s" % (cm.get("testerman.document_root"), baseDocRootDirectory))
		# The basename is unique per execution: datetime+ms+jobid, formatted so that the jobid looks to be part of the timestamp ms (and more) precision.
		# This enables old QTesterman clients to parse the log filename to display a visually meaningful date for it.
		timestamp = time.time()
		datetimems = time.strftime("%Y%m%d-%H%M%S", time.localtime(timestamp))  + "-%3.3d" % int((timestamp * 1000) % 1000)
		basename = "%s%s_%s" % (datetimems, self.getId(), self.getUsername())
		self._logFilename = "%s/%s.log" % (baseDocRootDirectory, basename)
		self._absoluteLogFilename = "%s/%s.log" % (baseDirectory, basename)

		try:
			os.makedirs(baseDirectory)
		except: 
			pass
		
	def run(self, inputSession = {}):
		"""
		Prepares the campaign, starts it, and only returns when it's over.
		
		inputSession contains parameters values that overrides default ones.
		The default ones are extracted (during the campaign preparation) from the
		metadata embedded within the campaign source.
		
		A campaign is prepared/expanded to a collection of child jobs,
		that can be ATSes or campaigns.
		
		A campaign job has a dedicated execution directory:
		%(docroot)/%(archives)/%(campaign_name)/
		contains:
			%Y%m%d-%H%M%S_%(user).log : the main execution log
		
		Each ATS job created from this campaigns are prepared and executed as if they
		were executed separately.
		
		A campaign always schedules a child job for an immediate execution, i.e. no
		child job is prepared/scheduled in advance.
		
		@type  inputSession: dict[unicode] of unicode
		@param inputSession: the override session parameters.
		
		@rtype: int
		@returns: the campaign return code
		"""
		
		# Now, execute the child jobs
		self._logEvent('event', 'campaign-started', {'id': self._name})
		self.setState(self.STATE_RUNNING)
		self._run(callingJob = self, inputSession = inputSession)
		self._waitForGroupsCompletion()
		if self.getState() == self.STATE_RUNNING:
			self.setResult(0) # a campaign always returns OK for now. Unless cancelled, etc ?
			self.setState(self.STATE_COMPLETE)
		elif self.getState() == self.STATE_CANCELLING:
			self.setResult(1)
			self.setState(self.STATE_CANCELLED)
		self._logEvent('event', 'campaign-stopped', {'id': self._name, 'result': self.getResult()})
		
		return self.getResult()

	def toXml(self, element, attributes, value = ''):
		return u'<%s %s>%s</%s>' % (element, " ".join(map(lambda e: '%s="%s"' % (e[0], str(e[1])), attributes.items())), value, element)

	def _logEvent(self, level, event, attributes, logClass = 'event', value = ''):
		"""
		Sends a log event notification through Il.
		Will be dumped by Il server.
		"""
		attributes['class'] = logClass
		attributes['timestamp'] = time.time()
		xml = self.toXml(event, attributes, value)
		
		notification = Messages.Notification("LOG", self.getUri(), "Il", "1.0")
		if self._absoluteLogFilename:
			notification.setHeader("Log-Filename", self._absoluteLogFilename)
		notification.setHeader("Log-Class", level)
		notification.setHeader("Log-Timestamp", time.time())
		notification.setHeader("Content-Encoding", "utf-8")
		notification.setHeader("Content-Type", "application/xml")
		notification.setBody(xml.encode('utf-8'))
		EventManager.instance().handleIlNotification(notification)

	def _run(self, callingJob, inputSession, branch = Job.BRANCH_UNCONDITIONAL):
		"""
		Runs all the available jobs in a branch.
		
		When a subjob is finished, resursively called according to 
		its status to execute its selected branch.
		"""
		if self.getState() != self.STATE_RUNNING:
			# We stop our recursion (killed, cancelled, etc).
			return
		
		try:
			scriptSignature = TEFactory.getMetadata(self._source).getSignature()
			if scriptSignature is None:
				getLogger().warning('%s: unable to extract script signature from Campaign. Missing metadata ?' % (str(self)))
				scriptSignature = {}
		except Exception as e:
			getLogger().error('%s: unable to extract Campaign signature from metadata: %s' % (str(self), str(e)))
			self.setResult(23)
			self.setState(self.STATE_ERROR)
			return self.getResult()

		mergedInputSession = mergeSessionParameters(inputSession, scriptSignature, callingJob._sessionParametersMapping)
		getLogger().info('%s: using merged input session parameters:\n%s' % (str(self), '\n'.join([ '%s = %s (%s)' % (x, y, repr(y)) for x, y in mergedInputSession.items()])))

		# Now, the child jobs according to the branch
		for job in callingJob._childBranches[branch]:
			if self.getState() != self.STATE_RUNNING:
				# We stop our loop (killed, cancelled, etc).
				return

			if isinstance(job, GroupJob):
				getLogger().info("%s: executing parallel group %s, invoked by %s, on branch %s" % (str(self), str(job), str(callingJob), branch))
				# Parallel group
				session = mergeSessionParameters(inputSession, scriptSignature, job._sessionParametersMapping)
				jobThread = threading.Thread(target = lambda j = job, s = session: self._run(j, s, branch = Job.BRANCH_UNCONDITIONAL))
				self._groupThreads.append(jobThread)
				jobThread.start()
				# Do not wait for the thread to end now
				# Do not insert any <include> statement in the campaign's logs
				# Go to next sibling after starting
				getLogger().info("%s: parallel group %s run, invoked by %s, on branch %s" % (str(self), str(job), str(callingJob), branch))
				continue

			# "Normal" job - synchronous execution
			# The job appears in the queue
			instance().registerJob(job)

			getLogger().info("%s: preparing child job %s, invoked by %s, on branch %s" % (str(self), str(job), str(callingJob), branch))
			prepared = False
			try:
				job.prepare()
				prepared = True
			except Exception:
				prepared = False

			if prepared:
				getLogger().info("%s: starting child job %s, invoked by %s, on branch %s" % (str(self), str(job), str(callingJob), branch))
				job.preRun()
				jobThread = threading.Thread(target = lambda: job.run(mergedInputSession))
				jobThread.start()
				# Now wait for the job to complete.
				jobThread.join()
				# For now, we only log an include event when the child job is over - leading to no realtime support for campaign logs,
				# but a kind of "half-realtime": a client such as QTesterman will be updated every time a child job is complete.
				self._logEvent('core', 'include', {'url': "testerman://%s" % job.getLogFilename()}, logClass = 'core')
				ret = job.getResult()
				getLogger().info("%s: started child job %s, invoked by %s, on branch %s returned %s" % (str(self), str(job), str(callingJob), branch, ret))
			else:
				ret = job.getResult()

			if ret == 0:
				nextBranch = self.BRANCH_SUCCESS
				nextInputSession = job.getOutputSession()
			else:
				nextBranch = self.BRANCH_ERROR
				# In case of an error, the output session may be empty. If empty, we use the initial session.
				nextInputSession = job.getOutputSession()
				if not nextInputSession:
					nextInputSession = inputSession

			self._run(job, nextInputSession, nextBranch)


	def _waitForGroupsCompletion(self):
		"""
		Waits for all parallel groups to complete.
		"""
		for t in self._groupThreads.getList():
			t.join()
			
	def _parse(self):
		"""
		Parses the source file, check that all referenced sources exist.
		
		A campaign format is a tree based on indentation:
		
		job
			job
			job
				job
		job
		
		The indentation is defined by the number of indent characters.
		Validindent characters are \t and ' '.
		
		a job line is formatted as:
		[<branch> ]<type> [<path>] [groups <groups>] [with <mapping>]
		where:
		<branch>, if present, is a keyword in *, on_error, on_success
		<type> is a keyword in 'ats', 'campaign', group (for now)
		<path> is a relative (not starting with a /) or 
		       absolute path (/-starting) within the repository.
		       path is required for type == ats and campaign only. group does not take any path			 
		<mapping>, if present, is formatted as KEY=value[,KEY=value]*
		<groups>, if present, is formatted as GROUP[,GROUP]*
		Branch values '*', 'on_error' indicate that the job should be
		executed if its parent returns a non-0 result ('on error' branch).
		
		Comments are indicated with a #.
		"""
		getLogger().info("%s: parsing campaign file" % str(self))

		# The path of the campaign within the docroot.
		path = self._path
		
		indent = 0
		currentParent = self
		lastCreatedJob = None
		lc = 0
		for line in self._source.splitlines():
			lc += 1
			# Remove comments
			line = line.split('#', 1)[0].rstrip()
			if not line:
				continue # empty line
			m = re.match(r'(?P<indent>\s*)((?P<branch>on_error|\*)\s+)?(?P<type>\w+)\s+(?P<filename>[^\s]+)(\s+groups\s+(?P<groups>[^\s]+))?(\s+with\s+(?P<mapping>.*)\s*)?', line)
			if not m:
				raise Exception('Parse error at line %s: invalid line format' % lc)
			
			type_ = m.group('type')
			filename = m.group('filename') # also used to name a group
			branch = m.group('branch') # may be None
			mapping = m.group('mapping') # may be None
			groups = m.group('groups') # may be None
			indentDiff = len(m.group('indent')) - indent
			indent = indent + indentDiff
			
			getLogger().info("Parsed line:\nfilename: %s\njob type: %s\nbranch: %s\nmapping: %s\ngroups: %s\n" % (filename, type_, branch, mapping, groups))
			
			# Type validation
			if not type_ in [ 'ats', 'campaign', 'group' ]:
				raise Exception('Error at line %s: invalid job type (%s)' % (lc, type_))

			# Indentation validation, parent selection
			if indentDiff > 1:
				raise Exception('Parse error at line %s: invalid indentation (too deep)' % lc)
			# Get the current parent
			elif indentDiff == 1:
				# the current parent is the previous created job
				if not lastCreatedJob:
					raise Exception('Parse error at line %s: invalid indentation (invalid initial indentation)' % lc)
				else:
					currentParent = lastCreatedJob
			elif indentDiff == 0:
				# the current parent remains the parent
				pass
			else:
				# negative indentation. 
				for _ in range(abs(indentDiff)):
					currentParent = currentParent.getParent()

			# Branch validation
			if currentParent == self or isinstance(currentParent, GroupJob):
				# Actually, this is the "native" branch containing root campaign jobs
				branch = self.BRANCH_UNCONDITIONAL
			elif branch in [ '*', 'on_error' ]: # * is an alias for the error branch
				branch = self.BRANCH_ERROR
			elif not branch or branch in ['on_success']:
				branch =  self.BRANCH_SUCCESS
			else:
				raise Exception('Error at line %s: invalid branch (%s)' % (lc, branch))
			
			if type_ in [ 'ats', 'campaign' ]:
				# Filename creation within the docroot
				if filename.startswith('/'):
					# absolute path within the *repository*
					filename = '/%s%s' % (cm.get_transient('constants.repository'), filename)
				else:
					# just add the local campaign path
					filename = '%s/%s' % (path, filename)
			
				# Now we can create our job.
				getLogger().debug('%s: creating child job based on file docroot:%s' % (str(self), filename))
				source = FileSystemManager.instance().read(filename)
				name = filename[len('/repository/'):] # TODO: find a way to compute a "job name" from a docroot path
				jobpath = '/'.join(filename.split('/')[:-1])
				if source is None:
					raise Exception('File %s is not in the repository.' % name)
				if type_ == 'ats':
					job = AtsJob(name = name, source = source, path = filename)
					# Groups are only supported in ATS jobs for now
					if groups:
						job.setSelectedGroups(groups.split(','))
				else: # campaign
					job = CampaignJob(name = name, source = source, path = filename)
				job.setUsername(self.getUsername())
				job.setSessionParametersMapping(parseParameters(mapping))
				currentParent.addChild(job, branch)
				getLogger().info('%s: child job %s has been created, branch %s' % (str(self), str(job), branch))
				lastCreatedJob = job
			
			else:
				# 'group' type - for parallel execution
				job = GroupJob(name = filename) # The filename token is used to optionally name the group
				job.setUsername(self.getUsername())
				job.setSessionParametersMapping(parseParameters(mapping))
				currentParent.addChild(job, branch)
				getLogger().info('%s: child pseudo group job %s has been created, branch %s' % (str(self), str(job), branch))
				lastCreatedJob = job
				

		# OK, we're done with parsing and job preparation.
		getLogger().info('%s: fully prepared, all children found and created.' % str(self))

	def getLog(self):
		"""
		Returns the current known log.
		"""
		if self._logFilename:
			f = open(self._absoluteLogFilename, 'r')
			fcntl.flock(f.fileno(), fcntl.LOCK_EX)
			# FIXME: we generate a 'ats' root element. Is that correct ?
			res = '<?xml version="1.0" encoding="utf-8" ?>\n<ats>\n%s</ats>' % f.read()
			f.close()
			return res
		else:
			return '<?xml version="1.0" encoding="utf-8" ?>\n<ats>\n</ats>'

################################################################################
# The Scheduler Thread
################################################################################

class Scheduler(threading.Thread):
	"""
	A Background thread that regularly checks the main job queue for job to start (job in STATE_WAITING).
	
	We should have a dedicated heap for that.
	However, since we won't have thousands of waiting jobs, a first straightforward implementation
	may be enough.
	"""
	def __init__(self, manager):	
		threading.Thread.__init__(self)
		self._manager = manager
		self._stopEvent = threading.Event()
		self._notifyEvent = threading.Event()
	
	def run(self):
		getLogger().info("Job scheduler started.")
		while not self._stopEvent.isSet():
			# this delay is dynamic - re-read at each iterations
			delay = float(cm.get('ts.jobscheduler.interval')) / 1000.0
			self._notifyEvent.wait(delay)
			self._notifyEvent.clear()
			self.check()
		getLogger().info("Job scheduler stopped.")
	
	def check(self):
		jobs = self._manager.getWaitingRootJobs()
		for job in jobs:
			if job.getScheduledStartTime() < time.time():
				getLogger().info("Scheduler: starting new job: %s" % str(job))
				# Prepare a new thread, execute the job
				job.preRun()
				jobThread = threading.Thread(target = lambda: job.run(job.getScheduledSession()))
				jobThread.start()

	def stop(self):
		self._stopEvent.set()
		self.join()
	
	def notify(self):
		self._notifyEvent.set()


class JobManager:
	"""
	A Main entry point to the job manager module.
	"""
	def __init__(self):
		self._mutex = threading.RLock()
		self._jobQueue = []
		self._scheduler = Scheduler(self)
	
	def start(self):
		self._scheduler.start()
	
	def stop(self):
		self._scheduler.stop()
	
	def _lock(self):
		self._mutex.acquire()
	
	def _unlock(self):
		self._mutex.release()
	
	def registerJob(self, job):
		"""
		Register a new job in the queue.
		Do not update its state or do anything with it.
		Typically used by a campaign to register the child jobs it manages.
		"""
		self._lock()
		self._jobQueue.append(job)
		self._unlock()

	def persist(self):
		"""
		Persists the current job queue to disk.
		"""
		if not cm.get('testerman.var_root'):
			return 

		queueFilename = cm.get('testerman.var_root') + '/jobqueue.dump'
		getLogger().debug("Persisting queue to %s..." % queueFilename)
		self._lock()
		try:
			dump = pickle.dumps(self._jobQueue)
			f = open(queueFilename, 'w')
			f.write(dump)
			f.close()
		except Exception as e:
			getLogger().warning("Unable to persist job queue to %s: %s" % (queueFilename, str(e)))
		self._unlock()

	def restore(self):
		"""
		Reload the queue for the persisted queue.
		Called on restart.
		Jobs in running states are flagged as "crashed".
		
		WARNING: not thread safe, must be called before any new job registration.
		"""
		if not cm.get('testerman.var_root'):
			return 

		maxId = 0
		queueFilename = cm.get('testerman.var_root') + '/jobqueue.dump'
		try:		
			f = open(queueFilename)
			dump = f.read()
			f.close()
		except:
			return
		
		getLogger().info("Restoring job queue from %s..." % queueFilename)
		try:
			self._jobQueue = pickle.loads(dump)
			for job in self._jobQueue:
				if job.getState() in [ job.STATE_RUNNING, job.STATE_PAUSED, job.STATE_CANCELLING, job.STATE_INITIALIZING ]:
					getLogger().info("Job %s marked as being crashed" % job.getId())
					job.setState(job.STATE_CRASHED)
				elif job.getState() in [ job.STATE_KILLING ]:
					job.setState(job.STATE_KILLED)
				if job.getId() > maxId:
					maxId = job.getId()
			getLogger().info("Job queue restored: %s jobs recovered." % len(self._jobQueue))
			global _GeneratorBaseId
			_GeneratorBaseId = maxId
			getLogger().info("Continuing job IDs at %s" % maxId)
		except Exception as e:
			getLogger().info("Unable to restore job queue: %s" % str(e))
#		self._unlock()
		
	def submitJob(self, job):
		"""
		Submits a new job in the queue.
		@rtype: int
		@returns: the submitted job Id
		"""
		self.registerJob(job)
		# Initialize the job (i.e. prepare it)
		# Raises exceptions in case of an error
		try:
			job.prepare()
		except Exception as e:
			getLogger().warning("JobManager: new job submitted: %s, scheduled to start on %s, unable to initialize" % (str(job), time.strftime("%Y%m%d, at %H:%H:%S", time.localtime(job.getScheduledStartTime()))))
			# Forward to the caller
			raise e

		getLogger().info("JobManager: new job submitted: %s, scheduled to start on %s" % (str(job), time.strftime("%Y%m%d, at %H:%H:%S", time.localtime(job.getScheduledStartTime()))))
		# Wake up the scheduler. Maybe an instant run is here.
		self._scheduler.notify()
		return job.getId()
	
	def getWaitingRootJobs(self):
		"""
		Only extracts the waiting root jobs subset from the queue.
		Useful for the scheduler.
		Non-root (waiting) jobs are started by their parents explicitely,
		not by the scheduler.

		@rtype: list of Job instances
		@returns: the list of waiting jobs.
		"""
		self._lock()
		try:
			ret = filter(lambda x: (x.getParent() is None) and (x.getState() == Job.STATE_WAITING), self._jobQueue)
		except Exception as e:
			ret = []
			getLogger().error("JobManager: unable to get jobs waiting for execution: %s" % str(e))	
		self._unlock()
		return ret
	
	def getJobInfo(self, id_ = None):
		"""
		@type  id_: integer, or None
		@param id_: the jobId for which we request some info, or None if we want all.
		
		@rtype: list of dict
		@returns: a list of job dict representations. May be empty if the id_ was not found.
		"""
		ret = []
		self._lock()
		try:
			for job in self._jobQueue:
				if id_ is None or job.getId() == id_:
					ret.append(job.toDict())
		except:
			pass
		self._unlock()
		return ret

	def getJobDetails(self, id_):
		"""
		@type  id_: integer, or None
		@param id_: the jobId for which we request some info, or None if we want all.
		
		@rtype: list of dict
		@returns: a list of job dict representations. May be empty if the id_ was not found.
		"""
		job = self.getJob(id_)
		if not job:
			return None
		return job.toDict(detailed = True)

	def killAll(self):
		"""
		Kills all existing jobs.
		"""
		self._lock()
		for job in self._jobQueue:
			try:
				job.handleSignal(Job.SIGNAL_KILL)
			except:
				pass
		self._unlock()

	def getJob(self, id_):
		"""
		Internal only ?
		Gets a job based on its id.
		"""
		j = None
		self._lock()
		for job in self._jobQueue:
			if job.getId() == id_:
				j = job
				break
		self._unlock()
		return j
	
	def sendSignal(self, id_, signal):
		job = self.getJob(id_)
		if job:
			job.handleSignal(signal)
			return True
		else:
			return False

	def getJobLogFilename(self, id_):
		job = self.getJob(id_)		
		if job:
			return job.getLogFilename()
		else:
			return None
	
	def getJobLog(self, id_):
		job = self.getJob(id_)
		if job:
			return job.getLog()
		else:
			return None

	def rescheduleJob(self, id_, at):
		job = self.getJob(id_)
		if job:
			return job.reschedule(at)
	
	def isBottomUpTreeCompleted(self, job):
		if not job._stopTime: return False
		parent = job._parent
		while parent:
			if not parent._stopTime:
				return False
			parent = parent._parent
		return True 

	def purgeJobs(self, older_than):
		"""
		Scans the queue and purge jobs whose completion time is older than older_than.
		
		If one of the parent jobs is still running (a campaign, etc),
		the job is kept even if it was completed before the older_than.
		"""
		self._lock()
		try:
		
			keptQueue = []
			count = len(self._jobQueue)
			# Let's select kept jobs instead of removing items in the current jobqueue
			for job in self._jobQueue:
				if not self.isBottomUpTreeCompleted(job) or not (job._stopTime and job._stopTime < older_than):
					keptQueue.append(job)
					count -= 1
			self._jobQueue = keptQueue
			return count
		finally:	
			self._unlock()

TheJobManager = None

def instance():
	global TheJobManager
	if TheJobManager is None:
		TheJobManager = JobManager()
	return TheJobManager

def initialize():
	# Enable to pickle - and unpickle - the RLock contained into a Job structure.
	copy_reg.pickle(threading._RLock, lambda x: (threading._RLock, (None,)))
	instance().restore()
	instance().start()

def finalize():
	try:
		instance().stop()
		getLogger().info("Killing all jobs...")
		instance().killAll()
		instance().persist()
	except Exception as e:
		getLogger().error("Unable to stop the job manager gracefully: %s" % str(e))

