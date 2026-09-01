# Search
Search in TDM is based off of [Elasticsearch](https://www.elastic.co/products/elasticsearch), used by the "deep search" UI (`/search_es`) for its relevance scoring, aggregations, and custom synonym analyzer described below. `data_path` also has its own weighted `tsvector`/GIN full-text column (see [Database](Database.html)) which backs the primary structured search API (`/api/v1/search`) directly in PostgreSQL. The two search implementations are independent of each other.

[[toc]]

Elasticsearch is *not* a source-of-truth for TDM. Elasticsearch should always be thought of as a cache of data which could potentially be out-of-sync with the PostgreSQL instance of TDM which all other operations use. Elasticsearch is very-specifically a point solution to the original searchability issues, and is refreshed only by re-running the ETL's search stage — there is no live sync from PostgreSQL writes (e.g. new mappings created via the web UI) back into Elasticsearch.

## Indexing
The index for Elasticsearch defines the schema of documents and how inputs should be processed and analyzed. The index was derived from:
* [Example Python](https://github.com/elastic/elasticsearch-py/blob/master/example/load.py#L17)
* [Analysis Documentation](https://www.elastic.co/guide/en/elasticsearch/reference/current/analysis.html)
* [Custom Analyzer](https://www.elastic.co/guide/en/elasticsearch/reference/current/analysis-custom-analyzer.html)
* [Simple Pattern Split Tokenizer](https://www.elastic.co/guide/en/elasticsearch/reference/current/analysis-simplepatternsplit-tokenizer.html)
* [Snowball Token Filter](https://www.elastic.co/guide/en/elasticsearch/reference/current/analysis-snowball-tokenfilter.html)
* [Unique Token Filter](https://www.elastic.co/guide/en/elasticsearch/reference/current/analysis-unique-tokenfilter.html)

This index specifies a custom analyzer and provides the `machine_id` and `human_id` of the DataPaths as fulltext search and as terms for aggregation.

```json
{
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "generic_path_analyzer": {
                    "type": "custom",
                    "tokenizer": "generic_path_tokenizer",
                    "filter": [
                        "preserve_word_delimiter",
                        "lowercase",
                        "network_synonym"
                    ]
                }
            },
            "tokenizer": {
                "generic_path_tokenizer": {
                    "type": "simple_pattern_split",
                    "tokenizer": "[\\.\\-\\/\\:]"
                }
            },
            "filter": {
                "preserve_word_delimiter": {
                    "type": "word_delimiter",
                    "preserve_original": true
                },
                "network_synonym": {
                    "type": "synonym",
                    "expand": true,
                    "synonyms": [
                        "optic, transceiver",
                        "intf, interface, if, int",
                        "ucast, unicast",
                        "mcast, multicast",
                        "pkt, packet",
                        "in, inbound"
                    ]
                }
            }
        }
    },
    "mappings": {
        "doc": {
            "properties": {
                "dp_key": {
                    "type": "keyword"
                },
                "dp_machine_id": {
                    "type": "text",
                    "analyzer": "generic_path_analyzer",
                    "fields": {
                        "keyword": {
                            "type": "keyword"
                        }
                    }
                },
                "dp_human_id": {
                    "type": "text",
                    "analyzer": "generic_path_analyzer",
                    "fields": {
                        "keyword": {
                            "type": "keyword"
                        }
                    }
                },
                "dp_description": {
                    "type": "text",
                    "analyzer": "snowball"
                },
                "dp_is_leaf": {
                    "type": "boolean"
                },
                "dp_is_configurable": {
                    "type": "boolean"
                },
                "dml_key": {
                    "type": "keyword"
                },
                "dml_name": {
                    "type": "keyword"
                },
                "dm_key": {
                    "type": "keyword"
                },
                "dm_name": {
                    "type": "keyword"
                },
                "dm_revision": {
                    "type": "keyword"
                },
                "release_key": {
                    "type": "keyword"
                },
                "release_name": {
                    "type": "keyword"
                },
                "os_key": {
                    "type": "keyword"
                },
                "os_name": {
                    "type": "keyword"
                }
            }
        }
    }
}
```

Loading of the data takes every permutation of a DataPath and its OS/Releases and creates a new document per permutation. This is not necessarily the *best* way to go about loading the data, but it was the clearest path forward. This does mean that there is a significant duplication albeit unique/qualified in document. If you are familiar with Elasticsearch and have recommendations, please [contact us](/Contact.html).

## Query
The Elasticsearch query in its current form performs an aggregation on the DataPath `human_id`. The results are ordered according to relevancy per the scoring of the query. An example query form is presented below.

```json
{
    "size": 0,
    "sort": [
        "_score"
    ],
    "query": {
        "bool": {
            "must": [
                {
                    "multi_match": {
                        "query": "openconfig interface name",
                        "operator": "and",
                        "type": "most_fields",
                        "fields": [
                            "dp_human_id^3",
                            "dp_machine_id",
                            "dp_description"
                        ]
                    }
                }
            ],
            "filter": []
        }
    },
    "aggs": {
        "human_id": {
            "terms": {
                "field": "dp_human_id.keyword",
                "size": 150,
                "order": {
                    "relevance": "desc"
                }
            },
            "aggs": {
                "relevance": {
                    "max": {
                        "script": "_score"
                    }
                },
                "dp_key": {
                    "terms": {
                        "field": "dp_key"
                    },
                    "aggs": {
                        "machine_id": {
                            "terms": {
                                "field": "dp_machine_id.keyword"
                            }
                        }
                    }
                }
            }
        }
    }
}
```

### PostgreSQL Query
PostgreSQL's `tsvector`/GIN full-text search on `data_path` backs the query above's counterpart in `/api/v1/search`, `fetch_search_data_paths` in `web/src/web/views.py:656` — the `WHERE` clause is built up dynamically (a filter string is optional; `os`/`release`/`dml` filters are always required), but this is the fully-filtered form:

```sql
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
WHERE (os.name, release.name) IN %(os_release_pairs)s
  AND dml.name = ANY(%(dml_names)s)
  AND dp.is_leaf = %(only_leaves)s
  AND dp.search_vector @@ plainto_tsquery('simple', %(filter_str)s)
  AND dp.is_configurable = FALSE
ORDER BY dp.human_id
LIMIT %(max_return_count)s OFFSET %(start_index)s
```

Rows come back flat, and `fetch_search_data_paths` builds an `OS → Release → DataModelLanguage → DataModel → [DataPath]` nested dict shape in plain Python afterward. See [Database](Database.html#example-sql) for more query examples against the current schema.
