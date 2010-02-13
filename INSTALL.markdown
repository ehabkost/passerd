Requirements for Passerd
------------------------

_Passerd_ is written in Python. You need Python (of course!), and the following
Python modules:

* twisted (and its 'twisted.words' module)
* sqlite
* sqlalchemy (recommended version 0.5.4 or later)
* oauth
* A patched version of twitty-twister -- but don't worry: it is automatically fetched by `git submodule` and `./run.sh` (see run instructions below)


Optional dependencies:

* For running Passerd in _daemon_ mode:
  * [python-daemon](http://pypi.python.org/pypi/python-daemon/)
  * Python [lockfile](http://pypi.python.org/pypi/lockfile/) module


Below you'll find instructions to easily get the dependencies on some operating
systems:

### On Fedora

To install the dependencies on a Fedora machine, run:

	yum install python-twisted python-twisted-words python-sqlalchemy python-oauth

For the optional packages:

	yum install python-daemon python-lockfile


### On Mandriva

These dependencies currently match the 2010.0 release but should be ok for others:

	urpmi python-twisted-words python-sqlite python-sqlalchemy python-oauth


How to run it
-------------

Right now the recommended way to run Passerd is to check out the source
directly from the git repository.

### From the git repository:

* Check out the [git repository][gitrepo]
  * `git clone git://github.com/ehabkost/passerd.git`
  * `cd passerd`
* Run `git submodule init`
* Run `git submodule update`
* Run `./run.sh`
* Point your IRC client to server `localhost`, port `6667`
* Join the `#new-user-setup` channel on the server
* Follow the instructions from `passerd-bot`
* Have fun!

See the `USERGUIDE` file for more information.

[gitrepo]: http://github.com/ehabkost/passerd
