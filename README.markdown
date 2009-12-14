
What is Passerd
===============

Passerd is an IRC client that runs an IRC server. Just run it and point an IRC
server to it, and you'll see your friends as IRC contacts, and tweets as
messages on an IRC channel.

Status
======

Currently, it can only fetch your home timeline, but posting to Twitter
sending/receiving Direct Messages, and advanced list/search/notification
features are planned.


What do I need to use it?
=========================

Passerd is written in Python. You need Python (of course!), and the following
Python modules:

* twisted (and its 'twisted.words' module)
* sqlite
* sqlalchemy
* oauth

On Fedora
---------

Run:
`yum install python-twisted python-twisted-words python-sqlalchemy python-oauth`


How do I use it?
================

* Check out the git repository
* Run `git submodule init`
* Run `git submodule update`
* Run ./run.sh
* Point your IRC client to server `localhost`, port 6667, using your
  IRC username as nickname, and your IRC password as password
* Join the #twitter channel on the server
* Have fun!



