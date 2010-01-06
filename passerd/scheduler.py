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

class RefreshSlot:
    def __init__(self, task):
        self.task = task

    def cancel(self):
        self.task.cancel()

    def active(self):
        #TODO: kill this method?
        return self.task.active()

class ApiScheduler:
    def __init__(self, api):
        self.api = api

    def request_slot(self, fn, delay):
        def do_call(*args, **kwargs):
            dbg("calling: %r(%r, %r)", fn, args, kwargs)
            return fn(*args, **kwargs)

        dbg("scheduling %r for %d seconds", fn, delay)
        #TODO: implement true scheduling
        return RefreshSlot(reactor.callLater(delay, do_call))
