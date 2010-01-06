import re

class Dialog:
    """An abstract interface to "user dialogs"
    """
    def __init__(self, *args, **kwargs):
        self.patterns = []
        self.message_func = None
        self.parent = kwargs.get('parent', None)
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

    def unknown_message(self, msg):
        self.message("Sorry, I don't know what you mean")

    def error_reply(self, msg, e):
        self.message("An error has occurred. Sorry. -- %s" % (e))

    def recv_message(self, msg):
        for filter,expr,func in self.patterns:
            s = filter(msg)
            m = expr.search(s)
            if m:
                try:
                    return func(msg, m)
                except Exception,e:
                    return self.error_reply(msg, e)
        return self.unknown_message(msg)

    def message(self, msg):
        """Send a message to the user"""
        if self.message_func is None:
            raise NotImplementedError("Dialog.message: not message_func set")
        return self.message_func(msg)


# command "importance", to order help system
CMD_IMP_IMPORTANT = 0    # important commands
CMD_IMP_COMMON = 2       # commonly-used commands
CMD_IMP_INTERESTING = 5  # interesting-to-know commands
CMD_IMP_UNIMPORTANT = 6  # usual but not important commands
CMD_IMP_ADVANCED = 8     # advanced commands
CMD_IMP_DEBUGGING = 8    # debugging/development commands

CMD_IMP_ALIAS = 7
CMD_IMP_DEFAULT = CMD_IMP_INTERESTING

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

    def add_alias(self, alias, cmd, imp=CMD_IMP_ALIAS):
        self.add_command(alias, self._command_fn(cmd))
        sh = self._short_help(cmd)
        if sh:
            setattr(self, 'shorthelp_%s' % (alias.lower()), 'Synonym to `%s`: %s' % (cmd, sh))
            setattr(self, 'importance_%s' % (alias.lower()), imp)

    def _command_fn(self, cmd):
        fn = getattr(self, 'command_%s' % (cmd.lower()), None)
        if fn is None:
            fn = self.commands.get(cmd.lower())
        return fn

    def add_subdialog(self, cmd, dialog, short_help=None):
        def show_help(args):
            dialog.show_help('%s: ' % (cmd.upper()), args)

        def handle_cmd(args):
            if not args:
                args = ''
            dialog.recv_message(args)

        self.subdialogs.append( (cmd, dialog) )

        dialog.set_message_func(self.message)
        self._set_subdialog_prefix(cmd, dialog)
        self.add_command(cmd, handle_cmd)
        setattr(self, 'help_%s' % (cmd.lower()), show_help)
        if short_help is None:
            short_help = dialog.get_help_header()
        if short_help:
            setattr(self, 'shorthelp_%s' % (cmd.lower()), short_help)

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
        return getattr(self, 'shorthelp_%s' % (cmd.lower()), None)

    def short_help(self, cmd):
        sh = self._short_help(cmd)
        if sh is None:
            return None

        # command get a full prefix
        if self._command_fn(cmd):
            prefix = self.cmd_prefix
        else:
            prefix = ''
        return '%s%s - %s' % (prefix, cmd.upper(), sh)

    def _long_help(self, cmd, args):
        fn = getattr(self, 'help_%s' % (cmd.lower()), None)
        if fn:
            return fn(args)
        sh = self.short_help(cmd)
        if sh:
            self.message(sh)
            return
        self.message("Unknown help topic: %s" % (cmd))

    def get_help_header(self):
        return getattr(self, 'help_header', None)

    def show_help_header(self, args):
        h = self.get_help_header()
        if h:
            self.message(h)

    def _topic_importance(self, t):
        r = getattr(self, 'importance_%s' % (t.lower()), None)
        if r is None:
            return CMD_IMP_DEFAULT
        else:
            return r

    def help_topics(self):
        r = []
        for a in dir(self):
            if a.startswith('shorthelp_'):
                _,t = a.split('_',1)
                imp = self._topic_importance(t)
                r.append( (imp, t) )
        r.sort()
        return r

    def show_help(self, prefix, args):
        if args:
            cmd,rest = self.split_args(args)
            return self._long_help(cmd, rest)

        topics = []
        commands = []
        for imp,t in self.help_topics():
            if self._command_fn(t):
                commands.append( (imp,t) )
            else:
                topics.append( (imp,t) )

        self.show_help_header(args)
        if commands:
            main = []
            rest = []
            for imp,c in commands:
                if imp <= CMD_IMP_INTERESTING:
                    main.append(c)
                else:
                    rest.append(c)
            if main:
                self.message('%sAvailable commands:' % (prefix))
                for c in main:
                    self.message(self.short_help(c))
            if rest:
                if main: name='Other commands'
                else: name='Available commands'
                pref = self.cmd_prefix
                clist = ' '.join(['%s%s' % (pref,c.upper()) for c in rest])
                self.message("%s: %s" % (name, clist))
        if topics:
            self.message('Other help topics:' % (prefix))
            for t in topics:
                self.message(self.short_help(t))
        self.show_help_footer(args)

    def show_help_footer(self, args):
        pass

    def cmd_syntax_str(self, cmd, args):
        if args: suffix = ' %s' % (args)
        else: suffix = ''
        return 'Usage: %s%s%s' % (self.cmd_prefix, cmd.upper(), suffix)

    def cmd_syntax(self, cmd, args):
        self.message(self.cmd_syntax_str(cmd, args))

    def help_help(self, args):
        self.cmd_syntax('help', 'command-or-topic')
    def command_help(self, args):
        self.show_help('', args)

    def try_msg(self, msg, unknown_fn=None):
        cmd,args = self.split_args(msg)
        fn = self._command_fn(cmd)
        if fn is None:
            return False,(cmd,args)
        return True,fn(args)

    def recv_message(self, msg):
        worked,r = self.try_msg(msg)
        if worked:
            return r
        else:
            cmd,args = r
            return self.unknown_command(cmd, args)


class CommandHelpMixin:
    shorthelp_help = 'Show help'
    importance_help = CMD_IMP_UNIMPORTANT


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

