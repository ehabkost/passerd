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

import sys, os, logging, time, re, random
import fcntl, signal
import gc
import optparse
import rfc822 # for date/time parsing

from twisted.words.protocols import irc
from twisted.words.protocols.irc import IRC
from twisted.internet.protocol import Factory
from twisted.internet import reactor, defer
import twisted.web.error


from twittytwister.twitter import Twitter, TwitterClientInfo

from passerd.data import DataStore, TwitterUserData
from passerd.callbacks import CallbackList
from passerd.utils import full_entity_decode
from passerd.feeds import HomeTimelineFeed, ListTimelineFeed, UserTimelineFeed, MentionsFeed, DirectMessagesFeed
from passerd.scheduler import ApiScheduler
from passerd import dialogs
from passerd.dialogs import Dialog, CommandDialog, CommandHelpMixin, attach_dialog_to_channel, attach_dialog_to_bot
from passerd.util import try_unicode, to_str
from passerd.irc import IrcUser, IrcChannel, IrcServer
from passerd.poauth import OAuthClient, oauth_consumer
from passerd import version
import oauth.oauth as oauth

from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
from sqlalchemy.sql.expression import func


# client/user-agent info:
####

#FIXME: use a real hostname?
MYHOST = 'passerd.server'
CLIENT_INFO = TwitterClientInfo(version.NAME, version.VERSION, version.URL)


# IRC protocol stuff:
####

# the only mode supported, right now:
SUPPORTED_CHAN_MODES = 'b'
# No user mode is supported, by now:
SUPPORTED_USER_MODES = '0'
IRC_ENCODING = 'utf-8'


# Twitter protocol stuff:
####

BASE_URL = 'https://twitter.com'
TWITTER_ENCODING = 'utf-8'
LENGTH_LIMIT = 140




# Some limits:
####


# keep latest 100 post on each channel, to create in_reply_to field.
REPLY_HISTORY_SIZE = 100

# if more than MAX_USER_INFO_FETCH users are unknown, use /statuses/friends to fetch user info.
# otherwise, just fetch individual user info

MAX_USER_INFO_FETCH = 0  # individual fetch is not implemented yet...

# the maximum number of sequential friend list page requests:
MAX_FRIEND_PAGE_REQS = 10


# minimum post age (in seconds) to allow it to be used for RTs.
# useful to avoid surprises when using the !RT command
#TODO: make this configurable
MIN_LATEST_POST_AGE = 2



# IRC protocol constants:
####

# Other error codes we may use:
ERR_NEEDREGGEDNICK = '477'



# logging helpers:
####

logger = logging.getLogger("passerd")

dbg = logger.debug
pinfo = logger.info
perror = logger.error



class ErrorReply(Exception):
    """Special exception class used to generate IRC numeric replies"""
    def __init__(self, command, *args):
        self.command = command
        self.args = args

class MissingOAuthRegistration(Exception):
    pass

class MessageTooLong(Exception):
    def __init__(self, text, length):
        self.text = text
        self.length = length
        Exception.__init__(self, 'message too long (%d characters)' % (length))



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
            u = self.proto.data.query(TwitterUserData).filter(func.lower(TwitterUserData.twitter_screen_name)==name.lower()).one()
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

        msg = try_unicode(msg, IRC_ENCODING)
        if len(msg) > LENGTH_LIMIT:
            #TODO: maybe there's a better error code for this?
            raise ErrorReply(irc.RPL_AWAY, self.nick, ':message too long (%d characters), not sent.' % (len(msg)))

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


REPLY_RE = re.compile(r'(@?)([a-zA-Z0-9_]+)([:, ])')

class TwitterChannel(IrcChannel):
    def __init__(self, proto, name):
        IrcChannel.__init__(self, proto, name)

        # REPLY_HISTORY_SIZE recent posts
        self.recent_posts = []
        # last post by each user ID
        self.recent_by_user = {}

        self.feeds = self._createFeeds()
        for f in self.feeds:
            f.addCallback(self.got_entry)
            f.addErrback(self.refresh_error)

        self.cmd_dialog = PasserdCommands(proto, self)
        self.cmd_dialog.set_message_func(self.bot_msg)
        self.cmd_dialog.set_cmd_prefix('!')

    def _createFeeds(self):
        raise NotImplementedError("_createFeeds not implemented on %s" % (self.name))

    def userModeChar(self, u):
        if u == self.proto.the_user:
            return '@'
        if u == self.proto.passerd_bot:
            return '@'
        return ''

    def printEntry(self, entry):
        e = entry
        is_rt = False
        if entry.retweeted_status:
            e = entry.retweeted_status
            is_rt = True
            dbg("Retweet! RT ID: %r", entry.id)
        u = self.proto.get_twitter_user(e.user.id)
        dbg("entry id: %r", e.id)
        text = e.text
        dbg('entry text: %r' % (text))

        if is_rt:
            rt_inline = self.proto.user_cfg_var_b('rt_inline')
            if rt_inline:
                #TODO: make RT inline format configurable
                text = '%s \x02[RT by @%s]\x02' % (text, entry.user.screen_name)

        self.proto.send_text(u, self, text)
        if is_rt:
            if not rt_inline:
                self.bot_msg("(%s retweeted by %s)" % (e.user.screen_name, entry.user.screen_name))

    def _drop_one_old_entry(self):
        #FIXME: Claudio reported a memory leak, I think it's here.

        # remove from two lists:
        # - recent_posts
        # - last_post_by_user
        drop = self.recent_posts.pop(0)
        uid = int(drop.user.id)

        # check if it is on the recent_by_user list, too:
        urec = self.recent_by_user.get(uid, [])
        if len(urec) > 0:
            # we always remove entries from recent_by_user, so
            # it should be the first on the list:
            if int(urec[0].id) == int(drop.id):
                urec.pop(0)

    def _add_to_history(self, e):
        self.recent_posts.append(e)
        if self.recent_posts > REPLY_HISTORY_SIZE:
            self._drop_one_old_entry()
        uid = int(e.user.id)
        self.recent_by_user.setdefault(uid, []).append(e)

    def recent_post(self, nick, substring=None, min_age=None):
        u = self.proto.global_twuser_cache.lookup_screen_name(nick)
        if u is None:
            dbg("nickname %s not found", nick)
            # nickname not found
            return None
        uid = u.twitter_id
        recent = self.recent_by_user.get(uid, [])
        if len(recent) < 1:
            dbg("no posts by uid %s", uid)
            return None


        if substring:
            matches = []
            i = 1
            while i <= len(recent):
                r = recent[-i]
                #TODO: make it more flexible, ignoring punctuation and spaces
                if substring.lower() in r.text.lower():
                    matches.append(r)
                i += 1

            if not matches:
                return None

            if len(matches) > 1:
                raise Exception("Multiple matches for [%s] on posts by %s" % (substring, nick))

            # yay, single match:
            r = matches[0]
        else:
            r = recent[-1]
            if min_age:
                #FIXME: add date parsing support to twitty-twister
                #FIXME: show a list of alternatives to the user
                #       (how to do that for replies?)
                t = rfc822.mktime_tz(rfc822.parsedate_tz(r.created_at))
                if t > time.time() - min_age:
                    raise Exception("latest post by %s is too recent, I don't know if it's the one you want. Use words from the text to identify it" % (nick))

        return r

    def last_post_id(self, nick):
        r = self.recent_post(nick)
        if r is None:
            return None
        return int(r.id)

    def cache_entry(self, e):
        u = e.user
        self.proto.global_twuser_cache.got_api_user_info(u)
        self._add_to_history(e)

    def got_entry(self, e):
        dbg("#twitter got_entry. id: %s", e.id)
        self.cache_entry(e)
        if e.retweeted_status:
            self.cache_entry(e.retweeted_status)
        self.printEntry(e)

    def bot_msg(self, msg):
        self.proto.send_privmsg(self.proto.passerd_bot, self, msg)

    def bot_notice(self, msg):
        self.proto.send_notice(self.proto.passerd_bot, self, msg)

    def refresh_error(self, e):
        dbg("#twitter refresh error")
        #FIXME: stop showing repeated errors and just let the user know when service is back
        if e.check(twisted.web.error.Error):
            if str(e.value.status) == '503':
                self.bot_notice("Look! A flying whale! -- %s" % (e.value))
                return
            remaining = self.proto.api.rate_limit_remaining
            # note that it will not wait when remaining is None, which is
            # intended
            if e.value.status == '400' and remaining == 0:
                self.wait_rate_limit()

        self.bot_notice("error refreshing feed: %s" % (e.value))

    def wait_rate_limit(self):
        reset = time.ctime(self.proto.api.rate_limit_reset)
        self.bot_msg('Ouch, the limit of requests per hour has been '
                'reached. I will wait until %s to start checking the '
                'Twitter timeline again.' % (reset))
        self.bot_msg('You can still try to fetch new tweets using `!`. Also, '
                'you can check the rate limit using `!rate`.')
        self.proto.scheduler.wait_rate_limit()

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
                self.bot_msg('people are quiet...')

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

        self.cmd_dialog.recv_message(cmd)

    def messageReceived(self, sender, msg):
        if msg.startswith('!'):
            return self.commandReceived(msg[1:])

        # careful mode?
        if not self.proto.user_cfg_var_b('careful'):
            # simply post directly
            self.do_send_twitter_post(msg)
        else:
            # careful mode: check if it's a valid command
            r,_ = self.cmd_dialog.try_msg(msg)
            if not r:
                self.bot_msg("I Can't Hear You! Use !tw to post, or disable careful mode using `!be brave`")

    def ctcp_ACTION(self, arg):
        dbg("ACTION: %r" % (arg))
        #TODO: make the behavior of "/me" messages configurable
        self.do_send_twitter_post('/me %s' % (arg))

    def do_send_twitter_post(self, msg):
        def doit():
            return self.send_twitter_post(msg)

        def done(*args):
            #FIXME: remove this notice once we update the channel topic. we don't need it.
            self.bot_notice("Twitter update posted!!")

        def error(e):
            if e.check(MessageTooLong):
                # message-too-long errors
                self.proto.send_reply(irc.ERR_CANNOTSENDTOCHAN, self.name, ':%s' % (str(e.value)))
                return
            self.bot_msg("%s: error while posting: %s" % (self.proto.the_user.nick, e.value))

        return defer.maybeDeferred(doit).addCallback(done).addErrback(error)

    def _add_in_reply_to(self, msg, args):
        m = REPLY_RE.match(msg)
        if m is None:
            return msg

        at = m.group(1)
        nick = m.group(2)
        end = m.group(3)

        # just "username " at the beginning won't be considered
        # a reply. User must use either "@username", "username:", or "username,"
        if at != '@' and end == ' ':
            return msg

        #TODO: have some timing check, just in case the
        #      last post is too recent to be replied to (e.g. 0.1 second ago)

        last_post = self.last_post_id(nick)
        if last_post:
            args['in_reply_to_status_id'] = str(last_post)
            if not msg.startswith("@"):
                msg = "@"+msg

        #TODO: add "@" for users that are on the channel, but haven't posted recently?

        return msg

    def send_twitter_post(self, msg):
        args = {}
        msg = self._add_in_reply_to(msg, args)
        dbg("msg: %r. args: %r", msg, args)
        return self.proto.send_twitter_post(msg, args)


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
            users = [self.proto.the_user, self.proto.passerd_bot]+users
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


class ProtoDialog:
    """A simple mixin to set a 'proto' attribute on dialog_init()"""
    def dialog_init(self, proto, *args, **kwargs):
        self.proto = proto


class ConfigInfo:
    """Just a namespace for definitions of configuration options
    """
    OPTIONS = set('rt_inline multiline careful'.split())

    help_rt_inline = 'Show inline "[RT by @user]" info on Retweets'
    help_multiline = 'Show multi-line posts as multiple IRC messages'
    help_careful = "Don't post non-command channel messages to Twitter directly"

    @classmethod
    def all_opts(klass):
        return list(klass.OPTIONS)

    @classmethod
    def has_opt(klass, o):
        return o in klass.OPTIONS

    @classmethod
    def opt_help(klass, o):
        return getattr(klass, 'help_%s' % (o), None)


class ConfigCommands(ProtoDialog, CommandDialog):
    shorthelp_set = 'Change a config option'
    def help_set(self, args):
        self.cmd_syntax('set', 'option value')
    def command_set(self, args):
        opt,value = self.split_args(args)
        if not value:
            self.help_set(None)
            return

        if not ConfigInfo.has_opt(opt):
            self.message("Invalid option name: %r" % (opt))
            return


        self.proto.set_user_cfg_var(opt, value)
        self.message(u'Option %s set to: %s' % (opt, value))

    def show_all(self):
        self.message('%-10s %s' % ('Option', 'Value'))
        for o in ConfigInfo.all_opts():
            v = self.proto.user_cfg_var(o)
            if v is None:
                v = '-'
            self.message(u'%-10s %s' % (o, v))

    shorthelp_show = 'Show the value of a config option'
    def command_show(self, args):
        if not args:
            return self.show_all()

        opt = args
        if not ConfigInfo.has_opt(opt):
            self.message("Invalid option name: %r" % (opt))
            return

        value = self.proto.user_cfg_var(opt)
        if value is None:
            self.message(u'Option %s is unset' % (opt))
        else:
            self.message(u'Option %s is set to: %s' % (opt, value))

    def show_help(self, prefix, args):
        CommandDialog.show_help(self, prefix, args)
        self.message('%-10s %s' % ('Option', 'Description'))
        for o in ConfigInfo.all_opts():
            self.message('%-10s %s' % (o, ConfigInfo.opt_help(o)))



class BeCommands(CommandDialog):
    """The 'be' command handler"""
    def dialog_init(self, proto, *args, **kwargs):
        self.proto = proto
        self.add_alias('paranoid', 'careful')

    help_header = 'Shortcut for many common config settings'
    def command_happy(self, args):
        self.message(':)')

    def unknown_command(self, cmd, args):
        self.message('Be what?')

    shorthelp_careful = "Don't post channel messages to Twitter directly"
    def command_careful(self, args):
        self.message('I will. From now on, Twitter updates can '
                      'only be sent with `%stw <message>`' % (self.parent.cmd_prefix))
        self.message('You can disable this setting using: `%sbe brave`' % (self.parent.cmd_prefix))
        self.proto.set_user_cfg_var('careful', True)

    shorthelp_brave = "Post channel messages to Twitter directly"
    def command_brave(self, args):
        self.message('So you are! Channel messages will be posted directly to Twitter')
        self.proto.set_user_cfg_var('careful', False)

    shorthelp_concise = "Show every post as a single IRC message"
    def command_concise(self, args):
        self.message("OK. I know I talk too much")
        self.proto.set_user_cfg_var('rt_inline', True)
        self.proto.set_user_cfg_var('multiline', False)

    shorthelp_verbose = "Show multi-line posts as multiple lines, and RT info as additional passerd-bot messages"
    def command_verbose(self, args):
        self.message("Thanks. I like to talk, you know  :)")
        self.proto.set_user_cfg_var('rt_inline', False)
        self.proto.set_user_cfg_var('multiline', True)

    def show_help(self, prefix, args):
        self.message(self.parent.cmd_syntax_str('be', '<flag>'))
        self.message("Available flags:")
        for imp,t in self.help_topics():
            self.message(" %s - %s" % (t, self._short_help(t)))


class PasserdCommands(CommandHelpMixin, CommandDialog):
    def dialog_init(self, proto, chan=None, *args, **kwargs):
        self.proto = proto
        self.chan = chan
        self.add_subdialog('config', ConfigCommands(proto), 'Query and change config settings')
        self.add_subdialog('be',  BeCommands(proto, parent=self))

        self.add_alias('s',   'post')
        self.add_alias('twit',   'post')
        self.add_alias('tw',     'post')
        self.add_alias('update', 'post')

    shorthelp_login = 'Log into Passerd/Twitter'
    def help_login(self, args):
        self.cmd_syntax('login', 'twitter-login password')
        self.message("If you don't have an account yet, join the #new-user-setup channel")

    def command_login(self, args):
        if not args:
            return self.help_login(None)

        parts = args.split(' ', 1)
        if len(parts) <> 2:
            return self.help_login(None)

        login,password = parts

        def doit():
            self.proto._do_auth(login, password).addCallback(done).addErrback(error)

        def done(u):
            self.message("Welcome to Passerd, %s" % (u.screen_name))
            self.proto.welcome_user()

        def error(e):
            self.message("Error while authenticating: %s" % (e.value))
            if e.check(MissingOAuthRegistration):
                self.proto.redirect_to_new_user_setup()

        doit()

    shorthelp_gc = 'Run Python garbage collection (debugging/testing command)'
    importance_gc = dialogs.CMD_IMP_DEBUGGING
    def command_gc(self, args):
        self.message("Object counts: %r" % (gc.get_count(),))
        r = gc.collect()
        self.message("Garbage collection run. %d objects freed" % (r))
        self.message("New object counts: %r" % (gc.get_count(),))

    shorthelp_rate = 'Show Twitter API rate-limit info'
    importance_rate = dialogs.CMD_IMP_ADVANCED
    def command_rate(self, args):
        api = self.proto.api
        if api is None:
            self.message('You are not logged in. No rate limit info is available')
            return
        self.message('Rate limit: %s. remaining: %s. reset: %s' % (api.rate_limit_limit, api.rate_limit_remaining, time.ctime(api.rate_limit_reset)))

    shorthelp_post = 'Post an update to Twitter'
    def help_post(self, args):
        self.cmd_syntax('post', 'text')
        self.message("Post an update to Twitter")

    def command_post(self, args):
        if not args:
            self.help_post(None)
            return

        def doit():
            # if there is an associated channel, use it as context for the
            # Twitter post. Otherwise, just use the general send_twitter_post()
            # method
            ch = self.chan
            if ch:
                d = ch.send_twitter_post(args)
            else:
                d = self.proto.send_twitter_post(args)
            return d

        def done(*args):
            self.message("Done. Twitter update posted")

        def error(e):
            self.message("Error while posting: %s" % (e.value))

        return defer.maybeDeferred(doit).addCallback(done).addErrback(error)

    #TODO: add 'needs_chan' decorator

    shorthelp_recent = "Debug the recent-post matching code"
    importance_recent = dialogs.CMD_IMP_DEBUGGING
    def command_recent(self, args):
        nick,substring = self.split_args(args)
        try:
            r = self.chan.recent_post(nick, substring, MIN_LATEST_POST_AGE)
        except Exception,e:
            self.message("error: %s" % (e))
            return

        if r:
            self.message("match: id: %r. text: %r" % (r.id, r.text))
        else:
            self.message("no match...")

    #TODO: add 'needs_chan' decorator
    shorthelp_rt = "Retweet a post"
    importance_rt = dialogs.CMD_IMP_COMMON
    def help_rt(self, args):
        self.cmd_syntax('rt', 'nick [part of post text]')
        self.message('If no text is specified, the latest post from <nick> is retweeted.')
        self.message('If text is specified, the post containing the supplied text is retweeted')
    def command_rt(self, args):
        if not self.chan:
            self.message("The RT command only works in a channel")
            return

        nick,substring = self.split_args(args)
        try:
            r = self.chan.recent_post(nick, substring, MIN_LATEST_POST_AGE)
        except Exception,e:
            self.message("error: %s" % (e))
            return

        if not r:
            if substring:
                self.message("no match for [%s] on posts by %s" % (substring, nick))
            else:
                self.message("no posts from %s" % (nick))
            return

        data = []
        def got_it(e):
            r = e.retweeted_status
            data.append(r)
            #FIXME: create a escape_post() function
            t = full_entity_decode(r.text).replace('\n', ' ').replace('\r', ' ')
            self.message("Retweeted: <%s> %s" % (r.user.screen_name, t))
        def done(*args):
            if not data:
                self.message("Unexpected error: no RT data returned by the Twitter server")

        def error(e):
            self.message("Error while retweeting: %s" % (e.value))

        self.proto.api.retweet(str(r.id), got_it).addCallback(done).addErrback(error)


class PasserdBot(IrcUser):
    """The Passerd IRC bot, that is used for Passerd messages on the channel"""
    def __init__(self, proto, nick):
        IrcUser.__init__(self, proto)
        self.proto = proto
        self.nick = nick

        self.dialog = d = PasserdCommands(proto, None)
        attach_dialog_to_bot(d, proto, proto.the_user, self)

    real_name = 'Passerd Bot'
    username = 'passerd'
    hostname = MYHOST


class NewUserDialog(ProtoDialog, Dialog):
    def begin(self, *args):
        def bm(msg):
            self.message(msg)

        def welcome():
            bm('Welcome!')
            bm('On this channel, we will set up an account for you.')
            bm("We will use the OAuth authentication method on Twitter,")
            bm("so you don't even need to give me your Twitter password.  :)")
            bm("Please tell me when you are ready, and we'll start the process")
            bm("Are you ready? (yes/no)")
            self.wait_for(r'^ *(y|yes|ok|start|restart) *$', start)
            self.wait_for(r'^ *n|no', lambda *a: bm("no problem..."))

        def ask_restart():
            bm("Do you want to restart?")
            self.wait_for('^ *y|yes', start)

        def start(msg, m):
            bm("OK, let's do it:")
            bm("(Note: at any moment, you can type 'restart', and the process will be restarted)")
            consumer = OAuthClient(url_cb=lambda url: show_url(consumer, url), progress_cb=show_progress)
            consumer.get_oauth_token().addErrback(error_get_token)

        def error_get_token(e):
            bm("Error while trying to get an OAuth token: %s" % (e.value))
            ask_restart()

        def show_progress(msg):
            bm("oauth progress: %s" % (msg))

        def show_url(consumer, url):
            bm("Now, go to: %s" % (url))
            bm("After authorizing Passerd to access your account, you'll get a PIN")
            bm("Please paste the PIN here")
            self.wait_for('[0-9][0-9][0-9]+', lambda msg,m: got_pin(consumer, m))

        def got_pin(consumer, m):
            pin = m.group(0)
            bm("Got it. Thanks!")
            bm("PIN: %r" % (pin))
            consumer.got_verifier(pin).addCallback(pin_worked).addErrback(pin_error)

        def pin_error(e):
            bm("The PIN didn't work. I got this error: %s" % (e.value))
            ask_restart()

        def pin_worked(token):
            bm('The PIN worked!')
            bm("Now I will check if I can access your account...")
            self.proto.test_oauth_token(token).addCallback(token_works).addErrback(token_error)

        def token_error(e):
            bm("The OAuth authentication didn't work. Sorry  :(")
            bm("Error message: %s" % (e.value))
            ask_restart()

        def token_works(args):
            token,api,u = args
            bm("OAuth authentication worked!")

            # change the user nickname to Twitter screen_name
            nick = str(u.screen_name)
            self.proto.the_user.force_nick(nick)
            bm("Welcome to Passerd, %s" % (nick))

            self.user_data = self.proto.set_user_token(u, token)
            self.twitter_user = u
            bm("Now Passerd can post to your account, but you still need to authenticate when connecting to Passerd")

            bm("You have two authentication options:")
            bm("1) Local password (recommended): Set a password just for Passerd, then you'll never need to reveal your Twitter password")
            bm("2) Twitter password: Just use your Twitter password when connecting to Passerd")
            bm("Which option do you want to use? (twitter/local)")
            self.wait_for(r'^ *loc|^ *1 *$', setup_password)
            #FIXME: clear password field if Twitter password option is chosen
            self.wait_for(r'^ *twi|^ *2 *$', all_set)

        def all_set(msg,m):
            bm("OK, so you are all set")
            bye_twpass()
            self.wait_for('.*', bye_twpass)

        def bye_twpass(*args):
            bm("Just reconnect to Passerd using your Twitter password,")
            bm("and your Twitter username (%s) as nickname" % (self.twitter_user.screen_name))

        def setup_password(msg,m):
            bm("OK. Send your password as a message to the channel, and I will set it")
            bm("Alternatively, I can generate a random password for you, just type 'generate' and I will do it")
            bm("What will be your password?")
            self.wait_for('.+', got_password)
            self.wait_for('^ *generate *$', gen_password)

        def got_password(msg,m):
            pw = msg
            if len(pw) < 6:
                bm("This is a short password! Are you sure you want to use it?")
                self.wait_for('.*', setup_password)
                self.wait_for('^ *y|yes', lambda msg,m: set_password(pw))
            else:
                set_password(pw)

        def gen_password(msg,m):
            length = 16
            chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
            pw = ''.join([random.choice(chars) for i in range(length)])
            set_password(pw)

        def set_password(pw):
            self.proto.set_user_password(self.user_data, pw)
            self.password = pw
            bm("Password set to: %s" % (pw))
            bye_pwset()
            self.wait_for('.*', bye_pwset)

        def bye_pwset(*args):
            bm("Just reconnect to Passerd using your Passerd password: %s" % (self.password))
            bm("and your Twitter username (%s) as nickname" % (self.twitter_user.screen_name))

        welcome()

class UserSetupChannel(IrcChannel):
    def __init__(self, proto, name):
        IrcChannel.__init__(self, proto, name)

        self.dialog = d = NewUserDialog(proto)
        attach_dialog_to_channel(d, self, self.proto.passerd_bot)

        # start the dialog automatically if any message is received on
        # the channel:
        d.wait_for('.*', lambda *a: d.begin())

    def list_members(self):
        return [self.proto.the_user, self.proto.passerd_bot]

    def afterUserJoined(self, who):
        self.dialog.begin()


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
        self.scheduler = None
        self.authenticated_user = None
        self.user_data = None
        self.got_user = False
        self.got_nick = False

        self.global_twuser_cache = self.factory.global_twuser_cache
        self.twitter_users = TwitterIrcUserCache(self, self.global_twuser_cache)


        self.my_irc_server = IrcServer(self, self.myhost)

        u = self.the_user = IrcUser(self)
        u.nick = 'guest'
        u.username = 'guest'
        u.hostname = self.hostname
        u.real_name = 'Unidentified User'

        self.passerd_bot = PasserdBot(self, 'passerd-bot')

        self.users = [self.the_user, self.passerd_bot]

        predef_chans = [MainChannel(self, '#twitter'),  MentionsChannel(self, '#mentions'), UserSetupChannel(self, '#new-user-setup')]

        #TODO: keep a list of the fixed and joined channels,
        #      but use short-lived channel objects for other channel-query
        #      commands
        self.channels = dict([(c.name, c) for c in predef_chans])

        #FIXME: make the auto-join optional:
        self.autojoin_channels = ['#twitter', '#mentions']
        #FIXME: make joined_channels a more efficient list of channels
        self.joined_channels = []

        self.dm_feed = DirectMessagesFeed(self)
        self.dm_feed.addCallback(self.gotDirectMessage)
        self.dm_feed.addErrback(self.dmError)

        dbg("Got new client")

    def welcome_user(self):
        for ch in self.autojoin_channels:
            self.join_cname(ch)
        self.dm_feed.start_refreshing()
        self.scheduler.start()

    def welcome_anonymous(self):
        self.notice("Welcome, anonymous user!")
        self.notice("If you already have a Passerd account set up, identify yourself with the command: /MSG PASSERD-BOT LOGIN username password")
        self.notice("If your account is not set up yet, please join the #new-user-setup channel to set up your account")


    def redirect_to_new_user_setup(self):
        """Send the user to the OAuth user setup channel"""
        self.send_notice(self.passerd_bot, self.the_user, "Please join #new-user-setup to set up your account")
        self.join_cname('#new-user-setup')

    def _set_scheduler(self, scheduler):
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None
        self.scheduler = scheduler

    def _userQuit(self, reason):
        self.dm_feed.stop_refreshing()
        for ch in self.joined_channels:
            self.leave_channel(ch, reason)
        self._set_scheduler(None)
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
        #FIXME: create a escape_post() function
        text = full_entity_decode(text)
        dbg('entities decoded: %r' % (text))
        text = text.replace('\r', '\n')

        lines = []
        if self.user_cfg_var_b('multiline'):
            first = True
            # handle newlines as multiple messages
            for line in text.split('\n'):
                if not line:
                    continue
                if not first:
                    #TODO: find a better way to indicate multi-line posts
                    line = '[...] '+line
                lines.append(line)
                first = False
        else:
            lines = [text.replace('\n', ' ')]

        for l in lines:
            self.send_privmsg(sender, target, l)

    def connectionLost(self, reason):
        pinfo("connection to %s lost: %s", self.hostname, reason.value)
        self.userQuit(str(reason))
        IRC.connectionLost(self, reason)

    def user_var(self, var):
        """Get any user var (config or internal feed state)"""
        return self.data.get_var(self.user_data, var)

    def set_user_var(self, var, value):
        return self.data.set_var(self.user_data, var, value)

    def set_user_cfg_var(self, var, value):
        """Set an user variable

        Note that all vars are strings, and other types
        are converted before setting the variables.
        """

        # convert some data types to string
        t = type(value)
        if t is bool:
            if value: value = '1'
            else: value = '0'

        vname = 'config:%s' % (var)
        return self.set_user_var(vname, value)

    def user_cfg_var(self, var):
        vname = 'config:%s' % (var)
        return self.user_var(vname)

    def user_cfg_var_b(self, var):
        v = self.user_cfg_var(var)
        true_values = ['true', 't', '1', 'y', 'yes', 'on']
        if v and v in true_values:
            return True
        else:
            return False

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
        method = getattr(self, "irc_%s" % (command), None)
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
        return self.sendMessage(prefix=sender.full_id(), *params)

    def server_message(self, cmd, *params):
        return self.send_message(self.my_irc_server, cmd, *params)

    def server_notice(self, target, msg):
        self.send_notice(self.my_irc_server, target, msg)

    def send_notice(self, sender, target, msg):
        if '\r' in msg or '\n' in msg: # just in case
            logger.error("Oops! newlines on channel notice: %r", msg)
            msg = msg.replace('\r',' ').replace('\n', ' ')
        self.send_message(sender, 'NOTICE', target.target_name(), ':%s' % (to_str(msg, IRC_ENCODING)))

    def send_privmsg(self, sender, target, msg):
        if '\r' in msg or '\n' in msg: # just in case
            logger.error("Oops! newlines on channel privmsg: %r", msg)
            msg = msg.replace('\r',' ').replace('\n', ' ')
        self.send_message(sender, 'PRIVMSG', target.target_name(), ':%s' % (to_str(msg, IRC_ENCODING)))

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

    def check_credentials(self, api, method):
        d = defer.Deferred()

        ok = []
        def doit():
            self.notice("Checking Twitter credentials using %s..." % (method))
            api.verify_credentials(got_user).addCallback(done).addErrback(error)

        def got_user(u):
            self.notice("%s authentication OK! Your Twitter user ID: %s. screen_name: %s" % (method, u.id, u.screen_name))
            ok.append(u)

        def done(*args):
            if not ok:
                d.errback(Exception("I got a reply from the Twitter server but no user info. This shouldn't have happened.  :("))
            else:
                d.callback(ok[0])

        def error(e):
            d.errback(e)

        doit()
        return d

    def is_authenticated(self):
        return (self.authenticated_user is not None)

    def _send_welcome_replies(self):
        """Send standard IRC numeric replies after registration"""
        self.send_reply(irc.RPL_WELCOME, ":Welcome to the Internet Relay Network %s!%s@%s" % (self.the_user.nick, self.the_user.username, self.the_user.hostname))
        self.send_reply(irc.RPL_YOURHOST, ":Your host is %s, running version %s" % (self.myhost, version.VERSION))
        self.send_reply(irc.RPL_CREATED, ":This server was created by the Flying Spaghetti Monster")
        self.send_reply(irc.RPL_MYINFO, self.myhost, version.VERSION, SUPPORTED_USER_MODES, SUPPORTED_CHAN_MODES)

        #TODO: send a MOTD with useful information


    def set_authenticated_user(self, u):
        self.authenticated_user = u
        self.user_data = self.data.get_user(int(u.id), u.screen_name, create=True)


    def _twitter_api(self, *args, **kwargs):
        """Create a Twitter API object"""
        api = Twitter(timeout=self.factory.opts.api_timeout, *args, **kwargs)
        #FIXME; patch twitty-twister to accept agent=foobar
        api.agent = version.USER_AGENT
        return api

    def _check_basic_auth(self, username, password):
        """Run verify_credentials API call using basic auth

        On success, pass a (api,auth_user) pair to the deferred callback
        """
        api = self._twitter_api(username, password, base_url=BASE_URL) #, client_info=CLIENT_INFO)
        def doit():
            return self.check_credentials(api, 'password').addCallback(done)

        def done(u):
            return (api, u)

        return doit()

    def test_oauth_token(self, token):
        """Check of the oauth token works

        On success, pass a (token, api, auth_user) pair to the deferred callback
        """
        api = self._twitter_api(consumer=oauth_consumer, token=token)
        def doit():
            return self.check_credentials(api, 'OAuth').addCallback(done)

        def done(u):
            return (token, api, u)

        return doit()

    def set_user_token(self, u, token):
        udata = self.data.get_user(int(u.id), u.screen_name, create=True)
        udata.oauth_token = token.key
        udata.oauth_token_secret = token.secret
        self.data.commit()
        return udata

    def set_user_password(self, udata, pw):
        udata.set_password(pw)
        assert udata.password_valid(pw) # sanity check
        self.data.commit()

    def _do_auth(self, username, password):
        """Authenticate username and password
        """
        d = defer.Deferred()
        def try_local_password():
            """Try to validate the password as a Passerd-only password"""
            udata = self.data.get_user(None, username)
            if udata is None:
                return None
            if not udata.password_valid(password):
                return None

            return udata

        def doit():
            # first, try the passerd-only passowrd:
            udata = try_local_password()
            if udata is not None:
                self.notice("Your local Passerd password is valid")
                return got_user(udata)

            # if Passerd password doesn't work, try Twitter basic auth password:
            self._check_basic_auth(username, password).addCallback(basic_auth_ok).addErrback(d.errback)

        def basic_auth_ok(args):
            api,u = args
            udata = self.data.get_user(int(u.id), u.screen_name)
            if udata is None:
                return no_oauth_setup()
            if not udata.oauth_token or not udata.oauth_token_secret:
                return no_oauth_setup()

            got_user(udata)

        def no_oauth_setup():
            d.errback(MissingOAuthRegistration("OAuth registration not done yet"))

        def got_user(udata):
            token = oauth.OAuthToken(udata.oauth_token, udata.oauth_token_secret)
            self.test_oauth_token(token).addCallbacks(oauth_works, oauth_error).addErrback(d.errback)

        def oauth_works(args):
            token,api,u = args

            # authentication worked. set up variables:
            self.api = api
            self._set_scheduler(ApiScheduler(api))
            self.set_authenticated_user(u)
            d.callback(u)

        def oauth_error(e):
            if e.check(twisted.web.error.Error):
                if str(e.value.status) == '401':
                    d.errback(MissingOAuthRegistration("OAuth token rejected by Twitter"))
                    return

            d.errback(e)

        doit()
        return d

    def _early_auth(self):
        """Run early password-authentication
        
        This should be used only on the early registration stages.
        """
        def doit():
            self._do_auth(self.the_user.nick, self.password).addCallback(auth_ok).addErrback(error)

        def auth_ok(u):
            self._send_welcome_replies()
            self.welcome_user()

        def error(e):
            if e.check(MissingOAuthRegistration):
                self.notice("Error while authenticating - %s" % (e.value))
                self.notice("Your connection will be considered anonymous, by now")
                self._send_welcome_replies()
                self.welcome_anonymous()
                self.redirect_to_new_user_setup()
            else:
                self.send_reply(irc.ERR_PASSWDMISMATCH, ":Error while authenticating - %s" % (e.value))
                # on the other cases, drop the connection
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

    def irc_PASS(self, p, args):
        self.password = args[0]
        self.try_early_auth()

    def irc_USER(self, prefix, params):
        dbg("USER %r" % (params))
        username,_,_,real_name = params[0:4]
        self.the_user.username = username
        self.the_user.real_name = real_name
        self.got_user = True

        #TODO: accept connections without password, and allow a nickserv-style method of authentication

        if self.password is not None:
            # password set, try early authentication
            self.try_early_auth()
        else:
            # not-authenticated-yet connection
            self._send_welcome_replies()
            self.welcome_anonymous()


    def send_twitter_post(self, msg, args={}):
        msg = try_unicode(msg, IRC_ENCODING)
        if len(msg) > LENGTH_LIMIT:
            return defer.fail(MessageTooLong(msg, len(msg)))

        return self.api.update(msg, params=args)


    def get_user(self, nick):
        #FIXME; index by nickname
        for u in self.users:
            if nick.lower() == u.nick.lower():
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
        def oneline(s):
            #FIXME: create a escape_post() function
            return full_entity_decode(s).encode(IRC_ENCODING).replace('\n', ' ').replace('\r', ' ')

        self.send_reply(irc.RPL_AWAY, u.nick, ':Location: %s' % oneline(tu.location))
        self.send_reply(irc.RPL_AWAY, u.nick, ':URL: %s' % oneline(tu.url))
        self.send_reply(irc.RPL_AWAY, u.nick, ':Bio: %s' % oneline(tu.description))
        self.send_reply(irc.RPL_AWAY, u.nick, ':Last update: %s' % oneline(tu.status.text))
        self.send_reply(irc.RPL_AWAY, u.nick, ':Twitter URL: http://twitter.com/%s' % oneline(tu.screen_name))
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

    def __init__(self, opts):
        url = 'sqlite:///%s' % (opts.database)
        self.opts = opts
        self.data = DataStore(url)
        self.data.create_tables()
        self.global_twuser_cache = TwitterUserCache(self)

class PasserdGlobalOptions:
    def __init__(self):
        # set the defaults:

        self.listen = ('0.0.0.0', 6667)

        #logging:
        self.logstream = sys.stderr

        # list of (logger_name, level) pairs
        # - sqlalchemy is too verbose on the INFO loglevel
        # - enable oauth debugging, by now
        self.loglevels = [(None,         logging.INFO),
                          ('sqlalchemy', logging.ERROR),
                          ('passerd.oauth',logging.DEBUG),
                          ('passerd.feeds',logging.DEBUG),
                          ('passerd.scheduler',logging.DEBUG)]
        self.logformat = '%(asctime)s:%(levelname)s:%(name)s:%(message)s'

        self.api_timeout = 60

        self.daemon_mode = False
        self.pidfile = None

def parse_cmdline(args, opts):
    def parse_hostport(option, optstr, value, parser):
        try:
            host, rawport = value.rsplit(":", 1)
            port = int(rawport)
        except ValueError:
            parser.error("invalid listen address, expected HOST:PORT")
        opts.listen = (host, port)

    def set_loglevels(levels):
        opts.loglevels = levels

    def open_logfile(option, optstr, value, parser):
        opts.logstream = open(value, 'a')

    parser = optparse.OptionParser("%prog [options] <database path>")
    parser.add_option("-l", "--listen", type="string",
            action="callback", callback=parse_hostport,
            metavar="HOST:PORT", help="listen address")
    parser.add_option("-D", "--debug",
            action="callback", callback=lambda *args: set_loglevels([(None,logging.DEBUG)]),
            help="Enable debug logging")
    parser.add_option("--logformat",
            metavar="FORMAT", type="string", default=None,
            help="Set the logging format")
    parser.add_option("-d", "--daemon",
            action="store_true", dest="daemon_mode")
    parser.add_option("-L", "--log-file",
            metavar="FILENAME", type="string",
            action="callback", callback=open_logfile)
    parser.add_option("-p", "--pid-file",
            metavar="FILENAME", type="string",
            dest="pidfile")
    _, args = parser.parse_args(args, opts)
    if not args:
        parser.error("the database path is needed!")
    opts.database = args[0]
    return opts

def setup_logging(opts):
    # root logger:
    r = logging.getLogger()

    if opts.logstream:
        handler = logging.StreamHandler(opts.logstream)
        f = logging.Formatter(opts.logformat)
        handler.setFormatter(f)
        r.addHandler(handler)

    for name,level in opts.loglevels:
        logging.getLogger(name).setLevel(level)

def _run(opts):
    pinfo("Starting Passerd. Will listen on address %s:%d" % opts.listen)
    reactor.listenTCP(interface=opts.listen[0], port=opts.listen[1],
             factory=PasserdFactory(opts))
    pinfo("Starting Twisted reactor loop")
    try:
        reactor.run()
    finally:
        pinfo("Terminating")

class PidFile:
    def __init__(self, filename):
        self.filename = filename

    def __enter__(self):
        dbg("locking pidfile %s", self.filename)
        self.file = open(self.filename, 'w')
        fcntl.lockf(self.file, fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.file.write('%d\n' % (os.getpid()))
        self.file.flush()

    def __exit__(self):
        dbg("unlocking pidfile %s", self.filename)
        os.unlink(self.filename)
        fcntl.lockf(self.file, fcntl.LOCK_UN)
        self.file.close()


def run_as_daemon(opts):
    try:
        import daemon
    except ImportError:
        raise Exception("You need the python-daemon module, to run Passerd on daemon mode")

    pidfile = None
    if opts.pidfile:
        pidfile = PidFile(opts.pidfile)

    # I don't want python-daemon to mess with any open file. The files we open
    # on initialization will be kept, because they are our log files and sockets.
    # stdin/stdout/stderr will be redirected to /dev/null, so they may be kept open,
    # too.
    preserve = range(MAXFD)
    try:
        with daemon.DaemonContext(files_preserve=preserve, pidfile=pidfile):
            _run(opts)
    except Exception,e:
        logger.exception(e)

MAXFD = 2048

def run():
    # be careful: avoid opening files before the _run() call. the
    # daemon module will close it if you don't include it on files_preserve.
    opts = PasserdGlobalOptions()
    parse_cmdline(sys.argv[1:], opts)

    setup_logging(opts)
    if opts.daemon_mode:
        run_as_daemon(opts)
    else:
        _run(opts)

__all__ = ['run']
