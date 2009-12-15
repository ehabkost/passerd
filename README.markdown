Passerd
=======

What is _Passerd_?
------------------

_Passerd_ is a Twitter client that runs an IRC server. Just run it and point an
IRC client to it, and you'll see your Twitter friends as IRC contacts, and
tweets appearing as messages on an IRC channel.

Does it work?
-------------

It's currently on _alpha_ status, but it can already be used to fetch your home
timeline and submit new posts.

Sending/receiving Direct Messages, following/unfollowing/blocking people,
advanced list/search/notification support, and other features are planned.


What do I need to use it?
-------------------------

_Passerd_ is written in Python. You need Python (of course!), and the following
Python modules:

* twisted (and its 'twisted.words' module)
* sqlite
* sqlalchemy
* oauth
* A patched version of twitty-twister -- but don't worry: it is automatically fetched by `git submodule` and `./run.sh` (see run instructions below)


Below you'll find instructions to easily get the dependencies on some operating
systems:

### On Fedora

To install the dependencies on a Fedora machine, run:

	yum install python-twisted python-twisted-words python-sqlalchemy python-oauth


### On Mandriva

These dependencies currently match the 2010.0 release but should be ok for others:

	urpmi python-twisted-words python-sqlite python-sqlalchemy python-oauth


How do I run it?
----------------

* Check out the git repository
* Run `git submodule init`
* Run `git submodule update`
* Run `./run.sh`
* Point your IRC client to server `localhost`, port 6667, using your
  Twitter login as nickname, and your Twitter password as password
* Join the `#twitter` channel on the server
* Have fun!


Credits
-------

* Passerd was written by:
  * [Eduardo Habkost](http://twitter.com/ehabkost)
* Early testing and ideas from:
  * [Caio Begotti](http://twitter.com/caio1982)
  * [Alexandre Possebom](http://twitter.com/possebom)
  * [Felipe Arruda](http://twitter.com/felipemiguel)
  * [Olavo Junior](http://twitter.com/olavojunior)
  * [Tiago Salem Herrmann](http://twitter.com/tiagosh)
* Some feature ideas were shamelessly borrowed from [tircd][tircd]


[tircd]: http://code.google.com/p/tircd/
