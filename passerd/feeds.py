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

import time
import logging

from twisted.internet import reactor, defer

from passerd.callbacks import CallbackList

# 'count' paremeter for feed queries. It's a bit high, but this shouldn't be a
# problem as we always use the last_id parameter.
QUERY_COUNT = 100

logger = logging.getLogger('passerd.feeds')
dbg = logger.debug

class ThrottlerMessage:
    """Class useful to identify throttler messages on error callbacks"""
    def __init__(self, msg):
        self._msg = msg

    def __str__(self):
        return str(self._msg)

class BackWorkingMessage(ThrottlerMessage):
    pass

class ThrottlerStopMessage(ThrottlerMessage):
    pass

class ErrorThrottler:
    """Filter repeated error messages"""

    # max number of times repeated error messages can be shown:
    MAX_SAME_ERROR = 1

    # max number of different error messages that can be shown:
    MAX_DIFF_ERROR = 4

    # informative messages:
    SAME_ERROR_MSG = "I got the same error again. I will shut up and just tell you when things are back to normal"
    LOTS_ERRORS_MSG = "I am getting too many errors. I will shut up and just tell you when things are back to normal"
    BACK_WORKING = "Good! Things seem to be working again"

    def __init__(self, real_error_fn):
        self._real_fn = real_error_fn
        self._last_err = None
        self._same_err_count = 0
        self._any_err_count = 0
        self._stopped = False

    def _stop(self, msg):
        self._real_fn(ThrottlerStopMessage(msg))
        self._stopped = True

    def error(self, e):
        self._any_err_count += 1
        msg = str(e)
        if msg == self._last_err:
            self._same_err_count += 1
        else:
            self._same_err_count = 1
        self._last_err = msg

        if self._stopped:
            return

        if self._same_err_count > self.MAX_SAME_ERROR:
            return self._stop(self.SAME_ERROR_MSG)

        if self._any_err_count > self.MAX_DIFF_ERROR:
            return self._stop(self.LOTS_ERRORS_MSG)

        return self._real_fn(e)

    def ok(self):
        if self._stopped:
            self._real_fn(BackWorkingMessage(self.BACK_WORKING))
        self._same_err_count = 0
        self._any_err_count = 0
        self._stopped = False


class TwitterFeed:

    def __init__(self, proto):
        self.proto = proto
        self.updater = None
        self.entry_cb = CallbackList()
        self.errbacks = CallbackList()
        self.raw_errbacks = CallbackList()
        self.continue_refreshing = False
        self.next_refresh = None
        self.loading = False
        self._last_id = None
        self._error_handler = ErrorThrottler(self.report_error)

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

    def addEntryCallback(self, *args, **kwargs):
        """Add a callback for new entries"""
        self.entry_cb.addCallback(*args, **kwargs)

    def addErrback(self, *args, **kwargs):
        """Add a callbck for loading errors"""
        self.errbacks.addCallback(*args, **kwargs)

    def addRawErrback(self, *args, **kwargs):
        """Add a "raw" error callback, without error throttling"""
        self.raw_errbacks.addCallback(*args, **kwargs)

    @property
    def api(self):
        return self.proto.api

    @property
    def scheduler(self):
        return self.proto.scheduler

    def refresh_resched(self):
        if self.updater is not None:
            self.updater.resched()

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
            return self._timeline(got_entry, args).addCallbacks(finished, error)
            dbg("_refresh returning")

        def error(e):
            dbg("_refresh error %r", e)
            return e

        # store the entries and then show them in chronological order:
        def got_entry(e):
            dbg("got an entry")
            entries.insert(0, e)

        def finished(*args):
            dbg("finished loading %r" % (args,))

            # tell the error throttler that things are ok, now:
            self._error_handler.ok()

            for e in entries:
                self.entry_cb.callback(e)
                if self.last_id is None or int(e.id) > int(self.last_id):
                    self.update_last_id(e.id)
            return len(entries)

        return doit()

    def report_error(self, e):
        """Send an error message back to interested parties"""
        self.errbacks.callback(e)

    def refresh(self):
        def doit():
            if self.loading:
                dbg("Won't refresh now. Still loading...")
                return

            self.loading = True
            self._refresh().addCallbacks(done, error).addBoth(resched)

        def error(e):
            dbg("ERROR while refreshing")
            self.raw_errbacks.callback(e)
            self._error_handler.error(e.value)
            return e

        def done(num_entries):
            dbg("got %d entries." % (num_entries))

        def resched(*args):
            self.loading = False
            dbg("rescheduling... [%r]", args)
            self.refresh_resched()

        return doit()

    def stop_refreshing(self):
        if self.updater is not None:
            self.updater.destroy()
            self.updater = None

    def start_refreshing(self):
        if self.updater is None:
            self.updater = self.scheduler.new_updater(self.refresh)
            # yes, this is cheating, but I don't want to make the user wait for
            # too long
            #FIXME: just add support for 'one-shot lower-latency' calls on
            #       the scheduler, instead of cheating
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
