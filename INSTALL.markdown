What do I need, to use Passerd?
-------------------------------

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



