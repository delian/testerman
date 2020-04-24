# -*- coding: utf-8 -*-
##
# This file is part of Testerman, a test automation system.
# Copyright (c) 2010 Sebastien Lefevre and other contributors
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
# Component Package Creation from Source.
#
# A Testerman component source must define a PACKAGE.py module
# in its root folder, will the following methods:
#
# getIncludedFiles(): returning a list of files to include, for instance:
# ../core/CodecManager.py
# ../core/ProbeImplementationManager.py
# ../plugins
# *.py
# ../common/*.py
#
# getExcludedFiles(): returning a list of files to exclude, for instance:
# .svn
# .CVS
# *.asn
# *.pyc
# 
# getVersion(): the current version of the component
#
# getComponentName(): returning a string representing the expected component name/base dir in package
#
##

import glob
import shutil
import tempfile
import tarfile
import os.path
import re
import sys

def fileExists(filename):
	try:
		os.stat(filename)
		return True
	except:
		return False

def walk(sources, exclude = None):
	"""
	Returns a list of files (complete paths) contained in sources
	(list of files/dir with wildcard support) that are not
	excluded.
	exclude is a function(name) -> True/False
	"""
	ret = []
	for source in sources:
		source = os.path.realpath(source)
		for obj in glob.glob(source):
			if exclude and exclude(obj):
				continue
			if os.path.isdir(obj):
				ret += walk([ '%s/*' % obj ], exclude)
			else:
				ret.append(obj)
	return ret

def wildcardToRegexp(s):
	"""
	Turns a wildcard-based string s into an equivalent regexp.
	(only * and ? are supported for now)
	"""
	return s.replace('.', '\\.').replace('?', '.').replace('*', '.*') + '$'


def createPackage(sources, filename, baseDir = "", excluded = []):
	"""
	Creates the component package.

	The baseDir is the resulting base directory in the final archive.
	"""
	excluded = [ wildcardToRegexp(x.strip()) for x in excluded if x.strip() ]
	
	def isExcluded(f):
		for regexp in excluded:
			if re.search(regexp, f):
				return True
		return False
	
	tmpdir = tempfile.mkdtemp()

	try:
		for entry in sources:
			for obj in glob.glob(entry):
#				print ("Copying %s to %s ..." % (obj, tmpdir))
				if os.path.isdir(obj):
					shutil.copytree(obj, tmpdir + "/" + os.path.basename(obj))
				else:
					shutil.copy(obj, tmpdir + "/")

		# Now, create a tar file
		t = tarfile.open(name = filename, mode = 'w:gz')
		for name in walk([tmpdir + '/*'], isExcluded):
			t.add(name, arcname = name.replace(tmpdir, baseDir), recursive = False) # strip the tmpdir from the archive name
		t.close()
#		print ("archive %s created." % filename)

		# Purge the temp dir
		shutil.rmtree(tmpdir)
	except Exception as e:
		try:
			shutil.rmtree(tmpdir)
		except:
			pass
		raise e


def getPackageInfo(sourceRoot):
	"""
	Extract the package information (version, name, files...)
	from the PACKAGE description in sourceRoot.
	
	Creates a package that is suitable for a deployment for the component 
	whose source is located in sourceRoot.
	
	This function automatically loads the PACKAGE module in this folder.
	"""
	moduleBasename = "PACKAGE.py"
	packageModuleFilename = os.path.join(sourceRoot, moduleBasename)
	
	if not fileExists(packageModuleFilename):
		raise Exception("Unable to find the PACKAGE module in %s." % sourceRoot)
	
	# Load the file as a module
	backup_syspath = [x for x in sys.path] # copy the current syspath
	backup_sysmodules = sys.modules.keys() # snapshot the currently loaded modules to track the imported ones
	sys.path = [ sourceRoot ]
	mod = None
	try:
		try:
			import imp
			f = open(packageModuleFilename)
			package = imp.load_module("package", f, sourceRoot, (".py", "r", imp.PY_SOURCE))
			f.close()
		except Exception as e:
			raise Exception("Unable to load module PACKAGE from %s (%s)." % (sourceRoot, str(e)))

		packageInfo = {}
		try:
			# The provided included files are relative to their own source path. Make then
			# absolute
			packageInfo['sources'] = [ "%s/%s" % (sourceRoot, x) for x in package.getIncludedFiles() ]
			packageInfo['component'] = package.getComponentName()
			# Exclude files are filename patterns, not including a path
			packageInfo['excluded'] = package.getExcludedFiles() + [moduleBasename] # automatically exclude the PACKAGE.py
			packageInfo['version'] = package.getVersion()
		except Exception as e:
			raise Exception("Unable to get component info (%s)." % str(e))
	finally:
		# Remove all modules that were imported during the PACKAGE importation.
		# (so that we can re-import a PACKAGE module from another dir without any interactions)
		for m in sys.modules.keys():
			if not m in backup_sysmodules:
				del sys.modules[m] # an explicit del is needed to unload the module
		sys.path = [x for x in backup_syspath] # restore the previous syspath

	return packageInfo 

	
if __name__ == "__main__":
	print (getPackageInfo("/home/seb/dev/testerman/trunk/qtesterman"))


