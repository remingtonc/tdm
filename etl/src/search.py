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
"""Load a search database with TDM data for fast/efficient searches
from search web interface. Uses ElasticSearch.
"""
import logging
from elasticsearch import Elasticsearch
from elasticsearch.helpers import streaming_bulk
from elasticsearch.exceptions import TransportError

DATAPATH_QUERY = """
    SELECT
        dp.data_path_id AS dp_key,
        dp.machine_id AS dp_machine_id,
        dp.human_id AS dp_human_id,
        dp.description AS dp_description,
        dp.is_leaf AS dp_is_leaf,
        dp.is_configurable AS dp_is_configurable,
        dml.data_model_language_id AS dml_key,
        dml.name AS dml_name,
        dm.data_model_id AS dm_key,
        dm.name AS dm_name,
        dm.revision AS dm_revision,
        r.release_id AS release_key,
        r.name AS release_name,
        os.os_id AS os_key,
        os.name AS os_name
    FROM data_path dp
    JOIN data_path_source dps ON dps.data_path_id = dp.data_path_id
    JOIN data_model dm ON dm.data_model_id = dps.data_model_id
    JOIN data_model_language dml ON dml.data_model_language_id = dm.data_model_language_id
    LEFT JOIN release_data_model rdm ON rdm.data_model_id = dm.data_model_id
    LEFT JOIN release r ON r.release_id = rdm.release_id
    LEFT JOIN os ON os.os_id = r.os_id
"""

def populate_search(conn, search_host='search:9200', index='datapath', doc_type='doc'):
    logging.getLogger('elasticsearch').setLevel(logging.WARN)
    logging.info('Acquiring DataPaths from TDM...')
    query_iterable = query_all_datapaths(conn)
    logging.info('Setting up ES...')
    es = Elasticsearch(search_host)
    setup_search_db(es, index)
    logging.info('Populating ES with DataPaths...')
    populate_search_db(es, query_iterable, index, doc_type)
    conn.commit()

def query_all_datapaths(conn):
    """Queries TDM and flattens the DataPath structure for our search purposes.
    Streams rows via a server-side cursor since this is ~2M rows.
    """
    cur = conn.cursor('datapath_search_cursor')
    cur.itersize = 1000
    cur.execute(DATAPATH_QUERY)
    columns = [desc[0] for desc in cur.description]
    for row in cur:
        yield dict(zip(columns, row))
    cur.close()

def setup_search_db(es, index):
    """Setup the index in ES for our data. Derived from:
    https://github.com/elastic/elasticsearch-py/blob/master/example/load.py#L17
    https://www.elastic.co/guide/en/elasticsearch/reference/current/analysis.html
    https://www.elastic.co/guide/en/elasticsearch/reference/current/analysis-custom-analyzer.html
    https://www.elastic.co/guide/en/elasticsearch/reference/current/analysis-simplepatternsplit-tokenizer.html
    https://www.elastic.co/guide/en/elasticsearch/reference/current/analysis-snowball-tokenfilter.html
    https://www.elastic.co/guide/en/elasticsearch/reference/current/analysis-unique-tokenfilter.html
    Applies a custom analyzer and provides the machine_id and human_id of the DataPaths as
    fulltext search and as terms for aggregation.
    """
    # Specifies both text and keyword for both fulltext and agg capability
    path_mapping = {
        'type': 'text',
        'analyzer': 'generic_path_analyzer',
        'fields': {
            'keyword': {
                'type': 'keyword'
            }
        }
    }
    index_payload = {
        'settings': {
            'number_of_shards': 1,
            'number_of_replicas': 0,
            'analysis': {
                'analyzer': {
                    'generic_path_analyzer': {
                        'type': 'custom',
                        'tokenizer': 'generic_path_tokenizer',
                        'filter': [
                            'preserve_word_delimiter',
                            'lowercase',
                            'network_synonym'
                        ]
                    }
                },
                'tokenizer': {
                    'generic_path_tokenizer': {
                        'type': 'simple_pattern_split',
                        'tokenizer': '[\.\-\/\:]'
                    }
                },
                'filter': {
                    'preserve_word_delimiter': {
                        'type': 'word_delimiter',
                        'preserve_original': True
                    },
                    'network_synonym': {
                        'type': 'synonym',
                        'expand': True,
                        'synonyms': [
                            'optic, transceiver',
                            'intf, interface, if, int',
                            'ucast, unicast',
                            'mcast, multicast',
                            'pkt, packet',
                            'in, inbound'
                        ]
                    }
                }
            }
        },
        'mappings': {
            'doc': {
                'properties': {
                    'dp_key': {'type': 'keyword'},
                    'dp_machine_id': path_mapping,
                    'dp_human_id': path_mapping,
                    'dp_description': {'type': 'text', 'analyzer': 'snowball'},
                    'dp_is_leaf': {'type': 'boolean'},
                    'dp_is_configurable': {'type': 'boolean'},
                    'dml_key': {'type': 'keyword'},
                    'dml_name': {'type': 'keyword'},
                    'dm_key': {'type': 'keyword'},
                    'dm_name': {'type': 'keyword'},
                    'dm_revision': {'type': 'keyword'},
                    'release_key': {'type': 'keyword'},
                    'release_name': {'type': 'keyword'},
                    'os_key': {'type': 'keyword'},
                    'os_name': {'type': 'keyword'}
                }
            }
        }
    }
    try:
        es.indices.create(
            index=index,
            body=index_payload,
        )
    except TransportError as e:
        if e.error == 'index_already_exists_exception':
            logging.info('Index already exists in ES!')
        else:
            logging.exception('Error when creating index in ES!')

def populate_search_db(es, query_iterable, index, doc_type):
    """Populate ElasticSearch with the flattened DataPaths.
    Derived from https://github.com/elastic/elasticsearch-py/blob/master/example/load.py#L102
    """
    def iter_add_id(iterable):
        for counter, element in enumerate(iterable):
            element['_id'] = counter
            yield element
    for ok, result in streaming_bulk(
            es,
            iter_add_id(query_iterable),
            index=index,
            doc_type=doc_type,
            request_timeout=None
        ):
        action, result = result.popitem()
        doc_id = '/%s/%s/%s' % (index, doc_type, result['_id'])
        if not ok:
            logging.error('Failed to %s document %s: %r' % (action, doc_id, result))
