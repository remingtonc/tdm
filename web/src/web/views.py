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
import json
import csv
import io
from collections import OrderedDict
from datetime import datetime, timezone
import flask
from itertools import chain
from elasticsearch import Elasticsearch
from werkzeug.utils import secure_filename
from . import forms
from . import app
from . import db

@app.route('/')
def index():
    return flask.render_template('index.html')

@app.route('/healthz/db')
def healthz_db():
    with db.cursor() as cur:
        cur.execute('SELECT 1')
        cur.fetchone()
    return flask.jsonify(status='ok')

@app.route('/datapath/matches', methods=['GET'])
def datapath_matches():
    return flask.render_template(
        'matches.html',
        dml_matches=fetch_all_matches()
    )

def fetch_all_matches():
    with db.cursor() as cur:
        cur.execute('SELECT name FROM data_model_language ORDER BY name')
        result = {row['name']: [] for row in cur.fetchall()}
        cur.execute("""
            SELECT DISTINCT dml.name AS dml_name, dp.data_path_id, dp.human_id
            FROM data_model_language dml
            JOIN data_model dm ON dm.data_model_language_id = dml.data_model_language_id
            JOIN data_path_source dps ON dps.data_model_id = dm.data_model_id
            JOIN data_path dp ON dp.data_path_id = dps.data_path_id
            JOIN data_path_match dpm
                ON dpm.data_path_a_id = dp.data_path_id OR dpm.data_path_b_id = dp.data_path_id
            ORDER BY dml.name, dp.human_id
        """)
        for row in cur.fetchall():
            result[row['dml_name']].append({
                'data_path_id': row['data_path_id'],
                'human_id': row['human_id']
            })
    return result

@app.route('/matchmaker')
def matchmaker():
    match_form = forms.MatchForm()
    return flask.render_template('matchmaker.html', match_form=match_form)

@app.route('/datapath/match/<int:data_path_id>', methods=['POST'])
def datapath_match(data_path_id):
    match_form = forms.DataPathMatchForm()
    if match_form.validate_on_submit():
        basepath_key = int(data_path_id)
        matchpath_key = int(match_form.matchpath_key.data)
        author = match_form.author.data.strip()
        annotation = match_form.annotation.data.strip()
        weight = int(match_form.weight.data)
        try:
            map_datapath_single_by_key(basepath_key, matchpath_key, author, weight, annotation)
        except Exception as e:
            flask.flash(getattr(e, 'message', repr(e)))
    else:
        error_msg = ''
        for field, errors in match_form.errors.items():
            error_msg += '<strong>%s</strong><br>%s<br>' % (field, '<br>'.join(errors))
        flask.flash(error_msg)
    return flask.redirect(flask.url_for('datapath_details', data_path_id=int(data_path_id)))

@app.route('/datapath/view/<int:data_path_id>')
def datapath_details(data_path_id):
    match_form = forms.DataPathMatchForm()
    datapath_oses = set()
    for dp_graph in fetch_datapath_os_graph(data_path_id):
        dp_os = dp_graph['os_name']
        if dp_os:
            if dp_graph['os_release']:
                dp_os = '%s - %s' % (dp_os, dp_graph['os_release'])
            datapath_oses.add(dp_os)
    datapath_dmls = set()
    datapath_models = {}
    for dp_graph in fetch_datapath_dml_graph(data_path_id):
        dml_name = dp_graph['dml_name']
        if dml_name:
            datapath_dmls.add(dml_name)
        datamodel_name = dp_graph['datamodel_name']
        if datamodel_name:
            if datamodel_name not in datapath_models.keys():
                datapath_models[datamodel_name] = []
            datapath_models[datamodel_name].append({'revision': dp_graph['datamodel_revision'] or '', 'dml': dml_name})
    return flask.render_template('datapath.html',
        datapath=fetch_datapath(data_path_id),
        datapath_models=datapath_models,
        datapath_oses=datapath_oses,
        datapath_dmls=datapath_dmls,
        datapath_parent=fetch_datapath_parent(data_path_id),
        datapath_children=fetch_datapath_children(data_path_id),
        datapath_datatypes=fetch_datapath_datatype(data_path_id),
        datapath_mappings=fetch_datapath_mappings(data_path_id),
        match_form=match_form
    )

def fetch_datapath_os_graph(data_path_id):
    with db.cursor() as cur:
        cur.execute("""
            SELECT dm.name AS datamodel_name, dm.revision AS datamodel_revision,
                   release.name AS os_release, os.name AS os_name
            FROM data_path_source dps
            JOIN data_model dm ON dm.data_model_id = dps.data_model_id
            JOIN release_data_model rdm ON rdm.data_model_id = dm.data_model_id
            JOIN release ON release.release_id = rdm.release_id
            JOIN os ON os.os_id = release.os_id
            WHERE dps.data_path_id = %s
        """, (data_path_id,))
        return cur.fetchall()

def fetch_datapath_dml_graph(data_path_id):
    with db.cursor() as cur:
        cur.execute("""
            SELECT dm.name AS datamodel_name, dm.revision AS datamodel_revision,
                   dml.name AS dml_name
            FROM data_path_source dps
            JOIN data_model dm ON dm.data_model_id = dps.data_model_id
            JOIN data_model_language dml ON dml.data_model_language_id = dm.data_model_language_id
            WHERE dps.data_path_id = %s
        """, (data_path_id,))
        return cur.fetchall()

def fetch_datapath_mappings(data_path_id):
    with db.cursor() as cur:
        cur.execute("""
            SELECT
                CASE WHEN dpm.data_path_a_id = %(key)s
                     THEN dpm.data_path_b_id ELSE dpm.data_path_a_id END AS data_path_id,
                other.human_id
            FROM data_path_match dpm
            JOIN data_path other ON other.data_path_id =
                CASE WHEN dpm.data_path_a_id = %(key)s
                     THEN dpm.data_path_b_id ELSE dpm.data_path_a_id END
            WHERE dpm.data_path_a_id = %(key)s OR dpm.data_path_b_id = %(key)s
        """, {'key': data_path_id})
        return cur.fetchall()

@app.route('/datapath/direct', methods=['GET', 'POST'])
def datapath_direct():
    direct_form = forms.DirectForm()
    multi_paths = []
    if direct_form.validate_on_submit():
        datapath_id = direct_form.path_id.data.strip()
        direct_dps = fetch_datapath_arbitrary_id(datapath_id)
        if len(direct_dps) > 1:
            flask.flash('Multiple potential DataPaths!', 'warning')
            multi_paths = direct_dps
        elif not direct_dps:
            flask.flash('No matching DataPaths found!', 'warning')
        else:
            return flask.redirect(flask.url_for('datapath_details', data_path_id=direct_dps[0]['data_path_id']), code=303)
    return flask.render_template('datapath_direct.html', direct_form=direct_form, multi_paths=multi_paths)

def fetch_datapath_arbitrary_id(path):
    with db.cursor() as cur:
        cur.execute("""
            SELECT data_path_id, machine_id FROM data_path
            WHERE human_id = %s OR machine_id = %s
        """, (path, path))
        return cur.fetchall()

def fetch_datapath_datatype(data_path_id):
    with db.cursor() as cur:
        cur.execute("""
            SELECT dt.data_type_id, dt.name FROM data_path dp
            JOIN data_type dt USING (data_type_id)
            WHERE dp.data_path_id = %s
        """, (data_path_id,))
        return cur.fetchall()

def fetch_datapath_parent(data_path_id):
    with db.cursor() as cur:
        cur.execute("""
            SELECT data_path_id, human_id FROM data_path
            WHERE data_path_id = (SELECT parent_id FROM data_path WHERE data_path_id = %s)
        """, (data_path_id,))
        return cur.fetchall()

def fetch_datapath_children(data_path_id):
    with db.cursor() as cur:
        cur.execute("""
            SELECT data_path_id, human_id FROM data_path
            WHERE parent_id = %s
            ORDER BY data_path_id
        """, (data_path_id,))
        return cur.fetchall()

def fetch_datapath_models(data_path_id):
    with db.cursor() as cur:
        cur.execute("""
            SELECT dm.name, dm.revision
            FROM data_path_source dps
            JOIN data_model dm ON dm.data_model_id = dps.data_model_id
            WHERE dps.data_path_id = %s
            ORDER BY dm.data_model_id
        """, (data_path_id,))
        return cur.fetchall()

def fetch_datapath(data_path_id):
    with db.cursor() as cur:
        cur.execute('SELECT * FROM data_path WHERE data_path_id = %s', (data_path_id,))
        return cur.fetchone()

def _fetch_given_data_paths(cur, given_dps):
    cur.execute("""
        SELECT data_path_id, human_id, machine_id FROM data_path
        WHERE human_id = ANY(%(dps)s) OR machine_id = ANY(%(dps)s)
    """, {'dps': list(given_dps)})
    return cur.fetchall()

def _resolve_data_path_ids(cur, values):
    """Batch equivalent of resolve_data_path_id: one query for many values.

    Priority matches resolve_data_path_id -- machine_id (unique) wins over
    human_id (not unique) for the same input value.
    """
    values = list(values)
    by_machine_id = {}
    by_human_id = {}
    for row in _fetch_given_data_paths(cur, values):
        if row['machine_id'] is not None:
            by_machine_id.setdefault(row['machine_id'], []).append(row)
        if row['human_id'] is not None:
            by_human_id.setdefault(row['human_id'], []).append(row)
    resolved = {}
    for value in values:
        if value in by_machine_id:
            resolved[value] = by_machine_id[value][0]['data_path_id']
            continue
        matches = by_human_id.get(value, [])
        if not matches:
            raise Exception('Unable to find (machine_id or human_id: %s)!' % value)
        if len(matches) > 1:
            raise Exception('More than one document exists for (machine_id or human_id: %s)!' % value)
        resolved[value] = matches[0]['data_path_id']
    return resolved

def fetch_matches(given_dps):
    results = []
    with db.cursor() as cur:
        for dp in _fetch_given_data_paths(cur, given_dps):
            cur.execute("""
                SELECT other.data_path_id, other.human_id, other.machine_id
                FROM data_path_match dpm
                JOIN data_path other ON other.data_path_id =
                    CASE WHEN dpm.data_path_a_id = %(id)s
                         THEN dpm.data_path_b_id ELSE dpm.data_path_a_id END
                WHERE dpm.data_path_a_id = %(id)s OR dpm.data_path_b_id = %(id)s
                ORDER BY other.human_id
            """, {'id': dp['data_path_id']})
            match_rows = cur.fetchall()
            if match_rows:
                results.append({
                    'data_path_id': dp['data_path_id'],
                    'human_id': dp['human_id'],
                    'machine_id': dp['machine_id'],
                    'matches': match_rows
                })
    results.sort(key=lambda r: r['human_id'] or '')
    return results

@app.route('/matches', methods=['POST'])
def matches():
    match_form = forms.MatchForm()
    if not match_form.validate_on_submit():
        return 'Invalid submission!'
    given_dps = [dp.strip('./,') for dp in match_form.paths.data.split()]
    if not given_dps:
        return 'Nothing specified to match!'
    matches = fetch_matches(given_dps)
    return flask.jsonify(matches)

def _calc_factors(cur, calculation_id):
    cur.execute("""
        SELECT factor.data_path_id, factor.human_id, factor.machine_id
        FROM calculation_input ci
        JOIN data_path factor ON factor.data_path_id = ci.data_path_id
        WHERE ci.calculation_id = %s
    """, (calculation_id,))
    return cur.fetchall()

def _calc_result_dps(cur, calculation_id):
    cur.execute("""
        SELECT result.data_path_id, result.human_id, result.machine_id
        FROM calculation_result cr
        JOIN data_path result ON result.data_path_id = cr.data_path_id
        WHERE cr.calculation_id = %s
    """, (calculation_id,))
    return cur.fetchall()

def _calcs_as_result(cur, dp):
    cur.execute("""
        SELECT calc.calculation_id, calc.name
        FROM calculation_result cr
        JOIN calculation calc USING (calculation_id)
        WHERE cr.data_path_id = %s
    """, (dp['data_path_id'],))
    return [
        {
            'calculation_id': calc['calculation_id'],
            'name': calc['name'],
            'result': {
                'data_path_id': dp['data_path_id'],
                'human_id': dp['human_id'],
                'machine_id': dp['machine_id']
            },
            'factors': _calc_factors(cur, calc['calculation_id'])
        }
        for calc in cur.fetchall()
    ]

def _calcs_as_factor(cur, dp):
    cur.execute("""
        SELECT calc.calculation_id, calc.name
        FROM calculation_input ci
        JOIN calculation calc USING (calculation_id)
        WHERE ci.data_path_id = %s
    """, (dp['data_path_id'],))
    calcs = cur.fetchall()
    results = []
    for calc in calcs:
        result_dps = _calc_result_dps(cur, calc['calculation_id'])
        results.append({
            'calculation_id': calc['calculation_id'],
            'name': calc['name'],
            'result': result_dps[0] if result_dps else None,
            'factors': _calc_factors(cur, calc['calculation_id'])
        })
    return results

def fetch_calculations(given_dps):
    results = []
    with db.cursor() as cur:
        for dp in _fetch_given_data_paths(cur, given_dps):
            as_result = _calcs_as_result(cur, dp)
            as_factor = _calcs_as_factor(cur, dp)
            if as_result or as_factor:
                results.append({
                    'data_path_id': dp['data_path_id'],
                    'human_id': dp['human_id'],
                    'machine_id': dp['machine_id'],
                    'calculations': {'as_result': as_result, 'as_factor': as_factor}
                })
    results.sort(key=lambda r: r['human_id'] or '')
    return results

def fetch_calculations_as_result(given_dps):
    results = []
    with db.cursor() as cur:
        for dp in _fetch_given_data_paths(cur, given_dps):
            calcs = _calcs_as_result(cur, dp)
            if calcs:
                results.append({
                    'data_path_id': dp['data_path_id'],
                    'human_id': dp['human_id'],
                    'machine_id': dp['machine_id'],
                    'calculations': calcs
                })
    results.sort(key=lambda r: r['human_id'] or '')
    return results

def fetch_calculations_as_factor(given_dps):
    results = []
    with db.cursor() as cur:
        for dp in _fetch_given_data_paths(cur, given_dps):
            calcs = _calcs_as_factor(cur, dp)
            if calcs:
                results.append({
                    'data_path_id': dp['data_path_id'],
                    'human_id': dp['human_id'],
                    'machine_id': dp['machine_id'],
                    'calculations': calcs
                })
    results.sort(key=lambda r: r['human_id'] or '')
    return results

@app.route('/calculations_as_result', methods=['POST'])
def calculations_as_result():
    match_form = forms.MatchForm()
    if not match_form.validate_on_submit():
        return 'Invalid submission!'
    given_dps = match_form.paths.data.split()
    if not given_dps:
        return 'Nothing specified to match!'
    calculations = fetch_calculations_as_result(given_dps)
    return flask.jsonify(calculations)

@app.route('/calculations_as_factor', methods=['POST'])
def calculations_as_factor():
    match_form = forms.MatchForm()
    if not match_form.validate_on_submit():
        return 'Invalid submission!'
    given_dps = match_form.paths.data.split()
    if not given_dps:
        return 'Nothing specified to match!'
    calculations = fetch_calculations_as_factor(given_dps)
    return flask.jsonify(calculations)

@app.route('/calculations', methods=['POST'])
def calculations():
    match_form = forms.MatchForm()
    if not match_form.validate_on_submit():
        return 'Invalid submission!'
    given_dps = match_form.paths.data.split()
    if not given_dps:
        return 'Nothing specified to match!'
    calculations = fetch_calculations(given_dps)
    return flask.jsonify(calculations)

# given_collections is client-controlled JSON; only ever resolve it through
# this fixed allow-list, never interpolate it directly into a query.
_COLLECTION_COUNT_TABLES = {
    'DataPath': 'data_path',
    'Release': 'release',
    'DataModel': 'data_model'
}

def fetch_collection_counts(given_collections):
    counts = {}
    with db.cursor() as cur:
        for collection in given_collections:
            table = _COLLECTION_COUNT_TABLES.get(collection)
            if table is None:
                continue
            cur.execute('SELECT COUNT(*) AS count FROM %s' % table)
            counts[collection] = cur.fetchone()['count']
    return counts

@app.route('/collection-counts', methods=['POST'])
def collection_counts():
    requested_counts = flask.request.get_json()
    if not requested_counts:
        return 'Nothing specified to match!'
    collection_counts = fetch_collection_counts(requested_counts)
    return flask.jsonify(collection_counts)

@app.route('/search', methods=['GET'])
def search():
    search_form = construct_search_form()
    return flask.render_template('search.html', search_form=search_form)

@app.route('/search_es', methods=['GET'])
def search_es():
    search_form = construct_search_form(es=True)
    return flask.render_template('search_es.html', search_form=search_form)

@app.route('/api/v1/search', methods=['POST'])
def search_api():
    search_form = construct_search_form()
    if not search_form.validate_on_submit():
        return flask.jsonify(search_form.errors), 400
    filter_os_releases = {}
    for filter_os_release in search_form.oses.data:
        filter_os, filter_release = filter_os_release.split(' - ')
        filter_os = filter_os.strip()
        filter_release = filter_release.strip()
        if filter_os not in filter_os_releases:
            filter_os_releases[filter_os] = []
        filter_os_releases[filter_os].append(filter_release)
    filter_dmls = search_form.dmls.data
    filter_str = search_form.filter_str.data.strip()
    start_index = int(search_form.start_index.data)
    max_return_count = int(search_form.max_return_count.data)
    exclude_config = search_form.exclude_config.data
    only_leaves = search_form.only_leaves.data
    search_query_return = fetch_search_data_paths(
        filter_os_releases, filter_dmls, filter_str,
        exclude_config, only_leaves, start_index, max_return_count
    )
    return flask.jsonify(search_query_return)

@app.route('/api/v1/search/es', methods=['POST'])
def search_deep_api():
    search_form = construct_search_form(es=True)
    if not search_form.validate_on_submit():
        return flask.jsonify(search_form.errors), 400
    filter_os_releases = {}
    for filter_os_release in search_form.oses.data:
        filter_os, filter_release = filter_os_release.split(' - ')
        filter_os = filter_os.strip()
        filter_release = filter_release.strip()
        if filter_os not in filter_os_releases:
            filter_os_releases[filter_os] = []
        filter_os_releases[filter_os].append(filter_release)
    filter_dmls = search_form.dmls.data
    filter_str = search_form.filter_str.data.strip().lower()
    num_results = int(search_form.num_results.data)
    exclude_config = search_form.exclude_config.data
    only_leaves = search_form.only_leaves.data
    search_query_return = fetch_search_data_paths_es(
        filter_os_releases, filter_dmls, filter_str,
        exclude_config, only_leaves, num_results
    )
    return_dict = OrderedDict()
    return_dict['took'] = search_query_return['took']
    return_dict['hits'] = search_query_return['hits']['total']
    return_dict['human_id'] = OrderedDict()
    sorted_human_ids = sorted(search_query_return['aggregations']['human_id']['buckets'], key=lambda k: k['relevance']['value'], reverse=True)
    for human_id in sorted_human_ids:
        curr_human_id = return_dict['human_id'][human_id['key']] = {}
        curr_human_id['relevance'] = human_id['relevance']['value']
        curr_human_id['machine_id'] = {}
        for _key in human_id['dp_key']['buckets']:
            curr_human_id['machine_id'][_key['key']] = _key['machine_id']['buckets'][0]['key']
    # Flask sorts JSON output to improve cacheability, we have disabled this in __init__
    # We should actually restructure this response with relevance as a key instead of relying on ordering
    # in something which is explicitly designated as not requiring ordering (JSON).
    return flask.jsonify(return_dict)

def fetch_search_data_paths_es(filter_os_releases, filter_dmls, filter_str, exclude_config=True, only_leaves=True, num_results=150):
    search_body = {
        'size': 0,
        'sort': [
            '_score'
        ],
        'query': {
            'bool': {
                'must': [
                    {
                        'multi_match': {
                            'query': filter_str,
                            'operator': 'and',
                            'type': 'most_fields',
                            'fields': ['dp_human_id^3', 'dp_machine_id', 'dp_description']
                        }
                    }
                ],
                'filter': []
            }
        },
        'aggs': {
            'human_id': {
                'terms': {
                    'field': 'dp_human_id.keyword',
                    'size': num_results,
                    'order': {
                        'relevance': 'desc'
                    }
                },
                'aggs': {
                    'relevance': {
                        'max': {
                            'script': '_score'
                        }
                    },
                    'dp_key': {
                        'terms': {
                            'field': 'dp_key'
                        },
                        'aggs': {
                            'machine_id': {
                                'terms': {
                                    'field': 'dp_machine_id.keyword'
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    def gen_multi_choice_filter(term, choices):
        return {
            'bool': {
                'minimum_should_match': 1,
                'should': [
                    {
                        'match_phrase': {
                            term: choice
                        }
                    } for choice in choices
                ]
            }
        }
    def gen_boolean_filter(term, state):
        return {
            'match_phrase': {
                term: {
                    'query': state
                }
            }
        }
    if filter_os_releases or filter_dmls or exclude_config or only_leaves:
        query_filter = search_body['query']['bool']['filter']
        if filter_os_releases:
            query_filter.append(
                gen_multi_choice_filter(
                    'os_name',
                    filter_os_releases.keys()
                )
            )
            query_filter.append(
                gen_multi_choice_filter(
                    'release_name',
                    set(
                        chain.from_iterable(
                            filter_os_releases.values()
                        )
                    )
                )
            )
        if filter_dmls:
            query_filter.append(
                gen_multi_choice_filter(
                    'dml_name',
                    filter_dmls
                )
            )
        if exclude_config:
            query_filter.append(
                gen_boolean_filter('dp_is_configurable', False)
            )
        if only_leaves:
            query_filter.append(
                gen_boolean_filter('dp_is_leaf', True)
            )
        else:
            query_filter.append(
                gen_boolean_filter('dp_is_leaf', False)
            )
    client = Elasticsearch('http://search:9200')
    response = client.search(
        index='datapath',
        body=search_body
    )
    return response

def fetch_search_data_paths(filter_os_releases, filter_dmls, filter_str, exclude_config=True, only_leaves=True, start_index=0, max_return_count=10):
    os_release_pairs = tuple(
        (os_name, release_name)
        for os_name, release_names in filter_os_releases.items()
        for release_name in release_names
    )
    if not os_release_pairs or not filter_dmls:
        return {}

    conditions = [
        '(os.name, release.name) IN %(os_release_pairs)s',
        'dml.name = ANY(%(dml_names)s)',
        'dp.is_leaf = %(only_leaves)s'
    ]
    if filter_str:
        conditions.append("dp.search_vector @@ plainto_tsquery('simple', %(filter_str)s)")
    if exclude_config:
        conditions.append('dp.is_configurable = FALSE')
    where_clause = ' AND '.join(conditions)

    search_data_paths_query = f"""
        SELECT os.name AS os_name, release.name AS release_name,
               dml.name AS dml_name, dm.name AS dm_name,
               dp.data_path_id, dp.human_id
        FROM data_path dp
        JOIN data_path_source dps ON dps.data_path_id = dp.data_path_id
        JOIN data_model dm ON dm.data_model_id = dps.data_model_id
        JOIN data_model_language dml ON dml.data_model_language_id = dm.data_model_language_id
        JOIN release_data_model rdm ON rdm.data_model_id = dm.data_model_id
        JOIN release ON release.release_id = rdm.release_id
        JOIN os ON os.os_id = release.os_id
        WHERE {where_clause}
        ORDER BY dp.human_id
        LIMIT %(max_return_count)s OFFSET %(start_index)s
    """
    bind_vars = {
        'os_release_pairs': os_release_pairs,
        'dml_names': list(filter_dmls),
        'filter_str': filter_str,
        'only_leaves': bool(only_leaves),
        'max_return_count': max_return_count,
        'start_index': start_index
    }
    with db.cursor() as cur:
        cur.execute(search_data_paths_query, bind_vars)
        rows = cur.fetchall()

    result = {}
    for row in rows:
        (result.setdefault(row['os_name'], {})
               .setdefault(row['release_name'], {})
               .setdefault(row['dml_name'], {})
               .setdefault(row['dm_name'], [])
               .append({'data_path_id': row['data_path_id'], 'human_id': row['human_id']}))
    return result

def construct_search_form(es=False):
    search_form = None
    if es:
        search_form = forms.SearchFormES()
    else:
        search_form = forms.SearchForm()
    search_form.oses.choices = [(pair, pair) for pair in fetch_os_releases()]
    search_form.dmls.choices = [(pair, pair) for pair in fetch_dmls()]
    return search_form

def fetch_os_releases():
    with db.cursor() as cur:
        cur.execute("""
            SELECT os.name AS os_name, release.name AS release_name
            FROM os
            JOIN release USING (os_id)
            ORDER BY os.name ASC, release.name DESC
        """)
        return ['%s - %s' % (row['os_name'], row['release_name']) for row in cur.fetchall()]

def fetch_dmls():
    with db.cursor() as cur:
        cur.execute('SELECT name FROM data_model_language ORDER BY name')
        return [row['name'] for row in cur.fetchall()]

@app.route('/map-bulk', methods=['GET'])
def html_map_bulk():
    return flask.render_template('map_bulk.html')

@app.route('/map-backup', methods=['GET'])
def html_map_backup():
    return flask.render_template('map_backup.html')

@app.route('/api/v1/datapath/arbitrary-id', methods=['POST'])
def api_datapath_from_arbitrary_id():
    api_req = flask.request.get_json()
    datapaths = fetch_datapath_arbitrary_id(api_req['id'])
    key = None
    error = None
    if not datapaths:
        error = 'No DataPaths found for ID!'
    elif len(datapaths) > 1:
        error = 'Multiple DataPaths found for ID! <a href="%s" target="_blank">Try being more specific with a Machine ID. :)</a>' % flask.url_for('datapath_direct')
    else:
        key = datapaths[0]['data_path_id']
    return flask.jsonify(
        {
            'error': error,
            'key': key
        }
    )

@app.route('/api/v1/map/datapath/bulk', methods=['POST'])
def api_map_bulk():
    """Expects CSV with columns 'First DataPath', 'Second DataPath', 'Author',
    'Annotation' -- the same format produced by /api/v1/map/dump/csv.
    """
    mappings_file = None
    if not flask.request.files:
        return 'No files uploaded!', 400
    elif 'file' not in flask.request.files.keys():
        return 'Unexpected file submission!', 400
    else:
        mappings_file = flask.request.files['file']
    if not mappings_file:
        return 'No file uploaded!', 400
    elif not mappings_file.filename:
        return 'No filename for uploaded file!', 400
    elif not allowed_file(mappings_file.filename):
        return 'File type is not allowed!', 400
    elif not allowed_file(secure_filename(mappings_file.filename)):
        return 'File appears insecure and not allowed!', 400
    bulk_results = {'success': [], 'fail': []}
    bulk_csv = csv.DictReader(io.StringIO(mappings_file.stream.read().decode('utf-8')))
    for row in bulk_csv:
        pair = [row['First DataPath'], row['Second DataPath']]
        try:
            map_datapath_single(row['First DataPath'], row['Second DataPath'], row['Author'], row['Annotation'])
            bulk_results['success'].append(pair)
        except Exception:
            bulk_results['fail'].append(pair)
    return flask.jsonify(bulk_results)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ['json', 'csv']

@app.route('/api/v1/map/load/native', methods=['POST'])
def api_map_load_native():
    mappings_file = None
    if not flask.request.files:
        return 'No files uploaded!', 400
    elif 'file' not in flask.request.files.keys():
        return 'Unexpected file submission!', 400
    else:
        mappings_file = flask.request.files['file']
    if not mappings_file:
        return 'No file uploaded!', 400
    elif not mappings_file.filename:
        return 'No filename for uploaded file!', 400
    elif not allowed_file(mappings_file.filename):
        return 'File type is not allowed!', 400
    elif not allowed_file(secure_filename(mappings_file.filename)):
        return 'File appears insecure and not allowed!', 400
    mappings_json = json.load(mappings_file)
    failures = {
        'DataPathMatch': [],
        'Calculation': []
    }
    for mapping in mappings_json['DataPathMatch']:
        status = True
        try:
            # TODO: JSON key validations
            map_datapath_single(**mapping)
        except Exception as e:
            failures['DataPathMatch'].append(
                {
                    '_from': mapping['_from'],
                    '_to': mapping['_to'],
                    'message': str(e)
                }
            )
    for calculation in mappings_json['Calculation']:
        status = True
        try:
            # TODO: JSON key validations
            map_datapath_calculation_single(**calculation)
        except Exception as e:
            failures['Calculation'].append(
                {
                    'name': calculation['name'],
                    'InCalculation': calculation['InCalculation'],
                    'CalculationResult': calculation['CalculationResult'],
                    'message': str(e)
                }
            )
    return flask.jsonify(failures)

def map_datapath_calculation_single(name, description, equation, author, InCalculation, CalculationResult):
    with db.cursor() as cur:
        resolved = _resolve_data_path_ids(cur, set(InCalculation) | set(CalculationResult))
        in_calculation_ids = {resolved[dp] for dp in InCalculation}
        calculation_result_ids = {resolved[dp] for dp in CalculationResult}
        app.logger.debug('Adding calculation %s', name)
        # calculation.name is UNIQUE; ON CONFLICT makes the existence check
        # atomic with the insert instead of a racy SELECT-then-INSERT.
        cur.execute("""
            INSERT INTO calculation (name, description, equation, author)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (name) DO NOTHING
            RETURNING calculation_id
        """, (name, description, equation, author))
        row = cur.fetchone()
        if row is None:
            raise Exception('Calculation of name %s already exists!' % name)
        calculation_id = row['calculation_id']
        for dp_id in in_calculation_ids:
            cur.execute(
                'INSERT INTO calculation_input (data_path_id, calculation_id) VALUES (%s, %s)',
                (dp_id, calculation_id)
            )
        for dp_id in calculation_result_ids:
            cur.execute(
                'INSERT INTO calculation_result (calculation_id, data_path_id) VALUES (%s, %s)',
                (calculation_id, dp_id)
            )

def map_datapath_single(_from, _to, author, annotation, timestamp=None, validated=False, weight=0, needs_human=True):
    with db.cursor() as cur:
        dp_one_id = resolve_data_path_id(cur, _from)
        dp_two_id = resolve_data_path_id(cur, _to)
        app.logger.debug('Mapping %s <-> %s', _from, _to)
        insert_data_path_match(
            cur, dp_one_id, dp_two_id, author, annotation,
            validated=validated,
            weight=weight,
            needs_human=needs_human or True if annotation else False,
            created_at=datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp is not None else None
        )

@app.route('/api/v1/map/datapath/single', methods=['POST'])
def api_map_datapath_single():
    required_api_keys = {'mapOne', 'mapTwo', 'author'}
    api_req = flask.request.get_json()
    if not api_req:
        return flask.jsonify({'error': 'Invalid JSON!'}), 400
    if not required_api_keys.issubset(set(api_req.keys())):
        return flask.jsonify({'error': 'Required keys missing!'}), 400
    map_one = api_req['mapOne']
    map_two = api_req['mapTwo']
    author = api_req['author']
    annotation = api_req['annotation'] if 'annotation' in api_req.keys() else None
    try:
        map_datapath_single(map_one, map_two, author, annotation)
        return flask.jsonify({'error': None, 'result': True})
    except Exception as e:
        return flask.jsonify({'error': str(e), 'result': False}), 400

def map_datapath_single_by_key(basepath_key, matchpath_key, author, weight, annotation):
    with db.cursor() as cur:
        cur.execute('SELECT 1 FROM data_path WHERE data_path_id = %s', (basepath_key,))
        if cur.fetchone() is None:
            raise Exception('Specified base DataPath not found!')
        cur.execute('SELECT 1 FROM data_path WHERE data_path_id = %s', (matchpath_key,))
        if cur.fetchone() is None:
            raise Exception('Specified matching DataPath not found!')
        app.logger.debug('Mapping %s <-> %s', basepath_key, matchpath_key)
        insert_data_path_match(
            cur, basepath_key, matchpath_key, author, annotation,
            validated=False,
            weight=weight,
            needs_human=False if not annotation or weight == 100 else True
        )

def resolve_data_path_id(cur, value):
    """Resolve a machine_id or human_id to its data_path_id.

    machine_id is UNIQUE so it can never be ambiguous on its own and is
    checked first, short-circuiting before human_id (which is not unique)
    is ever consulted -- matches the old Arango lookup's field priority.
    """
    cur.execute('SELECT data_path_id FROM data_path WHERE machine_id = %s', (value,))
    row = cur.fetchone()
    if row is not None:
        return row['data_path_id']
    cur.execute('SELECT data_path_id FROM data_path WHERE human_id = %s', (value,))
    rows = cur.fetchall()
    if not rows:
        raise Exception('Unable to find (machine_id or human_id: %s)!' % value)
    if len(rows) > 1:
        raise Exception('More than one document exists for (machine_id or human_id: %s)!' % value)
    return rows[0]['data_path_id']

def insert_data_path_match(cur, dp_one_id, dp_two_id, author, annotation, validated=False, weight=0, needs_human=True, created_at=None):
    """Insert a DataPathMatch row.

    data_path_match enforces CHECK (data_path_a_id < data_path_b_id) as its
    undirected-pair invariant, so the two resolved ids must be sorted
    ascending before insert regardless of which one is "base"/"match" on
    the caller's side. ON CONFLICT reproduces the old "mapping already
    exists" error instead of a raw constraint violation reaching the user.

    created_at defaults to now() at insert time; pass an explicit value
    to preserve a caller-supplied timestamp (e.g. restoring a native dump).
    """
    a_id, b_id = sorted((dp_one_id, dp_two_id))
    cur.execute("""
        INSERT INTO data_path_match
            (data_path_a_id, data_path_b_id, author, validated, weight, annotation, needs_human, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))
        ON CONFLICT (data_path_a_id, data_path_b_id) DO NOTHING
        RETURNING data_path_match_id
    """, (a_id, b_id, author, validated, weight, annotation, needs_human, created_at))
    if cur.fetchone() is None:
        raise Exception('Mapping already exists!')

@app.route('/api/v1/map/dump/csv')
def api_dump_mappings_csv():
    mappings = fetch_dump_mappings()
    output_csv = io.StringIO()
    csv_writer = csv.DictWriter(
        output_csv,
        fieldnames=['First DataPath', 'Second DataPath', 'Author', 'Annotation']
    )
    csv_writer.writeheader()
    for mapping in mappings:
        pretty_mapping = {
            'First DataPath': mapping['first_dp'],
            'Second DataPath': mapping['second_dp'],
            'Author': mapping['author'],
            'Annotation': mapping['annotation']
        }
        csv_writer.writerow(pretty_mapping)
    return send_stringio(output_csv, 'text/csv', 'tdm_mappings.csv')

@app.route('/api/v1/map/dump/native')
def api_dump_mappings_native():
    mappings = fetch_dump_mappings_native()
    output_json = io.StringIO()
    json.dump(
        mappings,
        output_json
    )
    return send_stringio(output_json, 'application/json', 'tdm_mappings.json')

def send_stringio(stringio_obj, mimetype, filename):
    """We typically use StringIO but send_file requires BytesIO. Errorless.
    Wrap it.
    """
    stringio_obj.flush()
    stringio_obj.seek(0)
    output_buffer = io.BytesIO(stringio_obj.getvalue().encode())
    output_buffer.flush()
    stringio_obj.close()
    output_buffer.seek(0)
    return flask.send_file(
        output_buffer,
        mimetype=mimetype,
        attachment_filename=filename,
        as_attachment=True,
        cache_timeout=-1
    )

def fetch_dump_mappings_native():
    with db.cursor() as cur:
        cur.execute("""
            SELECT a.machine_id AS from_machine_id, b.machine_id AS to_machine_id,
                   dpm.author, dpm.annotation, dpm.created_at,
                   dpm.validated, dpm.weight, dpm.needs_human
            FROM data_path_match dpm
            JOIN data_path a ON a.data_path_id = dpm.data_path_a_id
            JOIN data_path b ON b.data_path_id = dpm.data_path_b_id
        """)
        matches = [
            {
                '_from': row['from_machine_id'],
                '_to': row['to_machine_id'],
                'author': row['author'],
                'annotation': row['annotation'],
                'timestamp': row['created_at'].timestamp(),
                'validated': row['validated'],
                'weight': row['weight'],
                'needs_human': row['needs_human']
            }
            for row in cur.fetchall()
        ]
        cur.execute('SELECT calculation_id, name, description, equation, author FROM calculation')
        calculations = [
            {
                'name': calc['name'],
                'description': calc['description'],
                'equation': calc['equation'],
                'author': calc['author'],
                'InCalculation': [row['machine_id'] for row in _calc_factors(cur, calc['calculation_id'])],
                'CalculationResult': [row['machine_id'] for row in _calc_result_dps(cur, calc['calculation_id'])]
            }
            for calc in cur.fetchall()
        ]
    return {'DataPathMatch': matches, 'Calculation': calculations}

def fetch_dump_mappings():
    with db.cursor() as cur:
        cur.execute("""
            SELECT a.machine_id AS first_dp, b.machine_id AS second_dp,
                   dpm.author, dpm.annotation
            FROM data_path_match dpm
            JOIN data_path a ON a.data_path_id = dpm.data_path_a_id
            JOIN data_path b ON b.data_path_id = dpm.data_path_b_id
        """)
        return cur.fetchall()

"""Ugly Jinja2 bandaid for XPath issues."""

def machine_id_to_prefixed(machine_id):
    """Reformats the machine_id to prefixed specification.
    /oc-acl:acl/oc-acl:state
    """
    return machine_id_extract_xpath(machine_id, with_module=False)

def machine_id_to_module_prefixed(machine_id):
    """Reformats the machine_id to module prefixed specification.
    /openconfig-acl:acl/openconfig-acl:state
    """
    return machine_id_extract_xpath(machine_id, with_module=True)

def machine_id_to_module_prefixed_no_top_slash(machine_id):
    """Reformats the machine_id to module prefixed specification.
    openconfig-acl:acl/openconfig-acl:state
    """
    return machine_id_to_module_prefixed(machine_id)[1:]

def machine_id_extract_xpath(machine_id, with_module=True, fully_qualified=False):
    xpath_elements = machine_id.split('/')
    xpath_prefixed_elements = []
    running_prefix = None
    for element in xpath_elements:
        if not element:
            xpath_prefixed_elements.append('')
            continue
        module, prefix, name = element.split(':')
        desired_prefix = module if with_module else prefix
        if desired_prefix == running_prefix and not fully_qualified:
            xpath_prefixed_elements.append(name)
        else:
            xpath_prefixed_elements.append('%s:%s' % (desired_prefix, name))
            running_prefix = desired_prefix
    return '/'.join(xpath_prefixed_elements)

app.jinja_env.filters['machine_id_to_prefixed'] = machine_id_to_prefixed
app.jinja_env.filters['machine_id_to_module_prefixed'] = machine_id_to_module_prefixed
app.jinja_env.filters['machine_id_to_module_prefixed_no_top_slash'] = machine_id_to_module_prefixed_no_top_slash
"""End bandaid."""
