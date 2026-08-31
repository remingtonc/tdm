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
import os
import sys
from contextlib import contextmanager

import pytest
import psycopg2.extras

# Tests import the `web` package directly (like runserver.py does), so
# web/src must be on sys.path regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import db as db_module

@pytest.fixture
def db_rollback(monkeypatch):
    """Route every `db.cursor()` call (in views.py and in the test itself)
    through one connection/transaction for the duration of the test, then
    roll it back -- so Phase 3 write-path tests can exercise real inserts
    without permanently touching the 871k-row dataset etl/ already loaded.
    """
    conn = db_module.get_connection()

    @contextmanager
    def cursor():
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur

    monkeypatch.setattr(db_module, 'cursor', cursor)
    yield conn
    conn.rollback()
    db_module.put_connection(conn)
