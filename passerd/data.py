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

import logging

from sqlalchemy import create_engine, Table, Column, Integer, String, MetaData, ForeignKey, UniqueConstraint
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relation, backref, sessionmaker
from sqlalchemy.orm.exc import NoResultFound

logger = logging.getLogger('passerd.data')

Base = declarative_base()


# poor-man sqlalchemy migration system:
# (I don't want to depend on the availability of the python-migration package)
class DataMigration(Base):
    __tablename__ = 'data_migrations'
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    twitter_id = Column(Integer, unique=True)
    twitter_login = Column(String, unique=True) # legacy field
    oauth_token = Column(String)
    oauth_token_secret = Column(String)
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



MIGRATIONS = []

class Migration:
    def __init__(self, name, func):
        self.name = name
        self.func = func

    def _run(self, session):
        logger.info("running data migration %s", self.name)
        self.func(session)

    def check(self, smaker):
        s = smaker()
        r = s.query(DataMigration).filter_by(name=self.name).first()
        if r is None:
            self._run(s)
            s.add(DataMigration(name=self.name))
            s.commit()

# decorator:
def migration(name):
    def wrap(func):
        m = Migration(name, func)
        MIGRATIONS.append(m)
        return m
    return wrap


def add_column(session, table, column, type):
    """Helper function to easily add a new table column"""
    md = MetaData(bind=session.connection())
    md.reflect(only=[table])

    tb = md.tables[table]
    if tb.columns.has_key(column):
        logger.info("Good: column %s.%s already exists", table, column)
        return

    session.execute('alter table "%s" add column "%s" %s' % (table, column, type))
    session.commit()

## migrations functions:

# (please keep them in the right order)

@migration('twitter_id_col')
def twitter_id_col(s):
    add_column(s, 'users', 'twitter_id', 'INTEGER')

@migration('user_oauth_columns')
def add_oauth_columns(s):
    add_column(s, 'users', 'oauth_token', 'VARCHAR')
    add_column(s, 'users', 'oauth_token_secret', 'VARCHAR')

## end of migration functions


def run_migrations(engine):
    smaker = sessionmaker(bind=engine)
    for m in MIGRATIONS:
        m.check(smaker)

class DataStore:
    def __init__(self, url):
        self.engine = create_engine(url)
        self.session = sessionmaker(bind=self.engine)()

    def create_tables(self):
        Base.metadata.create_all(self.engine)
        run_migrations(self.engine)


    def query(self, *args, **kwargs):
        return self.session.query(*args, **kwargs)

    def new_user(self, twitter_id, screen_name):
        u = User(twitter_id=twitter_id, twitter_login=screen_name)
        self.session.add(u)
        self.session.commit()
        return u

    def get_user(self, twitter_id, screen_name, create=False):
        u = self.session.query(User).filter_by(twitter_id=twitter_id).first()
        if u is not None:
            return u

        # look for old screen_name-based data:
        u = self.session.query(User).filter_by(twitter_login=screen_name).first()
        if u is not None:
            logger.info("Converting old user data: screen_name: %s, id: %d" % (screen_name, twitter_id))
            # old data. update it to use the Twitter user ID
            u.twitter_id = twitter_id
            self.session.commit()
            return u

        # not found:
        if not create:
            return None
        return self.new_user(twitter_id, screen_name)

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

    def commit(self):
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
