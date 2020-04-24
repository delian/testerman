#!/usr/bin/env python
# -*- coding: utf-8 -*-
##
# This file is part of Testerman, a test automation system.
# Copyright (c) 2008-2012 Sebastien Lefevre and other contributors
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
# Testerman Server - main.
#
##

import ConfigManager
import EventManager
import FileSystemManager
import JobManager
import ProbeManager
import Tools
import Versions
import WebServices
import WebServer
import WebClientServer
import TestermanClient

import logging
import optparse
import os
import select
import socket
import sys
import threading
import time

if sys.version_info < (2, 7):
	# XML-RPC support: locally modified SimpleXMLRPCServer
	# to workaround a bug in the one distribued with Python <= 2.6
	# (allow_none=1 not taken into account to enable marshalling of None values).
	# So the SimpleXMLRPCServer.dumps() has been overriden here.
	import FixedSimpleXMLRPCServer as SimpleXMLRPCServer
else:
	import SimpleXMLRPCServer

import SocketServer


cm = ConfigManager.instance()

################################################################################
# Logging
################################################################################

def getLogger():
	return logging.getLogger('TS')


################################################################################
# Testerman Web Application
################################################################################

class TestermanWebApplication(WebServer.WebApplication):
	"""
	This application completes the base one with dynamic resources
	handlers implementations.
	
	Used to serve GET requests over the Ws interface, to
	download installers, component, and offer other
	server-oriented services.
	"""

	##
	# Dynamic resources handlers
	##

	def handle_teapot(self, args):
		"""
		Test function.
		"""
		self.request.sendError(418)
	
	def handle_qtestermaninstaller(self, args):
		"""
		Sends the latest installer script to install QTesterman.
		Substitute a default server/port on the fly.
		"""
		def xform(source):
			# The Ws connection url could be constructed from multipe sources:
			# - the HTTP 1.1 host header, if any
			# - the web connection IP address (self.connection.getsockname())
			#   but this is an IP address and not very friendly. However, it will always
			#   work, as the web listener is the same as for the Ws interface.
			# - from a dedicated configuration variable so that the admin
			#   can set a valid, resolvable hostname to connect to the server instead of an IP address.
			# - using the interface.ws.port and the local hostname, but this hostname
			#   may not be resolved by the client. However, we'll opt for this option as a fallback for now
			#   (more user friendly, and the chances that the hostname cannot be resolved or
			#   resolved to an incorrect IP/interfaces are low - let me know if you have the need
			#   for the second option instead).
			if "host" in self.request.getHeaders():
				url = 'http://%s' % self.request.getHeaders()["host"]
			else:
				url = 'http://%s:%s' % (socket.gethostname(), cm.get("interface.ws.port"))
			return source.replace('DEFAULT_SERVER_URL = "http://localhost:8080"', 'DEFAULT_SERVER_URL = %s' % repr(url))

		installerPath = os.path.abspath("%s/qtesterman/Installer.py" % cm.get_transient("testerman.testerman_home"))
		# We should pre-configure the server Url on the fly
		self._rawServeFile(installerPath, asFilename = "QTesterman-Installer.py", xform = xform)
	
	def handle_pyagentinstaller(self, args):
		"""
		Sends the latest installer script to install a PyAgent.
		Substitute a default server/port on the fly.
		"""
		def xform(source):
			return source.replace('DEFAULT_TACS_IP = "127.0.0.1"', 'DEFAULT_TACS_IP = %s' % repr(cm.get("interface.xa.ip"))).replace(
			'DEFAULT_TACS_PORT = 40000', 'DEFAULT_TACS_PORT = %s' % repr(int(cm.get("interface.xa.port"))))
		
		installerPath = os.path.abspath("%s/pyagent/agent-installer.py" % cm.get_transient("testerman.testerman_home"))
		# We should pre-configure the server Url on the fly
		self._rawServeFile(installerPath, asFilename = "agent-installer.py", xform = xform)

	def handle_docroot(self, path):
		"""
		Download a file from the testerman (not web) docroot
		"""

		path = os.path.abspath("%s/%s" % (cm.get('testerman.document_root'), path))
		
		getLogger().debug("Requested file: %s" % path)
		
		if not path.startswith(cm.get('testerman.document_root')):
			# The query is outside the testerman docroot. Forbidden.
			self.request.sendError(403)
			return

		self._rawServeFile(path, asFilename = os.path.basename(path))

	def handle_components(self, path):
		"""
		Displays the various published components on this server.
		"""

		context = {}
		# Published components
		if cm.get('testerman.document_root'):
			updateFile = '%s/updates.xml' % cm.get('testerman.document_root')
			um = UpdateMetadataWrapper(updateFile)
			try:
				context['components'] = um.getComponentsList()
			except:
				if self._getDebug():
					getLogger().error(Tools.getBacktrace())

		self._serveTemplate("components.vm", context)


###############################################################################
# updates.xml reader
###############################################################################

import xml.dom.minidom

class UpdateMetadataWrapper:
	"""
	A class to manage several actions on the updates.xml
	"""
	def __init__(self, filename):
		self._filename = filename
		self._docroot = os.path.split(self._filename)[0]

	def getComponentsList(self):
		"""
		Returns the currently published components and their status.
		"""
		f = open(self._filename, 'r')
		content = ''.join([x.strip() for x in f.readlines()])
		f.close()

		ret = []
		
		doc = xml.dom.minidom.parseString(content)
		for e in doc.firstChild.getElementsByTagName('update'):
			version = e.attributes['version'].value
			component = e.attributes['component'].value
			branch = e.attributes['branch'].value
			url = e.attributes['url'].value
			ret.append(dict(version = version, component = component, branch = branch, archive = url))

		# Ordered by component, then version
		ret.sort(lambda x, y: cmp((x.get('component'), x.get('version')), (y.get('component'), y.get('version'))))
		
		return ret


################################################################################
# XML-RPC: Ws Interface implementation
################################################################################

class RequestHandler(WebServer.WebApplicationDispatcherMixIn, SimpleXMLRPCServer.SimpleXMLRPCRequestHandler):
	"""
	This custom handler is able to manage XML-RPC requests (POST)
	but also supports file serving via GET.
	The do_GET implementation is provided by WebServer.WebApplicationDispatcherMixIn
	"""
	protocol_version = "HTTP/1.1" # Support for keep alive 
	pass

class XmlRpcServer(SocketServer.ThreadingMixIn, SimpleXMLRPCServer.SimpleXMLRPCServer):
	allow_reuse_address = True
	def handle_request_with_timeout(self, timeout):
		"""
		A handle_request reimplementation, with a timeout support
		so that we can interrupt the server easily.
		"""
		r, w, e = select.select([self.socket], [], [], timeout)
		if r:
			self.handle_request()

class XmlRpcServerThread(threading.Thread):
	def __init__(self):
		threading.Thread.__init__(self)
		self._stopEvent = threading.Event()
		address = (cm.get("interface.ws.ip"), cm.get("interface.ws.port"))
		self._server = XmlRpcServer(address, RequestHandler, allow_none = True)
		serverUrl = "http://%s:%s" % ((cm.get("interface.ws.ip") in ['', "0.0.0.0"] and "localhost") or cm.get("interface.ws.ip") , cm.get("interface.ws.port"))
		client = TestermanClient.Client(name = "Embedded Testerman WebClient", userAgent = "WebClient/%s" % WebClientServer.VERSION, serverUrl = serverUrl)
		getLogger().info("Embedded WCS using serverUrl: %s" % serverUrl)
		# Register applications in this server
		WebServer.WebApplicationDispatcherMixIn.registerApplication("/", TestermanWebApplication, 
			documentRoot = cm.get("testerman.web.document_root"), 
			debug = cm.get("ts.debug"),
			theme = cm.get("ts.webui.theme"))
		WebServer.WebApplicationDispatcherMixIn.registerApplication("/webclient", WebClientServer.WebClientApplication, 
			documentRoot = cm.get("testerman.webclient.document_root"), 
			testermanClient = client, 
			debug = cm.get("ts.debug"),
			authenticationRealm = 'Testerman WebClient',
			theme = cm.get("wcs.webui.theme"))
		WebServer.WebApplicationDispatcherMixIn.registerApplication('/websocket', WebClientServer.XcApplication, 
			testermanServerUrl = serverUrl,
			debug = cm.get("ts.debug"))

		# We should be more selective...
		self._server.register_instance(WebServices)
		self._server.logRequests = False

	def run(self):
		getLogger().info("XML-RPC server started")
		try:
			while not self._stopEvent.isSet(): 
				self._server.handle_request_with_timeout(0.01)
		except Exception as e:
			getLogger().error("Exception in XMLRPC server thread: " + str(e))
		getLogger().info("XML-RPC server stopped")

	def stop(self):
		try:
			self._stopEvent.set()
			self.join()
		except Exception as e:
			getLogger().error("Unable to stop XML-RPC server gracefully: %s" % str(e))
			

################################################################################
# Testerman Server: Main
################################################################################

def getVersion():
	ret = "Testerman Server %s" % Versions.getServerVersion() + "\n" + \
				"API versions:\n Ws: %s\n Xc: %s" % (Versions.getWsVersion(), Versions.getXcVersion())
	return ret

def main():
	server_root = os.path.abspath(os.path.dirname(sys.modules[globals()['__name__']].__file__))
	testerman_home = os.path.abspath("%s/.." % server_root)
	# Set transient values
	cm.set_transient("testerman.testerman_home", testerman_home)
	cm.set_transient("ts.server_root", server_root)
	# standard paths within the document root.
	cm.set_transient("constants.repository", "repository")
	cm.set_transient("constants.archives", "archives")
	cm.set_transient("constants.modules", "modules")
	cm.set_transient("constants.components", "components")
	cm.set_transient("ts.version", Versions.getServerVersion())


	# Register persistent variables
	expandPath = lambda x: x and os.path.abspath(os.path.expandvars(os.path.expanduser(x)))
	splitPaths = lambda paths: [ expandPath(x) for x in paths.split(',')]
	cm.register("interface.ws.ip", "0.0.0.0")
	cm.register("interface.ws.port", 8080)
	cm.register("interface.xc.ip", "0.0.0.0")
	cm.register("interface.xc.port", 8081)
	cm.register("interface.il.ip", "0.0.0.0")
	cm.register("interface.il.port", 8082)
	cm.register("interface.ih.ip", "0.0.0.0")
	cm.register("interface.ih.port", 8083)
	cm.register("interface.xa.ip", "0.0.0.0")
	cm.register("interface.xa.port", 40000)
	cm.register("tacs.ip", "127.0.0.1")
	cm.register("tacs.port", 8087)
	cm.register("ts.daemonize", False)
	cm.register("ts.debug", False)
	cm.register("ts.log_filename", "")
	cm.register("ts.pid_filename", "")
	cm.register("ts.name", socket.gethostname(), dynamic = True)
	cm.register("ts.jobscheduler.interval", 1000, dynamic = True)
	cm.register("testerman.document_root", "/tmp", xform = expandPath, dynamic = True)
	cm.register("testerman.var_root", "", xform = expandPath)
	cm.register("testerman.web.document_root", "%s/web" % testerman_home, xform = expandPath, dynamic = False)
	cm.register("testerman.webclient.document_root", "%s/webclient" % testerman_home, xform = expandPath, dynamic = False)
	cm.register("testerman.administrator.name", "administrator", dynamic = True)
	cm.register("testerman.administrator.email", "testerman-admin@localhost", dynamic = True)
	# testerman.te.*: test executable-related variables
	cm.register("testerman.te.codec_paths", "%s/plugins/codecs" % testerman_home, xform = splitPaths)
	cm.register("testerman.te.probe_paths", "%s/plugins/probes" % testerman_home, xform = splitPaths)
	cm.register("testerman.te.python.interpreter", "/usr/bin/python", dynamic = True)
	cm.register("testerman.te.python.ttcn3module", "TestermanTTCN3", dynamic = True) # TTCN3 adaptation lib (enable the easy use of previous versions to keep script compatibility)
	cm.register("testerman.te.python.additional_pythonpath", "", dynamic = True) # Additional search paths for system-wide modules (non-userland/in repository)
	cm.register("testerman.te.log.max_payload_size", 64*1024, dynamic = True) # the maximum dumpable payload in log (as a single value). Bigger payloads are truncated to this size, in bytes.
	cm.register("ts.webui.theme", "default", dynamic = True)
	cm.register("wcs.webui.theme", "default", dynamic = True)


	parser = optparse.OptionParser(version = getVersion())

	group = optparse.OptionGroup(parser, "Basic Options")
	group.add_option("--debug", dest = "debug", action = "store_true", help = "turn debug traces on")
	group.add_option("-d", dest = "daemonize", action = "store_true", help = "daemonize")
	group.add_option("-r", dest = "docRoot", metavar = "PATH", help = "use PATH as document root (default: %s)" % cm.get("testerman.document_root"))
	group.add_option("--log-filename", dest = "logFilename", metavar = "FILENAME", help = "write logs to FILENAME instead of stdout")
	group.add_option("--pid-filename", dest = "pidFilename", metavar = "FILENAME", help = "write the process PID to FILENAME when daemonizing (default: no pidfile)")
	parser.add_option_group(group)

	group = optparse.OptionGroup(parser, "IPs and Ports Options")
	group.add_option("--ws-ip", dest = "wsIp", metavar = "ADDRESS", help = "set listening Ws IP address to ADDRESS (default: listening on all interfaces)")
	group.add_option("--ws-port", dest = "wsPort", metavar = "PORT", help = "set listening Ws port to PORT (default: %s)" % cm.get("interface.ws.port"), type = "int")
	group.add_option("--xc-ip", dest = "xcIp", metavar = "ADDRESS", help = "set Xc service IP address to ADDRESS (default: Ws IP if set, fallback to hostname resolution)")
	group.add_option("--xc-port", dest = "xcPort", metavar = "PORT", help = "set Xc service port to PORT (default: %s)" % cm.get("interface.xc.port"), type = "int")
	group.add_option("--il-ip", dest = "ilIp", metavar = "ADDRESS", help = "set Il IP address to ADDRESS (default: listening on all interfaces)")
	group.add_option("--il-port", dest = "ilPort", metavar = "PORT", help = "set Il port address to PORT (default: %s)" % cm.get("interface.il.port"), type = "int")
	group.add_option("--ih-ip", dest = "ihIp", metavar = "ADDRESS", help = "set Ih IP address to ADDRESS (default: listening o all interfaces)")
	group.add_option("--ih-port", dest = "ihPort", metavar = "PORT", help = "set Ih port address to PORT (default: %s)" % cm.get("interface.ih.port"), type = "int")
	group.add_option("--tacs-ip", dest = "tacsIp", metavar = "ADDRESS", help = "set TACS Ia target IP address to ADDRESS (default: %s)" % cm.get("tacs.ip"))
	group.add_option("--tacs-port", dest = "tacsPort", metavar = "PORT", help = "set TACS Ia target port address to PORT (default: %s)" % cm.get("tacs.port"), type = "int")
	parser.add_option_group(group)

	group = optparse.OptionGroup(parser, "Advanced Options")
	group.add_option("-V", dest = "varDir", metavar = "PATH", help = "use PATH to persist Testerman Server runtime variables, such as the job queue. If not provided, no persistence occurs between restarts.")
	group.add_option("-C", "--conf-file", dest = "configurationFile", metavar = "FILENAME", help = "path to a configuration file. You may still use the command line options to override the values it contains.")
	group.add_option("-U", "--users-file", dest = "usersFile", metavar = "FILENAME", help = "path to the configuration file that contains authorized webclient users.")
	group.add_option("-A", "--apis-file", dest = "apisFile", metavar = "FILENAME", help = "path to the configuration file that contains supported language apis.")
	group.add_option("--codec-path", dest = "codecPaths", metavar = "PATHS", help = "search for codec modules in PATHS, which is a comma-separated list of paths")
	group.add_option("--probe-path", dest = "probePaths", metavar = "PATHS", help = "search for probe modules in PATHS, which is a comma-separated list of paths")
	group.add_option("--var", dest = "variables", metavar = "VARS", help = "set additional variables as VARS (format: key=value[,key=value]*)")
	parser.add_option_group(group)

	(options, args) = parser.parse_args()


	# Configuration 
	
	# Read the settings from the saved configuration, if any
	configFile = None
	# Provided on the command line ?
	if options.configurationFile is not None:
		configFile = options.configurationFile
	# No config file provided - fallback to $TESTERMAN_HOME/conf/testerman.conf if set and exists
	elif Tools.fileExists("%s/conf/testerman.conf" % testerman_home):
		configFile = "%s/conf/testerman.conf" % testerman_home
	cm.set_transient("ts.configuration_filename", configFile)

	usersFile = None
	# Provided on the command line ?
	if options.usersFile is not None:
		usersFile = options.usersFile
	# No config file provided - fallback to $TESTERMAN_HOME/conf/webclient-users.conf if set and exists
	elif Tools.fileExists("%s/conf/webclient-users.conf" % testerman_home):
		usersFile = "%s/conf/webclient-users.conf" % testerman_home
	cm.set_transient("wcs.users_filename", usersFile)

	apisFile = None
	# Provided on the command line ?
	if options.apisFile is not None:
		apisFile = options.apisFile
	# No config file provided - fallback to $TESTERMAN_HOME/conf/language-apis.conf if set and exists
	elif Tools.fileExists("%s/conf/language-apis.conf" % testerman_home):
		apisFile = "%s/conf/language-apis.conf" % testerman_home
	cm.set_transient("ts.apis_filename", apisFile)

	try:
		if configFile: cm.read(configFile)
		if usersFile: cm.read(usersFile, autoRegister = True)
		if apisFile: cm.read(apisFile, autoRegister = True)
	except Exception as e:
		print (str(e))
		return 1


	# Now, override read settings with those set on explicit command line flags
	# (or their default values inherited from the ConfigManager default values)
	cm.set_user("interface.ws.ip", options.wsIp)
	cm.set_user("interface.ws.port", options.wsPort)
	cm.set_user("interface.xc.ip", options.xcIp)
	cm.set_user("interface.xc.port", options.xcPort)
	cm.set_user("interface.il.ip", options.ilIp)
	cm.set_user("interface.il.port", options.ilPort)
	cm.set_user("interface.ih.ip", options.ihIp)
	cm.set_user("interface.ih.port", options.ihPort)
	cm.set_user("tacs.ip", options.tacsIp)
	cm.set_user("tacs.port", options.tacsPort)
	cm.set_user("ts.daemonize", options.daemonize)
	cm.set_user("ts.debug", options.debug)
	cm.set_user("ts.log_filename", options.logFilename)
	cm.set_user("ts.pid_filename", options.pidFilename)
	cm.set_user("testerman.document_root", options.docRoot)
	cm.set_user("testerman.te.codec_paths", options.codecPaths)
	cm.set_user("testerman.te.probe_paths", options.probePaths)
	cm.set_user("testerman.var_root", options.varDir)
	if options.variables:
		for var in options.variables.split(','):
			try:
				(key, val) = var.split('=')
				cm.set_user(key, val)
			except:
				pass

	# Commit all provided values (construct actual values via registered xforms)
	cm.commit()

	# Compute/adjust actual variables where applies
	# Actual Xc IP address: if not explictly set, fallback to ws.ip, then to hostname().	
	ip = cm.get("interface.xc.ip")
	if not ip or ip == '0.0.0.0':
		ip = cm.get("interface.ws.ip")
		cm.set_actual("interface.xc.ip", ip)
	if not ip or ip == '0.0.0.0':
		cm.set_actual("interface.xc.ip", socket.gethostbyname(socket.gethostname())) # Not fully qualified ? defaults to the hostname resolution.

	# Set the TACS IP address that can be used by agents
	# Normally, we should ask the TACS the server is connected to to get this value.
	tacs = cm.get("interface.xa.ip")
	if not tacs or tacs == '0.0.0.0':
		# We'll publish the XC as XA IP address. If it was also set to localhost, it's unlikely agents are deployed outside localhost too.
		cm.set_actual("interface.xa.ip", cm.get("interface.xc.ip"))

	# If an explicit pid file was provided, use it. Otherwise, fallback to the var_root/ts.pid if possible.
	pidfile = cm.get("ts.pid_filename")
	if not pidfile and cm.get("testerman.var_root"):
		# Set an actual value
		pidfile = cm.get("testerman.var_root") + "/ts.pid"
		cm.set_actual("ts.pid_filename", pidfile)

#	print (Tools.formatTable([ ('key', 'Name'), ('format', 'Type'), ('dynamic', 'Dynamic'), ('default', 'Default value'), ('user', 'User value'), ('actual', 'Actual value')], cm.getVariables(), order = "key"))

	# Logger initialization
	if cm.get("ts.debug"):
		level = logging.DEBUG
	else:
		level = logging.INFO
	logging.basicConfig(level = level, format = '%(asctime)s.%(msecs)03d %(thread)d %(levelname)-8s %(name)-20s %(message)s', datefmt = '%Y%m%d %H:%M:%S', filename = cm.get("ts.log_filename"))

	# Display startup info
	getLogger().info("Starting Testerman Server %s" % (Versions.getServerVersion()))
	getLogger().info("Web Service       (Ws) listening on tcp://%s:%s" % (cm.get("interface.ws.ip"), cm.get("interface.ws.port")))
	getLogger().info("Client events     (Xc) listening on tcp://%s:%s" % (cm.get("interface.xc.ip"), cm.get("interface.xc.port")))
	getLogger().info("Log manager       (Il) listening on tcp://%s:%s" % (cm.get("interface.il.ip"), cm.get("interface.il.port")))
	getLogger().info("Component manager (Ih) listening on tcp://%s:%s" % (cm.get("interface.ih.ip"), cm.get("interface.ih.port")))
	getLogger().info("Using TACS at tcp://%s:%s" % (cm.get("tacs.ip"), cm.get("tacs.port")))
	items = cm.getKeys()
	items.sort()
	for k in items:
		getLogger().info("Using %s = %s" % (str(k), cm.get(k)))

	# Now we can daemonize if needed
	if cm.get("ts.daemonize"):
		if pidfile:
			getLogger().info("Daemonizing, using pid file %s..." % pidfile)
		else:
			getLogger().info("Daemonizing...")
		Tools.daemonize(pidFilename = pidfile, displayPid = True)


	# Main start
	cm.set_transient("ts.pid", os.getpid())
	try:
		serverThread = XmlRpcServerThread() # Ws server
		FileSystemManager.initialize()
		EventManager.initialize() # Xc server, Ih server [TSE:CH], Il server [TSE:TL]
		ProbeManager.initialize() # Ia client
		JobManager.initialize() # Job scheduler
		serverThread.start()
		getLogger().info("Started.")
		while 1:
			time.sleep(1)
	except KeyboardInterrupt:
		getLogger().info("Shutting down Testerman Server...")
	except Exception as e:
		sys.stderr.write("Unable to start server: %s\n" % str(e))
		getLogger().critical("Unable to start server: " + str(e))

	serverThread.stop()
	JobManager.finalize()
	ProbeManager.finalize()
	EventManager.finalize()
	FileSystemManager.finalize()
	getLogger().info("Shut down.")
	logging.shutdown()
	Tools.cleanup(cm.get("ts.pid_filename"))

if __name__ == "__main__":
	main()
