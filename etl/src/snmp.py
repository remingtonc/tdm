#!/usr/bin/env python3
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
import json
import shutil
from concurrent.futures import ProcessPoolExecutor
from pysmi.reader import FileReader
from pysmi.searcher import AnyFileSearcher, StubSearcher
from pysmi.writer import FileWriter
from pysmi.parser import SmiV1CompatParser
from pysmi.codegen import JsonCodeGen
from pysmi.compiler import MibCompiler
from pysmi import error
from pysmi import debug
import logging


# RFC2578 section 7.1 base SMI types, spelled exactly as static.py seeds them
# into the `data_type` table. pysmi (pinned to 0.3.4, see Pipfile) emits these
# names verbatim in a compiled MIB's "syntax"/"type" fields, except Bits.
SMI_BASE_TYPES = {
    'INTEGER', 'OCTET STRING', 'OBJECT IDENTIFIER', 'BITS',
    'Integer32', 'IpAddress', 'Counter32', 'Gauge32', 'TimeTicks',
    'Opaque', 'Counter64', 'Unsigned32',
}
SMI_TYPE_ALIASES = {
    'Bits': 'BITS',
}


def resolve_base_type(type_name, type_index, _seen=None):
    """Walk a chain of TEXTUAL CONVENTION / type declarations back to the
    RFC2578 base type `type_name` ultimately derives from, using a global
    {type name: immediate parent type name} index built across every
    compiled MIB module. Returns None if the chain can't be resolved (the
    defining module was never compiled, e.g. some SNMPv2-TC types).
    """
    canonical = SMI_TYPE_ALIASES.get(type_name, type_name)
    if canonical in SMI_BASE_TYPES:
        return canonical
    if _seen is None:
        _seen = set()
    if type_name in _seen:
        return None
    _seen.add(type_name)
    parent_name = type_index.get(type_name)
    if not parent_name:
        return None
    return resolve_base_type(parent_name, type_index, _seen)


def create_oid_dict(oid_data, type_index):
    oid = {}
    oidname = oid_data['oid']
    oid[oidname] = {}
    oid[oidname]['oid'] = oid_data['oid']
    oid[oidname]['name'] = oid_data['name']
    if ('description') in oid_data:
        oid[oidname]["description"] = oid_data['description']
    else:
        oid[oidname]["description"] = ''

    if ('syntax') in oid_data:
        oid[oidname]['dataType'] = oid_data['syntax']['type']
        oid[oidname]['dataTypeBase'] = resolve_base_type(
            oid_data['syntax']['type'], type_index
        ) or ''
    else:
        oid[oidname]['dataType'] = ''
        oid[oidname]['dataTypeBase'] = ''
    return oid


# Populated once per worker process by _init_json_transform_worker, so
# transform_json_to_new's ProcessPoolExecutor doesn't repickle type_index
# (built from every compiled MIB) on every one of the ~1600 per-file tasks.
_worker_local_json_dir = None
_worker_new_json_dir = None
_worker_type_index = None


def _init_json_transform_worker(local_json_dir, new_json_dir, type_index):
    global _worker_local_json_dir, _worker_new_json_dir, _worker_type_index
    _worker_local_json_dir = local_json_dir
    _worker_new_json_dir = new_json_dir
    _worker_type_index = type_index


def _transform_one_json(filename):
    file_path = os.path.join(_worker_local_json_dir, filename)
    with open(file_path, 'r', encoding='utf8') as f:
        data = json.load(f)
    newdict = {}
    for i in data:
        if ('oid') in data[i]:
            newdict.update(create_oid_dict(data[i], _worker_type_index))
    new_file_path = os.path.join(_worker_new_json_dir, filename)
    with open(new_file_path, 'w') as f:
        json.dump(newdict, f, indent=4)


class SNMPPopulator:
    source = None
    ref = None
    repo_subdir = None
    local_repo_dir = None
    local_mib_dir = None
    local_json_dir = None
    new_json_dir = None
    donotdo_mibs = []
    exclude_mibs = []

    def __init__(self, source='https://github.com/cisco/cisco-mibs.git', ref='main', repo_subdir='v2',
                 local_repo_dir='mib_repo/', local_mib_dir='mibs/', local_json_dir='mibjson/',
                 new_json_dir='newmibjson/', abs_local_dir=False
                 ):
        self.source = source
        self.ref = ref
        self.repo_subdir = repo_subdir
        local_dir = os.path.dirname(os.path.abspath(__file__))
        if not abs_local_dir:
            self.local_repo_dir = os.path.join(local_dir, local_repo_dir)
            self.local_mib_dir = os.path.join(local_dir, local_mib_dir)
            self.local_json_dir = os.path.join(local_dir, local_json_dir)
            self.new_json_dir = os.path.join(local_dir, new_json_dir)
        else:
            self.local_repo_dir = local_repo_dir
            self.local_mib_dir = local_mib_dir
            self.local_json_dir = local_json_dir
            self.new_json_dir = new_json_dir

    def acquire_source(self):
        """Clone (or update) the Cisco MIBs repo into local_repo_dir."""
        if os.path.exists(os.path.join(self.local_repo_dir, '.git')):
            logging.debug('MIB repo exists! Pulling latest.')
            os.system('cd %s && git pull' % self.local_repo_dir)
        else:
            logging.debug('Cloning MIB repo for first time.')
            os.system(
                'git clone --depth 1 --branch %s %s %s'
                % (self.ref, self.source, self.local_repo_dir)
            )
        return os.path.join(self.local_repo_dir, self.repo_subdir)

    def download_mibs(self, specific_mibs=None, exclude_mibs=[], refresh=False):
        logging.info(
            'Acquiring MIBs from %s (%s/%s) to %s',
            self.source, self.ref, self.repo_subdir, self.local_mib_dir
        )
        source_dir = self.acquire_source()
        if specific_mibs is None:
            mib_filenames = os.listdir(source_dir)
        else:
            mib_filenames = specific_mibs.copy()
        num_mibs = len(mib_filenames)
        logging.debug('%i MIBs to copy.', num_mibs)
        if not os.path.isdir(self.local_mib_dir):
            os.makedirs(self.local_mib_dir)
        counter = 1
        for filename in mib_filenames:
            try:
                filename.rindex('.my', -3)
            except:
                logging.debug('Not a MIB.')
                counter += 1
                continue
            logging.debug('MIB %i/%i: %s', counter, num_mibs, filename)
            if filename in exclude_mibs:
                logging.debug('Excluding.')
                counter += 1
                continue
            local_path = os.path.join(self.local_mib_dir, filename)
            if not refresh and os.path.isfile(local_path):
                logging.debug('Skipping.')
                counter += 1
                continue
            shutil.copyfile(os.path.join(source_dir, filename), local_path)
            counter += 1
        logging.info('Finished acquiring MIBs.')
        return True

    def transform_mibs_to_json(self, specific_mibs: object = None, exclude_mibs: object = []) -> object:
        mib_names = []
        if specific_mibs is None:
            for filename in os.listdir(self.local_mib_dir):
                try:
                    filename.rindex('.my', -3)
                except:
                    continue
                mib_names.append(filename)
        else:
            mib_names = specific_mibs.copy()
        logging.info('Compiling MIBs to JSON.')
        try:
            mib_compiler = MibCompiler(
                SmiV1CompatParser(),
                JsonCodeGen(),
                FileWriter(self.local_json_dir).setOptions(suffix='.json')
            )
            mib_compiler.addSources(FileReader(self.local_mib_dir, recursive=True))
            mib_stubs = JsonCodeGen.baseMibs
            searchers = [AnyFileSearcher(self.local_json_dir).setOptions(exts=['.json']), StubSearcher(*mib_stubs)]
            mib_compiler.addSearchers(*searchers)
            if not os.path.isdir(self.local_json_dir):
                os.makedirs(self.local_json_dir)
            processed = mib_compiler.compile(
                *mib_names,
                **dict(
                    noDeps=False,
                    rebuild=True,
                    genTexts=True,
                    writeMibs=True,
                    ignoreErrors=True
                )
            )
            mib_compiler.buildIndex(
                processed,
                ignoreErrors=True
            )
        except error.PySmiError:
            logging.error('ERROR: %s', str(sys.exc_info()[1]))
            sys.exit(1)
        logging.info('Finished compiling MIBs to JSON.')

    def build_type_index(self):
        """Scan every compiled MIB JSON file and build a global map of
        type-declaration name -> immediate parent type name. A leaf's
        declared SYNTAX type is often a TEXTUAL CONVENTION defined in a
        different module than the one being transformed, so resolving it
        down to an RFC2578 base type needs this cross-module index.
        """
        type_index = {}
        for filename in os.listdir(self.local_json_dir):
            if not filename.endswith('.json'):
                continue
            file_path = os.path.join(self.local_json_dir, filename)
            with open(file_path, 'r', encoding='utf8') as f:
                try:
                    mib_json = json.load(f)
                except json.JSONDecodeError:
                    continue
            for name, entry in mib_json.items():
                if not isinstance(entry, dict) or entry.get('class') not in ('type', 'textualconvention'):
                    continue
                parent = entry.get('type')
                parent_name = parent.get('type') if isinstance(parent, dict) else parent
                if parent_name and name not in type_index:
                    type_index[name] = parent_name
        return type_index

    def transform_json_to_new(self, specific_mibs=None):
        json_names = []
        logging.info(
            'Reorganizing JSON MIBs and writing to %s',
            self.new_json_dir
        )
        if specific_mibs is None:
            for filename in os.listdir(self.local_json_dir):
                try:
                    filename.rindex('.json', -5)
                except:
                    continue
                json_names.append(filename)
        else:
            json_names = specific_mibs.copy()
        num_json = len(json_names)
        logging.debug('%i JSON to transform.', num_json)
        if not os.path.isdir(self.new_json_dir):
            os.makedirs(self.new_json_dir)
        type_index = self.build_type_index()
        with ProcessPoolExecutor(
            initializer=_init_json_transform_worker,
            initargs=(self.local_json_dir, self.new_json_dir, type_index)
        ) as pool:
            for counter, _ in enumerate(pool.map(_transform_one_json, json_names), start=1):
                logging.debug('JSON %i/%i transformed.', counter, num_json)

    def resolve_data_type_id(self, cur, dml_id, base_type_name, dt_cache):
        """Look up the data_type row for a resolved RFC2578 base type name.
        Returns None (and does not create anything) if it isn't already
        seeded in the table by static.py -- e.g. because the type couldn't
        be resolved down to a base type in the first place.
        """
        if base_type_name in dt_cache:
            return dt_cache[base_type_name]
        cur.execute(
            'SELECT data_type_id FROM data_type WHERE data_model_language_id = %s AND name = %s',
            (dml_id, base_type_name)
        )
        row = cur.fetchone()
        data_type_id = row[0] if row else None
        dt_cache[base_type_name] = data_type_id
        return data_type_id

    def parse_json_to_db(self, conn):
        json_filenames = []
        for filename in os.listdir(self.new_json_dir):
            try:
                filename.rindex('.json', -5)
            except:
                continue
            json_filenames.append(filename)
        if not json_filenames:
            logging.error('No files to parse into db!')
            return
        cur = conn.cursor()
        cur.execute(
            'SELECT data_model_language_id FROM data_model_language WHERE name = %s',
            ('SMI',)
        )
        dml_id = cur.fetchone()[0]
        oid_cache = {}
        dt_cache = {}
        for filename in json_filenames:
            model_name = filename[:-5]
            """TODO: THIS IS BARE MINIMUM
            content is the RAW MIB content, not JSON.
            parsed_checksum is md5 checksum of content.
            revision is a field that is in the JSON, but not super easy to see..e.g.
            "ciscoWirelessTextualConventions": {
                "name": "ciscoWirelessTextualConventions",
                "oid": "1.3.6.1.4.1.9.9.137",
                "class": "moduleidentity",
                "revisions": [
                {
                    "revision": "2000-04-03 00:00",
                    "description": "Added TEXTUAL-CONVENTIONs for CwrRfType CwrFixedPointScale CwrFixedPointPrecison CwrFixedPointValue P2mpSnapshotAttribute CwrPercentageValue CwrRfFreqRange CwrUpdateTime Modified P2mpRadioSignalAttribute"
                }
                ],
                "lastupdated": "200004030000Z",
                "organization": "Cisco Systems, Inc.",
                "contactinfo": " Cisco Systems Customer Service Postal: 170 W Tasman Drive San Jose, CA 95134 USA Tel: +1 800 553-NETS E-mail: wireless-nms@cisco.com",
                "description": "This module defines textual conventions used in Cisco Wireless MIBs."
            },
            revision is the latest revision, we can't know revision content without having each revision sequentially;
            revision column is NOT NULL so it's left as '' until this is resolved.
            """
            cur.execute(
                'INSERT INTO data_model (data_model_language_id, name, revision) VALUES (%s, %s, %s) '
                'RETURNING data_model_id',
                (dml_id, model_name, '')
            )
            dm_id = cur.fetchone()[0]
            mib_json = None
            file_path = os.path.join(self.new_json_dir, filename)
            with open(file_path, 'r') as json_fd:
                mib_json = json.load(json_fd)
            if not mib_json:
                logging.error('Nothing in %s', filename)
                continue
            for oid, oid_info in mib_json.items():
                """ TODO: THIS IS BARE MINIMUM
                is_leaf should actually check if dataType is a primitive data type defined by SMI/MIB specs,
                rather than just whether a SYNTAX was declared at all.
                """
                path_id = None
                if oid in oid_cache.keys():
                    logging.error('Duplicate oid %s from %s in %s!', oid, oid_cache[oid]['model'], model_name)
                    path_id = oid_cache[oid]['id']
                else:
                    data_type_id = None
                    if oid_info.get('dataTypeBase'):
                        data_type_id = self.resolve_data_type_id(
                            cur, dml_id, oid_info['dataTypeBase'], dt_cache
                        )
                    cur.execute(
                        'INSERT INTO data_path (machine_id, human_id, description, is_leaf, data_type_id) '
                        'VALUES (%s, %s, %s, %s, %s) RETURNING data_path_id',
                        (
                            oid,
                            oid_info['name'],
                            oid_info['description'],
                            True if oid_info['dataType'] else False,
                            data_type_id
                        )
                    )
                    path_id = cur.fetchone()[0]
                    oid_cache[oid] = {
                        'model': model_name,
                        'id': path_id
                    }
                cur.execute(
                    'INSERT INTO data_path_source (data_path_id, data_model_id) VALUES (%s, %s) '
                    'ON CONFLICT DO NOTHING',
                    (path_id, dm_id)
                )
        conn.commit()
        cur.close()

def populate_snmp(conn):
    """Entry point of populating SNMP data."""
    # TODO: Remove hardcoding of paths.
    # Customized to Docker volume pathing.
    # Files will present locally in etl/cache/... for debugging.
    snmppop = SNMPPopulator(
        local_repo_dir='/data/extract/mib_repo/',
        local_mib_dir='/data/extract/mib/',
        local_json_dir='/data/transform/mibjson/',
        new_json_dir='/data/transform/newmibjson/',
        abs_local_dir=True
    )
    snmppop.download_mibs()
    snmppop.transform_mibs_to_json()
    snmppop.transform_json_to_new()
    snmppop.parse_json_to_db(conn)

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    #debug.setLogger(debug.Debug('reader', 'searcher', 'compiler'))
    snmppop = SNMPPopulator()
    snmppop.download_mibs()
    snmppop.transform_mibs_to_json()
    snmppop.transform_json_to_new()
