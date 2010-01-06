#!/usr/bin/env python
#
# Passerd - An IRC server as a gateway to Twitter
#
# Rate-limiting scheduler code
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

logger = logging.getLogger('passerd.scheduler')
dbg = logger.debug

# refresh delay in seconds. This is multiplied by the number of feeds running,
# so we don't hit the rate-limit if the user is following too many feeds.
# Twitter default rate limit is 150 requests/hour. REFRESH_DELAY=45 should
# result on 80 requets per hour

MAX_REQS_PER_HOUR = 80
REFRESH_DELAY = int(3600/MAX_REQS_PER_HOUR)

class RefreshUpdater:
    def __init__(self, scheduler, fn):
        self.scheduler = scheduler
        self.fn = fn
        self.pending = False

    def call(self):
        dbg("calling: %r", self.fn)
        self.pending = False
        return self.fn()

    def resched(self):
        dbg("resched %r. pending: %r", self.fn, self.pending)
        if self.pending:
            return
        self.pending = True
        self.scheduler._resched_updater(self)

    def unsched(self):
        dbg("unsched %r. pending: %r", self.fn, self.pending)
        if not self.pending:
            return
        self.pending = False
        self.scheduler._unsched_updater(self)

    def destroy(self):
        dbg("destroy %r. pending: %r", self.fn, self.pending)
        self.unsched()
        self.scheduler._remove_updater(self)

class ApiScheduler:
    def __init__(self, api):
        self.api = api
        self.updaters = {}
        self.pending_queue = []
        self.next_call = None
        self.running = False
        self.shots_available = 0

    def new_updater(self, fn, active=True):
        u = RefreshUpdater(self, fn)
        self.updaters[id(u)] = u
        if active:
            u.resched()
        return u

    def _remove_updater(self, u):
        del self.updaters[id(u)]

    def _resched_updater(self, u):
        self.pending_queue.append(u)
        self._run_shots()

    def _unsched_updater(self, u):
        self.pending_queue.remove(u)

    def _run_shots(self):
        while self.shots_available > 0 and self.pending_queue:
            self.shots_available -= 1
            u = self.pending_queue.pop(0)
            u.call()

    def _run_next(self):
        # we will run all pending refreshs at the same time to give a better
        # user experience: all content will be fetched at the same time
        # (DMs, home timeline, mentions, other feeds), avoiding
        # bugging the user multiple times
        dbg("_run_next called")
        updater_count = len(self.updaters)

        self.shots_available = updater_count
        self._run_shots()

        if self.shots_available > 0:
            dbg("still have %d shots available", self.shots_available)

        delay = REFRESH_DELAY*updater_count
        dbg("%d updaters are running. delay for the next update: %d seconds", updater_count, delay)
        self._sched_next(delay)


    def _sched_next(self, delay):
        dbg("scheduling next call for %d seconds", delay)
        self.next_call = reactor.callLater(delay, self._run_next)

    def _cancel_next(self):
        dbg("cancelling next call")
        if self.next_call is not None:
            if self.next_call.active():
                self.next_call.cancel()
            self.next_call = None

    def start(self):
        if not self.running:
            self.running = True
            self._run_next()

    def stop(self):
        self.running = False
        self._cancel_next()

    def wait_rate_limit(self):
        delay = int(self.api.rate_limit_reset - time.time())
        reset = time.ctime(self.api.rate_limit_reset)
        if delay > REFRESH_DELAY:
            dbg("Rescheduling the next feed refresh to %s (%s seconds),"
                " as the rate limit was exhausted." % (reset, delay))
            self._cancel_next()
            self._sched_next(delay)
        else:
            dbg("No need to resched to wait for rate-limit, as the "
                "delay is only %s seconds" % (delay))
