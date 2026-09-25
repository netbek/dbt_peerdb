{{ config(
    materialized='bucketed_incremental',
    incremental_strategy='delete_insert',
    engine='MergeTree()',
    order_by='id',
    unique_key='id',
    bucket_key_column='id',
    bucket_snapshot_column='_peerdb_synced_at',
    bucket_ref=['example', 'bi_ref_source'],
    rows_per_bucket=3,
) }}
{{ bi_model_sql(relation=ref('example', 'bi_ref_source')) }}
