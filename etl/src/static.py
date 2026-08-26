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
"""Populate static business data that is otherwise conceptual
and not able to be directly parsed from data itself.
"""

def populate_data_model_languages(conn):
    """Populate everything related to individual Data Model Languages.
    Transport Protocols, Encodings, Control Protocols, Data Types, and DMLs.
    """
    cur = conn.cursor()
    # Define Transport Protocols
    transport_protocols = {
        'TCP': 'Transmission Control Protocol',
        'UDP': 'User Datagram Protocol',
        'SSH': 'Secure SHell',
        'Telnet': 'Insecure but oh so handy',
        'HTTP': 'HyperText Transfer Protocol'
    }
    transport_protocol_ids = {}
    for name, description in transport_protocols.items():
        cur.execute(
            'INSERT INTO transport_protocol (name, description) VALUES (%s, %s) '
            'RETURNING transport_protocol_id',
            (name, description)
        )
        transport_protocol_ids[name] = cur.fetchone()[0]
    # Define Encodings
    encodings = {
        'JSON': 'JavaScript Object Notation',
        'XML': 'eXtensible Markup Language',
        'GPB': 'Google Protocol Buffer',
        'KV-GPB': 'Key/Value Google Protocol Buffer',
        'BER': 'Basic Encoding Rules, defined in X.209. Used in SNMP.',
        'Text': 'Text'
    }
    encoding_ids = {}
    for name, description in encodings.items():
        cur.execute(
            'INSERT INTO encoding (name, description) VALUES (%s, %s) '
            'RETURNING encoding_id',
            (name, description)
        )
        encoding_ids[name] = cur.fetchone()[0]
    # Define Control Protocols
    control_protocols = {
        'gRPC': 'gRPC Remote Procedure Call',
        'NETCONF': 'Network Configuration Protocol',
        'SNMP': 'Simple Network Management Protocol',
        'MDT': 'Model-Driven Telemetry',
        'CLI': 'Command Line Interface',
        'RESTCONF': 'NETCONF -> REST',
        'NX-API': 'NX-API'
    }
    control_protocol_ids = {}
    for name, description in control_protocols.items():
        cur.execute(
            'INSERT INTO control_protocol (name, description) VALUES (%s, %s) '
            'RETURNING control_protocol_id',
            (name, description)
        )
        control_protocol_ids[name] = cur.fetchone()[0]
    # Control Protocols -> Encodings
    cp_has_encodings = {
        'gRPC': ['GPB', 'KV-GPB'],
        'NETCONF': ['XML'],
        'SNMP': ['BER'],
        'MDT': ['GPB', 'KV-GPB', 'JSON'],
        'CLI': ['Text'],
        'RESTCONF': ['XML', 'JSON'],
        'NX-API': ['XML', 'JSON']
    }
    for cp_name, enc_names in cp_has_encodings.items():
        for enc_name in enc_names:
            cur.execute(
                'INSERT INTO control_protocol_encoding (control_protocol_id, encoding_id) '
                'VALUES (%s, %s)',
                (control_protocol_ids[cp_name], encoding_ids[enc_name])
            )
    # Control Protocols -> Transport Protocols
    cp_has_tps = {
        'gRPC': ['HTTP'],
        'NETCONF': ['SSH'],
        'SNMP': ['UDP', 'TCP'],
        'MDT': ['UDP', 'TCP', 'HTTP'],
        'CLI': ['SSH', 'Telnet'],
        'RESTCONF': ['HTTP'],
        'NX-API': ['HTTP']
    }
    for cp_name, tp_names in cp_has_tps.items():
        for tp_name in tp_names:
            cur.execute(
                'INSERT INTO control_protocol_transport_protocol '
                '(control_protocol_id, transport_protocol_id) VALUES (%s, %s)',
                (control_protocol_ids[cp_name], transport_protocol_ids[tp_name])
            )
    # Define Data Model Languages
    # Data Model Languages -> Data Types
    # Data Model Languages -> Control Protocols
    data_model_languages = {
        'YANG': {
            'description': 'Yet Another Next Generation',
            'data_types': { # Derived from https://tools.ietf.org/html/rfc6020
                'binary': ('https://tools.ietf.org/html/rfc6020#section-9.8', True),
                'bits': ('https://tools.ietf.org/html/rfc6020#section-9.7', True),
                'boolean': ('https://tools.ietf.org/html/rfc6020#section-9.5', True),
                'decimal64': ('https://tools.ietf.org/html/rfc6020#section-9.3', True),
                'empty': ('https://tools.ietf.org/html/rfc6020#section-9.11', True),
                'enumeration': ('https://tools.ietf.org/html/rfc6020#section-9.6', True),
                'identityref': ('https://tools.ietf.org/html/rfc6020#section-9.10', True),
                'instance-identifier': ('https://tools.ietf.org/html/rfc6020#section-9.13', True),
                'int8': ('https://tools.ietf.org/html/rfc6020#section-9.2', True),
                'int16': ('https://tools.ietf.org/html/rfc6020#section-9.2', True),
                'int32': ('https://tools.ietf.org/html/rfc6020#section-9.2', True),
                'int64': ('https://tools.ietf.org/html/rfc6020#section-9.2', True),
                'leafref': ('https://tools.ietf.org/html/rfc6020#section-9.9', True),
                'string': ('https://tools.ietf.org/html/rfc6020#section-9.4', True),
                'uint8': ('https://tools.ietf.org/html/rfc6020#section-9.2', True),
                'uint16': ('https://tools.ietf.org/html/rfc6020#section-9.2', True),
                'uint32': ('https://tools.ietf.org/html/rfc6020#section-9.2', True),
                'uint64': ('https://tools.ietf.org/html/rfc6020#section-9.2', True),
                'union': ('https://tools.ietf.org/html/rfc6020#section-9.12', True)
            },
            'control_protocols': ['gRPC', 'NETCONF', 'RESTCONF', 'MDT']
        },
        'SMI': {
            'description': 'Structure of Management Information',
            'data_types': { # Derived from https://tools.ietf.org/html/rfc2578
                'Integer32': ('https://tools.ietf.org/html/rfc2578#section-7.1.1', True),
                'INTEGER': ('https://tools.ietf.org/html/rfc2578#section-7.1.1', True),
                'OCTET STRING': ('https://tools.ietf.org/html/rfc2578#section-7.1.2', True),
                'OBJECT IDENTIFIER': ('https://tools.ietf.org/html/rfc2578#section-7.1.3', True),
                'BITS': ('https://tools.ietf.org/html/rfc2578#section-7.1.4', True),
                'IpAddress': ('https://tools.ietf.org/html/rfc2578#section-7.1.5', True),
                'Counter32': ('https://tools.ietf.org/html/rfc2578#section-7.1.6', True),
                'Gauge32': ('https://tools.ietf.org/html/rfc2578#section-7.1.7', True),
                'TimeTicks': ('https://tools.ietf.org/html/rfc2578#section-7.1.8', True),
                'Opaque': ('https://tools.ietf.org/html/rfc2578#section-7.1.9', True),
                'Counter64': ('https://tools.ietf.org/html/rfc2578#section-7.1.10', True),
                'Unsigned32': ('https://tools.ietf.org/html/rfc2578#section-7.1.11', True),
                'Conceptual Tables': ('https://tools.ietf.org/html/rfc2578#section-7.1.12', True)
            },
            'control_protocols': ['MDT']
        },
        'DME': {
            'description': 'Data Management Engine',
            'data_types': {},
            'control_protocols': ['MDT', 'NX-API']
        },
        'CLI': {
            'description': 'Command Line Interface',
            'data_types': {},
            'control_protocols': ['CLI']
        }
    }
    for name, value in data_model_languages.items():
        cur.execute(
            'INSERT INTO data_model_language (name, description) VALUES (%s, %s) '
            'RETURNING data_model_language_id',
            (name, value['description'])
        )
        dml_id = cur.fetchone()[0]
        for dt_name, (dt_description, dt_is_primitive) in value['data_types'].items():
            cur.execute(
                'INSERT INTO data_type (data_model_language_id, name, description, is_primitive) '
                'VALUES (%s, %s, %s, %s)',
                (dml_id, dt_name, dt_description, dt_is_primitive)
            )
        for cp_name in value['control_protocols']:
            cur.execute(
                'INSERT INTO data_model_language_control_protocol '
                '(data_model_language_id, control_protocol_id) VALUES (%s, %s)',
                (dml_id, control_protocol_ids[cp_name])
            )
    conn.commit()
    cur.close()

def populate_os_releases(conn):
    """Populate OSes and releases.
    Derived from YANG repository directories for now.
    https://github.com/YangModels/yang/tree/master/vendor/cisco
    """
    oses = {
        'IOS XE': {
            'description': 'IOS XE',
            'releases': [
                '16.3.1',
                '16.3.2',
                '16.4.1',
                '16.5.1',
                '16.6.1',
                '16.6.2',
                '16.7.1',
                '16.8.1',
                '16.9.1',
                '16.9.3',
                '16.10.1',
                '16.11.1',
                '16.12.1',
                '17.1.1',
                '17.2.1'
            ]
        },
        'IOS XR': {
            'description': 'IOS XR',
            'releases': [
#                '5.3.0',
#                '5.3.1',
#                '5.3.2',
#                '5.3.3',
#                '5.3.4',
#                '6.0.0',
#                '6.0.1',
#                '6.0.2',
#                '6.1.1',
#                '6.1.2',
#                '6.1.3',
#                '6.2.1',
#                '6.2.2', # Deprecating stale OpenConfig
                '6.3.1',
                '6.3.2',
                '6.4.1',
                '6.4.2',
                '6.5.1',
                '6.5.2',
                '6.5.3',
                '6.6.2',
                '7.0.1',
                '7.0.2',
                '7.1.1'
            ]
        },
        'NX-OS': {
            'description': 'NX-OS',
            'releases': [
                '7.0(3)F1(1)',
                '7.0(3)F2(1)',
                '7.0(3)F2(2)',
                '7.0(3)I5(1)',
                '7.0(3)I5(2)',
                '7.0(3)I6(1)',
                '7.0(3)I6(2)',
                '7.0(3)I7(1)',
                '7.0(3)I7(2)',
                '7.0(3)I7(3)',
                '7.0(3)I7(4)',
                '9.2(1)',
                '9.2(2)',
                '9.2(3)',
                '9.2(4)',
                '9.3(1)',
                '9.3(2)',
                '9.3(3)',
                '9.3(4)',
                '9.3(5)'
            ]
        }
    }
    cur = conn.cursor()
    for os_name, os_value in oses.items():
        cur.execute(
            'INSERT INTO os (name, description) VALUES (%s, %s) RETURNING os_id',
            (os_name, os_value['description'])
        )
        os_id = cur.fetchone()[0]
        previous_release_id = None
        for release_name in os_value['releases']:
            cur.execute(
                'INSERT INTO release (os_id, name, previous_release_id) VALUES (%s, %s, %s) '
                'RETURNING release_id',
                (os_id, release_name, previous_release_id)
            )
            previous_release_id = cur.fetchone()[0]
    conn.commit()
    cur.close()

def populate_static(conn):
    populate_os_releases(conn)
    populate_data_model_languages(conn)
