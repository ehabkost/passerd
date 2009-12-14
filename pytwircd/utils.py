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

__all__ = ["htmlentitydecode"]
