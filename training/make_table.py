import argparse
import json
from pathlib import Path

import duckdb

from accuracy.tools import q


def _samples(con, table, column, limit):
    rows = con.execute(
        f"SELECT {q(column)}, count(*) AS n FROM {q(table)} WHERE {q(column)} IS NOT NULL "
        f"GROUP BY 1 ORDER BY n DESC, CAST({q(column)} AS VARCHAR) LIMIT ?",
        [limit],
    ).fetchall()
    return [value if isinstance(value, (int, float, str)) else str(value) for value, _ in rows]


def describe_table(con, table, name, type_names, samples, values_limit, values_chars_limit=0):
    columns = []
    for column, kind, *_ in con.execute(f"DESCRIBE {q(table)}").fetchall():
        if kind not in type_names:
            raise ValueError(f"column '{column}' has type {kind} with no name in the type map")
        distinct, chars = con.execute(
            f"SELECT count(*), coalesce(sum(length(CAST(value AS VARCHAR))), 0) "
            f"FROM (SELECT DISTINCT {q(column)} AS value FROM {q(table)} WHERE {q(column)} IS NOT NULL)"
        ).fetchone()
        fits = type_names[kind] == "text" and 0 < chars <= values_chars_limit
        if 0 < distinct <= values_limit or fits:
            columns.append({"name": column, "type": type_names[kind], "values": _samples(con, table, column, distinct)})
        else:
            columns.append({"name": column, "type": type_names[kind], "samples": _samples(con, table, column, samples)})
    rows = con.execute(f"SELECT count(*) FROM {q(table)}").fetchone()[0]
    return {"name": name, "rows": rows, "columns": columns}


def parse_args():
    parser = argparse.ArgumentParser(description="Write a domain table.json from a data file")
    parser.add_argument("--data", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--report")
    return parser.parse_args()


def main():
    args = parse_args()
    settings = json.loads(Path(args.settings).read_text(encoding="utf-8"))
    con = duckdb.connect()
    con.execute("CREATE TABLE source AS SELECT * FROM read_csv(?)", [args.data])
    table = describe_table(
        con, "source", args.name, settings["type_names"], settings["samples_per_column"],
        settings["values_limit"], settings["values_chars_limit"],
    )
    titles = json.loads(Path(args.report).read_text(encoding="utf-8"))["titles"] if args.report else []
    table = {**table, "titles": titles} if titles else table
    Path(args.out).write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: {len(table['columns'])} columns, {table['rows']} rows")


if __name__ == "__main__":
    main()
