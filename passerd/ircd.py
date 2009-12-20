#!/usr/bin/env python
#
# Passerd - An IRC server as a gateway to Twitter
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

import sys, logging, time
import optparse

from twisted.words.protocols import irc
from twisted.words.protocols.irc import IRC
from twisted.internet.protocol import Factory
from twisted.internet import reactor, defer
from twisted.python import log


from twittytwister.twitter import Twitter

from passerd.data import DataStore, TwitterUserData
from passerd.feeds import HomeTimelineFeed
from passerd.callbacks import CallbackList
from passerd.utils import full_entity_decode


MYAGENT = 'Passerd'
#FIXME: use a real hostname?
MYHOST = 'passerd.server'
VERSION = '0.0.3'
SUPPORTED_USER_MODES = '0'
SUPPORTED_CHAN_MODES = 'b'

ENCODING = 'utf-8'
FALLBACK_ENCODING = 'iso-8859-1'

TWITTER_ENCODING = 'utf-8'


# if more than MAX_USER_INFO_FETCH users are unknown, use /statuses/friends to fetch user info.
# otherwise, just fetch individual user info

MAX_USER_INFO_FETCH = 0  # individual fetch is not implemented yet...

# the maximum number of sequential friend list page requests:
MAX_FRIEND_PAGE_REQS = 10


LENGTH_LIMIT = 140

dbg = logging.debug
pinfo = logging.info


def hooks(fn):
    """Decorator that call beforeFoo() and afterFoo() methods if available"""
    name = fn.func_name
    upname = name[0].upper()+name[1:]
    before = 'before%s' % (upname)
    after = 'after%s' % (upname)
    def call_with_hooks(self, *args, **kwargs):
        if hasattr(self, before):
            getattr(self, before)(*args, **kwargs)
        r = fn(self, *args, **kwargs)
        if hasattr(self, after):
            getattr(self, after)(*args, **kwargs)
        return r
    return call_with_hooks

def try_unicode(s):
    for e in (ENCODING, FALLBACK_ENCODING):
        try:
            return unicode(s, e)
        except:
            pass

    # no success:
    raise Exception("couldn't decode message as unicode")


class IrcTarget:
    """Common class for IRC channels and users

    This may contain some common operations that work for both users and
    channels.
    """
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
        fn(args)

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
                        self.modeFlagQuery(self, f, params)
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


class IrcUser(IrcTarget):
    supported_modes = SUPPORTED_USER_MODES

    def __init__(self, proto):
        self.proto = proto

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

    def messageReceived(self, sender, msg):
        raise NotImplementedError("private messages aren't supported yet!")

    def notifyNickChange(self, new_nick):
        """Must be called before self.nick value changes, so the sender ID is correct"""
        self.proto.send_message(self, 'NICK', new_nick)


class IrcChannel(IrcTarget):
    supported_modes = SUPPORTED_CHAN_MODES

    def __init__(self, proto, name):
        self.name = name
        self.proto = proto

    def target_name(self):
        return self.name

    def ban_masks(self, params):
        return []

    def list_members(self):
        return []

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

    def sendMessage(self, sender, msg):
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
            d.addCallbacks(send_names, error)
            d.addErrback(log.err)

        def send_names(members):
            dbg("got member list: %d members" % (len(members)))
            self._sendNames(members)

        def error(*args):
            dbg("ERROR: failure getting member names")
            self._sendNames([self.proto.the_user])

        doit()

    @hooks
    def userJoined(self, user):
        self.notifyJoin(user)
        self.notifyTopic()
        self.sendNames()

    @hooks
    def userLeft(self, user, reason):
        self.notifyPart(user, reason)

    @hooks
    def userQuit(self, user, reason):
        pass

    def messageReceived(self, sender, msg):
        raise NotImplementedError("Channel %s doesn't handle incoming messages" % (self.name))

    def topic(self):
        return "[no topic set]"

    def kickUsers(self, sender, users):
        for u in users:
            self.kickUser(sender, u)


class IrcServer(IrcTarget):
    """An IrcTarget used for server messages"""
    def __init__(self, name):
        self.name = name

    def full_id(self):
        return self.name


class TwitterUserInfo:
    """Just carries simple data for a Twitter user
    """
    def from_data(self, d):
        self.screen_name = d.twitter_screen_name
        self.name = d.twitter_name
        return self

    def to_data(self, d):
        d.twitter_screen_name = self.screen_name
        d.twitter_name = self.name
        return self

    def __repr__(self):
        return 'TwitterUserInfo(%r, %r)' % (self.screen_name, self.name)

class TwitterUserCache:
    """Caches information about Twitter users

    This is server-global, and is just an interface to the twitter_users database
    table.
    """
    def __init__(self, proto):
        self.proto = proto
        self.callbacks = CallbackList()
        self.id2user = {}

    def addCallback(self, cb, *args, **kwargs):
        """Add a new callback function

        Callback function is called with arguments: (twitter_user, old_info, new_info)
        old_info may be None, if it's a new user
        """
        self.callbacks.addCallback(cb, *args, **kwargs)

    def _change_data(self, d, old_info, new_info):
        # callbacks must becalled _before_ updating the data:
        self.callbacks.callback(d.twitter_id, old_info, new_info)

        new_info.to_data(d)

    def _new_user(self, id, info):
        d = TwitterUserData(twitter_id=id)

        self._change_data(d, None, info)
        #FIXME: encapsulate the following session operations, somehow:
        self.proto.data.session.add(d)
        self.proto.data.session.commit()
        return d

    def _update_user_info(self, id, new_info):
        d = self.lookup_id(id)
        if d is None:
            return self._new_user(id, new_info)

        old_info = TwitterUserInfo().from_data(d)
        self._change_data(d, old_info, new_info)
        #FIXME: encapsulate the following session operation, somehow:
        self.proto.data.session.commit()
        return d

    def update_user_info(self, id, screen_name, name):
        id = int(id)
        i = TwitterUserInfo()
        i.screen_name = screen_name
        i.name = name
        return self._update_user_info(id, i)

    def got_api_user_info(self, u):
        self.update_user_info(u.id, u.screen_name, u.name)

    def lookup_id(self, id):
        id = int(id)
        #FIXME: encapsulate the following session operations, somehow:
        d = self.proto.data.query(TwitterUserData).get(id)
        if d is None:
            return None

        return d


class UnavailableTwitterData:
    """Fake TwitterUserData object for unavailable info"""
    def __init__(self, id):
        self.twitter_id = id

    twitter_screen_name = property(lambda self: 'user-id-%s' % (self.twitter_id))
    twitter_name = property(lambda self: 'Twitter User (info not fetched yet)')


class TwitterIrcUser(IrcUser):
    def __init__(self, proto, cache, id):
        IrcUser.__init__(self, proto)
        self._twitter_id = id
        self.cache = cache
        self._data = None

    def data_changed(self, old_info, new_info):
        dbg("TwitterIrcUser.data_changed! %r %r" % (old_info, new_info))
        if (old_info is None) or (old_info.screen_name != new_info.screen_name):
            self.notifyNickChange(str(new_info.screen_name))

    def __get_data(self):
        if self._data is None:
            self._data = self.cache.lookup_id(self._twitter_id)
        return self._data

    def _get_data(self):
        d = self.__get_data()
        if d is None:
            return UnavailableTwitterData(self._twitter_id)

        return d

    def has_data(self):
        """Checks if the Twitter user info is known"""
        return (self.__get_data() is not None)

    data = property(_get_data)
    nick = property(lambda self: str(self.data.twitter_screen_name))
    username = property(lambda self: str(self.data.twitter_screen_name))
    real_name = property(lambda self: self.data.twitter_name.encode('utf-8'))
    hostname = property(lambda self: 'twitter.com')


class TwitterIrcUserCache:
    """Cache of TwitterIrcUser objects

    A TwitterIrcUserCache is client-specific (not server-global), and takes
    care of the TwitterIrcUser objects that point to Twitter user data.
    """
    def __init__(self, proto, cache):
        self.proto = proto
        self.cache = cache
        self.cache.addCallback(self._user_changed)
        self.id2user = {}

    def _get_user(self, id):
        return self.id2user.get(int(id))

    def _user_changed(self, id, old_info, new_info):
        dbg("user_changed: %r, %r, %r" % (id, old_info, new_info))
        u = self._get_user(id)
        if u is not None:
            dbg("user_changed: got user.")
            u.data_changed(old_info, new_info)

    def _new_user(self, id):
        u = TwitterIrcUser(self.proto, self.cache, id)
        self.id2user[int(id)] = u
        return u

    def user_from_id(self, id):
        u = self._get_user(id)
        if u is not None:
            # already on current list
            return u

        # found on the DB, but not on our list:
        return self._new_user(id)

    def fetch_individual_user_info(self, unknown_users):
        #TODO: implement me
        pass

    def fetch_all_friend_info(self, unknown_users):
        reqs = []
        def request_cursor(cursor):
            self.proto.dbg("requesting a page from the friend list: %s" % (str(cursor)))
            reqs.append(cursor)
            self.proto.api.list_friends(got_user, params={'cursor':cursor},
                                        page_delegate=end_page).addCallbacks(done, error)

        def got_user(u):
            self.proto.global_twuser_cache.got_api_user_info(u)

        def end_page(next, prev):
            unk = [u for u in unknown_users if not u.has_data()]
            num = len(unk)

            if num == 0:
                self.proto.notice("I know all of your friends, now!")
                return

            self.proto.dbg("%d users are still unknown" % (num))

            if not next or next == '0':
                self.proto.dbg("yay! that was the last page!")
                if num > 0:
                    self.proto.notice("something seems to be wrong: I fetched all pages and I still don't know all of your friends")
                return

            if len(reqs) > MAX_FRIEND_PAGE_REQS:
                self.proto.notice("I already fetched %d pages of detailed friend info. I won't fetch more, sorry.")
                return

            request_cursor(next)

        def done(*args):
            self.proto.dbg("list_friends api request finished")

        def error(e):
            self.proto.dbg("list_friends error: %s" % (e))

        request_cursor('-1')

    def fetch_friend_info(self, users):
        dbg("fetch_friend_info: begin:")
        unknown_users = [u for u in users if not u.has_data()]
        dbg("fetch_friend_info: got unknown users...")
        if len(unknown_users) == 0:
            self.proto.notice("I already know all your friends. cool!")
            return

        dbg("%d unknown users..." % (len(unknown_users)))
        self.proto.notice("There are %d users I don't know about. I will fetch your detailed friend list" % (len(unknown_users)))

        if len(unknown_users) < MAX_USER_INFO_FETCH:
            self.fetch_individual_user_info(unknown_users)
        else:
            self.fetch_all_friend_info(unknown_users)

class TwitterChannel(IrcChannel):
    """The #twitter channel"""
    def __init__(self, proto, name):
        IrcChannel.__init__(self, proto, name)
        self.feed = HomeTimelineFeed(proto)
        self.feed.addCallback(self.got_entry)
        self.feed.addErrback(self.refresh_error)

    def topic(self):
        return "Passerd -- Twitter home timeline channel"

    def get_friend_list(self):
        d = defer.Deferred()
        ids = set()

        def doit():
            self.proto.dbg("requesting list of friends...")
            self.proto.api.friends_ids(got_id, self.proto.the_user.nick).addCallbacks(finished, d.errback)

        def got_id(id):
            ids.add(int(id))

        def finished(*args):
            d.callback(ids)

        doit()
        return d

    def userModeChar(self, u):
        if u == self.proto.the_user:
            return '@'
        return ''

    def list_members(self):
        d = defer.Deferred()
        ids = []

        def doit():
            dbg("requesting friend IDs")
            self.get_friend_list().addCallbacks(got_list, d.errback)

        def got_list(ids):
            dbg("Finished getting friend IDs")
            self.proto.dbg("you are following %d people" % (len(ids)))
            users = [self.proto.get_twitter_user(id) for id in ids]
            self.proto.twitter_users.fetch_friend_info(users)
            d.callback([self.proto.the_user]+users)

        doit()
        return d

    def inviteUser(self, nickname):
        #TODO: send a better error message if user is already being followed

        user_ids = []
        def doit():
            self.proto.api.follow_user(nickname, got_user_info).addCallbacks(done, error)
            self.proto.send_reply(irc.RPL_INVITING, nickname, self.name)

        def got_user_info(u):
            user_ids.append(u.id)
            self.proto.global_twuser_cache.got_api_user_info(u)
            u = self.proto.twitter_users.user_from_id(u.id)
            self.notifyJoin(u)

        def done(*args):
            if not user_ids:
                self.proto.notice("follow: got reply but no user info!?")
                return
            self.proto.dbg("follow request for %s done" % (nickname))

        def error(e):
            self.proto.notice('error when trying to follow user: %s' % (e.value))
            self.proto.send_reply(irc.ERR_UNAVAILRESOURCE, nickname, ':Nick/channel is temporarily unavailable')

        doit()

    def kickUser(self, sender, nickname):
        #TODO: send a better error message if the user is not being followed

        user_ids = []
        def doit():
            self.proto.api.unfollow_user(nickname, got_user_info).addCallbacks(done, error)

        def got_user_info(u):
            user_ids.append(u.id)
            self.proto.global_twuser_cache.got_api_user_info(u)
            u = self.proto.twitter_users.user_from_id(u.id)
            self.notifyKick(sender, u)

        def done(*args):
            if not user_ids:
                self.proto.notice("unfollow: got reply but no user info!?")
                return
            self.proto.dbg("unfollow request for %s done" % (nickname))

        def error(e):
            self.proto.notice('error when trying to unfollow user: %s' % (e.value))
            self.proto.send_reply(irc.ERR_UNAVAILRESOURCE, nickname, ':Nick/channel is temporarily unavailable')

        doit()


    def printEntry(self, entry):
        u = self.proto.get_twitter_user(entry.user.id)
        text = entry.text

        dbg("entry id: %r" % (entry.id))
        # security:

        dbg('entry text: %r' % (text))
        text = full_entity_decode(text)
        # security: remove invalid chars from text:
        text = text.replace('\n', '').replace('\r', '')
        dbg('entities decoded: %r' % (text))
        self.sendMessage(u, text.encode('utf-8'))

    def got_entry(self, e):
        dbg("#twitter got_entry: %r" % (e))
        u = e.user
        self.proto.global_twuser_cache.got_api_user_info(u)
        self.printEntry(e)

    def refresh_error(self, e):
        dbg("#twitter refresh error")
        self.proto.send_notice(self.proto.the_user, self, "error refreshing feed: %s" % (e.value))

    def afterUserJoined(self, user):
        dbg("user %s has joined!" % (user.full_id()))
        self.feed.start_refreshing()

    def stop(self):
        dbg("stopping refresh of %s channel", self.name)
        self.feed.stop_refreshing()

    def beforeUserLeft(self, user, reason):
        self.stop()

    def beforeUserQuit(self, user, reason):
        self.stop()

    def forceRefresh(self, last):
        def doit():
            self.feed._refresh(last_status=last).addCallback(done)

        def done(num_args):
            if num_args == 0:
                #FIXME: we are sending notice as if it was from the user, here
                self.proto.send_notice(self.proto.the_user, self, 'people are quiet...')

        doit()

    def commandReceived(self, cmd):
        """Handle lines starting with '!'
        """
        if cmd == '' or cmd == '!':
            last = None
            if cmd == '!':
                last = 0
            return self.forceRefresh(last)

        if cmd == 'rate':
            api = self.proto.api
            self.proto.notice('Rate limit: %d. remaining: %d. reset: %s' % (api.rate_limit_limit, api.rate_limit_remaining, time.ctime(api.rate_limit_reset)))
            return

    def messageReceived(self, sender, msg):
        if msg.startswith('!'):
            return self.commandReceived(msg[1:])

        self.sendTwitterUpdate(msg)

    def sendTwitterUpdate(self, msg):
        msg = try_unicode(msg)
        if len(msg) > LENGTH_LIMIT:
            self.proto.send_reply(irc.ERR_CANNOTSENDTOCHAN, self.name, ':message too long (%d characters)' % (len(msg)))
            return

        def doit():
            self.proto.api.update(msg).addCallbacks(done, error)

        def done(*args):
            self.proto.dbg("Success!!")

        def error(e):
            self.proto.dbg("error while posting: %s" % (e))

        doit()

    def ctcp_ACTION(self, arg):
        dbg("ACTION: %r" % (arg))
        #TODO: make the behavior of "/me" messages configurable
        self.sendTwitterUpdate('/me %s' % (arg))

class PasserdProtocol(IRC):
    def connectionMade(self):
        self.quit_sent = False

        IRC.connectionMade(self)
        pinfo("Got connection from %s", self.hostname)

        self.data = self.factory.data

        #FIXME: use real names
        self.myhost = MYHOST
        self.password = None
        self.api = None

        self.global_twuser_cache = self.factory.global_twuser_cache
        self.twitter_users = TwitterIrcUserCache(self, self.global_twuser_cache)

        self.my_irc_server = IrcServer(self.myhost)

        u = self.the_user = IrcUser(self)
        u.nick = 'guest'
        u.username = 'guest'
        u.hostname = self.hostname
        u.real_name = 'Unidentified User'

        self.users = [self.the_user]

        dbg("Got new client")

    def welcomeUser(self):
        self.twitter_chan.userJoined(self.the_user)

    def _userQuit(self, reason):
        #FIXME: keep track of the channels where the user is on
        self.twitter_chan.userQuit(self.the_user, reason)
        self.quit_sent = True

    def userQuit(self, reason):
        if not self.quit_sent:
            self._userQuit(reason)
            self.quit_sent = True


    def connectionLost(self, reason):
        pinfo("connection to %s lost: %s", self.hostname, reason)
        self.userQuit(reason)
        IRC.connectionLost(self, reason)

    def user_var(self, var):
        return self.data.get_var(self.user_data, var)

    def set_user_var(self, var, value):
        return self.data.set_var(self.user_data, var, value)

    def get_twitter_user(self, id):
        return self.twitter_users.user_from_id(id)

    def dbg(self, msg):
        self.notice(msg)

    def sendMessage(self, *args, **kwargs):
        dbg("sending message: %r %r" % (args, kwargs))
        return IRC.sendMessage(self, *args, **kwargs)

    def sendLine(self, *args, **kwargs):
        dbg("sending line: %r %r" % (args, kwargs))
        return IRC.sendLine(self, *args, **kwargs)

    def handleCommand(self, *args, **kwargs):
        dbg("got command: %r %r" % (args, kwargs))
        return IRC.handleCommand(self, *args, **kwargs)

    def send_reply(self, cmd, *params, **kwargs):
        return self.server_message(cmd, self.the_user.nick, *params, **kwargs)

    def send_message(self, sender, *params):
        return self.sendMessage(*params, prefix=sender.full_id())

    def server_message(self, cmd, *params):
        return self.send_message(self.my_irc_server, cmd, *params)

    def server_notice(self, target, msg):
        self.send_notice(self.my_irc_server, target, msg)

    def send_notice(self, sender, target, msg):
        self.send_message(sender, 'NOTICE', target.target_name(), ':%s' % (msg))

    def send_privmsg(self, sender, target, msg):
        self.send_message(sender, 'PRIVMSG', target.target_name(), ':%s' % (msg))

    def notice(self, msg):
        self.server_notice(self.the_user, msg)

    def irc_PING(self, prefix, args):
        self.server_message('PONG', args[0])

    def irc_JOIN(self, prefix, params):
        dbg("JOIN! %r %r" % (prefix, params))
        cname = params[0]
        channel = self.get_channel(cname)
        if channel is not None:
            channel.userJoined(self.the_user)

    def irc_INVITE(self, prefix, params):
        nick = params[0]
        cname = params[1]
        chan = self.get_channel(cname)
        if chan is not None:
            chan.inviteUser(nick)

    def irc_KICK(self, prefix, params):
        chans = params[0].split(',')
        users = params[1].split(',')
        for cname in chans:
            chan = self.get_channel(cname)
            if chan is not None:
                chan.kickUsers(self.the_user, users)

    def leave_channel(self, cname, reason):
        channel = self.get_channel(cname)
        if channel is not None:
            channel.userLeft(self.the_user, reason)

    def irc_PART(self, prefix, params):
        chans = params[0]
        reason = None
        if len(params) > 1:
            reason = params[1]
        for c in chans.split(','):
            self.leave_channel(c, reason)

    def irc_QUIT(self, pref, params):
        reason = None
        if len(params) > 0:
            reason = params[0]

        self.userQuit(reason)
        self.sendMessage('ERROR', ':Quit command received')
        self.transport.loseConnection()

    def irc_NICK(self, prefix, params):
        dbg("NICK %r" % (params))
        self.the_user.nick = params[0]

    def check_credentials(self):
        ok = []
        def doit():
            self.notice("Checking Twitter credentials...")
            self.api.verify_credentials(got_user).addCallbacks(done, error).addErrback(error)

        def got_user(u):
            self.notice("Credentials OK!")
            self.credentials_ok()
            ok.append(1)

        def done(*args):
            if not ok:
                self.notice("I got a reply from the Twitter server but no user info. This shouldn't have happened.  :(")
                self.send_reply(irc.ERR_FILEERROR, ":Error doing authentication on Twitter")
                self.transport.loseConnection()

        def error(e):
            #FIXME: send proper numeric error reply
            self.send_reply(irc.ERR_PASSWDMISMATCH, ":error validating Twitter credentials - %s" % (e.value))
            self.transport.loseConnection()

        doit()


    def credentials_ok(self):
        self.user_data = self.data.get_user(self.the_user.nick, create=True)

        self.twitter_chan = TwitterChannel(self, '#twitter')
        self.channels = {'#twitter':self.twitter_chan}

        self.send_reply(irc.RPL_WELCOME, ":Welcome to the Internet Relay Network %s!%s@%s" % (self.the_user.nick, self.the_user.username, self.the_user.hostname))
        self.send_reply(irc.RPL_YOURHOST, ":Your host is %s, running version %s" % (self.myhost, VERSION))
        self.send_reply(irc.RPL_CREATED, ":This server was created by the Flying Spaghetti Monster")
        self.send_reply(irc.RPL_MYINFO, self.myhost, VERSION, SUPPORTED_USER_MODES, SUPPORTED_CHAN_MODES)

        self.welcomeUser()

    def irc_USER(self, prefix, params):
        dbg("USER %r" % (params))
        username,_,_,real_name = params[0:4]
        self.the_user.username = username
        self.the_user.real_name = real_name

        #FIXME: refuse any other command before _USER, to avoid references to
        # undefined attributes
        self.api = Twitter(self.the_user.nick, self.password)
        #FIXME; patch twitty-twister to accept agent=foobar
        self.api.agent = MYAGENT

        self.check_credentials()


    def irc_PASS(self, p, args):
        self.password = args[0]

    def get_user(self, nick):
        #FIXME; index by nickname
        for u in self.users:
            if nick == u.nick:
                return u

    def get_channel(self, name):
        return self.channels.get(name)

    def get_target(self, name):
        if name.startswith('#'):
            return self.get_channel(name)
        else:
            return self.get_user(name)

    def mask_matches(self, mask):
        #FIXME: match wildcards?
        u = self.get_user(mask)
        if u:
            yield u

    def who_matches(self, mask):
        #TODO: make WHO list channel users
        for u in self.mask_matches(mask):
            #XXX: WTF do "H" and "G" mean?
            yield ('*', u.username, u.hostname, self.myhost, u.nick, 'H', ':0', u.real_name)

    def irc_WHO(self, p, args):
        for m in self.who_matches(args[0]):
            self.send_reply(irc.RPL_WHOREPLY, *m)
        self.send_reply(irc.RPL_ENDOFWHO, ':End of WHO list')

    def whois_twitter_user(self, u, tu):
        self.send_reply(irc.RPL_WHOISUSER, u.nick, u.username, u.hostname, '*', ':%s' % (u.real_name))
        #FIXME: find a better way to send user information, instead of RPL_AWAY
        #      - maybe just a pointer to a #!userinfo-nickname channel, where this info is available
        self.send_reply(irc.RPL_AWAY, u.nick, ':Location: %s' % (tu.location).encode(ENCODING))
        self.send_reply(irc.RPL_AWAY, u.nick, ':URL: %s' % (tu.url).encode(ENCODING))
        self.send_reply(irc.RPL_AWAY, u.nick, ':Bio: %s' % (tu.description).encode(ENCODING))
        self.send_reply(irc.RPL_AWAY, u.nick, ':Last update: %s' % (tu.status.text).encode(ENCODING))
        self.send_reply(irc.RPL_AWAY, u.nick, ':Twitter URL: http://twitter.com/%s' % (tu.screen_name).encode(ENCODING))
        self.send_reply(irc.RPL_ENDOFWHOIS, u.nick, ':End of WHOIS')

    def whois_mask(self, mask):
        def doit():
            self.dbg("fetching user info for %s" % (mask))
            self.api.show_user(mask).addCallback(got_user).addErrback(error)

        def got_user(tu):
            self.dbg("got user info!")
            self.global_twuser_cache.got_api_user_info(tu)
            u = self.twitter_users.user_from_id(tu.id)
            self.whois_twitter_user(u, tu)

        def error(e):
            self.send_reply(irc.ERR_NOSUCHNICK, mask, ':Error fetching user info - %s' % (e.value))

        doit()

    def irc_WHOIS(self, p, args):
        if len(args) > 2:
            # invalid command
            return
        elif len(args) == 2:
            # ignore server part
            masks = args[1]
        else:
            masks = args[0]

        masks = masks.split(',')
        for m in masks:
            self.whois_mask(m)

    def irc_MODE(self, p, args):
        tname = args[0]
        target = self.get_target(tname)
        if target is None:
            self.send_reply(irc.ERR_NOSUCHNICK, tname, ':No such nick/channel')
            return
        target.modeRequest(self.the_user, args)

    def irc_USERHOST(self, p, args):
        if len(args) > 5:
            args = args[:5]
        r = []
        for a in args:
            u = self.get_user(a)
            if u:
                r.append('%s=%s%s' % (u.nick, u.away_char(), u.userhost()))
        if r:
            self.send_reply(irc.RPL_USERHOST, ':%s' % (' '.join(r)))

    def irc_PRIVMSG(self, prefix, args):
        tname = args[0]
        msg = args[1]

        sender = self.the_user
        target = self.get_target(tname)
        if target is None:
            self.send_reply(irc.ERR_NOSUCHNICK, tname, ':No such nick/channel')
            return

        # CTCP data:
        if msg[0]==irc.X_DELIM:
            m = irc.ctcpExtract(msg)
            if m['extended']:
                target.ctcpQueryReceived(sender, m['extended'])
            # I won't handle the m['normal'] part. I don't trust this level of
            # crazyness on the protocol
        else:
            target.messageReceived(sender, msg)

    def irc_unknown(self, prefix, cmd, params):
        dbg("CMD! %r %r %r" % (prefix, cmd, params))
        self.dbg("Got unknown command: %r %r %r" % (prefix, cmd, params))
        self.send_reply(irc.ERR_UNKNOWNCOMMAND, cmd, ':Unknown command')


class PasserdFactory(Factory):
    protocol = PasserdProtocol

    def __init__(self, dbpath):
        url = 'sqlite:///%s' % (dbpath)
        self.data = DataStore(url)
        self.data.create_tables()
        self.global_twuser_cache = TwitterUserCache(self)

class PasserdGlobalOptions:
    def __init__(self):
        # set the defaults:

        self.listen = ('0.0.0.0', 6667)

        #logging:
        self.logstream = sys.stderr
        self.loglevel = logging.INFO
        # sqlalchemy is too verbose on the INFO loglevel
        self.dbloglevel = logging.ERROR

def parse_cmdline(args, opts):
    def parse_hostport(option, optstr, value, parser):
        try:
            host, rawport = value.rsplit(":", 1)
            port = int(rawport)
        except ValueError:
            parser.error("invalid listen address, expected HOST:PORT")
        opts.listen = (host, port)

    def set_loglevels(level, dblevel):
        opts.loglevel = level
        opts.dbloglevel = dblevel

    parser = optparse.OptionParser("%prog [options] <database path>")
    parser.add_option("-l", "--listen", type="string",
            action="callback", callback=parse_hostport,
            metavar="HOST:PORT", help="listen address")
    parser.add_option("-D", "--debug",
            action="callback", callback=lambda *args: set_loglevels(logging.DEBUG, logging.DEBUG),
            help="Enable debug logging")
    _, args = parser.parse_args(args)
    if not args:
        parser.error("the database path is needed!")
    opts.database = args[0]
    return opts

def setup_logging(opts):

    ch = logging.StreamHandler(opts.logstream)
    f = logging.Formatter(logging.BASIC_FORMAT)
    ch.setFormatter(f)

    # root logger:
    r = logging.getLogger()
    r.addHandler(ch)
    r.setLevel(opts.loglevel)

    #sqlalchemy logging:
    l = logging.getLogger('sqlalchemy').setLevel(opts.dbloglevel)


def run():
    opts = PasserdGlobalOptions()
    parse_cmdline(sys.argv[1:], opts)
    setup_logging(opts)

    pinfo("Starting Passerd. Will listen on address %s:%d" % opts.listen)
    reactor.listenTCP(interface=opts.listen[0], port=opts.listen[1],
             factory=PasserdFactory(opts.database))
    pinfo("Starting Twisted reactor loop.")
    reactor.run()

__all__ = ['run']
