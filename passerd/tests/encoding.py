# -*- coding: utf-8 -*-

import unittest

from passerd.util import try_unicode

class EncodingTests(unittest.TestCase):
    def assertEqualsU(self, a, b):
        self.assertTrue(isinstance(a, unicode))
        self.assertTrue(isinstance(b, unicode))
        self.assertEquals(len(a), len(b))
        for i in range(len(a)):
            self.assertEquals(a[i], b[i], 'mismatch at char %d: %r != %r' % (i, a[i], b[i]))

    def testAscii(self):
        self.assertEqualsU(try_unicode('abcde'), u'abcde')

    def testSimpleUtf8(self):
        # 'abcáéíxń'
        self.assertEqualsU(try_unicode('abc\xc3\xa1\xc3\xa9\xc3\xadx\xc5\x84'), u'abc\xe1\xe9\xedx\u0144')

    def testUtf8BMP(self):
        u = u''.join([unichr(i) for i in range(0, 0xdc00)])
        e = u.encode('utf-8')
        self.assertEquals(try_unicode(e), u)
        self.assertEqualsU(try_unicode(e), u)

    def testSimpleLatin1(self):
        # 'abcáàüx'
        l = 'abc\xe1\xe0\xfcx'
        self.assertEqualsU(try_unicode(l), u'abcáàüx')

    def testFullLatin1(self):
        """ISO-8859-1 support, for all printable chars"""
        chars = range(0x20, 0x7f)+range(0xa0,0x100)
        l = ''.join(chr(c) for c in chars)
        u = try_unicode(l)
        for i in range(len(chars)):
            self.assertEquals(chars[i], ord(u[i]))

    def testSimpleWindows1252(self):
        w = 'this is a test \x99 - d\xe9j\xe0 vu. \x9c'
        u = try_unicode(w)
        self.assertEqualsU(u, u'this is a test ™ - déjà vu. œ')
