# -*- coding: utf-8 -*-
##
# This file is part of Testerman, a test automation system.
# Copyright (c) 2009 QTesterman contributors
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
# A plugin to create Testerman-compliant resources
# from a file (either binary or text)
#
##

from PyQt4.Qt import *

import os
import base64

import Plugin
import PluginManager
import DocumentModels

# Plugin ID, as generated by uuidgen / uuid.uuid1()
PLUGIN_ID = "a161584b-3330-4c1a-831c-229b5fdbbd5e"
PLUGIN_LABEL = "Resource importer"
PLUGIN_DESCRIPTION = "Embeds external files into strings so that they can be used in your scripts"
PLUGIN_VERSION = "1.0.0"


def generateResource(filename, name, mode):
	"""
	Generates a embeddable code from the filename contents,
	something like:
	
	# Binary mode
	myresource_id = "\x01\xfd" +
		"\xaa\xbc" +
		"\xbd\x93"
	
	# Text mode
	myresource_id = u"one line here\n" +
		u"another line here\n"
	
	@type  filename: unicode string
	@param filename: the filename to load
	@type  name: unicode string
	@param name: the name of the resource (may contain special characters, will be collapsed into something python-compliant)
	@type  mode: string in [ 'bin', 'ascii', 'utf-8', 'base64' ]
	
	@rtype: unicode string
	@returns: a valid code for the resource.
	
	"""
	
	def nameToId(name):
		# Something better to find
		return name.replace(' ', '_').replace('-', '_').replace('.', '_')

	try:	
		data = ''

		if mode in [ 'bin']:
			# Binary mode
			f = open(filename, 'rb')
			buf = f.read()
			f.close()
			lines = []
			line = None
			i = 0
			for c in buf:
				if not (i % 19):
					if line is not None:
						line += "'"
						lines.append(line)
						line = "'"
					else:
						line = "'"
				line += "\\x%2.2x" % ord(c)
				i += 1

			if line is not None:	
				line += "'"
				lines.append(line)

			data = ' \\\n'.join(lines)

		elif mode in [ 'base64']:
			# Binary mode, BASE64 enncoding
			f = open(filename, 'rb')
			buf = f.read()
			f.close()
			data = 'base64.decodestring('+ ' \\\n'.join(map(lambda line: repr(line), base64.encodestring(buf).split('\n'))) + ')'

		else:
			# Default mode: text
			f  = open(filename)
			lines = f.readlines()
			f.close()
			data = ' \\\n'.join(map(lambda line: repr(line), lines))

		ret = "# Resource imported from:\n"
		ret += "# %s (%d bytes)\n" % (filename, os.stat(filename).st_size)
		ret += "# Embedding format: %s\n" % mode
		ret += nameToId(name) + " = \\\n" + data + "\n"
	except Exception as e:
		print "Unable to import resource from %s: " % filename + str(e)
		return None
	
	return ret

# Resource importation dialog
class WResourceImportationDialog(QDialog):
	"""
	Resource importation dialog.
	
	Path to the filename to import,
	the resource name/id,
	the importation type (bin, ascii, utf-8, ...)
	
	"""
	def __init__(self, parent = None):
		QDialog.__init__(self, parent)
		self.__createWidgets()

	def __createWidgets(self):
		layout = QVBoxLayout()

		layout.addWidget(QLabel("File to embed:"))

		fileLayout = QHBoxLayout()
		self.filenameLineEdit = QLineEdit()
		self.filenameLineEdit.setMinimumWidth(150)
		self.browseButton = QPushButton("...")
		self.connect(self.browseButton, SIGNAL('clicked()'), self.browseFile)
		fileLayout.addWidget(self.filenameLineEdit)
		fileLayout.addWidget(self.browseButton)
		
		layout.addLayout(fileLayout)
		
		layout.addWidget(QLabel("Resource Name/ID:"))
		self.resourceNameLineEdit = QLineEdit()
		layout.addWidget(self.resourceNameLineEdit)
		
		bin = QRadioButton("binary (raw)")
		b64 = QRadioButton("base64")
		ascii = QRadioButton("ascii")
#		utf8 = QRadioButton("utf-8")
		bin.setChecked(True)
		self.typeButtonGroup = QButtonGroup()
		self.typeButtonGroup.addButton(bin, 0)
		self.typeButtonGroup.addButton(ascii, 1)
#		self.typeButtonGroup.addButton(utf8, 2)
		self.typeButtonGroup.addButton(b64, 3)
		typeButtonLayout = QVBoxLayout()
		typeButtonLayout.addWidget(bin)
		typeButtonLayout.addWidget(b64)
		typeButtonLayout.addWidget(ascii)
#		typeButtonLayout.addWidget(utf8)
		typeGroupBox = QGroupBox("Embedding format")
		typeGroupBox.setLayout(typeButtonLayout)
		
		layout.addWidget(typeGroupBox)

		# Buttons
		self.okButton = QPushButton("Ok")
		self.connect(self.okButton, SIGNAL("clicked()"), self.accept)
		self.cancelButton = QPushButton("Cancel")
		self.connect(self.cancelButton, SIGNAL("clicked()"), self.reject)
		buttonLayout = QHBoxLayout()
		buttonLayout.addStretch()
		buttonLayout.addWidget(self.okButton)
		buttonLayout.addWidget(self.cancelButton)
		layout.addLayout(buttonLayout)

		self.setLayout(layout)

	def browseFile(self):
		filename = QFileDialog.getOpenFileName(self, "Resource filename", self.filenameLineEdit.text())
		if not filename.isEmpty():
			self.filenameLineEdit.setText(os.path.normpath(unicode(filename)))
		
	def getFileMode(self):
		values = [ 'bin', 'ascii', 'utf-8', 'base64' ]
		return values[self.typeButtonGroup.checkedId()]
	
	def getFilename(self):
		return unicode(self.filenameLineEdit.text())
	
	def getResourceName(self):
		return unicode(self.resourceNameLineEdit.text())

class WResourceCodeWriter(Plugin.CodeWriter):
	def __init__(self, parent = None):
		Plugin.CodeWriter.__init__(self, parent)

	def activate(self):
		dialog = WResourceImportationDialog(self.parent())
		if dialog.exec_() == dialog.Accepted:
			return generateResource(filename = dialog.getFilename(), name = dialog.getResourceName(), mode = dialog.getFileMode())
		return None
	
	def isDocumentTypeSupported(self, documentType):
		return documentType in [ DocumentModels.TYPE_ATS, DocumentModels.TYPE_MODULE ]


###############################################################################
# Template-based code writer
###############################################################################

class WResourceCodeWriterConfiguration(Plugin.WPluginConfiguration):
	def __init__(self, parent = None):
		Plugin.WPluginConfiguration.__init__(self, parent)
		self.__createWidgets()

	def __createWidgets(self):
		"""
		The model is in the saved settings.
		"""
		layout = QVBoxLayout()
		paramLayout = QGridLayout()
		layout.addLayout(paramLayout)

		self.setLayout(layout)

	def displayConfiguration(self):
		path = "plugins/%s" % PLUGIN_ID
		# Read the settings
		settings = QSettings()
		# No settings to read for now

	def saveConfiguration(self):
		"""
		Update the data model.
		"""
		settings = QSettings()
		path = "plugins/%s" % PLUGIN_ID
		# No settings to save for now
		return True

	def checkConfiguration(self):
		"""
		Check the data model, return 1 if OK, 0 if not.
		"""
		return True


PluginManager.registerPluginClass(PLUGIN_LABEL, PLUGIN_ID, WResourceCodeWriter, description = PLUGIN_DESCRIPTION, version = PLUGIN_VERSION)

