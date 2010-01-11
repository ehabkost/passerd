Passerd User Guide
==================

Running
-------

After starting _Passerd_, just point your favorit IRC client to it. If you are
running it locally, just use server `localhost`, port `6667`


Initial User Setup
------------------

The first time you connect to Passerd, you will be asked to join the
`#new-user-setup` channel. Follow the instructions from `passerd-bot` at that
channel to authorize Passerd to post to your account.

You don't even need to give Passerd your Twitter password, if you don't want
to. On the other hand, if you don't want to create a new password just for
Passerd, you can simply use your Twitter password when logging in.

After you set up your account, you can just set up your client to use the
password when connecting, or use a _nickserv-style_ command to authenticate
after connecting: `/MSG PASSERD-BOT LOGIN username password`.


Basic Operation
---------------

Once you authenticate and join the `#twitter` channel on the server, the
following features are available:

* Sending new updates to Twitter is as simple as sending a message to the channel
* Your friends timeline will appear as IRC messages on the channel
* You can follow/unfollow people using the IRC `/kick` and `/invite` commands

Other available features:

* Direct Messages sent/received as simple IRC private messages
* Following public lists, by joining any `#@username/listname` channel
* Reading messages from individual users without following them on Twitter,
  by joining any `#@username` channel
* Querying Twitter user information using the IRC `/WHOIS` command


### Special commands

You can type some special commands on the IRC channel:

* `!` - force the Twitter timeline to be fetched imeediately
* `!!` - force the Twitter timeline to be fetched, _including older posts_
* `!rate` - query the Twitter API rate limit stats
* `!be paranoid` - enable "paranoid mode", to avoid accidentaly posting to Twitter
* `!help` or `/msg passerd-bot help` - Show help and other commands


I need help!
------------

Feel free to contact the author at: [ehabkost@raisama.net](mailto:ehabkost@raisama.net).
