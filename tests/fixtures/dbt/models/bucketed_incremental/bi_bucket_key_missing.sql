{{ config(
    materialized='bucketed_incremental',
    incremental_strategy='delete_insert',
    engine='MergeTree()',
    order_by='id',
    unique_key='id',
    bucket_snapshot_column='_peerdb_synced_at',
    bucket_source_table='default.bi_source',
    rows_per_bucket=3,
) }}
select 1 as id
