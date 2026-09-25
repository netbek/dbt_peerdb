{{ config(
    materialized='bucketed_incremental',
    incremental_strategy='delete_insert',
    engine='MergeTree()',
    order_by='id',
    unique_key='id',
    bucket_key_column='id',
    bucket_snapshot_column='bad-column',
    bucket_source=['bi', 'bi_source'],
    rows_per_bucket=3,
) }}
select 1 as id
