#!/usr/bin/env python
#
# 'callback list' code
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

import traceback


# maybe Twisted has something equivalent to this, already?
class CallbackList:
    def __init__(self, ignore_exceptions=True, print_exceptions=True):
        self._cbs = []
        self._ignore_exceptions = ignore_exceptions
        self._print_exceptions = print_exceptions

    def _doCall(self, cb, cbargs, cbkwargs, *args, **kwargs):
        a = []
        a.extend(args)
        a.extend(cbargs)

        kw = {}
        kw.update(kwargs)
        kw.update(cbkwargs)
        return cb(*a, **kw)

    def addCallback(self, cb, *args, **kwargs):
        self._cbs.append( (cb, args, kwargs) )

    def callback(self, *args, **kwargs):
        for cb, ca, ckw in self._cbs:
            try:
                self._doCall(cb, ca, ckw, *args, **kwargs)
            except:
                if not self._ignore_exceptions:
                    raise
                elif self._print_exceptions:
                    traceback.print_exc()


__all__ = ['CallbackList']
