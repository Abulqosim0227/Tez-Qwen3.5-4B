import json
from itertools import product
from pathlib import Path

HERE = Path(__file__).parent


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _plain(value, settings):
    if not isinstance(value, str):
        return value
    return value.translate({ord(mark): settings["apostrophe"] for mark in settings["apostrophe_variants"]})


def plain_values(table, settings):
    columns = [{**column, "values": [_plain(value, settings) for value in column["values"]]} if "values" in column else column
               for column in table["columns"]]
    return {**table, "columns": columns}


def make_context(table, glossary, examples):
    settings = _load_json(HERE / "settings.json")
    return {
        "tools": _load_json(HERE / "tools.json"),
        "settings": settings,
        "system": (HERE / "system.txt").read_text(encoding="utf-8"),
        "table": plain_values(table, settings),
        "glossary": glossary,
        "examples": examples,
    }


def load_context(domain):
    domain = Path(domain)
    return make_context(*(_load_json(domain / f"{name}.json") for name in ("table", "glossary", "examples")))


def _describe_type(spec):
    if "ref" in spec:
        return spec["ref"]
    if "properties" in spec:
        return "{" + ", ".join(f"{name}: {_describe_type(sub)}" for name, sub in spec["properties"].items()) + "}"
    if "enum" in spec:
        return "|".join(map(str, spec["enum"]))
    kind = spec["type"]
    return "/".join(kind) if isinstance(kind, list) else kind


def _describe_arg(spec):
    return _describe_type(spec) + ("?" if spec.get("optional") else "")


def tool_lines(tools):
    return "\n".join(
        f"- {tool['name']}({', '.join(f'{name}: {_describe_arg(spec)}' for name, spec in tool['args'].items())}): "
        f"{tool['description']}"
        for tool in tools
    )


def _column_line(column):
    label, values = ("all values", column["values"]) if "values" in column else ("samples", column["samples"])
    return f"- {column['name']} ({column['type']}), {label}: {json.dumps(values, ensure_ascii=False)}"


def column_lines(table):
    return "\n".join(_column_line(column) for column in table["columns"])


def table_titles(table):
    titles = table.get("titles", [])
    return ", titled: " + ", ".join(json.dumps(title, ensure_ascii=False) for title in titles) if titles else ""


def glossary_lines(glossary):
    return "\n".join(f"- {term}: {meaning}" for term, meaning in glossary.items())


def _example_lines(examples):
    return "\n".join(
        f"Request: {example['request']}\nAnswer: {json.dumps(example['answer'], ensure_ascii=False)}"
        for example in examples
    )


def system_prompt(context):
    table = context["table"]
    return (
        context["system"]
        .replace("{max_steps}", str(context["settings"]["max_steps"]))
        .replace("{tools}", tool_lines(context["tools"]))
        .replace("{table_name}", table["name"])
        .replace("{table_rows}", f"{table['rows']:,}")
        .replace("{table_titles}", table_titles(table))
        .replace("{columns}", column_lines(table))
        .replace("{glossary}", glossary_lines(context["glossary"]))
        .replace("{examples}", _example_lines(context["examples"]))
    )


def _path_values(args, path):
    value = args
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return [] if value is None else value if isinstance(value, list) else [value]


def _effective_steps(history):
    steps = []
    for turn in history:
        own = list(turn["answer"].get("steps", ()))
        steps = own if turn["answer"].get("on") == "table" else [*steps, *own]
    return steps


def _step_values(tools, step, key):
    paths = next(tool.get(key, ()) for tool in tools if tool["name"] == step["tool"])
    return [value for path in paths for value in _path_values(step["args"], path)]


def result_groups(tools, history):
    groups = []
    for step in _effective_steps(history):
        groups = _step_values(tools, step, "groups") or groups
    return groups


def number_columns(context, history=()):
    table = context["table"]["columns"]
    names = {column["name"] for column in table}
    numbers = [column["name"] for column in table if column["type"] in context["settings"]["number_types"]]
    if not history:
        return numbers
    columns = history[-1]["columns"]
    fixed = [value for step in _effective_steps(history) for value in _step_values(context["tools"], step, "makes_numbers")]
    groups = result_groups(context["tools"], history)
    totals = [name for name in columns if name not in groups] if groups else []
    extra = [name for name in columns if name not in names]
    return list(dict.fromkeys([*numbers, *fixed, *totals, *extra]))


def listed_values(context, history=()):
    changed = {value for step in _effective_steps(history) for value in _step_values(context["tools"], step, "changes_values")}
    return {column["name"]: column["values"] for column in context["table"]["columns"] if "values" in column and column["name"] not in changed}


def _notes(context, history):
    settings = context["settings"]
    groups = result_groups(context["tools"], history)
    notes = [f"{settings['totalled_note']}: {', '.join(groups)}"] if groups else []
    notes.append(f"{settings['result_note']}: {', '.join(history[-1]['columns'])}")
    return "\n".join(f"[{note}]" for note in notes)


def messages(context, request, history=()):
    turns = [{"role": "system", "content": system_prompt(context)}]
    for turn in history:
        turns.append({"role": "user", "content": turn["request"]})
        turns.append({"role": "assistant", "content": json.dumps(turn["answer"], ensure_ascii=False)})
    if history:
        request = f"{request}\n{_notes(context, history)}"
    turns.append({"role": "user", "content": request})
    return turns


def placed(answer, history):
    if not history or "steps" not in answer:
        return answer
    return {"on": answer.get("on", "result"), **{key: value for key, value in answer.items() if key != "on"}}


def _arg_schema(spec, columns):
    if spec.get("ref") == "column_value":
        values = columns["column_value"]
        return {"enum": values} if values is not None else {key: value for key, value in spec.items() if key not in ("ref", "optional")}
    if "items" in spec:
        return {**_arg_schema({key: value for key, value in spec.items() if key != "items"}, columns), "items": _arg_schema(spec["items"], columns)}
    if spec.get("ref") == "columns":
        return {"type": "array", "items": {"type": "string", "enum": columns["column"]}, "minItems": 1}
    if "ref" in spec:
        return {"type": "string", "enum": columns[spec["ref"]]}
    if "properties" in spec:
        return _object_schema(spec["properties"], columns, list(spec["properties"]))
    return {key: value for key, value in spec.items() if key != "optional"}


def _object_schema(args, columns, required):
    return {
        "type": "object",
        "properties": {name: _arg_schema(spec, columns) for name, spec in args.items()},
        "required": required,
        "additionalProperties": False,
    }


def _has_empty_enum(schema):
    if isinstance(schema, dict):
        return schema.get("enum") == [] or any(_has_empty_enum(value) for value in schema.values())
    return False


def _uses(spec, ref):
    return isinstance(spec, dict) and (spec.get("ref") == ref or any(_uses(value, ref) for value in spec.values()))


def _value_bindings(args, columns):
    if not _uses(args, "column_value"):
        return [columns]
    listed = columns["values"]
    free = [name for name in columns["column"] if name not in listed]
    return [*({**columns, "column": [name], "column_value": values} for name, values in listed.items() if name in columns["column"]),
            {**columns, "column": free, "column_value": None}]


def _key_choices(args):
    optional = [name for name, spec in args.items() if spec.get("optional")]
    for kept in product((True, False), repeat=len(optional)):
        dropped = {name for name, keep in zip(optional, kept) if not keep}
        yield {name: spec for name, spec in args.items() if name not in dropped}


def _tool_schemas(tool, columns):
    for variant in tool.get("variants", [{}]):
        args = {name: spec for name, spec in {**tool["args"], **variant}.items() if spec is not None}
        for chosen, bound in ((chosen, bound) for chosen in _key_choices(args) for bound in _value_bindings(chosen, columns)):
            schema = {
                "type": "object",
                "properties": {
                    "tool": {"const": tool["name"]},
                    "args": _object_schema(chosen, bound, list(chosen)),
                },
                "required": ["tool", "args"],
                "additionalProperties": False,
            }
            if not _has_empty_enum(schema):
                yield schema


def output_schema(context, history=()):
    names = [column["name"] for column in context["table"]["columns"]]
    extra = history[-1]["columns"] if history else ()
    column_names = [*names, *(name for name in dict.fromkeys(extra) if name not in names)]
    columns = {"column": column_names, "number_column": number_columns(context, history), "values": listed_values(context, history)}
    ask = {
        "type": "object",
        "properties": {"ask": {"type": "string", "minLength": 1, "maxLength": 300}},
        "required": ["ask"],
        "additionalProperties": False,
    }
    on = {"on": {"enum": ["result", "table"]}} if history else {}
    steps = {
        "type": "object",
        "properties": {
            **on,
            "steps": {
                "type": "array",
                "items": {"anyOf": [schema for tool in context["tools"] for schema in _tool_schemas(tool, columns)]},
                "minItems": 1,
                "maxItems": context["settings"]["max_steps"],
            },
        },
        "required": [*on, "steps"],
        "additionalProperties": False,
    }
    return {"anyOf": [steps, ask]}
