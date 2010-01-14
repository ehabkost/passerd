# -*- coding: utf-8 -*-

import unittest

from passerd import ircd


class FakeProto(ircd.PasserdProtocol):
    """Fake PasserdProtocol object"""
    def __init__(self):
        self.user_cfg_vars = {}
        self.privmsg_log = []
        self.fake_users = {}
        self.passerd_bot = 'this_is_passerd-bot'

    def user_cfg_var_b(self, var):
        return self.user_cfg_vars.get(var, False)

    def get_twitter_user(self, twid):
        return self.fake_users[twid]

    def send_privmsg(self, sender, target, msg):
        self.privmsg_log.append( (sender, target, msg) )

class FakeChannel(ircd.TwitterChannel):
    def __init__(self, proto):
        self.proto = proto


class O:
    """Automatic kwargs->attributes object"""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class TestMsgFormatting(unittest.TestCase):
    def setUp(self):
        self.proto = FakeProto()
        self.chan = FakeChannel(self.proto)

    def testNoMultiOneLine(self):
        self.proto.user_cfg_vars['multiline'] = False
        t = u'aeióú'
        self.proto.send_text('s', 't', t)
        self.assertEquals(self.proto.privmsg_log, [('s','t', t)])

    def testMultiOneLine(self):
        self.proto.user_cfg_vars['multiline'] = True
        t = u'aeióú2'
        self.proto.send_text('s', 't', t)
        self.assertEquals(self.proto.privmsg_log, [('s','t', t)])

    def testNoMultiManyLines(self):
        self.proto.user_cfg_vars['multiline'] = False
        t = u'aei óú\nfoo barß\nüber yeah!'
        self.proto.send_text('s', 't', t)
        self.assertEquals(self.proto.privmsg_log, [('s','t', u'aei óú foo barß über yeah!')])

    def testMultiManyLines(self):
        self.proto.user_cfg_vars['multiline'] = True
        t = u'aei óú\nfoo barß\nüber yeah!'
        self.proto.send_text('s', 't', t)
        self.assertEquals(self.proto.privmsg_log, [('s','t', u'aei óú'),('s','t',u'[...] foo barß'),('s','t',u'[...] über yeah!')])

    def sendAliceBobRT(self):
        self.alice_u = self.proto.fake_users[1] = 'this_is_alice'

        alice = O(screen_name='alice', id=1)
        bob = O(screen_name='bob', id=2)
        orig = O(id=123, text=u'this is über cool!', user=alice)
        rt = O(id=456, text=u'RT @alice: this is über cool ...', user=bob, retweeted_status=orig)

        self.chan.printEntry(rt)

    def testRtNoInline(self):
        self.proto.user_cfg_vars['rt_inline'] = False
        self.sendAliceBobRT()
        self.assertEquals(self.proto.privmsg_log,
                          [(self.alice_u, self.chan, u'this is über cool!'),
                           (self.proto.passerd_bot, self.chan, '(alice retweeted by bob)')])

    def testRtNoInline(self):
        self.proto.user_cfg_vars['rt_inline'] = True
        self.sendAliceBobRT()
        self.assertEquals(self.proto.privmsg_log,
                          [(self.alice_u, self.chan, u'this is über cool! \x02[RT by @bob]\x02')])
