import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
import sqlglot
from pydantic import Field
from sqlglot import exp

from opspilot.models import StrictModel

DATA = Path(__file__).resolve().parents[2] / "data"
if not DATA.is_dir():
    DATA = Path(__file__).with_name("data")


class HealthArgs(StrictModel):
    service: str = Field(min_length=1, max_length=80)


class LogArgs(HealthArgs):
    level: Literal["ALL", "INFO", "WARN", "ERROR"] = "ALL"
    since: str | None = None
    until: str | None = None


class SQLArgs(StrictModel):
    query: str = Field(min_length=1, max_length=2000)


class RunbookArgs(StrictModel):
    query: str = Field(min_length=1, max_length=1000)


MODELS = {
    "get_service_health": HealthArgs,
    "search_logs": LogArgs,
    "run_sql": SQLArgs,
    "search_runbooks": RunbookArgs,
}
DESCRIPTIONS = {
    "get_service_health": "Read the selected fixture service health. No external requests.",
    "search_logs": "Read service logs filtered by level and ISO time range.",
    "run_sql": "Read incident_state(service,status,open_orders); SELECT only, max 100 rows.",
    "search_runbooks": "Find operational runbooks relevant to the observed symptoms.",
}


def load_scenario(sid: str):
    for observation in json.loads((DATA / "observations.json").read_text()):
        if observation["id"] == sid:
            return observation
    raise ValueError("Unknown synthetic scenario")


def safe_sql(path: Path, query: str):
    trees = sqlglot.parse(query, read="sqlite")
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        raise ValueError("Only a single SELECT is allowed")
    tree = trees[0]
    # A deliberately narrow grammar; this is not a general SQL sandbox.
    forbidden = (exp.Subquery, exp.With, exp.Join, exp.Union, exp.Command)
    if any(tree.find(t) is not None for t in forbidden):
        raise ValueError("Subqueries, CTEs, joins and commands are disabled")
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or tables[0].name != "incident_state" or tables[0].db or tables[0].catalog:
        raise ValueError("Only incident_state may be read")
    if any(isinstance(node, exp.Func) for node in tree.walk()):
        raise ValueError("SQL functions are disabled")
    sql = tree.limit(100).sql(dialect="sqlite")
    c = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        c.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 0.2
        c.set_progress_handler(lambda: int(time.monotonic() > deadline), 100)
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ}

        def authorize(action, arg1, arg2, database, source):
            if action == sqlite3.SQLITE_READ and arg1 != "incident_state":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        c.set_authorizer(authorize)
        cur = c.execute(sql)
        names = [item[0] for item in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchmany(100)]
    finally:
        c.close()


class Tools:
    def __init__(self, settings, scenario, incident_id):
        self.settings = settings
        self.scenario = scenario
        folder = settings.state_dir / "fixtures"
        folder.mkdir(exist_ok=True)
        self.db_path = folder / f"{incident_id}.sqlite"
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                (
                    "CREATE TABLE IF NOT EXISTS incident_state(service "
                    "TEXT,status TEXT,open_orders INT)"
                )
            )
            if not c.execute("SELECT count(*) FROM incident_state").fetchone()[0]:
                c.execute(
                    "INSERT INTO incident_state VALUES (?,?,?)",
                    (scenario["service"], scenario["db_state"], scenario["open_orders"]),
                )

    def call(self, name, args):
        if name not in MODELS:
            raise ValueError("Tool is not allowed")
        args = MODELS[name].model_validate(args).model_dump()
        if name in {"get_service_health", "search_logs"}:
            if args["service"] != self.scenario["service"]:
                raise ValueError("Service is outside this incident's scope")
        if name == "get_service_health":
            return {"service": args["service"], **self.scenario["health"]}
        if name == "search_logs":
            since = datetime.fromisoformat(args["since"]) if args["since"] else None
            until = datetime.fromisoformat(args["until"]) if args["until"] else None
            logs = []
            for item in self.scenario["logs"]:
                stamp = datetime.fromisoformat(item["timestamp"])
                if args["level"] not in ("ALL", item["level"]):
                    continue
                if (since and stamp < since) or (until and stamp > until):
                    continue
                logs.append(item)
            return logs[:50]
        if name == "run_sql":
            return safe_sql(self.db_path, args["query"])
        if self.settings.docurag_url:
            # Fixed admin-configured endpoint, not a model-supplied URL.
            url = self.settings.docurag_url.rstrip("/") + "/search"
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                response = client.post(
                    url,
                    json={"query": args["query"], "mode": "keyword", "top_k": 3, "rerank": False},
                )
                response.raise_for_status()
                return [
                    {"id": h["chunk_id"], "title": h["filename"], "content": h["content"][:4000]}
                    for h in response.json()["results"][:3]
                ]
        query = set(re.findall(r"[a-z0-9]+", args["query"].lower()))
        books = json.loads((DATA / "runbooks.json").read_text())
        scored = [
            (len(query & set(re.findall(r"[a-z0-9]+", b["title"] + " " + b["content"].lower()))), b)
            for b in books
        ]
        return [
            b for score, b in sorted(scored, key=lambda pair: pair[0], reverse=True)[:3] if score
        ]
