Passerd User Guide
==================

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


How to use it
-------------

Once you join the `#twitter` channel on the server, the following features are
available:

* Sending new updates to Twitter is as simple as sending a message to the channel
* Your friends timeline will appear as IRC messages on the channel
* You can follow/unfollow people using the IRC `/kick` and `/invite` commands
* You can send and receive Direct Messages as simple IRC private messages
* You can follow public lists by joining any #@username/listname channel


### Special commands

You can type some special commands on the IRC channel:

* `!` - force the Twitter timeline to be fetched imeediately
* `!!` - force the Twitter timeline to be fetched, _including older posts_
