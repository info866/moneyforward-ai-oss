# Dependency and license inventory

Snapshot: 2026-11-11. Inspected installed distribution/wheel metadata; packages are not vendored. The core defaults to Python standard library and SQLite. Python 3.13.6 on macOS was used for the tested editable install. This is not a certification of every optional platform/dependency combination.

| Package | Version inspected | License declared | Role |
| --- | --- | --- | --- |
| pip | 25.2 | MIT | installer |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | test/development |
| Pygments | 2.21.0 | BSD-2-Clause | test/development |
| ruff | 0.17.4 | MIT | test/development |
| iniconfig | 2.3.1 | MIT | test/development |
| pluggy | 1.7.0 | MIT | test/development |
| pytest | 8.4.2 | MIT | test/development |
| setuptools | 84.2.1 | MIT | build; wheel metadata |
| mcp | 2.3.0 | MIT | optional MCP; wheel metadata only |

`constraints-test.txt` pins the inspected test/development versions. No optional PostgreSQL driver is bundled or certified. The optional MCP dependency graph must be inventoried if it becomes part of an advertised release installation. The upstream provider's API terms and source copyright ownership are distinct from these package metadata declarations.
