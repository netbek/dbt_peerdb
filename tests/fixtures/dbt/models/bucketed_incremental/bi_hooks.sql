{{ config(
    materialized='bucketed_incremental',
    incremental_strategy='delete_insert',
    engine='MergeTree()',
    order_by='id',
    unique_key='id',
    bucket_key_column='id',
    bucket_snapshot_column='_peerdb_synced_at',
    bucket_source=['bi', 'bi_source'],
    rows_per_bucket=3,
    pre_hook=["insert into default.bi_hook_log values ('pre')"],
    post_hook=["insert into default.bi_hook_log values ('post')"],
) }}
{{ bi_model_sql() }}
