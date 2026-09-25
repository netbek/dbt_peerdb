{{ config(
    materialized='table',
    engine='MergeTree()',
    order_by='id',
) }}
select id, payload, region, _peerdb_synced_at, _peerdb_is_deleted, _peerdb_version
from {{ source('bi', 'bi_source') }}
