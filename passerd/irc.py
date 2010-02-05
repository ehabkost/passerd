#!/usr/bin/env python
#
# Passerd - An IRC server as a gateway to Twitter
#
# Basic IRC protocol abstraction classes
#
# Author: Eduardo Habkost <ehabkost@raisama.net>
#
# Copyright (c) 2009 Eduardo Pereira Habkost <ehabkost@raisama.net>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import logging

from twisted.words.protocols import irc
from twisted.internet import defer
from passerd.util import hooks

logger = logging.getLogger("passerd.irc")
dbg = logger.debug
pinfo = logger.info
perror = logger.error


class IrcTarget:
    """Common class for IRC channels and users

    This may contain some common operations that work for both users and
    channels.
    """
    def __init__(self, proto):
        self.proto = proto
        self.msg_notifiers  = []

    def add_msg_notifier(self, func):
        self.msg_notifiers.append(func)

    def notify_message(self, sender, msg):
        for func in self.msg_notifiers:
            func(self, sender, msg)

    def parseModeSetRequest(self, args):
        """Parse a mode-change request, generating (flags,params) tuples

        Whoever invented the MODE command syntax is _really_ evil.
        """
        i = 0
        dbg("parsing mode request: %r" % (args))
        while i < len(args):
            flags = args[i]
            i += 1
            params = []
            while i < len(args):
                a = args[i]
                if a[:1] in '+-':
                    # a new flag set/unset was requested
                    break
                params.append(a)
                i += 1
            dbg("flags: %r, params: %r" % (flags, params))
            yield flags,params

    def modeFlagQuery(self, flag, params):
        dbg("mode flag query: %r %r" % (flag, params))
        # specific mode query/set request:
        if not flag in self.supported_modes:
            self.proto.send_reply(irc.ERR_UNKNOWNMODE, 'Mode %s is not known to me' % (flag))
            return
        fn = getattr(self, 'mode_query_%s' % (flag))
        fn(params)

    def flagChangeRequest(self, flag, value, params):
        dbg("mode change request: %r %s %r" % (flag, value, params))
        if not flag in self.supported_modes:
            self.proto.send_reply(irc.ERR_UNKNOWNMODE, flag, ':Mode %s is not known to me' % (flag))
            return
        fn = getattr(self, 'mode_set_%s' % (flag))
        fn(value, params)

    def modeFlagRequest(self, sender, args):
        for flags,params in self.parseModeSetRequest(args):
            value = 0
            for f in flags:
                if f == '+': value = 1
                elif f == '-': value = -1
                else:
                    if value == 0:
                        # no "+" or "-" => simple query
                        self.modeFlagQuery(f, params)
                    else:
                        self.flagChangeRequest(f, value, params)


    def modeRequest(self, sender, args):
        if len(args) == 1:
            # general mode query
            self.sendModes()
        else:
            self.modeFlagRequest(self, args[1:])

    def ctcp_unknown(self, tag, data):
        dbg("Unsupported CTCP query: %r %r" % (tag, data))

    def ctcpQueryReceived(self, sender, query):
        for tag,data in query:
            m = getattr(self, 'ctcp_%s' % (tag), None)
            if m is not None:
                m(data)
            else:
                self.ctcp_unknown(tag, data)

    def messageReceived(self, sender, msg):
        assert (sender is self.proto.the_user)
        self.notify_message(sender, msg)


class IrcUser(IrcTarget):
    supported_modes = ''

    def __cmp__(self, o):
        return cmp(self.nick, o.nick)

    def target_name(self):
        return self.nick

    def is_away(self):
        return False

    def away_char(self):
        if self.is_away(): return '-'
        else: return '+'

    def userhost(self):
        return '%s@%s' % (self.username, self.hostname)

    def full_id(self):
        return '%s!%s@%s' % (self.nick, self.username, self.hostname)

    def notifyNickChange(self, new_nick):
        """Must be called before self.nick value changes, so the sender ID is correct"""
        self.proto.send_message(self, 'NICK', new_nick)

    def force_nick(self, new_nick):
        if self.nick != new_nick:
            self.notifyNickChange(new_nick)
            self.nick = new_nick

class IrcChannel(IrcTarget):
    supported_modes = 'b'

    def __init__(self, proto, name):
        IrcTarget.__init__(self, proto)
        self.name = name

    def target_name(self):
        return self.name

    def ban_masks(self, params):
        return []

    def list_members(self):
        #FIXME: include the_user only if the user already joined
        return [self.proto.the_user]

    def mode_query_b(self, params):
        """Query ban list"""
        dbg("checking the ban list for %s" % (self.name))
        for m in self.ban_masks(params):
            self.proto.send_reply(irc.RPL_BANLIST, self.name, m)
        self.proto.send_reply(irc.RPL_ENDOFBANLIST, self.name, ":End of channel ban list")

    def mode_set_b(self, value, params):
        dbg("ban mode set request: %r %r" % (value, params))
        if len(params) == 0:
            # no params means this is a mode query
            return self.mode_query_b(params)

        raise NotImplementedError("Ban setting is not implemented")

    def send_message(self, sender, msg):
        self.proto.send_privmsg(sender, self, msg)

    def typeChar(self):
        """Return '@', '*', or '=', for RPL_NAMREPLY"""
        return '=' # show channel as public by default

    def userModeChar(self, user):
        """Retuern '', '@', or '+', depending on user mode"""
        return ''

    def fullModeSpec(self):
        # return no modes, by default
        return ''

    def notifyJoin(self, who):
        self.proto.send_message(who, 'JOIN', self.name)
    def notifyKick(self, kicker, kicked):
        self.proto.send_message(kicker, 'KICK', self.name, kicked.nick)
    def notifyPart(self, who, reason):
        if reason is not None:
            self.proto.send_message(who, 'PART', self.name, reason)
        else:
            self.proto.send_message(who, 'PART', self.name)
    def notifyTopic(self):
        self.proto.send_reply(irc.RPL_TOPIC, self.name, ':%s' % (self.topic()))
    def sendModes(self):
        self.proto.send_reply(irc.RPL_CHANNELMODEIS, self.name, self.fullModeSpec())

    def _sendNames(self, members):
        namelist = []
        def flush():
            names = ' '.join(namelist)
            self.proto.send_reply(irc.RPL_NAMREPLY, '=', self.name, ':%s' % (names))
            namelist[:] = []

        for m in members:
            namelist.append('%s%s' % (self.userModeChar(m), m.nick))
            if len(namelist) > 30:
                flush()
        flush()
        self.proto.send_reply(irc.RPL_ENDOFNAMES, self.name, ':End of NAMES list')

    def sendNames(self):
        def doit():
            d = defer.maybeDeferred(self.list_members)
            d.addCallback(send_names).addErrback(error)

        def send_names(members):
            dbg("got member list: %d members" % (len(members)))
            self._sendNames(members)

        def error(e):
            self.proto.notice("ERROR: failure getting member names for %s -- %s" % (self.name, e.value))
            #FIXME: include the_user only if the user already joined
            self._sendNames([self.proto.the_user])

        doit()

    @hooks
    def userJoined(self, user):
        self.notifyJoin(user)
        self.notifyTopic()
        self.sendNames()

    @hooks
    def userLeft(self, user, reason):
        """Called when the user just joined the channel"""
        pass

    @hooks
    def userQuit(self, user, reason):
        pass

    def topic(self):
        return "[no topic set]"

    def kickUser(self, sender, nickname):
        return NotImplementedError("Can't kick users from %s" % (self.name))

    def inviteUser(self, nickname):
        return NotImplementedError("Can't invite users to %s" % (self.name))

    def kickUsers(self, sender, users):
        for u in users:
            self.kickUser(sender, u)


class IrcServer(IrcTarget):
    """An IrcTarget used for server messages"""
    def __init__(self, proto, name):
        IrcTarget.__init__(self, proto)
        self.name = name

    def full_id(self):
        return self.name



