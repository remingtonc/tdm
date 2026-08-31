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
"""Coverage for the Phase 2 read-path ports in web.views.

There is no live ArangoDB to diff these against (see
agentdoc/impl-phases/phase-2-read-paths.md), so this isn't a parity port --
it's a fresh regression baseline against the real Postgres data etl/ already
loaded (871k+ data_path rows). Fixtures below are real rows looked up via
psql, keyed off the worked examples in the top-level README.
"""
from web import views

BGP_LOCAL_AS = {'data_path_id': 5228, 'machine_id': '1.3.6.1.2.1.15.2', 'human_id': 'bgpLocalAs'}
IF_MTU = {'data_path_id': 128470, 'machine_id': '1.3.6.1.2.1.2.2.1.4', 'human_id': 'ifMtu'}
CDP_CACHE_DEVICE_ID = {
    'data_path_id': 14712,
    'machine_id': '1.3.6.1.4.1.9.9.23.1.2.1.1.6',
    'human_id': 'cdpCacheDeviceId',
    'data_type_id': 22,
    'data_type_name': 'OCTET STRING'
}

# A YANG data_path with a real parent/children/os/dml graph to exercise the
# join-based fetches (the three README paths above are flat MIB OIDs with no
# parent/children of their own).
PARENT_ID = 740106
CHILD_ID = 740111
CHILD_COUNT = 6


def test_fetch_datapath_found():
    dp = views.fetch_datapath(IF_MTU['data_path_id'])
    assert dp['data_path_id'] == IF_MTU['data_path_id']
    assert dp['human_id'] == IF_MTU['human_id']
    assert dp['machine_id'] == IF_MTU['machine_id']
    assert '_key' not in dp

def test_fetch_datapath_missing():
    assert views.fetch_datapath(-1) is None

def test_fetch_datapath_arbitrary_id_by_human_id():
    results = views.fetch_datapath_arbitrary_id(IF_MTU['human_id'])
    assert len(results) == 1
    assert results[0]['data_path_id'] == IF_MTU['data_path_id']
    assert results[0]['machine_id'] == IF_MTU['machine_id']

def test_fetch_datapath_arbitrary_id_by_machine_id():
    results = views.fetch_datapath_arbitrary_id(CDP_CACHE_DEVICE_ID['machine_id'])
    assert len(results) == 1
    assert results[0]['data_path_id'] == CDP_CACHE_DEVICE_ID['data_path_id']

def test_fetch_datapath_arbitrary_id_not_found():
    assert views.fetch_datapath_arbitrary_id('does-not-exist-anywhere') == []

def test_fetch_datapath_parent():
    parents = views.fetch_datapath_parent(CHILD_ID)
    assert len(parents) == 1
    assert parents[0]['data_path_id'] == PARENT_ID

def test_fetch_datapath_parent_none():
    assert views.fetch_datapath_parent(IF_MTU['data_path_id']) == []

def test_fetch_datapath_children():
    children = views.fetch_datapath_children(PARENT_ID)
    assert len(children) == CHILD_COUNT
    assert CHILD_ID in {child['data_path_id'] for child in children}

def test_fetch_datapath_children_none():
    assert views.fetch_datapath_children(IF_MTU['data_path_id']) == []

def test_fetch_datapath_datatype():
    datatypes = views.fetch_datapath_datatype(CDP_CACHE_DEVICE_ID['data_path_id'])
    assert len(datatypes) == 1
    assert datatypes[0]['data_type_id'] == CDP_CACHE_DEVICE_ID['data_type_id']
    assert datatypes[0]['name'] == CDP_CACHE_DEVICE_ID['data_type_name']

def test_fetch_datapath_models():
    models = views.fetch_datapath_models(IF_MTU['data_path_id'])
    assert any(model['name'] == 'IF-MIB' for model in models)

def test_fetch_datapath_os_graph():
    rows = views.fetch_datapath_os_graph(CHILD_ID)
    assert rows
    assert {'datamodel_name', 'datamodel_revision', 'os_release', 'os_name'} <= rows[0].keys()
    assert any(row['os_name'] == 'IOS XE' and row['os_release'] == '16.3.1' for row in rows)

def test_fetch_datapath_os_graph_none():
    assert views.fetch_datapath_os_graph(IF_MTU['data_path_id']) == []

def test_fetch_datapath_dml_graph():
    rows = views.fetch_datapath_dml_graph(CHILD_ID)
    assert rows
    assert any(row['dml_name'] == 'YANG' and row['datamodel_name'] == 'CISCO-IMAGE-LICENSE-MGMT-MIB' for row in rows)

def test_fetch_datapath_mappings_empty():
    # No DataPathMatch rows exist yet -- Phase 3 is what creates them.
    assert views.fetch_datapath_mappings(IF_MTU['data_path_id']) == []

def test_fetch_all_matches_shape():
    result = views.fetch_all_matches()
    assert set(result.keys()) == set(views.fetch_dmls())
    assert all(isinstance(matches, list) for matches in result.values())
    # No matches exist yet, so every bucket should be empty.
    assert all(matches == [] for matches in result.values())

def test_fetch_matches_empty():
    assert views.fetch_matches([IF_MTU['human_id'], BGP_LOCAL_AS['human_id']]) == []

def test_fetch_matches_unknown_dp():
    assert views.fetch_matches(['this-does-not-exist']) == []

def test_fetch_calculations_empty():
    given = [IF_MTU['human_id']]
    assert views.fetch_calculations(given) == []
    assert views.fetch_calculations_as_result(given) == []
    assert views.fetch_calculations_as_factor(given) == []

def test_fetch_collection_counts_allowlist():
    counts = views.fetch_collection_counts(['DataPath', 'Release', 'DataModel'])
    assert counts['DataPath'] > 800000
    assert counts['Release'] > 0
    assert counts['DataModel'] > 0

def test_fetch_collection_counts_rejects_unlisted_names():
    counts = views.fetch_collection_counts(['DataPath', 'data_path; DROP TABLE data_path; --'])
    assert list(counts.keys()) == ['DataPath']

def test_fetch_os_releases():
    releases = views.fetch_os_releases()
    assert 'IOS XE - 16.3.1' in releases

def test_fetch_dmls():
    dmls = views.fetch_dmls()
    assert dmls == sorted(dmls)
    assert 'YANG' in dmls

def test_fetch_search_data_paths():
    result = views.fetch_search_data_paths(
        filter_os_releases={'IOS XE': ['16.3.1']},
        filter_dmls=['YANG'],
        filter_str='cilmImageLicenseName',
        exclude_config=True,
        only_leaves=True,
        start_index=0,
        max_return_count=10
    )
    dps = result['IOS XE']['16.3.1']['YANG']['CISCO-IMAGE-LICENSE-MGMT-MIB']
    assert CHILD_ID in {dp['data_path_id'] for dp in dps}

def test_fetch_search_data_paths_requires_os_and_dml():
    assert views.fetch_search_data_paths({}, ['YANG'], 'foo') == {}
    assert views.fetch_search_data_paths({'IOS XE': ['16.3.1']}, [], 'foo') == {}

def test_fetch_search_data_paths_no_match():
    result = views.fetch_search_data_paths(
        filter_os_releases={'IOS XE': ['16.3.1']},
        filter_dmls=['YANG'],
        filter_str='thisstringmatchesnothinguseful'
    )
    assert result == {}

def test_fetch_dump_mappings_shape():
    assert views.fetch_dump_mappings() == []

def test_fetch_dump_mappings_native_shape():
    dump = views.fetch_dump_mappings_native()
    assert set(dump.keys()) == {'DataPathMatch', 'Calculation'}
    assert dump['DataPathMatch'] == []
    assert dump['Calculation'] == []
