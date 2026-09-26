# AGENTS.md

## Project skills

Load any skill below with the `skill` tool by name (e.g., `skill find-skills`), or read its `SKILL.md` directly (e.g., `read .agents/skills/find-skills/SKILL.md`). If `.agents/skills/` is empty, run `npx skills-manager install` to populate it (see [skills-manager docs](https://github.com/netbek/skills-manager)).

| Skill | Path | Description |
|-------|------|-------------|
| `clickhouse-best-practices` | `.agents/skills/clickhouse-best-practices` | MUST USE when reviewing ClickHouse schemas, queries, or configurations. Contains 31 rules that MUST be checked before providing recommendations. Always read relevant rule files and cite specific rules in responses. |
| `economist-style` | `.agents/skills/economist-style` | Apply The Economist style guide to written content. Use when editing markdown, HTML, documentation, or any written text that needs professional editing for clarity, precision, and brevity. Detects weasel words, fillers, passive voice, and style issues. |
| `fetching-dbt-docs` | `.agents/skills/fetching-dbt-docs` | Retrieves and searches dbt documentation pages in LLM-friendly markdown format. Use when fetching dbt documentation, looking up dbt features, or answering questions about dbt Cloud, dbt Core, or the dbt Semantic Layer. |
| `find-skills` | `.agents/skills/find-skills` | Helps users discover and install agent skills when they ask questions like "how do I do X", "find a skill for X", "is there a skill that can...", or express interest in extending capabilities. This skill should be used when the user is looking for functionality that might exist as an installable skill. |
| `using-dbt-for-analytics-engineering` | `.agents/skills/using-dbt-for-analytics-engineering` | Builds and modifies dbt models, writes SQL transformations using ref() and source(), creates tests, and validates results with dbt show. Use when doing any dbt work - building or modifying models, debugging errors, exploring unfamiliar data sources, writing tests, or evaluating impact of changes. |
| `writing-spec-and-design-docs` | `.agents/skills/writing-spec-and-design-docs` | Write or revise a feature spec and design doc before implementation. Use when the user asks for a spec, design doc, feature plan, or says "spec this", "design this", or "write the plan docs". |

## ClickHouse

Create ClickHouse config once per shell session:

```shell
mkdir -p ~/.clickhouse/configs
cat > ~/.clickhouse/configs/dbt_peerdb.yaml <<'EOF'
query_log:
    database: system
    table: query_log
EOF
```

Then use `.venv/bin/clickhousectl` to manage the local server.
Always use ClickHouse v26.3.33.24 with HTTP port `18123` and TCP port `19000`.

| Action | Command |
|--------|---------|
| Install server | `.venv/bin/clickhousectl local install 26.3.33.24` |
| Start server | `.venv/bin/clickhousectl local server start --version 26.3.33.24 --http-port 18123 --tcp-port 19000 --config-file dbt_peerdb` |
| Stop server | `.venv/bin/clickhousectl local server stop` |
| Connect to server | `.venv/bin/clickhousectl local client --port 19000` |
| Run query | `.venv/bin/clickhousectl local client --port 19000 --query "select version()"` |

## Tests

Use `.venv/bin/pytest`. Integration tests require a running ClickHouse server (see above). Example session:

```shell
mkdir -p ~/.clickhouse/configs
cat > ~/.clickhouse/configs/dbt_peerdb.yaml <<'EOF'
query_log:
    database: system
    table: query_log
EOF

.venv/bin/clickhousectl local server start --version 26.3.33.24 --http-port 18123 --tcp-port 19000 --config-file dbt_peerdb
.venv/bin/pytest -s
.venv/bin/clickhousectl local server stop
```
