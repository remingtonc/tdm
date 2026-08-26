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
"""Schema creation functionality for the PostgreSQL-backed database."""
import os

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'schema.sql')

def schema_exists(conn):
    """Check whether the schema has already been created."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('tdm.os')")
        return cur.fetchone()[0] is not None

def create_schema(conn):
    """Create the schema!"""
    with open(SCHEMA_PATH, 'r') as schema_fd:
        schema_sql = schema_fd.read()
    with conn.cursor() as cur:
        cur.execute(schema_sql)
    conn.commit()
