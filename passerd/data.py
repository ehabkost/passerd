#!/usr/bin/env python
#
# Passerd - An IRC server as a gateway to Twitter
#
# Persistent state & settings module
#
# Author: Eduardo Habkost <ehabkost@raisama.net>
#
# Copyright (c) 2009 Eduardo Pereira Habkost <ehabkost@raisama.net>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from sqlalchemy import create_engine, Table, Column, Integer, String, MetaData, ForeignKey, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relation, backref, sessionmaker
from sqlalchemy.orm.exc import NoResultFound



DEBUG=True

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    __table_args__ = (
        UniqueConstraint('twitter_login'),
        {}
    )
    id = Column(Integer, primary_key=True)
    twitter_login = Column(String)
    password_sha255 = Column(String)

class UserVar(Base):
    __tablename__ = 'user_vars'
    __table_args__ = (
        UniqueConstraint('user_id','name'),
        {}
    )
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    user = relation(User, backref='variables')
    name = Column(String)
    value = Column(String)


class TwitterUserData(Base):
    """Cache of twitter user information"""
    __tablename__ = 'twitter_users'
    twitter_id = Column(Integer, primary_key=True)
    twitter_screen_name = Column(String)
    twitter_name = Column(String)


class DataStore:
    def __init__(self, url):
        self.engine = create_engine(url, echo=DEBUG)
        self.session = sessionmaker(bind=self.engine)()

    def create_tables(self):
        Base.metadata.create_all(self.engine)


    def query(self, *args, **kwargs):
        return self.session.query(*args, **kwargs)

    def new_user(self, login):
        u = User(twitter_login=login)
        self.session.add(u)
        self.session.commit()
        return u

    def get_user(self, login, create=False):
        try:
            return self.session.query(User).filter_by(twitter_login=login).one()
        except NoResultFound:
            if not create:
                return None
            return self.new_user(login)

    def _var(self, user, var):
        return self.session.query(UserVar).filter_by(user_id=user.id, name=var).scalar()

    def get_var(self, user, var):
        v = self._var(user, var)
        if v is None:
            return None
        return v.value

    def set_var(self, user, var, value):
        v = self._var(user, var)
        if v is None:
            v = UserVar(user_id=user.id, name=var, value=value)
            self.session.add(v)
        else:
            v.value = value
        self.session.commit()

__all__ = ['DataStore', 'TwitterUserData']

if __name__ == '__main__':
    import logging, sys
    #logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
    s = DataStore('sqlite:///%s' % (sys.argv[1]))
    s.create_tables()
    u = s.get_user('foo', create=True)
    print 'bar1:',s.get_var(u, 'bar')
    s.set_var(u, 'bar', 'baz')
    print 'bar2:',s.get_var(u, 'bar')
    s.set_var(u, 'bar', 'baz2')
    print 'bar3:',s.get_var(u, 'bar')
