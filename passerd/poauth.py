
#!/usr/bin/env python
#
# Passerd - An IRC server as a gateway to Twitter
#
# OAuth code
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

from twisted.internet import defer
from twisted.web import client as twclient
import oauth.oauth as oauth


logger = logging.getLogger('passerd.oauth')
dbg = logger.debug


OAUTH_CONSUMER_KEY='1K2bNGyqs7dtDKTaTlfnQ'
OAUTH_CONSUMER_SECRET='frpQHgjN21ajybwA0ZQ2utwlu9O6A36r8YLy6PxY5c'

OAUTH_REQUEST_TOKEN_URL='http://twitter.com/oauth/request_token'
OAUTH_ACCESS_TOKEN_URL='http://twitter.com/oauth/access_token'
OAUTH_AUTHORIZE_URL='http://twitter.com/oauth/authorize'

OAUTH_SIGN_METHOD=oauth.OAuthSignatureMethod_HMAC_SHA1()

oauth_consumer = oauth.OAuthConsumer(OAUTH_CONSUMER_KEY, OAUTH_CONSUMER_SECRET)


class OAuthClient:
    def __init__(self, url_cb=None, progress_cb=None):
        self.verifier_callback = None
        self.url_callback = url_cb
        self.progress_cb = progress_cb

    def progress(self, msg):
        """Can be overwritten, to show a status message"""
        if self.progress_cb:
            self.progress_cb(msg)

    def send_to_url(self, url):
        """Must be overwritten"""
        if self.url_callback:
            self.url_callback(url)
        else:
            raise NotImplementedError("oauth send_to_url not implemented")

    def got_verifier(self, verifier):
        """Must be called when the verifier code is received"""
        if self.verifier_callback:
            return self.verifier_callback(verifier)
        else:
            raise Exception("Not waiting for OAuth verifier")

    def request_token(self):
        def doit():
            req = oauth.OAuthRequest.from_consumer_and_token(oauth_consumer, callback='oob', http_url=OAUTH_REQUEST_TOKEN_URL)
            req.sign_request(OAUTH_SIGN_METHOD, oauth_consumer, None)
            return twclient.getPage(req.to_url()).addCallback(done)

        def done(data):
            return oauth.OAuthToken.from_string(data)

        return doit()

    def authorize_url(self, req_token):
        """Return the URL used for authorization"""
        req = oauth.OAuthRequest.from_token_and_callback(token=req_token, http_url=OAUTH_AUTHORIZE_URL)
        return req.to_url()

    def _send_verifier(self, req_token, verifier):
        def doit():
            req = oauth.OAuthRequest.from_consumer_and_token(oauth_consumer, http_method='POST', token=req_token, verifier=verifier, http_url=OAUTH_ACCESS_TOKEN_URL)
            req.sign_request(OAUTH_SIGN_METHOD, oauth_consumer, req_token)
            postdata = req.to_postdata()
            dbg("access token url: %r. postdata: %r. token: %s", OAUTH_ACCESS_TOKEN_URL, postdata, req_token)
            return twclient.getPage(OAUTH_ACCESS_TOKEN_URL, method='POST',
                    postdata=postdata).addCallback(done)

        def done(data):
            return oauth.OAuthToken.from_string(data)

        return doit()

    def get_oauth_token(self):
        """Request a new oauth token

        It returns a deferred, but you should use the returned Deferred only if
        got_verifier() is called only once. If you are going to allow the user
        to retry, use the Deferred returned by got_verifier()
        """
        d = defer.Deferred()
        def doit():
            self.progress("getting a request token...")
            self.request_token().addCallbacks(got_req_token, d.errback)

        def got_req_token(req_token):
            self.progress("got request token.")

            # now we will wait for the PIN:
            self.verifier_callback = lambda pin: got_pin(req_token, pin)
            self.send_to_url(self.authorize_url(req_token))

        def got_pin(req_token, pin):
            self.progress("getting access token...")
            return self._send_verifier(req_token, pin).addCallback(got_access_token).addErrback(error)

        def got_access_token(token):
            self.progress("got the access token")
            # the get_oauth_token() deferred works only once.
            if not d.called:
                d.callback(token)
            return token

        def error(e):
            if not d.called:
                # the get_oauth_token() deferred works only once.
                d.errback(e)
            return e

        doit()
        return d



