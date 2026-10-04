"""cygnus.mcp - the Project Connector's MCP server and the read-only helpers
behind it (docs/CONNECTOR.md).

  project_connector  the server: nine tools over stdio
                     (python -m cygnus.mcp.project_connector)
  scanner            path guards, blocked secrets files, content screening,
                     file listing and the project index
  gitinfo            read-only git: status, commits, diffs
  analyzer           static code analysis

The projects themselves live in data/projects.json, owned by
cygnus.agents.project.
"""
