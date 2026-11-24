import argparse
import json
import urllib.request

from prompt import build


def ask(server, context, request, max_tokens):
    body = {
        "messages": build.messages(context, request),
        "temperature": 0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": build.output_schema(context)}},
    }
    call = urllib.request.Request(f"{server}/v1/chat/completions", json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
    with urllib.request.urlopen(call) as response:
        return json.loads(json.load(response)["choices"][0]["message"]["content"])


def main():
    parser = argparse.ArgumentParser(description="Turn one request into a Tez plan for one table")
    parser.add_argument("request")
    parser.add_argument("--table", default="examples/bank_loans", help="folder with table.json, glossary.json, examples.json")
    parser.add_argument("--server", default="http://127.0.0.1:8080")
    parser.add_argument("--max-tokens", type=int, default=200)
    args = parser.parse_args()
    plan = ask(args.server, build.load_context(args.table), args.request, args.max_tokens)
    print(json.dumps(plan, ensure_ascii=False, indent=2))


main()
