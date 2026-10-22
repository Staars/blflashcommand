# decompyle3 version 3.9.0
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.16 (default, Mar 30 2023, 01:25:49) 
# [GCC 12.2.1 20220924]
# Embedded file name: libs/bflb_ecdh.py
from libs.bflb_optional import require_dependency
import binascii
from libs.bflb_utils import *

class BflbEcdh(object):

    def __init__(self, curve=None):
        ecdsa = require_dependency("ecdsa", "ecdsa", "ECDH authentication")
        self.ecdh = ecdsa.ECDH(curve or ecdsa.NIST256p)
        self.local_public_key = None
        self.sharedsecret = ''

    def create_public_key(self):
        self.ecdh.generate_private_key()
        self.local_public_key = self.ecdh.get_public_key()
        ret = binascii.hexlify(self.local_public_key.to_string()).decode('utf-8')
        printf('local public key:')
        printf(ret)
        return ret

    def create_shared_key(self, peer_pk):
        self.ecdh.load_received_public_key_bytes(binascii.unhexlify(peer_pk))
        self.sharedsecret = self.ecdh.generate_sharedsecret_bytes()
        ret = binascii.hexlify(self.sharedsecret).decode('utf-8')
        printf('secret key:')
        printf(ret)
        return ret
# okay decompiling ./libs/bflb_ecdh.pyc
