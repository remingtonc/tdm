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
"""Coverage for the Phase 3 write-path ports in web.views.

Every test here runs inside the `db_rollback` fixture (see conftest.py) so
these inserts never permanently land in the 871k-row dataset etl/ already
loaded. Fixtures are the same real rows test_views.py resolved via psql.
"""
import io

import pytest

from web import views, db
from web import app as flask_app
from test_views import BGP_LOCAL_AS, IF_MTU, CDP_CACHE_DEVICE_ID

# BGP_LOCAL_AS.data_path_id (5228) < IF_MTU.data_path_id (128470); several
# tests below deliberately submit them in descending id order to exercise
# the sort-before-insert logic that satisfies data_path_match's
# CHECK (data_path_a_id < data_path_b_id).
assert BGP_LOCAL_AS['data_path_id'] < IF_MTU['data_path_id']


def test_resolve_data_path_id_by_machine_id(db_rollback):
    with db.cursor() as cur:
        assert views.resolve_data_path_id(cur, IF_MTU['machine_id']) == IF_MTU['data_path_id']

def test_resolve_data_path_id_by_human_id(db_rollback):
    with db.cursor() as cur:
        assert views.resolve_data_path_id(cur, IF_MTU['human_id']) == IF_MTU['data_path_id']

def test_resolve_data_path_id_not_found(db_rollback):
    with db.cursor() as cur:
        with pytest.raises(Exception):
            views.resolve_data_path_id(cur, 'does-not-exist-anywhere')


def test_map_datapath_single_creates_match(db_rollback):
    views.map_datapath_single(IF_MTU['machine_id'], BGP_LOCAL_AS['machine_id'], 'tester@example.com', 'note')
    mappings = views.fetch_datapath_mappings(IF_MTU['data_path_id'])
    assert BGP_LOCAL_AS['data_path_id'] in {m['data_path_id'] for m in mappings}

def test_map_datapath_single_resolves_by_human_id(db_rollback):
    views.map_datapath_single(IF_MTU['human_id'], CDP_CACHE_DEVICE_ID['human_id'], 'tester@example.com', None)
    mappings = views.fetch_datapath_mappings(IF_MTU['data_path_id'])
    assert CDP_CACHE_DEVICE_ID['data_path_id'] in {m['data_path_id'] for m in mappings}

def test_map_datapath_single_unknown_dp_raises(db_rollback):
    with pytest.raises(Exception):
        views.map_datapath_single('does-not-exist-anywhere', IF_MTU['machine_id'], 'tester@example.com', None)

def test_map_datapath_single_duplicate_regardless_of_id_order(db_rollback):
    # Submit the numerically larger id first -- this is the one behavior
    # most likely to regress silently if the sort-before-insert is dropped.
    views.map_datapath_single(IF_MTU['machine_id'], BGP_LOCAL_AS['machine_id'], 'tester@example.com', None)
    with pytest.raises(Exception):
        views.map_datapath_single(BGP_LOCAL_AS['machine_id'], IF_MTU['machine_id'], 'tester@example.com', None)


def test_map_datapath_single_by_key_creates_match(db_rollback):
    views.map_datapath_single_by_key(IF_MTU['data_path_id'], BGP_LOCAL_AS['data_path_id'], 'tester@example.com', 90, 'note')
    mappings = views.fetch_datapath_mappings(IF_MTU['data_path_id'])
    assert BGP_LOCAL_AS['data_path_id'] in {m['data_path_id'] for m in mappings}

def test_map_datapath_single_by_key_duplicate_regardless_of_id_order(db_rollback):
    views.map_datapath_single_by_key(IF_MTU['data_path_id'], BGP_LOCAL_AS['data_path_id'], 'tester@example.com', 50, None)
    with pytest.raises(Exception):
        views.map_datapath_single_by_key(BGP_LOCAL_AS['data_path_id'], IF_MTU['data_path_id'], 'tester@example.com', 50, None)

def test_map_datapath_single_by_key_missing_datapath(db_rollback):
    with pytest.raises(Exception):
        views.map_datapath_single_by_key(-1, IF_MTU['data_path_id'], 'tester@example.com', 50, None)


def test_map_datapath_calculation_single(db_rollback):
    views.map_datapath_calculation_single(
        name='Phase 3 test calculation',
        description='desc',
        equation='a + b',
        author='tester@example.com',
        InCalculation=[IF_MTU['machine_id'], BGP_LOCAL_AS['human_id']],
        CalculationResult=[CDP_CACHE_DEVICE_ID['machine_id']]
    )
    as_factor = views.fetch_calculations_as_factor([IF_MTU['human_id']])
    assert as_factor
    calc = as_factor[0]['calculations'][0]
    assert calc['name'] == 'Phase 3 test calculation'
    assert calc['result']['data_path_id'] == CDP_CACHE_DEVICE_ID['data_path_id']
    factor_ids = {f['data_path_id'] for f in calc['factors']}
    assert {IF_MTU['data_path_id'], BGP_LOCAL_AS['data_path_id']} <= factor_ids

def test_map_datapath_calculation_single_duplicate_name_rejected(db_rollback):
    views.map_datapath_calculation_single(
        name='Phase 3 duplicate calc',
        description='d', equation='e', author='tester@example.com',
        InCalculation=[IF_MTU['machine_id']],
        CalculationResult=[BGP_LOCAL_AS['machine_id']]
    )
    with pytest.raises(Exception):
        views.map_datapath_calculation_single(
            name='Phase 3 duplicate calc',
            description='d2', equation='e2', author='tester2@example.com',
            InCalculation=[CDP_CACHE_DEVICE_ID['machine_id']],
            CalculationResult=[IF_MTU['machine_id']]
        )


def test_api_map_bulk_success_and_export_round_trip(db_rollback):
    csv_content = (
        'First DataPath,Second DataPath,Author,Annotation\n'
        '{},{},{},{}\n'
    ).format(IF_MTU['machine_id'], BGP_LOCAL_AS['machine_id'], 'tester@example.com', 'bulk test')
    client = flask_app.test_client()
    response = client.post(
        '/api/v1/map/datapath/bulk',
        data={'file': (io.BytesIO(csv_content.encode()), 'mappings.csv')},
        content_type='multipart/form-data'
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body['fail'] == []
    assert body['success'] == [[IF_MTU['machine_id'], BGP_LOCAL_AS['machine_id']]]

    dumped = views.fetch_dump_mappings()
    assert any(
        {row['first_dp'], row['second_dp']} == {IF_MTU['machine_id'], BGP_LOCAL_AS['machine_id']}
        for row in dumped
    )

def test_api_map_bulk_collects_row_failures(db_rollback):
    csv_content = (
        'First DataPath,Second DataPath,Author,Annotation\n'
        'does-not-exist-anywhere,{},{},{}\n'
    ).format(BGP_LOCAL_AS['machine_id'], 'tester@example.com', 'bulk test')
    client = flask_app.test_client()
    response = client.post(
        '/api/v1/map/datapath/bulk',
        data={'file': (io.BytesIO(csv_content.encode()), 'mappings.csv')},
        content_type='multipart/form-data'
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body['success'] == []
    assert body['fail'] == [['does-not-exist-anywhere', BGP_LOCAL_AS['machine_id']]]
