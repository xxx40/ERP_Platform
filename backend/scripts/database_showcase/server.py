from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[3]
STATIC_ROOT = Path(__file__).resolve().parent
BUSINESS_DB = PROJECT_ROOT / "purchase_order_service" / "data" / "purchase_orders.db"
PLATFORM_DB = PROJECT_ROOT / "backend" / "data" / "app.db"
ORDER_PREVIEW_LIMIT = 400
LINE_PREVIEW_LIMIT = 1_200
DOCUMENT_PREVIEW_LIMIT = 1_200

TRACE_ATTRIBUTE_ALLOWLIST = {
    "aspect_count",
    "candidate_count",
    "chunk_count",
    "connector_id",
    "degraded",
    "dimension_count",
    "document_count",
    "fallback_reason",
    "follow_up_queries",
    "http_status",
    "input_tokens",
    "intent",
    "metric_count",
    "metric_version",
    "missing_aspects",
    "mock_data",
    "model",
    "output_tokens",
    "plan_strategy",
    "planner",
    "query",
    "query_count",
    "response_status",
    "result_chunk_count",
    "selected_source_count",
    "strategy",
    "sufficient",
}


@contextmanager
def open_database(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=2,
    )
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def rows(connection: sqlite3.Connection, statement: str, parameters: tuple = ()) -> list[dict]:
    return [dict(row) for row in connection.execute(statement, parameters).fetchall()]


def scalar(connection: sqlite3.Connection, statement: str, parameters: tuple = ()) -> Any:
    result = connection.execute(statement, parameters).fetchone()
    return result[0] if result else None


def parse_json(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def short_id(value: str | None, width: int = 8) -> str:
    if not value:
        return "-"
    return value if len(value) <= width else f"{value[:width]}..."


def database_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def summary_payload() -> dict[str, Any]:
    with open_database(BUSINESS_DB) as business, open_database(PLATFORM_DB) as platform:
        business_counts = {
            "orders": scalar(business, "SELECT COUNT(*) FROM purchase_orders"),
            "lines": scalar(business, "SELECT COUNT(*) FROM purchase_order_lines"),
            "documents": scalar(business, "SELECT COUNT(*) FROM related_documents"),
            "period_metrics": scalar(business, "SELECT COUNT(*) FROM purchase_period_metrics"),
            "dimension_metrics": scalar(
                business, "SELECT COUNT(*) FROM purchase_dimension_metrics"
            ),
        }
        dataset_profile = dict(
            business.execute(
                """
                SELECT MIN(order_date) AS start_date,
                       MAX(order_date) AS end_date,
                       COUNT(DISTINCT supplier_code) AS suppliers,
                       COUNT(DISTINCT buyer_code) AS buyers,
                       COUNT(DISTINCT purchase_org_code) AS organizations,
                       ROUND(SUM(total_amount), 2) AS total_amount
                FROM purchase_orders
                """
            ).fetchone()
        )
        dataset_profile["materials"] = scalar(
            business,
            "SELECT COUNT(DISTINCT material_code) FROM purchase_order_lines",
        )
        dataset_profile["synthetic_order_count"] = scalar(
            business,
            "SELECT value FROM connector_metadata WHERE key = 'synthetic_order_count'",
        )
        platform_counts = {
            "conversations": scalar(platform, "SELECT COUNT(*) FROM conversations"),
            "interactions": scalar(platform, "SELECT COUNT(*) FROM interactions"),
            "evidence": scalar(platform, "SELECT COUNT(*) FROM source_evidence"),
            "trace_spans": scalar(platform, "SELECT COUNT(*) FROM trace_spans"),
            "feedback": scalar(platform, "SELECT COUNT(*) FROM answer_feedback"),
            "workflow_runs": scalar(platform, "SELECT COUNT(*) FROM workflow_runs"),
            "tool_calls": scalar(platform, "SELECT COUNT(*) FROM workflow_tool_calls"),
            "policy_decisions": scalar(
                platform, "SELECT COUNT(*) FROM workflow_policy_decisions"
            ),
        }
        recent_rows = rows(
            platform,
            """
            SELECT request_id, session_id, question, response_json, created_at
            FROM interactions ORDER BY id DESC LIMIT 12
            """,
        )
        recent_requests = []
        for item in recent_rows:
            response = parse_json(item.pop("response_json", None))
            understanding = response.get("understanding") or {}
            error = response.get("error") or {}
            item.update(
                {
                    "request_short": short_id(item["request_id"]),
                    "session_short": short_id(item["session_id"]),
                    "status": response.get("status") or "unknown",
                    "intent": understanding.get("intent") or "unknown",
                    "error_code": error.get("code"),
                }
            )
            recent_requests.append(item)
    return {
        "read_only": True,
        "business_db": database_info(BUSINESS_DB),
        "platform_db": database_info(PLATFORM_DB),
        "business_counts": business_counts,
        "dataset_profile": dataset_profile,
        "platform_counts": platform_counts,
        "recent_requests": recent_requests,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def business_payload() -> dict[str, Any]:
    with open_database(BUSINESS_DB) as connection:
        orders = rows(
            connection,
            """
            SELECT id, order_number, order_type, bill_status_code,
                   business_status_code, logistics_status_code, supplier_name,
                   purchase_org_name, buyer_name, order_date, currency,
                   total_amount, tenant_id, org_code, owner_user_id,
                   access_scope, status_reason
            FROM purchase_orders ORDER BY order_date DESC, order_number
            LIMIT ?
            """,
            (ORDER_PREVIEW_LIMIT,),
        )
        lines = rows(
            connection,
            """
            SELECT po.order_number, pol.line_no, pol.material_code,
                   pol.material_name, pol.ordered_qty, pol.received_qty,
                   pol.inbound_qty, pol.unit, pol.line_amount,
                   pol.warehouse_name, pol.promised_date
            FROM purchase_order_lines pol
            JOIN purchase_orders po ON po.id = pol.order_id
            ORDER BY po.order_date DESC, po.order_number, pol.line_no
            LIMIT ?
            """,
            (LINE_PREVIEW_LIMIT,),
        )
        documents = rows(
            connection,
            """
            SELECT po.order_number, rd.document_type_label, rd.document_number,
                   rd.status_code, rd.business_date, rd.source_line_no
            FROM related_documents rd
            JOIN purchase_orders po ON po.id = rd.order_id
            ORDER BY po.order_date DESC, po.order_number, rd.id
            LIMIT ?
            """,
            (DOCUMENT_PREVIEW_LIMIT,),
        )
        period_metrics = rows(
            connection,
            """
            SELECT period_key, period_type, period_label, purchase_amount,
                   order_count, average_order_amount, on_time_rate, currency,
                   data_as_of, comparison_key, year_over_year_key
            FROM purchase_period_metrics
            ORDER BY start_date DESC
            """,
        )
        current_period = next(
            (item for item in period_metrics if item["period_type"] == "quarter_to_date"),
            period_metrics[0] if period_metrics else None,
        )
        dimensions = []
        if current_period:
            dimensions = rows(
                connection,
                """
                SELECT dimension_type, dimension_code, dimension_name,
                       purchase_amount, order_count
                FROM purchase_dimension_metrics
                WHERE period_key = ?
                ORDER BY dimension_type, purchase_amount DESC
                """,
                (current_period["period_key"],),
            )
        definitions = rows(
            connection,
            """
            SELECT metric_key, version, label, unit, definition, formula,
                   allowed_dimensions, effective_from, is_active
            FROM analytics_metric_definitions ORDER BY metric_key
            """,
        )
    return {
        "orders": orders,
        "lines": lines,
        "documents": documents,
        "preview": {
            "orders": {"returned": len(orders), "limit": ORDER_PREVIEW_LIMIT},
            "lines": {"returned": len(lines), "limit": LINE_PREVIEW_LIMIT},
            "documents": {
                "returned": len(documents),
                "limit": DOCUMENT_PREVIEW_LIMIT,
            },
        },
        "period_metrics": period_metrics,
        "current_period": current_period,
        "dimensions": dimensions,
        "metric_definitions": definitions,
    }


def platform_payload() -> dict[str, Any]:
    with open_database(PLATFORM_DB) as connection:
        interactions_raw = rows(
            connection,
            """
            SELECT request_id, session_id, question, response_json, created_at
            FROM interactions ORDER BY id DESC LIMIT 40
            """,
        )
        interactions = []
        for item in interactions_raw:
            response = parse_json(item.pop("response_json", None))
            understanding = response.get("understanding") or {}
            error = response.get("error") or {}
            sources = response.get("sources") or []
            item.update(
                {
                    "request_short": short_id(item["request_id"]),
                    "session_short": short_id(item["session_id"]),
                    "status": response.get("status") or "unknown",
                    "intent": understanding.get("intent") or "unknown",
                    "summary": understanding.get("summary"),
                    "source_count": len(sources) if isinstance(sources, list) else 0,
                    "error_code": error.get("code"),
                }
            )
            interactions.append(item)
        evidence = rows(
            connection,
            """
            SELECT request_id, source_id, title, filename, source_system,
                   authority_level, score, source_updated_at,
                   LENGTH(content) AS content_chars, created_at
            FROM source_evidence ORDER BY id DESC LIMIT 40
            """,
        )
        for item in evidence:
            item["request_short"] = short_id(item["request_id"])
        feedback = rows(
            connection,
            """
            SELECT request_id, rating, reason_codes, comment, user_id,
                   tenant_id, org_code, created_at, updated_at
            FROM answer_feedback ORDER BY id DESC LIMIT 30
            """,
        )
        for item in feedback:
            item["request_short"] = short_id(item["request_id"])
        workflow_runs = rows(
            connection,
            """
            SELECT wr.request_id, wr.workflow_id, wr.workflow_version,
                   wr.user_id, wr.tenant_id, wr.org_code, wr.status,
                   wr.started_at, wr.ended_at, wr.error_code,
                   COUNT(DISTINCT wnr.id) AS node_count,
                   COUNT(DISTINCT wtc.id) AS tool_call_count,
                   COUNT(DISTINCT CASE WHEN wpd.allowed = 0 THEN wpd.id END)
                       AS denied_count
            FROM workflow_runs wr
            LEFT JOIN workflow_node_runs wnr ON wnr.request_id = wr.request_id
            LEFT JOIN workflow_tool_calls wtc ON wtc.request_id = wr.request_id
            LEFT JOIN workflow_policy_decisions wpd ON wpd.request_id = wr.request_id
            GROUP BY wr.request_id
            ORDER BY wr.started_at DESC LIMIT 40
            """,
        )
        for item in workflow_runs:
            item["request_short"] = short_id(item["request_id"])
        tool_calls = rows(
            connection,
            """
            SELECT request_id, node_id, tool_id, tool_version, connector_id,
                   arguments, status, duration_ms, error_code, started_at
            FROM workflow_tool_calls ORDER BY id DESC LIMIT 60
            """,
        )
        for item in tool_calls:
            item["request_short"] = short_id(item["request_id"])
        policy_decisions = rows(
            connection,
            """
            SELECT request_id, node_id, tool_id, user_id, action, resource,
                   allowed, reason, policy_id, policy_version, created_at
            FROM workflow_policy_decisions ORDER BY id DESC LIMIT 60
            """,
        )
        for item in policy_decisions:
            item["request_short"] = short_id(item["request_id"])
    return {
        "interactions": interactions,
        "evidence": evidence,
        "feedback": feedback,
        "workflow_runs": workflow_runs,
        "tool_calls": tool_calls,
        "policy_decisions": policy_decisions,
    }


def trace_list_payload() -> dict[str, Any]:
    with open_database(PLATFORM_DB) as connection:
        interaction_rows = rows(
            connection,
            """
            SELECT request_id, question, response_json, created_at
            FROM interactions ORDER BY id DESC LIMIT 30
            """,
        )
        requests = []
        for item in interaction_rows:
            response = parse_json(item.pop("response_json", None))
            span_summary = connection.execute(
                """
                SELECT COUNT(*) AS span_count,
                       MAX(duration_ms) AS max_duration_ms,
                       SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count
                FROM trace_spans WHERE request_id = ?
                """,
                (item["request_id"],),
            ).fetchone()
            error = response.get("error") or {}
            item.update(
                {
                    "request_short": short_id(item["request_id"]),
                    "status": response.get("status") or "unknown",
                    "intent": (response.get("understanding") or {}).get("intent")
                    or "unknown",
                    "error_code": error.get("code"),
                    "span_count": span_summary["span_count"] if span_summary else 0,
                    "max_duration_ms": round(
                        float(span_summary["max_duration_ms"] or 0), 1
                    ),
                    "error_count": span_summary["error_count"] if span_summary else 0,
                }
            )
            requests.append(item)
    return {"requests": requests}


def trace_detail_payload(request_id: str) -> dict[str, Any] | None:
    with open_database(PLATFORM_DB) as connection:
        interaction = connection.execute(
            """
            SELECT request_id, session_id, question, response_json, created_at
            FROM interactions WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if interaction is None:
            return None
        interaction_dict = dict(interaction)
        response = parse_json(interaction_dict.pop("response_json", None))
        interaction_dict.update(
            {
                "status": response.get("status") or "unknown",
                "intent": (response.get("understanding") or {}).get("intent")
                or "unknown",
                "error": response.get("error"),
            }
        )
        span_rows = rows(
            connection,
            """
            SELECT span_id, name, kind, status, started_at, ended_at,
                   duration_ms, attributes, error_code
            FROM trace_spans WHERE request_id = ? ORDER BY id
            """,
            (request_id,),
        )
        for span in span_rows:
            attributes = parse_json(span.pop("attributes", None))
            span["attributes"] = {
                key: attributes[key]
                for key in TRACE_ATTRIBUTE_ALLOWLIST
                if key in attributes
            }
    return {"interaction": interaction_dict, "spans": span_rows}


def schema_payload() -> dict[str, Any]:
    databases = {}
    for key, path in (("business", BUSINESS_DB), ("platform", PLATFORM_DB)):
        with open_database(path) as connection:
            table_names = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                ).fetchall()
            ]
            tables = []
            for table_name in table_names:
                columns = [
                    {
                        "name": row[1],
                        "type": row[2] or "-",
                        "nullable": not bool(row[3]),
                        "primary_key": bool(row[5]),
                    }
                    for row in connection.execute(
                        f'PRAGMA table_info("{table_name}")'
                    ).fetchall()
                ]
                tables.append(
                    {
                        "name": table_name,
                        "row_count": scalar(
                            connection, f'SELECT COUNT(*) FROM "{table_name}"'
                        ),
                        "columns": columns,
                    }
                )
            databases[key] = {"info": database_info(path), "tables": tables}
    return {"databases": databases}


class ShowcaseHandler(BaseHTTPRequestHandler):
    server_version = "ERPDatabaseShowcase/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/summary":
                self.send_json(summary_payload())
                return
            if path == "/api/business":
                self.send_json(business_payload())
                return
            if path == "/api/platform":
                self.send_json(platform_payload())
                return
            if path == "/api/traces":
                self.send_json(trace_list_payload())
                return
            if path.startswith("/api/traces/"):
                request_id = path.removeprefix("/api/traces/").strip()
                if not request_id or len(request_id) > 64:
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                payload = trace_detail_payload(request_id)
                if payload is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    self.send_json(payload)
                return
            if path == "/api/schema":
                self.send_json(schema_payload())
                return
            self.send_static(path)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.send_json(
                {"error": "DATABASE_READ_FAILED", "message": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, request_path: str) -> None:
        filename = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        if filename not in {"index.html", "app.js", "styles.css"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        file_path = STATIC_ROOT / filename
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'",
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only ERP database showcase")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8201, type=int)
    arguments = parser.parse_args()
    if arguments.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("The showcase must only bind to the local machine.")
    if not BUSINESS_DB.is_file() or not PLATFORM_DB.is_file():
        raise SystemExit("Required SQLite database files were not found.")
    server = ThreadingHTTPServer((arguments.host, arguments.port), ShowcaseHandler)
    print(f"Database showcase: http://{arguments.host}:{arguments.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
