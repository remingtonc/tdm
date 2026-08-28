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
"""Load YANG models in to the database representation
of OS/Releases/DataModels/DataPaths/DataTypes. Heavy parsing.
Streams one version at a time (see YANGBase.parse_versions) so a whole
OS's parsed data is never held in memory at once.
"""
import os
import json
import logging
from .yang_base import YANGBase

"""Commented out releases have model bugs.
TODO: Resolve model bugs.
TODO: Move to a configuration file somehow.
"""
os_version_folder_map = {
    'nx': {
        '7.0(3)F1(1)': '7.0-3-F1-1',
        '7.0(3)F2(1)': '7.0-3-F2-1',
        '7.0(3)F2(2)': '7.0-3-F2-2',
        '7.0(3)I5(1)': '7.0-3-I5-1',
        '7.0(3)I5(2)': '7.0-3-I5-2',
        '7.0(3)I6(1)': '7.0-3-I6-1',
        '7.0(3)I6(2)': '7.0-3-I6-2',
        '7.0(3)I7(1)': '7.0-3-I7-1',
        '7.0(3)I7(2)': '7.0-3-I7-2',
        '7.0(3)I7(3)': '7.0-3-I7-3',
        '7.0(3)I7(4)': '7.0-3-I7-4',
        '9.2(1)': '9.2-1',
        '9.2(2)': '9.2-2',
        '9.2(3)': '9.2-3',
        '9.2(4)': '9.2-4',
        '9.3(1)': '9.3-1',
        '9.3(2)': '9.3-2',
        '9.3(3)': '9.3-3',
        '9.3(4)': '9.3-4',
        '9.3(5)': '9.3-5'
    },
    'xe': {
        '16.3.1': '1631',
        '16.3.2': '1632',
        '16.4.1': '1641',
        '16.5.1': '1651',
        '16.6.1': '1661',
        '16.6.2': '1662',
        '16.7.1': '1671',
        '16.8.1': '1681',
        '16.9.1': '1691',
        '16.9.3': '1693',
        '16.10.1': '16101',
        '16.11.1': '16111',
        '16.12.1': '16121',
        '17.1.1': '1711',
        '17.2.1': '1721'
    },
    'xr': {
#        '5.3.0': '530',
#        '5.3.1': '531',
#       '5.3.2': '532',
#        '5.3.3': '533',
#        '5.3.4': '534',
#        '6.0.0': '600',
#        '6.0.1': '601',
#        '6.0.2': '602',
#        '6.1.1': '611',
#        '6.1.2': '612',
#        '6.1.3': '613',
#        '6.2.1': '621',
#        '6.2.2': '622', # Deprecating stale OpenConfig
        '6.3.1': '631',
        '6.3.2': '632',
        '6.4.1': '641',
        '6.4.2': '642',
        '6.5.1': '651',
        '6.5.2': '652',
        '6.5.3': '653',
        '6.6.2': '662',
        '7.0.1': '701',
        '7.0.2': '702',
        '7.1.1': '711'
    }
}

# TODO: Create common functionality for this mapping.
os_map = {
    'nx': 'NX-OS',
    'xe': 'IOS XE',
    'xr': 'IOS XR'
}

def acquire_source():
    """Acquire the YANG models in to the
    /data/extract/yang location. Relies on priori knowledge
    to know where to parse.
    TODO: Generalize a priori knowledge to configuration.
    """
    base_path = '/data/extract/'
    yang_base_path = os.path.join(base_path, 'yang/')
    cisco_yang_base_path = os.path.join(yang_base_path, 'vendor/cisco/')
    if os.path.exists(yang_base_path):
        logging.debug('YANG repo exists! Pulling latest models.')
        os.system('cd %s && git pull' % yang_base_path)
    else:
        logging.debug('Cloning YANG repo for first time.')
        os.system('cd %s && git clone --recursive https://github.com/cisco-ie/yang.git -b fix-ietf-types-cisco' % base_path)
        logging.debug('Cloned to %s.', yang_base_path)
    return cisco_yang_base_path

# dm_cache dedupes data_model INSERTs (unique on name+revision, no ON CONFLICT
# handling) and dt_cache is a small read cache over the fixed data_type table.
# data_path and data_path_source dedup is handled DB-side via ON CONFLICT
# instead of Python-side caches (formerly dp_cache/dm_dp_cache) -- see
# add_data_paths_to_dm.
dm_cache = {}
dt_cache = {}

def get_release_id(cur, os_name, release_name):
    cur.execute(
        'SELECT r.release_id FROM release r JOIN os USING (os_id) '
        'WHERE os.name = %s AND r.name = %s',
        (os_name, release_name)
    )
    row = cur.fetchone()
    if row is None:
        raise KeyError('Release %s %s not found' % (os_name, release_name))
    return row[0]

# TODO: Revise everything beneath this line.
def populate_yang(conn):
    """Entry point of populating YANG data."""
    logging.info('Acquiring YANG models for data extraction.')
    base_model_path = acquire_source()
    cur = conn.cursor()
    cur.execute(
        "SELECT data_model_language_id FROM data_model_language WHERE name = 'YANG'"
    )
    dml_id = cur.fetchone()[0]
    for os_key, version_map in os_version_folder_map.items():
        logging.info('Transforming %s data.', os_map[os_key])
        yang_base = YANGBase(base_model_path, os_key, version_map)
        for version, modules in yang_base.parse_versions():
            logging.info('Loading %s %s data.', os_map[os_key], version)
            release_id = get_release_id(cur, os_map[os_key], version)
            add_version_modules(conn, cur, dml_id, release_id, modules)

def add_version_modules(conn, cur, dml_id, release_id, modules):
    """Add the DataModels to the corresponding OS/Release."""
    for module_name, revisions in modules.items():
        parent_dm_id = None
        for revision in sorted(revisions.keys()):
            module = revisions[revision]
            dm_key = '%s+%s' % (module_name, revision)
            dm_id = dm_cache.get(dm_key)
            if dm_id is None:
                cur.execute(
                    'INSERT INTO data_model (data_model_language_id, name, revision, parent_id) '
                    'VALUES (%s, %s, %s, %s) RETURNING data_model_id',
                    (dml_id, module_name, revision, parent_dm_id)
                )
                dm_id = cur.fetchone()[0]
                dm_cache[dm_key] = dm_id
            cur.execute(
                'INSERT INTO release_data_model (release_id, data_model_id) VALUES (%s, %s) '
                'ON CONFLICT DO NOTHING',
                (release_id, dm_id)
            )
            add_data_paths_to_dm(cur, dm_id, dml_id, module)
            parent_dm_id = dm_id
    conn.commit()

def add_data_paths_to_dm(cur, dm_id, dml_id, module, dp_parent_id=None):
    """Add the parsed DataPaths from the corresponding DataModels.
    data_path is deduped DB-side on the machine_id UNIQUE constraint: ON
    CONFLICT DO UPDATE overwrites the mutable columns with the incoming
    row's values, so whichever revision is processed LAST wins. Callers
    (add_version_modules's sorted(revisions.keys()) loop, and
    os_version_folder_map's oldest-to-newest version ordering) already
    guarantee revisions are processed oldest-to-newest, so "last processed"
    means "most recent revision" -- matching the data_type_id UPDATE below,
    which has always been last-write-wins. data_path_source is deduped the
    same way it always was, via its own ON CONFLICT DO NOTHING on the
    (data_path_id, data_model_id) primary key.
    """
    for _, path_data in module.items():
        path_key = path_data['machine_id']
        cur.execute(
            'INSERT INTO data_path (machine_id, human_id, description, is_leaf, is_configurable, parent_id) '
            'VALUES (%s, %s, %s, %s, %s, %s) '
            'ON CONFLICT (machine_id) DO UPDATE SET '
            'human_id = EXCLUDED.human_id, '
            'description = EXCLUDED.description, '
            'is_leaf = EXCLUDED.is_leaf, '
            'is_configurable = EXCLUDED.is_configurable, '
            'parent_id = EXCLUDED.parent_id '
            'RETURNING data_path_id',
            (
                path_key,
                path_data['xpath'],
                path_data['description'],
                False if path_data['children'] else True,
                path_data['rw'],
                dp_parent_id
            )
        )
        path_id = cur.fetchone()[0]
        cur.execute(
            'INSERT INTO data_path_source (data_path_id, data_model_id) VALUES (%s, %s) '
            'ON CONFLICT DO NOTHING',
            (path_id, dm_id)
        )
        if path_data['primitive_type'] is not None:
            type_key = 'YANG+%s' % path_data['primitive_type']
            type_id = dt_cache.get(type_key)
            if type_id is None:
                cur.execute(
                    'SELECT data_type_id FROM data_type '
                    'WHERE data_model_language_id = %s AND name = %s',
                    (dml_id, path_data['primitive_type'])
                )
                row = cur.fetchone()
                if row is None:
                    logging.error('Could not resolve DataType %s!', type_key)
                    raise KeyError(type_key)
                type_id = row[0]
                dt_cache[type_key] = type_id
            cur.execute(
                'UPDATE data_path SET data_type_id = %s WHERE data_path_id = %s',
                (type_id, path_id)
            )
        if path_data['children']:
            add_data_paths_to_dm(cur, dm_id, dml_id, path_data['children'], path_id)
