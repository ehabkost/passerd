"""
Random utility functions

`htmlentitydecode` based on example from:
    http://wiki.python.org/moin/EscapingHtml
"""


import re
from htmlentitydefs import name2codepoint

def htmlentitydecode(s):
    s = re.sub(
        '&(%s);' % '|'.join(name2codepoint), 
        lambda m: unichr(name2codepoint[m.group(1)]), s)
    s = re.sub('&#([0-9]+);', lambda m: unichr(int(m.group(1))), s)
    return s

def undo_xss_escaping(s):
    # undo the '<' and '>' escaping done by Twitter
    return s.replace('&lt;', '<').replace('&gt;', '>')

def full_entity_decode(s):
    """Undo the stupid entity encoding done by Twitter

    '>' and '<' are entity-encoded twice!
    """
    return undo_xss_escaping(htmlentitydecode(s))

__all__ = ["htmlentitydecode", "undo_xss_escaping", "full_entity_decode"]
