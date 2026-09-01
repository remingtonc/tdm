"""Copyright 2018 Cisco Systems

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
"""Pooled PostgreSQL connection helper for the web app.

Replaces the ad hoc ArangoClient(...) blocks scattered through views.py
with a single place credentials live and connections are handed out.
"""

import json
import logging
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
import psycopg2.pool

def load_config(filename='config.json'):
    with open(filename, 'r') as config_fd:
        return json.load(config_fd)

def create_pool(pg_config, minconn=1, maxconn=10):
    return psycopg2.pool.ThreadedConnectionPool(
        minconn,
        maxconn,
        host=pg_config['host'],
        port=pg_config['port'],
        dbname=pg_config['dbname'],
        user=pg_config['user'],
        password=pg_config['password'],
        options='-c search_path=tdm'
    )

_config = load_config()
_pool = create_pool(_config['postgres'])

def get_connection():
    return _pool.getconn()

def put_connection(conn):
    _pool.putconn(conn)

@contextmanager
def cursor():
    """Acquire a pooled connection, yield a RealDictCursor.

    Commits on success, rolls back on exception, always returns the
    connection to the pool.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_connection(conn)
