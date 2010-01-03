
ENCODINGS = ['utf-8', 'iso-8859-1']

def hooks(fn):
    """Decorator that call beforeFoo() and afterFoo() methods if available"""
    name = fn.func_name
    upname = name[0].upper()+name[1:]
    before = 'before%s' % (upname)
    after = 'after%s' % (upname)
    def call_with_hooks(self, *args, **kwargs):
        if hasattr(self, before):
            getattr(self, before)(*args, **kwargs)
        r = fn(self, *args, **kwargs)
        if hasattr(self, after):
            getattr(self, after)(*args, **kwargs)
        return r
    return call_with_hooks


def try_unicode(s, enc=None):
    for e in [enc]+ENCODINGS:
        if not e:
            continue

        try:
            return unicode(s, e)
        except:
            pass

    # no success:
    raise Exception("couldn't decode message as unicode")

def to_str(s, enc):
    if isinstance(s, unicode):
        return s.encode(enc)
    elif isinstance(s, str):
        return s
    else:
        raise Exception("%r is not str (type: %r)" % (s, type(s)))


