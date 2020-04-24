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
# TCAP (itu-t) Codec
# To compile TcapAsn: ./py_output.py --explicit asn/tcap.asn > TcapAsn.py
##

import CodecManager

import ber.BerCodec as BerCodec
import ber.TcapAsn as TcapAsn
import ber.TcapDialoguePdusAsn as TcapDialoguePdusAsn

class TcapCodec(BerCodec.BerCodec):
	"""
	= Identification and Properties =
	
	Codec IDs: `tcap.*`
	 * `tcap.DialoguePDU`
	
	Properties:
	|| '''Name''' || '''Type''' || '''Default value''' || '''Description''' ||
	
	= Overview =
	
	== Decoding ==
	
	...
	
	== Encoding ==
	
	...

	== Availability ==

	All platforms.

	== Dependencies ==

	None.

	== See Also ==
	
	The mapping between ASN.1 and Testerman structures is documented [Asn1ToTesterman here].

	"""
	PDU = TcapAsn.TCMessage

	def getSummary(self, message):
		try:
			# message is expected to be a choice (begin/end/continue...)
			return 'TCAP %s' % message[0]
		except:
			return super.getSummary(message)

CodecManager.registerCodecClass('tcap', TcapCodec)

class DialoguePDUCodec(BerCodec.BerCodec):
	PDU = TcapDialoguePdusAsn.DialoguePDU
	def getSummary(self, message): return 'DialoguePDU'
CodecManager.registerCodecClass('tcap.DialoguePDU', DialoguePDUCodec)

if __name__ == '__main__':
	import binascii
	
	tcapBegin = \
	 "62644804000227846b3e283c060700118605010101a031602fa109060704000001001302be222820060704000001010101a015a01380099622123008016901f98106a807000000016c1ca11a02010102013b301204010f0405a3986c36028006a80700000001"

# From gsm_map_with_ussd_string.pcap sample
	tcapBegin2 = \
	 "626a48042f3b46026b3a2838060700118605010101a02d602b80020780a109060704000001001302be1a2818060704000001010101a00da00b80099656051124006913f66c26a12402010102013b301c04010f040eaa180da682dd6c31192d36bbdd468007917267415827f2"

	dialoguePDU = \
	"601aa109060704000001001902be0d280ba009a00780059691214365"

	print (80*'-')
	print ("TCAP (ITU-T) Codec unit tests")
	print (80*'-')
	samples = [	
		('tcap', tcapBegin),
		('tcap', tcapBegin2),
		('tcap.DialoguePDU', dialoguePDU)
	]

	for pdu, s in samples:
		print ()
		print (80*'-')
		print ("Testing: %s" % s)
		s = binascii.unhexlify(s)
		(decoded, summary) = CodecManager.decode(pdu, s)
		print ("Decoded: %s\nSummary: %s" % (decoded, summary))
		(reencoded, summary) = CodecManager.encode(pdu, decoded)
		print ("Reencoded: %s\nSummary: %s" % (binascii.hexlify(reencoded), summary))
		print ("Original : %s" % binascii.hexlify(s))
#		assert(s == reencoded) # Cannot always work due to default value decoding/reencoding
		(redecoded, summary) = CodecManager.decode(pdu, reencoded)
		print ("Decoded: %s\nSummary: %s" % (redecoded, summary))
		assert(redecoded == decoded)
