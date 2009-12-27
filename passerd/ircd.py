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
from twisted.web import client as twclient


from twittytwister.twitter import Twitter, TwitterClientInfo

from passerd.data import DataStore, TwitterUserData
from passerd.callbacks import CallbackList
from passerd.utils import full_entity_decode
from passerd.feeds import HomeTimelineFeed, ListTimelineFeed, UserTimelineFeed, MentionsFeed, DirectMessagesFeed

from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound

import oauth.oauth as oauth


MYAGENT = 'Passerd'
#FIXME: use a real hostname?
MYHOST = 'passerd.server'
MYURL = 'http://passerd.raisama.net/'
VERSION = '0.0.3'
CLIENT_INFO = TwitterClientInfo(MYAGENT, VERSION, MYURL)

SUPPORTED_USER_MODES = '0'
SUPPORTED_CHAN_MODES = 'b'

BASE_URL = 'https://twitter.com'



ENCODING = 'utf-8'
FALLBACK_ENCODING = 'iso-8859-1'

TWITTER_ENCODING = 'utf-8'


# if more than MAX_USER_INFO_FETCH users are unknown, use /statuses/friends to fetch user info.
# otherwise, just fetch individual user info

MAX_USER_INFO_FETCH = 0  # individual fetch is not implemented yet...

# the maximum number of sequential friend list page requests:
MAX_FRIEND_PAGE_REQS = 10


LENGTH_LIMIT = 140



# IRC protocol constants:

# Other error codes we may use:
ERR_NEEDREGGEDNICK = 477



# logging helpers:
logger = logging.getLogger("passerd")

dbg = logger.debug
pinfo = logger.info
perror = logger.error



# OAuth stuff:
OAUTH_CONSUMER_KEY='1K2bNGyqs7dtDKTaTlfnQ'
OAUTH_CONSUMER_SECRET='frpQHgjN21ajybwA0ZQ2utwlu9O6A36r8YLy6PxY5c'

OAUTH_REQUEST_TOKEN_URL='http://twitter.com/oauth/request_token'
OAUTH_ACCESS_TOKEN_URL='http://twitter.com/oauth/access_token'
OAUTH_AUTHORIZE_URL='http://twitter.com/oauth/authorize'

OAUTH_SIGN_METHOD=oauth.OAuthSignatureMethod_HMAC_SHA1()

oauth_consumer = oauth.OAuthConsumer(OAUTH_CONSUMER_KEY, OAUTH_CONSUMER_SECRET)


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

def to_str(s):
    if isinstance(s, unicode):
        return s.encode(ENCODING)
    elif isinstance(s, str):
        return s
    else:
        raise Exception("%r is not str (type: %r)" % (s, type(s)))


class ErrorReply(Exception):
    """Special exception class used to generate IRC numeric replies"""
    def __init__(self, command, *args):
        self.command = command
        self.args = args


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

    def force_nick(self, new_nick):
        if self.nick != new_nick:
            self.notifyNickChange(new_nick)
            self.nick = new_nick

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
            d.addCallbacks(send_names, error)
            d.addErrback(log.err)

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
        self.notifyPart(user, reason)

    @hooks
    def userQuit(self, user, reason):
        pass

    def messageReceived(self, sender, msg):
        raise NotImplementedError("Channel %s doesn't handle incoming messages" % (self.name))

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

    def lookup_screen_name(self, name):
        # not 
        try:
            u = self.proto.data.query(TwitterUserData).filter_by(screen_name=name).one()
        except MultipleResultsFound:
            # multiple matches are possible if screen_names are reused.
            # if that happens, be on the safe side: don't return anything
            u = None
        except NoResultFound:
            u = None
        return u



class TwitterIrcUser(IrcUser):
    """Common class for multiple methods of contacting IRC users"""
    def _target_params(self, params):
        """Must return a dictionary containing user_id or screen_name, depending on
        how much information we already have about the user.
        """
        raise NotImplementedError("_target_params not implemented")

    def send_direct_message(self, text):
        d = defer.Deferred()
        ok = []

        def got_msg(msg):
            ok.append(msg)
            d.callback(msg)

        def done(*args):
            if not ok:
                self.proto.notice("ERROR: Didn't get direct message info back when sending DM")

        self.proto.api.send_direct_message(text, delegate=got_msg, params=self._target_params()).addCallbacks(done, d.errback)
        return d

    def messageReceived(self, sender, msg):
        assert (sender is self.proto.the_user)

        msg = try_unicode(msg)
        if len(msg) > LENGTH_LIMIT:
            #TODO: maybe there's a better error code for this?
            self.proto.send_reply(irc.RPL_AWAY, self.nick, ':message too long (%d characters), not sent.' % (len(msg)))
            return

        def doit():
            self.send_direct_message(msg).addCallbacks(done, error)

        def done(msg):
            self.proto.notice("Direct Message to %s sent. ID: %s" % (self.nick, msg.id))

        def error(e):
            self.proto.send_reply(irc.RPL_AWAY, self.nick, ":Error sending Direct Message: %s" % (e.value))

        doit()


class UnavailableTwitterData:
    """Fake TwitterUserData object for unavailable info"""
    def __init__(self, id):
        self.twitter_id = id

    twitter_screen_name = property(lambda self: 'user-id-%s' % (self.twitter_id))
    twitter_name = property(lambda self: 'Twitter User (info not fetched yet)')


class CachedTwitterIrcUser(TwitterIrcUser):
    """An IrcUser object for cached Twitter user info

    Objects of this class may be short-lived, just to return info of a random
    Twitter user for which we don't have much data.
    """
    def __init__(self, proto, cache, id):
        IrcUser.__init__(self, proto)
        self._twitter_id = id
        self.cache = cache
        self._data = None

    def _target_params(self):
        return {'user_id':self._twitter_id}

    def data_changed(self, old_info, new_info):
        dbg("CachedTwitterIrcUser.data_changed! %r %r" % (old_info, new_info))
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


class UnknownTwitterUser(TwitterIrcUser):
    """An IrcUser object for an user we don't know anything about, but may be a valid Twitter user"""
    def __init__(self, proto, nickname):
        self.proto = proto
        self.nick = nickname
        self.username = nickname

    real_name = 'Unknown User'
    hostname = 'twitter.com'

    def _target_params(self):
        return {'screen_name':self.nick}



class TwitterIrcUserCache:
    """Cache of CachedTwitterIrcUser objects

    A TwitterIrcUserCache is client-specific (not server-global), and takes
    care of creating CachedTwitterIrcUser objects for some queries.
    """
    def __init__(self, proto, cache):
        self.proto = proto
        self.cache = cache
        self.cache.addCallback(self._user_changed)
        self._watched_ids = {}

    def _user_changed(self, id, old_info, new_info):
        dbg("user_changed: %r, %r, %r" % (id, old_info, new_info))
        if id in self._watched_ids:
            dbg("user_changed (%s): is being watched." % (id))
            u = self._get_user(id).data_changed(old_info, new_info)

    def _get_user(self, id):
        u = CachedTwitterIrcUser(self.proto, self.cache, id)
        return u

    def watch_user_id(self, id):
        """Start watching user ID for changes"""
        self._watched_ids[id] = True

    def watch_user_ids(self, ids):
        for id in ids:
            self.watch_user_id(id)

    def watch_user(self, u):
        self.watch_user_id(u._twitter_id)

    def get_user(self, id):
        return self._get_user(id)


    def fetch_individual_user_info(self, unknown_users):
        #TODO: implement me
        pass

    def fetch_all_friend_info(self, user, unknown_users):
        #TODO: unify this paging code with the one on FriendlistMixIn
        reqs = []
        def request_cursor(cursor):
            self.proto.dbg("requesting a page from the friend list: %s" % (str(cursor)))
            reqs.append(cursor)
            self.proto.api.list_friends(got_user, user=user, params={'cursor':cursor},
                                        page_delegate=end_page).addCallbacks(done, error)

        def got_user(u):
            self.proto.global_twuser_cache.got_api_user_info(u)

        def end_page(next, prev):
            unk = [u for u in unknown_users if not u.has_data()]
            num = len(unk)

            if num == 0:
                self.proto.notice("I know all friends of %s, now!" % (user))
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

    def fetch_friend_info(self, user, friends):
        dbg("fetch_friend_info: begin:")
        unknown_users = [u for u in friends if not u.has_data()]
        dbg("fetch_friend_info: got unknown friends...")
        if len(unknown_users) == 0:
            self.proto.notice("I already know all friends of %s. cool!" % (user))
            return

        dbg("%d unknown users..." % (len(unknown_users)))
        self.proto.notice("There are %d users I don't know about. I will fetch the detailed friend list" % (len(unknown_users)))

        if len(unknown_users) < MAX_USER_INFO_FETCH:
            self.fetch_individual_user_info(unknown_users)
        else:
            self.fetch_all_friend_info(user, unknown_users)


class TwitterChannel(IrcChannel):
    def __init__(self, proto, name):
        IrcChannel.__init__(self, proto, name)
        self.feeds = self._createFeeds()
        for f in self.feeds:
            f.addCallback(self.got_entry)
            f.addErrback(self.refresh_error)

    def _createFeeds(self):
        raise NotImplementedError("_createFeeds not implemented on %s" % (self.name))

    def userModeChar(self, u):
        if u == self.proto.the_user:
            return '@'
        return ''

    def printEntry(self, entry):
        u = self.proto.get_twitter_user(entry.user.id)
        dbg("entry id: %r" % (entry.id))
        text = entry.text
        dbg('entry text: %r' % (text))
        self.proto.send_text(u, self, text)

    def got_entry(self, e):
        dbg("#twitter got_entry: %r" % (e))
        u = e.user
        self.proto.global_twuser_cache.got_api_user_info(u)
        self.printEntry(e)

    def refresh_error(self, e):
        dbg("#twitter refresh error")
        self.proto.chan_notice(self, "error refreshing feed: %s" % (e.value))

    def start(self):
        for f in self.feeds:
            f.start_refreshing()

    def stop(self):
        dbg("stopping refresh of %s channel", self.name)
        for f in self.feeds:
            f.stop_refreshing()

    def beforeUserJoined(self, user):
        if not self.proto.is_authenticated():
            # use the same numeric that Freenode uses
            raise ErrorReply(ERR_NEEDREGGEDNICK, self.name, ':You need to be identified to join that channel')

    def afterUserJoined(self, user):
        dbg("user %s has joined!" % (user.full_id()))
        self.start()

    def beforeUserLeft(self, user, reason):
        self.stop()

    def beforeUserQuit(self, user, reason):
        self.stop()

    def forceRefresh(self, last):
        def doit(f):
            f._refresh(last_id=last).addCallback(done)

        def done(num_args):
            if num_args == 0:
                #FIXME: we are sending notice as if it was from the user, here
                self.proto.chan_notice(self, 'people are quiet...')

        for f in self.feeds:
            doit(f)

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
            self.proto.notice('Rate limit: %s. remaining: %s. reset: %s' % (api.rate_limit_limit, api.rate_limit_remaining, time.ctime(api.rate_limit_reset)))
            return

    def messageReceived(self, sender, msg):
        if msg.startswith('!'):
            return self.commandReceived(msg[1:])

        self.sendTwitterUpdate(msg)

    def ctcp_ACTION(self, arg):
        dbg("ACTION: %r" % (arg))
        #TODO: make the behavior of "/me" messages configurable
        self.sendTwitterUpdate('/me %s' % (arg))

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


class FriendlistMixIn:
    """An extension to TwitterChannel to handle list of friends/members"""

    def _friendList(self, delegate, params={}, page_delegate=None):
        raise NotImplementedError("_friendList not implemented")

    def _handleUserRefs(self, userrefs):
        users = []
        for u in userrefs:
            users.append(self._user_object(u))
        return users

    def _fetch_user_info(self, users):
        """Can be used to trigger fetching of complete user info, if needed"""
        pass

    def _user_object(self, tu):
        """Can be overriden when get_friend_list() contains only user IDs"""
        self.proto.global_twuser_cache.got_api_user_info(tu)
        return self.proto.get_twitter_user(tu.id, watch=True)

    def _get_friend_list(self):
        d = defer.Deferred()
        friends = set()

        def got_page(next, prev):
            self.proto.dbg("%s user list: got page: %s<-%s" % (self.name, next, prev))
            self.proto.dbg("%s friends so far: %d" % (self.name, len(friends)))
            if not next or next == "0":
                self.proto.dbg("%s user list: this was the last page" % (self.name))
                d.callback(friends)
                return
            doit(next)

        def doit(cursor):
            params = {"cursor": cursor}
            self._friendList(got_friend, params, page_delegate=got_page).addErrback(d.errback)

        def got_friend(ref):
            friends.add(ref)

        self.proto.dbg("I will fetch the list of users for %s" % (self.name))
        doit("-1")
        return d

    def list_members(self):
        #FIXME: return a empty (or almost-empty) list, if the user is not authenticated yet
        d = defer.Deferred()
        ids = []

        def doit():
            self._get_friend_list().addCallbacks(got_list, d.errback)

        def got_list(userrefs):
            dbg("Finished getting friend IDs for %s", self.name)
            users = self._handleUserRefs(userrefs)

            #FIXME: call _fetch_user_info() on JOIN time, not on list_members() time
            self._fetch_user_info(users)

            #FIXME: 1) show the_user only if it really has joined the channel
            #FIXME: 2) check if the_user is on the list used as input, and don't include it,
            #          to avoid duplicate entries on the list
            users = [self.proto.the_user]+users
            d.callback(users)

        doit()
        return d

class FriendIDsMixIn:
    """MixIn that can be used when the friend list is just a list of IDs"""
    def _user_object(self, id):
        return self.proto.get_twitter_user(int(id), watch=True)


#TODO: make mentions appear on #twitter, if configured to do so

class MainChannel(FriendIDsMixIn, FriendlistMixIn, TwitterChannel):
    """The #twitter channel"""

    def topic(self):
        return "Passerd -- Twitter home timeline channel"

    def _createFeeds(self):
        return [HomeTimelineFeed(self.proto)]

    def _friendList(self, delegate, params={}, page_delegate=None):
        return self.proto.api.friends_ids(delegate, str(self.proto.authenticated_user.screen_name),
                params=params, page_delegate=page_delegate)

    def inviteUser(self, nickname):
        #TODO: send a better error message if user is already being followed

        user_ids = []
        def doit():
            self.proto.api.follow_user(nickname, got_user_info).addCallbacks(done, error)
            self.proto.send_reply(irc.RPL_INVITING, nickname, self.name)

        def got_user_info(u):
            user_ids.append(u.id)
            self.proto.global_twuser_cache.got_api_user_info(u)
            u = self.proto.get_twitter_user(u.id, watch=True)
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
            u = self.proto.get_twitter_user(u.id)
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

    def _fetch_user_info(self, users):
        self.proto.dbg("Now will fetch user info")
        return self.proto.twitter_users.fetch_friend_info(str(self.proto.authenticated_user.screen_name), users)


class MentionsChannel(TwitterChannel):
    """The #mentions channel"""
    def topic(self):
        return "Passerd -- @mentions"

    def _createFeeds(self):
        return [MentionsFeed(self.proto)]


class ListChannel(FriendlistMixIn, TwitterChannel):

    def __init__(self, proto, list_user, list_name):
        self.list_user = list_user
        self.list_name = list_name
        TwitterChannel.__init__(self, proto, self._channelName())

    def _createFeeds(self):
        return [ListTimelineFeed(self.proto, self.list_user, self.list_name)]

    def _channelName(self):
        return "#@%s/%s" % (self.list_user, self.list_name)

    def topic(self):
        return "Passerd -- @%s/%s" % (self.list_user, self.list_name)

    def _friendList(self, delegate, params={}, page_delegate=None):
        return self.proto.api.list_members(delegate, self.list_user,
                self.list_name, params=params, page_delegate=page_delegate)


class UserChannel(FriendIDsMixIn, FriendlistMixIn, TwitterChannel):

    def __init__(self, proto, user):
        self.user = user
        TwitterChannel.__init__(self, proto, self._channelName())

    def _channelName(self):
        return "#@%s" % (self.user)

    def _createFeeds(self):
        return [UserTimelineFeed(self.proto, self.user)]

    def topic(self):
        return "User timeline -- %s" % (self.user)

    def _friendList(self, delegate, params={}, page_delegate=None):
        #TODO: include the user on the list of channel members, too
        #      (the user whose timeline is being followed, not the Passerd
        #      authenticated user)
        return self.proto.api.friends_ids(delegate, self.user, params=params,
                page_delegate=page_delegate)

    def _fetch_user_info(self, users):
        return self.proto.twitter_users.fetch_friend_info(self.user, users)


def requires_auth(fn):
    """A decorator for a generic authentication check before handling certain commands

    This function should die soon. Having command-specific handling of
    non-authenticated users would be better. Then we could make the commands
    work as if everything is OK on the server, but without any real Twitter
    user or channels available.
    """
    def wrapper(self, *args, **kwargs):
        if not self.is_authenticated():
            raise ErrorReply(irc.ERR_NOPRIVILEGES, ':Sorry, you must authenticate first (%s command)' % (fn.__name__))
        return fn(self, *args, **kwargs)
    return wrapper


class PasserdProtocol(IRC):
    def connectionMade(self):
        self.quit_sent = False

        IRC.connectionMade(self)
        pinfo("Got connection from %s", self.hostname)

        self.data = self.factory.data

        #FIXME: use real names
        self.myhost = MYHOST
        self.password = None


        # fields that will be available only after authentication:
        self.api = None
        self.authenticated_user = None
        self.user_data = None
        self.got_user = False
        self.got_nick = False
        self.oauth_pin_callback = None

        self.global_twuser_cache = self.factory.global_twuser_cache
        self.twitter_users = TwitterIrcUserCache(self, self.global_twuser_cache)


        self.my_irc_server = IrcServer(self.myhost)

        u = self.the_user = IrcUser(self)
        u.nick = 'guest'
        u.username = 'guest'
        u.hostname = self.hostname
        u.real_name = 'Unidentified User'

        self.users = [self.the_user]

        tc = MainChannel(self, '#twitter')
        mc = MentionsChannel(self, '#mentions')

        #TODO: keep a list of the fixed and joined channels,
        #      but use short-lived channel objects for other channel-query
        #      commands
        self.channels = {'#twitter':tc, '#mentions':mc}

        #FIXME: make the auto-join optional:
        self.autojoin_channels = [tc, mc]
        #FIXME: make joined_channels a more efficient list of channels
        self.joined_channels = []

        self.dm_feed = DirectMessagesFeed(self)
        self.dm_feed.addCallback(self.gotDirectMessage)
        self.dm_feed.addErrback(self.dmError)

        dbg("Got new client")

    def welcomeUser(self):
        for ch in self.autojoin_channels:
            self.join_channel(ch)
        self.dm_feed.start_refreshing()

    def _userQuit(self, reason):
        self.dm_feed.stop_refreshing()
        for ch in self.joined_channels:
            self.leave_channel(ch, reason)
        self.quit_sent = True

    def userQuit(self, reason):
        if not self.quit_sent:
            self._userQuit(reason)
            self.quit_sent = True

    def gotDirectMessage(self, msg):
        self.global_twuser_cache.got_api_user_info(msg.sender)
        sender = self.get_twitter_user(msg.sender.id, watch=True)
        self.send_text(sender, self.the_user, msg.text)

    def dmError(self, e):
        self.notice("Error pulling Direct Messages: %s" % (e.value))

    def send_text(self, sender, target, text):
        # security:
        text = full_entity_decode(text)
        # security: remove invalid chars from text:
        text = text.replace('\n', '').replace('\r', '')
        dbg('entities decoded: %r' % (text))
        self.send_privmsg(sender, target, text.encode(ENCODING))

    def connectionLost(self, reason):
        pinfo("connection to %s lost: %s", self.hostname, reason)
        self.userQuit(str(reason))
        IRC.connectionLost(self, reason)

    def user_var(self, var):
        return self.data.get_var(self.user_data, var)

    def set_user_var(self, var, value):
        return self.data.set_var(self.user_data, var, value)


    def get_twitter_user(self, id, watch=False):
        u = self.twitter_users.get_user(id)
        if watch:
            self.twitter_users.watch_user_id(id)
        return u


    def dbg(self, msg):
        self.notice(msg)


    ## overwrite some methods of the twisted.words IRC class:

    def sendMessage(self, *args, **kwargs):
        dbg("sending message: %r %r" % (args, kwargs))
        return IRC.sendMessage(self, *args, **kwargs)

    def sendLine(self, *args, **kwargs):
        dbg("sending line: %r %r" % (args, kwargs))
        return IRC.sendLine(self, *args, **kwargs)

    def _handleCommand(self, command, prefix, params):
        """Like IRC.handleCommand, but with no exception handling"""
        method = getattr(self, "irc_%s" % command, None)
        if method is not None:
            return method(prefix, params)
        else:
            return self.irc_unknown(prefix, command, params)

    def handleCommand(self, *args, **kwargs):
        def doit():
            dbg("got command: %r %r" % (args, kwargs))
            d = defer.maybeDeferred(self._handleCommand, *args, **kwargs)
            d.addErrback(error)

        def error(e):
            ex = e.value

            # ErrorReply exceptions are special: they generate a IRC error reply
            if e.check(ErrorReply):
                self.send_reply(ex.command, *ex.args)
                return

            perror("Got an exception: %s", e.getErrorMessage())
            logger.exception(ex)
            self.notice("*** An internal error has occurred. Sorry. -- %s: %s" % (e.type, e.getErrorMessage()))

        doit()

    def send_reply(self, cmd, *params, **kwargs):
        return self.server_message(cmd, self.the_user.nick, *params, **kwargs)

    def send_message(self, sender, *params):
        return self.sendMessage(*params, prefix=sender.full_id())

    def server_message(self, cmd, *params):
        return self.send_message(self.my_irc_server, cmd, *params)

    def server_notice(self, target, msg):
        self.send_notice(self.my_irc_server, target, msg)

    def send_notice(self, sender, target, msg):
        self.send_message(sender, 'NOTICE', target.target_name(), ':%s' % (to_str(msg)))

    def chan_notice(self, target, msg):
        #FIXME: use a 'bot' user for those kinds of notices
        self.send_notice(self.the_user, target, msg)

    def send_privmsg(self, sender, target, msg):
        self.send_message(sender, 'PRIVMSG', target.target_name(), ':%s' % (to_str(msg)))

    def notice(self, msg):
        self.server_notice(self.the_user, msg)

    def join_channel(self, chan):
        if not (chan in self.joined_channels):
            chan.userJoined(self.the_user)
            self.joined_channels.append(chan)

    def leave_channel(self, chan, reason):
        if chan in self.joined_channels:
            chan.userLeft(self.the_user, reason)
            self.joined_channels.remove(chan)

    def leave_cname(self, cname, reason):
        channel = self.get_channel(cname)
        if channel is not None:
            self.leave_channel(channel, reason)

    def join_cname(self, cname):
        channel = self.get_channel(cname)
        if channel is None:
            channel = self.create_channel(cname)
        dbg("get_channel %r" % (channel))
        if channel is not None:
            self.join_channel(channel)

    def irc_PING(self, prefix, args):
        self.server_message('PONG', args[0])

    def irc_JOIN(self, prefix, params):
        dbg("JOIN! %r %r" % (prefix, params))
        cnames = params[0]
        for c in cnames.split(','):
            self.join_cname(c)

    def irc_PART(self, prefix, params):
        chans = params[0]
        reason = None
        if len(params) > 1:
            reason = params[1]
        for c in chans.split(','):
            self.leave_cname(c, reason)

    @requires_auth
    def irc_INVITE(self, prefix, params):
        nick = params[0]
        cname = params[1]
        chan = self.get_channel(cname)
        if chan is not None:
            chan.inviteUser(nick)

    @requires_auth
    def irc_KICK(self, prefix, params):
        chans = params[0].split(',')
        users = params[1].split(',')
        for cname in chans:
            chan = self.get_channel(cname)
            if chan is not None:
                chan.kickUsers(self.the_user, users)

    @requires_auth
    def irc_QUIT(self, pref, params):
        reason = None
        if len(params) > 0:
            reason = params[0]

        self.userQuit(reason)
        self.sendMessage('ERROR', ':Quit command received')
        self.transport.loseConnection()

    def irc_WHO(self, p, args):
        for m in self.who_matches(args[0]):
            self.send_reply(irc.RPL_WHOREPLY, *m)
        self.send_reply(irc.RPL_ENDOFWHO, ':End of WHO list')

    @requires_auth
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

    @requires_auth
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

    ### authentication code:

    def check_credentials(self, api):
        d = defer.Deferred()

        ok = []
        def doit():
            self.notice("Checking Twitter credentials...")
            api.verify_credentials(got_user).addCallbacks(done, error).addErrback(error)

        def got_user(u):
            self.notice("Credentials OK! Your Twitter user ID: %s. screen_name: %s" % (u.id, u.screen_name))
            ok.append(1)
            d.callback(u)

        def done(*args):
            if not ok:
                d.errback(Exception("I got a reply from the Twitter server but no user info. This shouldn't have happened.  :("))

        def error(e):
            d.errback(e)

        doit()
        return d

    def is_authenticated(self):
        return (self.authenticated_user is not None)

    def _send_welcome_replies(self):
        """Send standard IRC numeric replies after registration"""
        self.send_reply(irc.RPL_WELCOME, ":Welcome to the Internet Relay Network %s!%s@%s" % (self.the_user.nick, self.the_user.username, self.the_user.hostname))
        self.send_reply(irc.RPL_YOURHOST, ":Your host is %s, running version %s" % (self.myhost, VERSION))
        self.send_reply(irc.RPL_CREATED, ":This server was created by the Flying Spaghetti Monster")
        self.send_reply(irc.RPL_MYINFO, self.myhost, VERSION, SUPPORTED_USER_MODES, SUPPORTED_CHAN_MODES)


    def set_authenticated_user(self, u):
        self.authenticated_user = u
        self.user_data = self.data.get_user(int(u.id), u.screen_name, create=True)


    def _check_basic_auth(self, username, password):
        """Run verify_credentials API call using basic auth

        On success, pass a (api,auth_user) pair to the deferred callback
        """
        api = Twitter(username, password, base_url=BASE_URL) #, client_info=CLIENT_INFO)
        #FIXME; patch twitty-twister to accept agent=foobar
        api.agent = MYAGENT
        def doit():
            return self.check_credentials(api).addCallback(done)

        def done(u):
            return (api, u)

        return doit()


    def _early_auth(self):
        """Run early password-authentication
        
        This should be used only on the early registration stages
        """
        def doit():
            self._check_basic_auth(self.the_user.nick, self.password).addCallbacks(done, error)

        def done(args):
            api,u = args

            self.api = api
            self.set_authenticated_user(u)
            self._send_welcome_replies()
            self.welcomeUser()

        def error(e):
            self.send_reply(irc.ERR_PASSWDMISMATCH, ":error validating Twitter credentials - %s" % (e.value))
            self.transport.loseConnection()

        doit()

    def try_early_auth(self):
        """Try password-authentication on Twitter, if already got enough info"""
        if self.api is not None:
            # already set up authentication
            return

        if self.password is not None and self.got_user and self.got_nick:
            self._early_auth()

    def irc_NICK(self, prefix, params):
        dbg("NICK %r" % (params))
        nick = params[0]
        if self.got_nick:
            self.the_user.force_nick(nick)
        else:
            self.the_user.nick = nick
            self.got_nick = True
            self.try_early_auth()
        self.try_early_auth()

    def irc_USER(self, prefix, params):
        dbg("USER %r" % (params))
        username,_,_,real_name = params[0:4]
        self.the_user.username = username
        self.the_user.real_name = real_name
        self.got_user = True

        #TODO: accept connections without password, and allow a nickserv-style method of authentication

        if self.password is None:
            self.send_reply(irc.ERR_PASSWDMISMATCH, ':You must use your Twitter password as password to connect')
            self.transport.loseConnection()

        self.try_early_auth()


    def irc_PASS(self, p, args):
        self.password = args[0]
        self.try_early_auth()


    ### Oauth authentication code:

    def oauth_request_token(self):
        def doit():
            req = oauth.OAuthRequest.from_consumer_and_token(oauth_consumer, callback='oob', http_url=OAUTH_REQUEST_TOKEN_URL)
            req.sign_request(OAUTH_SIGN_METHOD, oauth_consumer, None)
            return twclient.getPage(req.to_url()).addCallback(done)

        def done(data):
            return oauth.OAuthToken.from_string(data)

        return doit()

    def oauth_authorize_url(self, req_token):
        req = oauth.OAuthRequest.from_token_and_callback(token=req_token, http_url=OAUTH_AUTHORIZE_URL)
        return req.to_url()

    def _oauth_send_verifier(self, req_token, verifier):
        def doit():
            req = oauth.OAuthRequest.from_consumer_and_token(oauth_consumer, token=req_token, verifier=verifier, http_url=OAUTH_ACCESS_TOKEN_URL)
            req.sign_request(OAUTH_SIGN_METHOD, oauth_consumer, req_token)
            return twclient.getPage(OAUTH_ACCESS_TOKEN_URL, method='POST', headers=req.to_header()).addCallback(done)

        def done(data):
            self.notice("access token URL returned!")
            return oauth.OAuthToken.from_string(data)

        return doit()

    def _get_oauth_token(self, udata):
        d = defer.Deferred()
        def doit():
            self.notice("oauth: getting request token...")
            self.oauth_request_token().addCallbacks(got_req_token, d.errback)

        def got_req_token(req_token):
            self.notice("oauth: got request token.")
            self.oauth_pin_callback = lambda pin: got_pin(req_token, pin)
            self.notice("Please go to %s and send the PIN to me" % (self.oauth_authorize_url(req_token)))

        def got_pin(req_token, pin):
            self.notice("Got pin: %s" % (pin))
            self._oauth_send_verifier(req_token, pin).addCallbacks(got_access_token, d.errback)

        def got_access_token(token):
            self.notice("YAY!")
            #TODO: check if token works, with check_credentials()
            udata.oauth_token = token.key
            udata.oauth_token_secret = token.secret
            self.data.commit()
            return token

        doit()
        return d

    def get_oauth_token(self, udata):
        token,secret = udata.oauth_token,udata.oauth_token_secret
        if token is not None and secret is not None:
            return success(oauth.OAuthToken(token, secret))

        return self._get_oauth_token(udata)

    def oauth_auth(self, args):
        #FIXME: use Twitter screen_name, somehow, not user ID
        id = args[0]
        login = args[1]

        udata = self.data.get_user(id, login)
        if udata is None:
            #FIXME: create user, if necessary
            self.notice("No existing user data found")
            return

        self.notice("Setting up oauth authentication...")

        def doit():
            self.get_oauth_token(udata).addCallback(done, error)

        def done(token):
            self.notice('Got token: %s' % (token))

        def error(e):
            self.notice('ERROR getting oauth token')

        doit()

    def oauth_pin(self, args):
        pin = args[0]
        if self.oauth_pin_callback is None:
            self.notice("Please use /OAUTH AUTH <id> <login> first")
            return

        self.notice("Thanks! Let's do it now...")
        self.oauth_pin_callback(pin)

    def irc_OAUTH(self, prefix, params):
        cmd = params[0]
        args = params[1:]
        if cmd == 'auth':
            self.oauth_auth(args)
        elif cmd == 'pin':
            self.oauth_pin(args)


    ### end of oauth code

    def get_user(self, nick):
        #FIXME; index by nickname
        for u in self.users:
            if nick == u.nick:
                return u

        # No Twitter user is available, if not authenticated yet
        if not self.is_authenticated():
            return None

        #TODO: use cache lookup_screen_name() method before returning UnknownTwitterUser()

        # if not found, consider it's a potential Twitter user we don't know yet
        return UnknownTwitterUser(self, nick)

    def create_channel(self, name):
        #TODO make it generic to allow more types of channels
        #TODO: make it possible to create a short-lived channel object for
        #      channel-query commands
        dbg("about to join channel: %s" % (name))
        channel = None
        if name.startswith("#@"):
            rawname = name[2:]
            try:
                user, list_name = rawname.split('/', 1)
            except ValueError:
                # user channel:
                user = rawname
                if user:
                    channel = UserChannel(self, user)
                else:
                    perror('invalid twitter user spec: %r' % (user))
            else:
                # list channel:
                if not user or not list_name:
                    perror('invalid twitter list spec: %r' % (name))
                else:
                    channel = ListChannel(self, user, list_name)
        if channel:
            self.channels[name] = channel
            return channel

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
            u = self.get_twitter_user(tu.id)
            self.whois_twitter_user(u, tu)

        def error(e):
            self.send_reply(irc.ERR_NOSUCHNICK, mask, ':Error fetching user info - %s' % (e.value))

        doit()

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
