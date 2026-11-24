from collections import defaultdict
from datetime import date, timedelta

import duckdb

AGGREGATES = {"sum": "sum({})", "count": "count({})", "avg": "avg({})", "min": "min({})", "max": "max({})", "count_distinct": "count(DISTINCT {})"}
COMPUTATIONS = {"ratio": "{} / nullif({}, 0)", "difference": "{} - {}", "sum": "{} + {}", "product": "{} * {}", "percent": "100.0 * {} / nullif({}, 0)"}
COMPARISONS = {"=": "=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}
SPELLING_OPS = {"=": "in", "!=": "not_in", "in": "in", "not_in": "not_in"}
DATE_TYPES = {"DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE"}
TEXT = "VARCHAR"
ROW_ID = "_rid"
SPELLINGS = "spelling_map"
PREVIOUS = "previous_"


class ToolError(ValueError):
    pass


def q(name):
    return '"' + name.replace('"', '""') + '"'


def lit(text):
    return "'" + text.replace("'", "''") + "'"


def columns(con, table):
    return {name: kind for name, kind, *_ in con.execute(f"DESCRIBE {q(table)}").fetchall()}


def _require(con, table, *names):
    present = columns(con, table)
    missing = [name for name in names if name not in present]
    if missing:
        raise ToolError(f"columns not in current result: {', '.join(missing)}")
    return present


def _order_key(present):
    return q(ROW_ID) if ROW_ID in present else ", ".join(map(q, present))


def _create(con, target, sql, params=None):
    try:
        con.execute(f"CREATE TABLE {q(target)} AS {sql}", params or [])
    except duckdb.Error as error:
        raise ToolError(str(error).splitlines()[0]) from error


def moment_expr(column, kind, settings):
    if kind in DATE_TYPES:
        return q(column)
    attempts = ", ".join(f"try_strptime(CAST({q(column)} AS VARCHAR), {lit(fmt)})" for fmt in settings["date_formats"])
    return f"coalesce({attempts})"


def date_expr(column, kind, settings):
    return f"CAST({moment_expr(column, kind, settings)} AS DATE)"


def _empty(column):
    return f"({column} IS NULL OR trim(CAST({column} AS VARCHAR)) = '')"


def _condition(args):
    column = q(args["column"])
    if args["op"] == "is_empty":
        return _empty(column), []
    if args["op"] == "not_empty":
        return f"NOT {_empty(column)}", []
    if args["op"] in ("in", "not_in"):
        values = args.get("values")
        if not values:
            raise ToolError(f"operator {args['op']} needs a list of values")
        marks = ", ".join("?" for _ in values)
        negate = "NOT " if args["op"] == "not_in" else ""
        return f"{column} {negate}IN ({marks})", list(values)
    if "value" not in args:
        raise ToolError(f"operator {args['op']} needs a value")
    if args["op"] == "contains":
        return f"CAST({column} AS VARCHAR) ILIKE '%' || ? || '%'", [str(args["value"])]
    return f"{column} {COMPARISONS[args['op']]} ?", [args["value"]]


def _as_text(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return str(int(value)) if float(value).is_integer() else str(value)


def _with_spellings(con, source, args, settings):
    if args["op"] not in SPELLING_OPS:
        return args
    text = columns(con, source)[args["column"]] == TEXT
    asked = [_as_text(value) if text else value for value in args.get("values") or [args.get("value")]]
    if not all(isinstance(value, str) for value in asked):
        return args
    cells = defaultdict(list)
    for (cell,) in con.execute(f"SELECT DISTINCT CAST({q(args['column'])} AS VARCHAR) FROM {q(source)}").fetchall():
        if cell is not None:
            cells[_normalise(cell, settings)].append(cell)
    keys = {_normalise(value, settings) for value in asked if not _digits(value)}
    found = {key if key in cells else _nearest(key, cells, settings) for key in keys} - {None}
    same = [cell for key in found for cell in cells[key]]
    return {"column": args["column"], "op": SPELLING_OPS[args["op"]], "values": [*asked, *same]}


def _ending(a, b, settings):
    short, long = sorted((a, b), key=len)
    return long.startswith(short) and len(long) - len(short) <= settings["spelling_max_ending"]


def _nearest(key, cells, settings):
    close = {
        cell for cell in cells
        if min(len(cell), len(key)) >= settings["spelling_min_length"] and _digits(cell) == _digits(key)
        and (_edits(cell, key) <= settings["spelling_max_edits"] or _ending(cell, key, settings))
    }
    return close.pop() if len(close) == 1 else None


def filter_rows(con, source, target, args, settings):
    _require(con, source, args["column"])
    condition, params = _condition(_with_spellings(con, source, args, settings))
    _create(con, target, f"SELECT * FROM {q(source)} WHERE {condition}", params)
    return []


def remove_rows(con, source, target, args, settings):
    _require(con, source, args["column"])
    condition, params = _condition(_with_spellings(con, source, args, settings))
    _create(con, target, f"SELECT * FROM {q(source)} WHERE NOT coalesce({condition}, false)", params)
    return []


def group_and_total(con, source, target, args, settings):
    group_by = args.get("group_by", [])
    _require(con, source, *group_by, args["value_column"])
    groups = ", ".join(map(q, group_by))
    value = q(args["value_column"])
    total = AGGREGATES[args["agg"]].format(value)
    share = f", 100.0 * {total} / sum({total}) OVER () AS {q(args['value_column'] + settings['share_suffix'])}" if args.get("share") else ""
    if group_by:
        _create(con, target, f"SELECT {groups}, {total} AS {value}{share} FROM {q(source)} GROUP BY {groups}")
    else:
        _create(con, target, f"SELECT {total} AS {value}{share} FROM {q(source)}")
    return []


def _period_bounds(today, unit, back):
    if unit == "year":
        start = date(today.year - back, 1, 1)
        return start, date(start.year + 1, 1, 1)
    size = 3 if unit == "quarter" else 1
    index = (today.year * 12 + (today.month - 1) // size * size) - back * size
    start = date(index // 12, index % 12 + 1, 1)
    end_index = index + size
    return start, date(end_index // 12, end_index % 12 + 1, 1)


def date_bounds(args, settings):
    today = date.fromisoformat(settings["today"]) if settings.get("today") else date.today()
    period = args["period"]
    if period in settings["date_periods"]:
        unit, back = settings["date_periods"][period]
        return _period_bounds(today, unit, back)
    if period in settings["recent_days"]:
        return today - timedelta(days=settings["recent_days"][period]), today + timedelta(days=1)
    if period == "between":
        if not (args.get("from") and args.get("to")):
            raise ToolError("period between needs both from and to")
        try:
            return date.fromisoformat(args["from"]), date.fromisoformat(args["to"]) + timedelta(days=1)
        except ValueError as error:
            raise ToolError(f"bad date: {error}") from error
    raise ToolError(f"unknown period {period}")


def filter_dates(con, source, target, args, settings):
    present = _require(con, source, args["column"])
    expr = date_expr(args["column"], present[args["column"]], settings)
    start, end = date_bounds(args, settings)
    _create(con, target, f"SELECT * FROM {q(source)} WHERE {expr} >= ? AND {expr} < ?", [start, end])
    return []


def sort_rows(con, source, target, args, settings):
    present = _require(con, source, args["by"])
    direction = "DESC" if args["descending"] else "ASC"
    _create(con, target, f"SELECT * FROM {q(source)} ORDER BY {q(args['by'])} {direction} NULLS LAST, {_order_key(present)}")
    return [{"sort": args["by"], "descending": args["descending"]}]


def select_columns(con, source, target, args, settings):
    _require(con, source, *args["columns"])
    _create(con, target, f"SELECT {', '.join(map(q, args['columns']))} FROM {q(source)}")
    return []


def period_compare(con, source, target, args, settings):
    groups = args.get("group_by", [])
    present = _require(con, source, args["date_column"], args["value_column"], *groups)
    if args["period"] not in settings["period_intervals"]:
        raise ToolError(f"unknown period {args['period']}")
    step = settings["compare_intervals"].get(args["compare"], settings["period_intervals"][args["period"]])
    date = date_expr(args["date_column"], present[args["date_column"]], settings)
    value = q(args["value_column"])
    previous = q(PREVIOUS + args["value_column"])
    joins = [f"a.{q(name)} IS NOT DISTINCT FROM p.{q(name)}" for name in groups]
    joins.append(f"p.period = a.period - INTERVAL {lit(step)}")
    sql = (
        f"WITH totals AS (SELECT {''.join(q(n) + ', ' for n in groups)}"
        f"CAST(date_trunc({lit(args['period'])}, {date}) AS DATE) AS period, sum({value}) AS total "
        f"FROM {q(source)} GROUP BY ALL) "
        f"SELECT {''.join('a.' + q(n) + ', ' for n in groups)}a.period AS {q(args['date_column'])}, "
        f"a.total AS {value}, p.total AS {previous}, a.total - p.total AS change "
        f"FROM totals a LEFT JOIN totals p ON {' AND '.join(joins)}"
    )
    _create(con, target, sql)
    return []


def top_n(con, source, target, args, settings):
    by = q(args["by"])
    direction = "DESC" if args["descending"] else "ASC"
    group = args.get("group")
    if group:
        _require(con, source, args["by"], *group["columns"])
        groups = ", ".join(map(q, group["columns"]))
        sql = (
            f"SELECT {groups}, {AGGREGATES[group['agg']].format(by)} AS {by} FROM {q(source)} "
            f"GROUP BY {groups} ORDER BY {by} {direction} NULLS LAST, {groups} LIMIT ?"
        )
    else:
        present = _require(con, source, args["by"])
        sql = f"SELECT * FROM {q(source)} ORDER BY {by} {direction} NULLS LAST, {_order_key(present)} LIMIT ?"
    _create(con, target, sql, [int(args["n"])])
    return []


def fix_dates(con, source, target, args, settings):
    present = _require(con, source, args["column"])
    expr = date_expr(args["column"], present[args["column"]], settings)
    _create(con, target, f"SELECT * REPLACE ({expr} AS {q(args['column'])}) FROM {q(source)}")
    return []


def number_expr(column, settings):
    expr = f"CAST({q(column)} AS VARCHAR)"
    for separator in settings["thousands_separators"]:
        expr = f"replace({expr}, {lit(separator)}, '')"
    return f"TRY_CAST(replace({expr}, {lit(settings['decimal_comma'])}, '.') AS DOUBLE)"


def fix_numbers(con, source, target, args, settings):
    _require(con, source, args["column"])
    expr = number_expr(args["column"], settings)
    _create(con, target, f"SELECT * REPLACE ({expr} AS {q(args['column'])}) FROM {q(source)}")
    return []


def _normalise(value, settings):
    text = "".join(settings["transliteration"].get(ch, ch) for ch in value.strip().lower())
    return "".join(ch for ch in text if ch not in settings["ignored_characters"])


def _edits(a, b):
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def _digits(text):
    return "".join(ch for ch in text if ch.isdigit())


def spelling_map(counts, settings):
    clusters = []
    mapping = {}
    for value, _ in sorted(counts, key=lambda item: (-item[1], item[0])):
        key = _normalise(value, settings)
        canonical = next(
            (name for k, name in clusters if _digits(k) == _digits(key) and _edits(k, key) <= settings["spelling_max_edits"]),
            None,
        )
        if canonical is None:
            canonical = value.strip()
            clusters.append((key, canonical))
        mapping[value] = canonical
    return mapping


def unify_spelling(con, source, target, args, settings):
    _require(con, source, args["column"])
    text = f"CAST(s.{q(args['column'])} AS VARCHAR)"
    counts = con.execute(
        f"SELECT CAST({q(args['column'])} AS VARCHAR), count(*) FROM {q(source)} "
        f"WHERE {q(args['column'])} IS NOT NULL GROUP BY 1"
    ).fetchall()
    con.execute(f"CREATE OR REPLACE TEMP TABLE {SPELLINGS} (original VARCHAR, canonical VARCHAR)")
    if counts:
        con.executemany(f"INSERT INTO {SPELLINGS} VALUES (?, ?)", list(spelling_map(counts, settings).items()))
    _create(
        con, target,
        f"SELECT s.* REPLACE (coalesce(m.canonical, {text}) AS {q(args['column'])}) "
        f"FROM {q(source)} s LEFT JOIN {SPELLINGS} m ON {text} = m.original",
    )
    return []


def remove_duplicates(con, source, target, args, settings):
    present = _require(con, source, *args["columns"])
    partition = ", ".join(map(q, args["columns"]))
    _create(
        con, target,
        f"SELECT * FROM {q(source)} QUALIFY row_number() OVER (PARTITION BY {partition} ORDER BY {_order_key(present)}) = 1",
    )
    return []


def fill_empty(con, source, target, args, settings):
    present = _require(con, source, args["column"])
    column = q(args["column"])
    kind = present[args["column"]]
    _create(con, target, f"SELECT * REPLACE (coalesce({column}, TRY_CAST(? AS {kind})) AS {column}) FROM {q(source)}", [str(args["value"])])
    return []


def make_chart(con, source, target, args, settings):
    names = [args["x"], *([args["group"]] if "group" in args else [])]
    _require(con, source, *names, args["y"])
    keys, y = ", ".join(map(q, names)), q(args["y"])
    if args["agg"] in AGGREGATES:
        _create(con, target, f"SELECT {keys}, {AGGREGATES[args['agg']].format(y)} AS {y} FROM {q(source)} GROUP BY {keys}")
    else:
        _create(con, target, f"SELECT * FROM {q(source)}")
    return [{"chart": args["chart"], "x": args["x"], "y": args["y"], **({"group": args["group"]} if "group" in args else {})}]


def _number_text(value):
    return str(int(value)) if float(value).is_integer() else str(value)


def _given_name(text, present, settings):
    name = text.strip() if isinstance(text, str) else ""
    if not name or len(name) > settings["column_name_max"]:
        raise ToolError(f"a column name needs 1 to {settings['column_name_max']} characters")
    if name.casefold() in {column.casefold() for column in [*present, ROW_ID]}:
        raise ToolError(f"column already exists: {name}")
    return name


def add_computed_column(con, source, target, args, settings):
    constant = "value" in args
    if constant and args["op"] in settings["dividing_ops"] and args["value"] == 0:
        raise ToolError("cannot divide by zero")
    present = _require(con, source, args["left"], *([] if constant else [args["right"]]))
    right = _number_text(args["value"]) if constant else args["right"]
    name = _given_name(args["name"], present, settings) if "name" in args else settings["computed_names"][args["op"]].format(left=args["left"], right=right)
    if name in present:
        raise ToolError(f"column already exists: {name}")
    expr = COMPUTATIONS[args["op"]].format(q(args["left"]), "?" if constant else q(args["right"]))
    _create(con, target, f"SELECT *, {expr} AS {q(name)} FROM {q(source)}", [args["value"]] if constant else None)
    return []


def _export(kind):
    def export(con, source, target, args, settings):
        _create(con, target, f"SELECT * FROM {q(source)}")
        return [{"export": kind, **{key: args[key] for key in settings["export_place_keys"] if args.get(key)}}]
    return export


export_xlsx = _export("xlsx")
export_html = _export("html")


TOOLS = {
    "filter": filter_rows,
    "remove_rows": remove_rows,
    "group_and_total": group_and_total,
    "filter_dates": filter_dates,
    "sort": sort_rows,
    "select_columns": select_columns,
    "period_compare": period_compare,
    "top_n": top_n,
    "fix_dates": fix_dates,
    "fix_numbers": fix_numbers,
    "unify_spelling": unify_spelling,
    "remove_duplicates": remove_duplicates,
    "fill_empty": fill_empty,
    "make_chart": make_chart,
    "add_computed_column": add_computed_column,
    "add_value_column": add_computed_column,
    "export_xlsx": export_xlsx,
    "export_html": export_html,
}
