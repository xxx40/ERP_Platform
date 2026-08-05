import ipaddress
import json
import socket
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


class DataSourceSecurityError(ValueError):
    pass


class GovernedDataSourceService:
    DATABASE_DRIVERS = {
        "postgresql": "postgresql+psycopg",
        "mysql": "mysql+pymysql",
        "sqlserver": "mssql+pyodbc",
        "oracle": "oracle+oracledb",
    }

    def __init__(self, repository, secret_provider, settings) -> None:
        self.repository = repository
        self.secret_provider = secret_provider
        self.settings = settings
        self.allowed_networks = [
            ipaddress.ip_network(value, strict=False)
            for value in settings.allowed_data_connector_cidrs
        ]

    def require_secret_provider(self) -> None:
        if self.secret_provider is None:
            raise RuntimeError("A configured SecretProvider is required")

    def create_secret_payload(self, request) -> str:
        if request.dialect == "http":
            return json.dumps(
                {"base_url": request.base_url, "api_token": request.api_token},
                ensure_ascii=False,
            )
        driver = self.DATABASE_DRIVERS[request.dialect]
        dsn = (
            f"{driver}://{quote(request.username, safe='')}:{quote(request.password, safe='')}"
            f"@{request.host}:{request.port}/{quote(request.database_name, safe='')}"
        )
        return dsn

    def create_rotated_secret_payload(self, connection: dict, request) -> str:
        """Build a replacement secret without exposing the current value to clients."""

        self.require_secret_provider()
        current_value = self.secret_provider.get(connection["secret_id"])
        if connection["dialect"] == "http":
            if request.username is not None or request.password is not None:
                raise DataSourceSecurityError(
                    "HTTP data sources only accept base_url and api_token rotation"
                )
            current = json.loads(current_value)
            if request.base_url is not None:
                current["base_url"] = request.base_url
            if request.api_token is not None:
                current["api_token"] = request.api_token
            return json.dumps(current, ensure_ascii=False)

        if request.base_url is not None or request.api_token is not None:
            raise DataSourceSecurityError(
                "database sources only accept username and password rotation"
            )
        url = make_url(current_value)
        rotated = url.set(
            username=request.username if request.username is not None else url.username,
            password=request.password if request.password is not None else url.password,
        )
        return rotated.render_as_string(hide_password=False)

    @staticmethod
    def mask_host(host: str | None) -> str:
        if not host:
            return "********"
        if len(host) <= 4:
            return "****"
        return f"{host[:2]}***{host[-2:]}"

    def assert_endpoint_allowed(self, host: str) -> list[str]:
        normalized = host.strip().rstrip(".")
        if not normalized or normalized.lower() in {"localhost", "localhost.localdomain"}:
            raise DataSourceSecurityError("localhost and loopback targets are forbidden")
        try:
            addresses = {str(ipaddress.ip_address(normalized))}
        except ValueError:
            try:
                addresses = {
                    item[4][0]
                    for item in socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
                }
            except socket.gaierror as exc:
                raise DataSourceSecurityError("data source host cannot be resolved") from exc
        for raw in addresses:
            address = ipaddress.ip_address(raw)
            approved = any(address in network for network in self.allowed_networks)
            if (
                address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or address.is_reserved
                or (not address.is_global and not approved)
            ):
                raise DataSourceSecurityError(
                    f"data source address is outside approved networks: {address}"
                )
        return sorted(addresses)

    def test_and_introspect(self, connection: dict, *, introspect_schema: bool) -> dict:
        self.require_secret_provider()
        secret_value = self.secret_provider.get(connection["secret_id"])
        if connection["dialect"] == "http":
            secret = json.loads(secret_value)
            base_url = str(secret["base_url"]).rstrip("/")
            parsed = urlparse(base_url)
            if parsed.scheme != "https" and self.settings.app_env.lower() == "production":
                raise DataSourceSecurityError("production HTTP data sources require HTTPS")
            before = self.assert_endpoint_allowed(parsed.hostname or "")
            headers = {}
            if secret.get("api_token"):
                headers["Authorization"] = f"Bearer {secret['api_token']}"
            response = httpx.get(
                f"{base_url}/api/v1/health",
                headers=headers,
                timeout=self.settings.data_connector_test_timeout_seconds,
            )
            response.raise_for_status()
            after = self.assert_endpoint_allowed(parsed.hostname or "")
            if before != after:
                raise DataSourceSecurityError("DNS answer changed during connector test")
            return {
                "ready": True,
                "read_only_verified": True,
                "type": "data_http",
                "tables": [],
                "resolved_addresses": after,
            }
        url = make_url(secret_value)
        host = str(url.host or "")
        before = self.assert_endpoint_allowed(host)
        engine = create_engine(
            secret_value,
            pool_pre_ping=True,
            connect_args={},
        )
        try:
            with engine.connect() as connection_handle:
                connection_handle.execute(text("SELECT 1")).first()
                read_only = self._verify_read_only(connection["dialect"], connection_handle)
            after = self.assert_endpoint_allowed(host)
            if before != after:
                raise DataSourceSecurityError("DNS answer changed during connector test")
            tables = []
            if introspect_schema:
                inspector = inspect(engine)
                for schema in inspector.get_schema_names():
                    if schema.lower() in {"information_schema", "pg_catalog", "sys"}:
                        continue
                    for table_name in inspector.get_table_names(schema=schema):
                        tables.append(
                            {
                                "schema": schema,
                                "name": table_name,
                                "columns": [
                                    {
                                        **{
                                            key: value
                                            for key, value in column.items()
                                            if key != "type"
                                        },
                                        "type": str(column.get("type") or "unknown"),
                                    }
                                    for column in inspector.get_columns(
                                        table_name, schema=schema
                                    )
                                ],
                                "primary_key": inspector.get_pk_constraint(table_name, schema=schema),
                                "foreign_keys": inspector.get_foreign_keys(table_name, schema=schema),
                            }
                        )
            return {
                "ready": True,
                "read_only_verified": read_only,
                "type": "database",
                "tables": tables,
                "resolved_addresses": after,
            }
        finally:
            engine.dispose()

    @staticmethod
    def _verify_read_only(dialect: str, connection) -> bool:
        checks = {
            "postgresql": "SELECT NOT has_database_privilege(current_user, current_database(), 'CREATE')",
            "mysql": "SELECT @@transaction_read_only",
            "sqlserver": "SELECT CASE WHEN HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE') = 0 THEN 1 ELSE 0 END",
            "oracle": "SELECT CASE WHEN COUNT(*) = 0 THEN 1 ELSE 0 END FROM SESSION_PRIVS WHERE PRIVILEGE IN ('CREATE TABLE','INSERT ANY TABLE','UPDATE ANY TABLE','DELETE ANY TABLE')",
        }
        try:
            return bool(connection.execute(text(checks[dialect])).scalar())
        except Exception:
            return False

    @staticmethod
    def validate_logical_model(model: dict) -> dict:
        """Validate the safe logical model before it reaches the data worker."""

        errors: list[str] = []
        sources = list(model.get("sources") or [])
        relationships = list(model.get("relationships") or [])
        fields = list(model.get("fields") or [])
        metrics = list(model.get("metrics") or [])
        grain = list(model.get("grain") or [])
        scope = str(model.get("scope") or "")

        aliases = [str(item.get("alias") or "") for item in sources]
        field_names = [str(item.get("name") or "") for item in fields]
        field_name_set = set(field_names)
        field_map = {str(item.get("name") or ""): item for item in fields}

        if not sources:
            errors.append("模型至少需要一个数据表。")
        if not fields:
            errors.append("模型至少需要一个可查询字段。")
        if not grain:
            errors.append("模型必须声明 Grain（数据的唯一粒度）。")
        if set(grain) - field_name_set:
            errors.append("Grain 引用了未注册字段。")
        if len(field_names) != len(field_name_set) or any(not name for name in field_names):
            errors.append("字段名称必须非空且唯一。")
        if len(aliases) != len(set(aliases)) or any(not alias for alias in aliases):
            errors.append("数据表别名必须非空且唯一。")

        source_aliases = set(aliases)
        for field in fields:
            if str(field.get("source") or "") not in source_aliases:
                errors.append(f"字段 {field.get('name') or '<未命名>'} 引用了不存在的数据表。")

        parent = {alias: alias for alias in aliases}

        def root(value: str) -> str:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        for relation in relationships:
            left = str(relation.get("left_source") or "")
            right = str(relation.get("right_source") or "")
            if left not in parent or right not in parent:
                errors.append("Join 引用了不存在的数据表。")
                continue
            if relation.get("cardinality") == "many_to_many":
                errors.append("多对多关系必须先建立显式桥接模型。")
            left_root, right_root = root(left), root(right)
            if left_root == right_root:
                errors.append("模型存在循环 Join。")
            else:
                parent[left_root] = right_root
            if metrics and relation.get("cardinality") == "one_to_many":
                errors.append("当前指标会经过一对多 Join，存在重复聚合 fan-out 风险。")
        if sources and len(relationships) != len(sources) - 1:
            errors.append("Join 必须形成一棵连通树。")

        metric_names = [str(item.get("name") or "") for item in metrics]
        metric_name_set = set(metric_names)
        if len(metric_names) != len(metric_name_set) or any(
            not name for name in metric_names
        ):
            errors.append("指标 ID 必须非空且唯一。")
        supported_aggregations = {
            "count",
            "count_distinct",
            "sum",
            "avg",
            "min",
            "max",
            "ratio",
        }
        numeric_types = {"integer", "number"}
        for metric in metrics:
            name = str(metric.get("name") or "<未命名>")
            aggregation = str(metric.get("aggregation") or "")
            field_name = metric.get("field")
            if aggregation not in supported_aggregations:
                errors.append(f"指标 {name} 使用了不支持的聚合函数。")
                continue
            if aggregation == "ratio":
                if (
                    metric.get("numerator") not in metric_name_set
                    or metric.get("denominator") not in metric_name_set
                ):
                    errors.append(f"比例指标 {name} 引用了不存在的基础指标。")
            elif aggregation != "count" and not field_name:
                errors.append(f"指标 {name} 必须选择来源字段。")
            if field_name and field_name not in field_name_set:
                errors.append(f"指标 {name} 引用了不存在的字段。")
            if (
                aggregation in {"sum", "avg"}
                and field_name in field_map
                and field_map[field_name].get("data_type") not in numeric_types
            ):
                errors.append(f"指标 {name} 的 sum/avg 来源字段必须是数字类型。")
            if set(metric.get("allowed_dimensions") or []) - field_name_set:
                errors.append(f"指标 {name} 包含不存在的可用维度。")

        restricted = {
            str(item.get("name"))
            for item in fields
            if item.get("sensitivity") == "restricted"
        }
        if restricted and not model.get("required_permission"):
            errors.append("受限字段必须声明独立权限。")

        security_fields = {
            "tenant_field": model.get("tenant_field"),
            "org_field": model.get("org_field"),
            "owner_field": model.get("owner_field"),
            "access_scope_field": model.get("access_scope_field"),
        }
        for setting, field_name in security_fields.items():
            if field_name and field_name not in field_name_set:
                errors.append(f"{setting} 引用了不存在的字段。")
        if scope not in {"personal", "team", "tenant"}:
            errors.append("模型范围必须是 personal、team 或 tenant。")
        if not model.get("tenant_field") and (
            scope in {"team", "tenant"} or len(sources) > 1
        ):
            errors.append("团队、租户或多表模型必须声明 tenant_field。")
        if scope == "personal" and not all(
            (model.get("owner_field"), model.get("access_scope_field"))
        ):
            errors.append("个人模型必须声明 owner_field 和 access_scope_field。")

        try:
            max_rows = int(model.get("max_rows") or 0)
        except (TypeError, ValueError):
            max_rows = 0
        if max_rows < 1 or max_rows > 5000:
            errors.append("max_rows 必须在 1..5000。")
        return {"valid": not errors, "errors": list(dict.fromkeys(errors))}
