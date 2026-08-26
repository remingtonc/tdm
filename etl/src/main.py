#!/usr/bin/env python
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
"""The entry point of the TDM ETL process.
This file initializes the ETL process and calls all
necessary functionality.
"""

import logging
import json
import socket
import time
import argparse
from urllib.parse import urlparse
import psycopg2
from models import create_schema, schema_exists
from static import populate_static
from yang import populate_yang
from snmp import populate_snmp
from search import populate_search

def load_config(filename='config.json'):
    config = None
    with open(filename, 'r') as config_fd:
        config = json.load(config_fd)
    return config

def await_url(url, interval=3):
    """Await a certain URL to be open.
    url expects a port parameter in url string.
    Adapted from:
    http://code.activestate.com/recipes/576655-wait-for-network-service-to-appear/
    """
    url_attrs = urlparse(url)
    sock = socket.socket()
    connected = False
    while not connected:
        try:
            sock.connect((url_attrs.hostname, url_attrs.port))
        except Exception:
            time.sleep(interval)
        else:
            sock.close()
            connected = True

def connect(pg_config):
    """Connect to Postgres, retrying until it accepts connections."""
    conn = None
    while conn is None:
        try:
            conn = psycopg2.connect(
                host=pg_config['host'],
                port=pg_config['port'],
                dbname=pg_config['dbname'],
                user=pg_config['user'],
                password=pg_config['password']
            )
        except psycopg2.OperationalError:
            time.sleep(3)
    return conn

def main():
    """Entry point."""
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    args = setup_args()
    logging.info('Loading configuration.')
    config = load_config()
    pg_config = config['postgres']
    logging.info('Awaiting DBMS availability.')
    await_url('postgres://%s:%s' % (pg_config['host'], pg_config['port']))
    logging.info('Awaiting DBMS connectivity.')
    conn = connect(pg_config)
    logging.info('Checking database schema.')
    created = not schema_exists(conn)
    if not created:
        logging.error('TDM schema already exists! Not overwriting.')
    else:
        logging.info('Creating database schema.')
        create_schema(conn)
        logging.info('Populating static data.')
        populate_static(conn)
        logging.info('Populating MIB data.')
        populate_snmp(conn)
        logging.info('Populating YANG data.')
        populate_yang(conn)
    if created or args.stage == 'search':
        logging.info('Awaiting Search availability.')
        await_url(config['search']['searchURL'])
        logging.info('Populating search database with parsed data.')
        populate_search(conn, config['search']['searchURL'])
    conn.close()
    logging.info('ETL process complete!')

def setup_args():
    parser = argparse.ArgumentParser(
        description="TDM ETL"
    )
    parser.add_argument('--stage',
        nargs='?',
        help='None | search',
        default=None
    )
    return parser.parse_args()

if __name__ == '__main__':
    main()
