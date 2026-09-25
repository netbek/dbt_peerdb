{{ config(
    materialized='bucketed_incremental',
    incremental_strategy='delete_insert',
    engine='MergeTree()',
    order_by='id',
    unique_key='id',
    bucket_key_column='id',
    bucket_snapshot_column='_peerdb_synced_at',
    bucket_ref=['bi_not_a_model'],
    rows_per_bucket=3,
) }}
-- __BUCKET_PREDICATE__
select 1 as id
