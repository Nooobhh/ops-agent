"""Extract display-only resources from a structured command pipeline."""
from __future__ import annotations

import re
from typing import Optional, Sequence

from .path_policy import check_paths

_MYSQL_TOOLS = {"mysql", "mysqladmin"}
_SQL_TARGET = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(`?([A-Za-z0-9_.$-]+)`?)",
    re.IGNORECASE,
)


def extract_targets(pipeline: Sequence[Sequence[str]]) -> dict:
    """Return paths and conservative MySQL targets for display."""
    paths = check_paths(pipeline).paths
    mysql: list[dict] = []
    mysql_seen = False
    parsed = True
    for segment in pipeline:
        if not segment or str(segment[0]).rsplit("/", 1)[-1] not in _MYSQL_TOOLS:
            continue
        mysql_seen = True
        database: Optional[str] = None
        execute: Optional[str] = None
        positional: list[str] = []
        tokens = [str(token) for token in segment[1:]]
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.startswith("-D") and token != "-D":
                database = token[2:]
            elif token in {"-D", "--database"} and index + 1 < len(tokens):
                database = tokens[index + 1]
                index += 1
            elif token.startswith("--database="):
                database = token.split("=", 1)[1]
            elif token in {"-e", "--execute"} and index + 1 < len(tokens):
                execute = tokens[index + 1]
                index += 1
            elif token.startswith("--execute="):
                execute = token.split("=", 1)[1]
            elif not token.startswith("-"):
                positional.append(token)
            index += 1
        if database is None and positional:
            database = positional[-1]
        tables = []
        if execute:
            tables = [match.group(2).strip("`") for match in _SQL_TARGET.finditer(execute)]
        if not execute or not tables:
            parsed = False
        mysql.append({"database": database, "tables": tables})
    return {
        "paths": paths,
        "mysql": mysql,
        "mysql_parsed": parsed if mysql_seen else True,
    }
