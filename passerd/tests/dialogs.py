import re

from passerd.dialogs import *

from unittest import TestCase

class DialogLogger:
    def __init__(self, d):
        self.msgs = []
        d.set_message_func(self.got_msg)

    def got_msg(self, msg):
        self.msgs.append(msg)

class DialogTestMixin:
    def assertMsgs(self, *args):
        self.assertEquals(self.log.msgs, list(args))

    def assertSomeMsg(self, msg):
        self.assertTrue(msg in self.log.msgs, '%r is not among the replies' % (msg))

    def _hasSomeRe(self, pat, flags=0):
        r = re.compile(pat, flags)
        for m in self.log.msgs:
            if r.search(m):
                return True
        return False

    def assertSomeRe(self, pat, flags=0):
        self.assertTrue(self._hasSomeRe(pat, flags), '%r is not a match on any reply' % (pat))

    def msgs(self, *args):
        for a in args:
            self.d.message(a)

    def rcv(self, msg):
        self.d.recv_message(msg)

    def wait(self, pattern, *args):
        replies = list(args)
        self.d.wait_for(pattern, lambda msg,m: self.msgs(*args))


class SimpleDialogTest(DialogTestMixin, TestCase):
    def setUp(self):
        self.d = d = Dialog()
        self.log = DialogLogger(d)

    def testSimple(self):
        self.wait('hi', 'hi!')
        self.rcv('hi')
        self.assertMsgs('hi!')
        self.assertFalse(self._hasSomeRe('nononono'))

    def testMulti(self):
        self.wait('bye', 'bye', 'see you later')
        self.wait('hi', 'hi!', 'how are you?')
        self.rcv('hi!')
        self.rcv('bye')
        self.assertMsgs('hi!', 'how are you?', 'bye', 'see you later')

    def testNoMatch(self):
        self.d.unknown_message = lambda msg: self.msgs('what?')
        self.wait('hello', 'hello world')
        self.rcv('hi')
        self.assertMsgs('what?')


    def testOverride(self):
        self.wait('hi', 'first hi')
        self.wait('hi1', 'hi1 reply')
        self.rcv('hi')
        self.rcv('hi1')
        self.assertMsgs('first hi', 'hi1 reply')
        self.wait('hi', 'second hi')
        self.rcv('hi1')
        self.assertMsgs('first hi', 'hi1 reply', 'second hi')

    def testException(self):
        def err(msg, m):
            raise Exception('[error: %s - %s]' % (msg, m.group(1)))

        self.d.wait_for('explode (.*)', err)
        self.rcv('explode now')
        self.assertEquals(len(self.log.msgs), 1)
        # check just if the exception message is inside the received reply:
        self.assertTrue('[error: explode now - now]' in self.log.msgs[0], 'no exception text on error reply')


class _TestCommands(CommandDialog):
    shorthelp_hi = 'say hi'
    def command_hi(self, args):
        self.message('hi %s!' % (args))

class TestCommands(DialogTestMixin, TestCase):
    def setUp(self):
        self.d = _TestCommands()
        self.log = DialogLogger(self.d)

    def testHi(self):
        self.rcv('hi you')
        self.assertMsgs('hi you!')

    def testHelp(self):
        self.rcv('help')
        self.assertSomeRe('HI - say hi')

    def testPrefix(self):
        self.d.set_cmd_prefix('!FOO-')
        self.rcv('help')
        self.assertSomeRe('!FOO-HI - say hi')

    def testUnknown(self):
        self.d.unknown_command = lambda cmd,args: self.d.message('%s-%s' % (cmd,args))
        self.rcv('nono yesyes no')
        self.assertMsgs('nono-yesyes no')

    def testAlias(self):
        self.d.add_alias('hello', 'hi')
        self.rcv('hello world')
        self.assertMsgs('hi world!')
        self.rcv('help')
        self.assertSomeRe('HELLO - Synonym to HI: say hi')

if __name__ == '__main__':
    unittest.main()
