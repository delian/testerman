# -*- coding: utf-8 -*-
##
# This file is part of Testerman, a test automation system.
# Copyright (c) 2009-2013 QTesterman contributors
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
# Auto update management.
#
# GUI interface over the TestermanClient-provided updates features.
#
##


from PyQt4.Qt import *

import TestermanClient

################################################################################
# Restarter/Reinitializer facility
################################################################################

class Restarter:
	"""
	Static class that enables to restart a python program at any time.
	
	Usage:
	call Restarter.initialize() as soon as your program is started, before 
	any other operations (in particular argv consumption, chdir)
	
	call Restarter.restart() when you're ready to restart/reinitialize your script.
	It will be executed with the same arguments, from the same path, with the same
	environment as the original one.
	"""
	env = None
	cwd = None
	executable = None
	argv = None
	
	def initialize():
		import os
		import sys
		Restarter.env = os.environ
		Restarter.argv = sys.argv
		Restarter.executable = sys.executable
		Restarter.cwd = os.getcwd()
	
	initialize = staticmethod(initialize)

	def restart():
		import os
		import sys
		args = [ Restarter.executable ] + Restarter.argv
		if sys.platform in [ 'win32', 'win64' ]:
			# we need to quote arguments containing spaces... why ?
			args = map(lambda arg: (' ' in arg and not arg.startswith('"')) and '"%s"' % arg or arg, args)
		os.chdir(Restarter.cwd)
		os.execvpe(Restarter.executable, args, Restarter.env)
		
	restart = staticmethod(restart)

################################################################################
# Auto update management
################################################################################

def checkAndUpdateComponent(proxy, destinationPath, component, currentVersion = None, branches = [ "stable" ]):
	"""
	Checks for updates, and proposes the user to update if a newer version is available.

	@type  basepath: unicode string 
	@param basepath: the application basepath were we should unpack the update archive

	@throws exceptions
	
	@rtype: bool
	@returns: True if the component was updated. False otherwise (on error or user abort)
	"""
	updates = proxy.getComponentVersions(component, branches)

	if not updates:
		# No updates available - nothing to do
		print ("No updates available on this server.")
		return False

	print ("Available updates:")
	print ("\n".join([ "%s (%s)" % (x['version'], x['branch']) for x in updates]))

	# Let's check if we have a better version than the current one
	if not currentVersion or (TestermanClient.compareVersions(currentVersion, updates[0]['version']) < 0):
		newerVersion = updates[0]['version']
		url = updates[0]['url']
		branch = updates[0]['branch']
		print ("Newer version available: %s" % newerVersion)
		
		ret = QMessageBox.question(None, "Update manager", "A new QTesterman Client version is available on the server:\n%s (%s)\nDo you want to update now ?" % (newerVersion, branch), QMessageBox.Yes, QMessageBox.No)
		if ret == QMessageBox.Yes:
			# Download and unpack the archive
			try:
				proxy.installComponent(url, destinationPath)
			except Exception as e:
				QMessageBox.warning(None, "Update manager", "Unable to install the update:\n%s\nContinuing with the current version." % str(e))
				return False

			QMessageBox.information(None, "Update manager", "Update succesfully installed.")
			# Let the caller propose a restart
			return True
		else:
			return False

def getNewVersionInfo(proxy, component, currentVersion = None, branches = [ "stable" ]):
	"""
	Checks for updates, and returns (version, branch, url) of the latest version
	if one is available.

	@type  basepath: unicode string 
	@param basepath: the application basepath were we should unpack the update archive

	@throws exceptions
	
	@rtype: (version, branch, url) or None
	@returns: None if no update is available.
	"""
	updates = proxy.getComponentVersions(component, branches)

	if not updates:
		# No updates available - nothing to do
		print ("No updates available on this server.")
		return None

	print ("Available updates:")
	print ("\n".join([ "%s (%s)" % (x['version'], x['branch']) for x in updates]))

	# Let's check if we have a better version than the current one
	if not currentVersion or (TestermanClient.compareVersions(currentVersion < updates[0]['version']) < 0):
		newerVersion = updates[0]['version']
		url = updates[0]['url']
		branch = updates[0]['branch']
		print ("Newer version available: %s" % newerVersion)
		return (newerVersion, branch, url)

	return None

def updateComponent(proxy, url, destinationPath):
	try:
		proxy.installComponent(url, destinationPath)
	except Exception as e:
		QMessageBox.warning(None, "Update manager", "Unable to install the update:\n%s\nContinuing with the current version." % str(e))
		return False

	QMessageBox.information(None, "Update manager", "Update succesfully installed.")
	# Let the caller propose a restart
	return True

