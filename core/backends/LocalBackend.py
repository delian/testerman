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
# A FileSystemBackend implementation for local files management.
# 
# No revision management.
##

import FileSystemBackend
import FileSystemBackendManager

import logging
import os
import shutil

################################################################################
# Logging
################################################################################

def getLogger():
	return logging.getLogger('TS.FSB.Local')


def fileExists(path):
	try:
		os.stat(path)
		return True
	except:
		return False

class LocalBackend(FileSystemBackend.FileSystemBackend):
	"""
	Properties:
	- basepath: the file basepath files are looked from. No default.
	- excluded: a space separated of patterns to exclude from dir listing. Default: .svn CVS
	- strict_basepath: 0/1. If 1, only serve files that are in basepath. If not, 
	  accept to follow fs links to other locations. Default: 0
	"""
	def __init__(self):
		FileSystemBackend.FileSystemBackend.__init__(self)
		# Some default properties
		self.setProperty('excluded', '.svn CVS')
		self.setProperty('strict_basepath', '0')

		# Mandatory properties (defined here to serve as documentation)
		self.setProperty('basepath', None)
	
	def initialize(self):
		self._strictBasepath = (self['strict_basepath'].lower() in [ '1', 'true' ])
		
		# Exclusion list.
		# Something based on a glob pattern should be better,
		# so that the user can configure in the backends.ini file something like
		# excluded = *.pyc .svn CVS
		# that generated an _excluded_files to [ '*.pyc', '.svn', 'CVS' ], ...
		self._excludedPatterns = filter(lambda x: x, self['excluded'].split(' '))

		# Check that the base path actually exists		
		if not os.path.isdir(self['basepath']):
			return False
			
		return True
	
	def _realpath(self, path):
		"""
		Compute the local path of a filename.
		filename is relative to the mountpoint.
		
		Returns None if we compute a filename/path which is outside
		the basepath.
		"""
		path = os.path.realpath("%s/%s" % (self['basepath'], path))
		if self._strictBasepath and not path.startswith(self['basepath']):
			getLogger().warning("Attempted to handle a path that is not under basepath (%s). Ignoring." % path)
			return None
		else:
			return path
	
	def read(self, filename, revision = None):
		filename = self._realpath(filename)
		if not filename: 
			return None
		
		try:
			f = open(filename)
			content = f.read()
			f.close()
			return content
		except Exception as e:
			getLogger().warning("Unable to read file %s: %s" % (filename, str(e)))
			return None

	def write(self, filename, content, baseRevision = None, reason = None, username = None):
		"""
		Makes sure that we can overwrite the file, if already exists:
		1 - rename the current one to a filename.backup
		2 - create the new file
		3 - remove the filename.backup
		In case of an error in 2, rollback by renaming filename.backup to filename.
		This avoids creating an empty file in case of no space left on device,
		resetting an existing file.
		"""
		filename = self._realpath(filename)
		if not filename: 
			raise Exception('Invalid file: not in base path')

		backupFile = None
		try:
			if fileExists(filename):
				b = '%s.backup' % filename
				os.rename(filename, b)
				backupFile = b
			f = open(filename, 'w')
			f.write(content)
			f.close()
			if backupFile:
				try:
					os.remove(backupFile)
				except:
					pass
			return None # No new revision created.
		except Exception as e:
			if backupFile:
				os.rename(backupFile, filename)
			getLogger().warning("Unable to write content to %s: %s" % (filename, str(e)))
			raise(e)

	def rename(self, filename, newname, reason = None, username = None):
		newname = os.path.split(filename)[0] + '/%s' % newname
		filename = self._realpath(filename)
		if not filename: 
			return False
		
		newn = self._realpath(newname)
		if not newn: 
			getLogger().warning("Unable to rename %s to %s: the target name is not in base path" % (filename, newname))
			return False
		
		profilesdir = "%s.profiles" % filename
		newprofilesdir = "%s.profiles" % newn
		try:
			os.rename(filename, newn)
		except Exception as e:
			getLogger().warning("Unable to rename %s to %s: %s" % (filename, newname, str(e)))
			return False
		
		# rename profiles dir, too - not a problem if the dir did not exist
		try:
			os.rename(profilesdir, newprofilesdir)
		except:
			pass
		return True

	def unlink(self, filename, reason = None, username = None):
		filename = self._realpath(filename)
		if not filename: 
			return False
		
		# Remove associated profiles, if any
		profilesdir = "%s.profiles" % filename
		try:
			shutil.rmtree(profilesdir, ignore_errors = True)
		except Exception as e:
			getLogger().warning("Unable to remove profiles associated to %s: %s" % (filename, str(e)))

		try:
			os.remove(filename)
			return True
		except Exception as e:
			getLogger().warning("Unable to unlink %s: %s" % (filename, str(e)))
		return False

	def getdir(self, path):
		path = self._realpath(path)
		if not path: 
			return None

		try:
			entries = os.listdir(path)
			ret = []
			for entry in entries:
				if os.path.isfile("%s/%s" % (path, entry)):
					ret.append({'name': entry, 'type': 'file'})
				elif os.path.isdir("%s/%s" % (path, entry)) and not entry in self._excludedPatterns and not entry.endswith('.profiles'):
					ret.append({'name': entry, 'type': 'directory'})
			return ret
		except Exception as e:
			getLogger().warning("Unable to list directory %s: %s" % (path, str(e)))
		return None

	def mkdir(self, path, username = None):
		path = self._realpath(path)
		if not path: 
			return False

		if fileExists(path):
			return False

		try:
			os.makedirs(path, mode=0o755)
		except:
			# already exists only ?...
			pass
		return True
	
	def rmdir(self, path, username = None):
		path = self._realpath(path)
		if not path: 
			return False

		try:
			if not os.path.isdir(path):
				return False
			else:
				os.rmdir(path)
				return True
		except Exception as e:
			getLogger().warning("Unable to rmdir %s: %s" % (path, str(e)))
		return False

	def renamedir(self, path, newname, reason = None, username = None):
		path = self._realpath(path)
		if not path: 
			return False
		
		try:
			os.rename(path, newname)
			return True
		except Exception as e:
			getLogger().warning("Unable to rename dir %s to %s: %s" % (path, newname, str(e)))
		return False

	def attributes(self, filename, revision = None):
		filename = self._realpath(filename)
		if not filename: 
			return None

		try:
			s = os.stat(filename)
			a = FileSystemBackend.Attributes()
			a.mtime = s.st_ctime
			a.size = s.st_size
			return a
		except Exception as e:
			getLogger().warning("Unable to get file attributes for %s: %s" % (filename, str(e)))			
		return None

	def revisions(self, filename, baseRevision, scope):
		filename = self._realpath(filename)
		if not filename: 
			return None
		
		# Not yet implemented
		return None
	
	def isdir(self, path):
		path = self._realpath(path)
		if not path: 
			return False
		return os.path.isdir(path)

	def isfile(self, path):
		path = self._realpath(path)
		if not path:
			return False
		return os.path.isfile(path)

	# Profiles Management			

	def getprofiles(self, filename):
		filename = self._realpath(filename)
		if not filename:
			return None
		
		profilesdir = "%s.profiles" % filename
		
		try:
			entries = os.listdir(profilesdir)
			ret = []
			for entry in entries:
				if os.path.isfile("%s/%s" % (profilesdir, entry)):
					ret.append({'name': entry, 'type': 'file'})
				elif os.path.isdir("%s/%s" % (profilesdir, entry)) and not entry in self._excludedPatterns:
					ret.append({'name': entry, 'type': 'directory'})
			return ret
		except Exception as e:
			getLogger().warning("Unable to list profiles directory %s: %s" % (profilesdir, str(e)))
		return None

	def readprofile(self, filename, profilename):
		filename = self._realpath(filename)
		if not filename:
			return None
		
		profilefilename = "%s.profiles/%s" % (filename, profilename)
		try:
			f = open(profilefilename)
			content = f.read()
			f.close()
			return content
		except Exception as e:
			getLogger().warning("Unable to read file %s: %s" % (filename, str(e)))
			return None
		
	def writeprofile(self, filename, profilename, content, username = None):
		filename = self._realpath(filename)
		if not filename: 
			raise Exception('Invalid file: not in base path')

		# Automatically creates this backend-specific dir
		profilesdir = "%s.profiles" % filename

		try:
			os.makedirs(profilesdir, mode=0o755)
		except:
			pass

		profilefilename = "%s.profiles/%s" % (filename, profilename)
		backupFile = None
		try:
			if fileExists(profilefilename):
				b = '%s.backup' % profilefilename
				os.rename(profilefilename, b)
				backupFile = b
			f = open(profilefilename, 'w')
			f.write(content)
			f.close()
			if backupFile:
				try:
					os.remove(backupFile)
				except:
					pass
			return None # No new revision created.
		except Exception as e:
			if backupFile:
				os.rename(backupFile, profilefilename)
			getLogger().warning("Unable to write content to %s: %s" % (profilefilename, str(e)))
			raise(e)

	def unlinkprofile(self, filename, profilename, username = None):
		filename = self._realpath(filename)
		if not filename:
			return None
		
		profilefilename = "%s.profiles/%s" % (filename, profilename)
		try:
			os.remove(profilefilename)
			return True
		except Exception as e:
			getLogger().warning("Unable to unlink %s: %s" % (profilefilename, str(e)))
		return False
		

	def profileattributes(self, filename, profilename):
		filename = self._realpath(filename)
		if not filename: 
			return None

		profilefilename = "%s.profiles/%s" % (filename, profilename)

		try:
			s = os.stat(profilefilename)
			a = FileSystemBackend.Attributes()
			a.mtime = s.st_ctime
			a.size = s.st_size
			return a
		except Exception as e:
			getLogger().warning("Unable to get file attributes for %s: %s" % (profilefilename, str(e)))			
		return None
	

FileSystemBackendManager.registerFileSystemBackendClass("local", LocalBackend)
