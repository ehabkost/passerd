import re

class Dialog:
    """An abstract interface to "user dialogs"
    """
    def __init__(self, *args, **kwargs):
        self.patterns = []
        self.message_func = None
        self.dialog_init(*args, **kwargs)

    def set_message_func(self, fn):
        self.message_func = fn

    def dialog_init(self, *args, **kwargs):
        """Initialize the Dialog object"""
        pass

    def begin(self):
        """Start the dialog"""
        pass

    def wait_for(self, regexp, func, flags=re.I, strip=True):
        if strip:
            filter = (lambda s: s.strip())
        else:
            filter = (lambda s: s)

        self.patterns.insert(0, (filter, re.compile(regexp, flags), func) )

    def recv_message(self, msg):
        for filter,expr,func in self.patterns:
            s = filter(msg)
            m = expr.search(s)
            if m:
                try:
                    func(msg, m)
                except Exception,e:
                    self.message("An error has occurred. Sorry. -- %s" % (e))
                return
        self.message("Sorry, I don't know what you mean")

    def message(self, msg):
        """Send a message to the user"""
        if self.message_func is None:
            raise NotImplementedError("Dialog.message: not message_func set")
        return self.message_func(msg)


class CommandDialog(Dialog):
    """A dialog for simple 'command args' commands"""
    def __init__(self, *args, **kwargs):
        self.subdialogs = []
        self.commands = {}
        self.cmd_prefix = ''
        Dialog.__init__(self, *args, **kwargs)
        self.add_alias('?', 'help')

    def _set_subdialog_prefix(self, cmd, dialog):
        dialog.set_cmd_prefix('%s%s ' % (self.cmd_prefix, cmd.upper()))

    def set_cmd_prefix(self, prefix):
        """Set prefix for command examples on help messages

        Useful for the "!command" format or for subdialogs
        """
        self.cmd_prefix = prefix
        for cmd,sd in self.subdialogs:
            self._set_subdialog_prefix(cmd, sd)

    def unknown_command(self, cmd, args):
        #TODO: show help
        self.message("Sorry, I don't get it. Type '%sHELP' for available commands" % (self.cmd_prefix))

    def add_command(self, cmd, fn):
        self.commands[cmd.lower()] = fn

    def add_alias(self, alias, cmd):
        self.add_command(alias, self._command_fn(cmd))

    def _command_fn(self, cmd):
        fn = getattr(self, 'command_%s' % (cmd.lower()), None)
        if fn is None:
            fn = self.commands.get(cmd.lower())
        return fn

    def add_subdialog(self, cmd, dialog, short_help):
        def doit():
            self.subdialogs.append( (cmd, dialog) )

            dialog.set_message_func(self.message)
            self._set_subdialog_prefix(cmd, dialog)
            self.add_command(cmd, handle_cmd)
            setattr(self, 'help_%s' % (cmd), show_help)
            setattr(self, 'shorthelp_%s' % (cmd), short_help)

        def show_help(args):
            dialog.show_help('%s: ' % (cmd.upper()), args)

        def handle_cmd(args):
            if not args:
                args = ''
            dialog.recv_message(args)

        doit()

    def split_args(self, s):
        s = s.lstrip()
        parts = s.split(' ',1)
        cmd = parts[0]
        if len(parts) > 1:
            args = parts[1]
        else:
            args = None
        return cmd,args

    def _short_help(self, cmd):
        sh = getattr(self, 'shorthelp_%s' % (cmd.lower()), None)
        if sh:
            return '%s - %s' % (cmd.upper(), sh)
        else:
            return None

    def _long_help(self, cmd, args):
        fn = getattr(self, 'help_%s' % (cmd.lower()), None)
        if fn:
            return fn(args)
        sh = self._short_help(cmd)
        if sh:
            self.message(sh)
            return
        self.message("Unknown help topic: %s" % (cmd))

    def show_help(self, prefix, args):
        if args:
            cmd,rest = self.split_args(args)
            return self._long_help(cmd, rest)

        topics = []
        for a in dir(self):
            if a.startswith('shorthelp_'):
                _,t = a.split('_',1)
                topics.append(t)
        topics.sort()
        self.message('%sAvailable topics/commands:' % (prefix))
        for t in topics:
            self.message(self._short_help(t))
        self.message("Use '%sHELP topic' to get more info on a topic" % (self.cmd_prefix))

    shorthelp_help = 'Show help'
    def command_help(self, args):
        return self.show_help('', args)

    def recv_message(self, msg):
        cmd,args = self.split_args(msg)
        fn = self._command_fn(cmd)
        if fn is None:
            return self.unknown_command(cmd, args)
        return fn(args)



def attach_dialog_to_channel(dialog, chan, bot_user):
    def doit():
        chan.add_msg_notifier(got_chan_msg)
        dialog.set_message_func(send_message)

    def got_chan_msg(ch, sender, msg):
        assert ch is chan
        dialog.recv_message(msg)

    def send_message(msg):
        chan.send_message(bot_user, msg)

    doit()

def attach_dialog_to_bot(dialog, proto, real_user, bot):
    def doit():
        bot.add_msg_notifier(got_msg)
        dialog.set_message_func(send_message)

    def got_msg(u, sender, msg):
        assert u is bot
        dialog.recv_message(msg)

    def send_message(msg):
        proto.send_notice(bot, real_user, msg)

    doit()
