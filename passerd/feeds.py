#!/usr/bin/env python
#
# Passerd - An IRC server as a gateway to Twitter
#
# Code for continuously-updating feeds
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

from twisted.internet import reactor, defer

from passerd.callbacks import CallbackList

# refresh delay in seconds. We don't have a rate-limiting scheduler (yet),
# so be careful when increasing this. By default we have three feeds running:
# home_timeline, direct_messages, and mentions. Keep this above 72 seconds
REFRESH_DELAY = 90

# 'count' paremeter for feed queries. It's a bit high, but this shouldn't be a
# problem as we always use the last_id parameter.
QUERY_COUNT = 100

dbg = logging.debug

class TwitterFeed:

    def __init__(self, proto):
        self.proto = proto
        self.callbacks = CallbackList()
        self.errbacks = CallbackList()
        self.continue_refreshing = False
        self.next_refresh = None
        self.loading = False
        self._last_id = None

    def _last_id_var(self):
        return self.LAST_ID_VAR

    def update_last_id(self, last_id):
        self._last_id = last_id
        self.proto.set_user_var(self._last_id_var(), last_id)

    @property
    def last_id(self):
        if self._last_id is None:
            self._last_id = self.proto.user_var(self._last_id_var())
        return self._last_id

    def addCallback(self, *args, **kwargs):
        """Add a callback for new entries"""
        self.callbacks.addCallback(*args, **kwargs)

    def addErrback(self, *args, **kwargs):
        """Add a callbck for loading errors"""
        self.errbacks.addCallback(*args, **kwargs)

    def get_api(self):
        return self.proto.api
    api = property(get_api)

    def cancel_next_refresh(self):
        if self.next_refresh is not None:
            if self.next_refresh.active():
                self.next_refresh.cancel()
            self.next_refresh = None

    def refresh_resched(self):
        self.cancel_next_refresh()
        self.next_refresh = reactor.callLater(REFRESH_DELAY, self.refresh)

    def _refresh(self, last_id=None):
        if last_id is None:
            last_id = self.last_id

        entries = []
        d = defer.Deferred()

        def doit():
            args = {}
            if last_id:
                args['since_id'] = last_id
            args['count'] = str(QUERY_COUNT)
            self._timeline(got_entry, args).addCallbacks(finished, error)
            dbg("_refresh returning")

        def error(*args):
            dbg("_refresh error %r" % (args,))
            self.errbacks.callback(*args)
            d.errback(*args)

        # store the entries and then show them in chronological order:
        def got_entry(e):
            dbg("got an entry: %r" % (repr(e)))
            entries.insert(0, e)

        def finished(*args):
            dbg("finished loading %r" % (args,))
            for e in entries:
                self.callbacks.callback(e)
                if self.last_id is None or int(e.id) > int(self.last_id):
                    self.update_last_id(e.id)
            d.callback(len(entries))

        doit()
        return d

    def refresh(self):
        def doit():
            if self.loading:
                dbg("Won't refresh now. Still loading...")
                return

            self.cancel_next_refresh()
            self.loading = True
            self._refresh().addCallbacks(done, error)

        def error(*args):
            self.loading = False
            dbg("ERROR while refreshing")
            resched()

        def done(num_entries):
            self.loading = False
            dbg("got %d entries." % (num_entries))
            resched()

        def resched():
            dbg("rescheduling...")
            if self.continue_refreshing:
                self.refresh_resched()

        return doit()

    def stop_refreshing(self):
        self.continue_refreshing = False
        self.cancel_next_refresh()

    def start_refreshing(self):
        self.continue_refreshing = True
        self.refresh()

class ListTimelineFeed(TwitterFeed):

    def __init__(self, proto, list_user, list_name):
        TwitterFeed.__init__(self, proto)
        self.list_user = list_user
        self.list_name = list_name

    def _last_id_var(self):
        return "last_status_id_@%s/%s" % (self.list_user, self.list_name)

    def _timeline(self, delegate, args):
        return self.api.list_timeline(delegate, self.list_user,
                self.list_name, args)


class HomeTimelineFeed(TwitterFeed):
    LAST_ID_VAR = 'home_last_status_id'

    def _timeline(self, delegate, args):
        dbg("will try to use the API:")
        return self.api.home_timeline(delegate, args)


class UserTimelineFeed(TwitterFeed):

    def __init__(self, proto, user):
        TwitterFeed.__init__(self, proto)
        self.user = user

    def _last_id_var(self):
        return "last_status_id_@%s" % (self.user)

    def _timeline(self, delegate, args):
        return self.api.user_timeline(delegate, self.user, args)


class MentionsFeed(TwitterFeed):
    LAST_ID_VAR = 'mentions_last_status_id'

    def _timeline(self, delegate, args):
        return self.api.mentions(delegate, args)

class DirectMessagesFeed(TwitterFeed):
    LAST_ID_VAR = 'direct_messages_last_id'

    def _timeline(self, delegate, args):
        return self.api.direct_messages(delegate, args)
