# dbt_peerdb

## Installation

1. Add the package to your `packages.yml`:

    ```yaml
    packages:
      - package: https://github.com/netbek/dbt_peerdb
        version: 0.0.5
    ```

2. Configure the package in your `dbt_project.yml`:

    ```yaml
    vars:
      dbt_peerdb_columns: [_peerdb_synced_at, _peerdb_is_deleted, _peerdb_version]
    ```

3. Run `dbt deps` to install the package.

## License

Copyright (c) 2024 Hein Bekker. Licensed under the Apache License, version 2.
