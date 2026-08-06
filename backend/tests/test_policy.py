from types import SimpleNamespace

import pytest

from app.agents.routing import RequestKind, SemanticRoutePlan
from app.business_data.catalog import BusinessDatasetCatalog
from app.core.errors import NotFoundError
from app.identity.contracts import IdentityContext
from app.policy.contracts import PolicyRequest
from app.policy.providers import ConfigPolicyProvider
from app.tools.contracts import ToolExecutionContext, ToolSpec
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.business_data.contracts import UniversalBusinessDataQueryInput


ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]


def identity(**updates):
    values = dict(
        user_id='u1', tenant_id='tenant-a', org_code='org-a',
        roles=['procurement_manager'], auth_source='test', trusted=True,
    )
    values.update(updates)
    return IdentityContext(**values)


@pytest.mark.asyncio
async def test_config_policy_enforces_dataset_tenant_and_org_scope():
    provider = ConfigPolicyProvider.from_yaml(ROOT / 'config' / 'policies.yaml')
    request = PolicyRequest(
        action='business.data.read',
        resource='dataset:inventory.stock',
        attributes={'target_tenant_id': 'tenant-a', 'target_org_code': 'org-a'},
    )
    allowed = await provider.authorize(identity(), request)
    assert allowed.allowed is True
    assert allowed.obligations['scope_tenant_id'] == 'tenant-a'
    assert allowed.obligations['scope_org_code'] == 'org-a'

    cross_tenant = await provider.authorize(
        identity(), request.model_copy(update={'attributes': {'target_tenant_id': 'tenant-b', 'target_org_code': 'org-a'}})
    )
    assert cross_tenant.allowed is False

    cross_org = await provider.authorize(
        identity(), request.model_copy(update={'attributes': {'target_tenant_id': 'tenant-a', 'target_org_code': 'org-b'}})
    )
    assert cross_org.allowed is False


@pytest.mark.asyncio
async def test_executor_rejects_unknown_dataset_before_handler():
    catalog = BusinessDatasetCatalog.model_validate({
        'version': '1',
        'datasets': [{
            'id': 'inventory.stock', 'version': '1', 'name': 'Stock',
            'description': 'stock', 'domain': 'inventory',
            'connector_id': 'mock', 'enabled': True,
        }],
    })
    registry = ToolRegistry()
    invoked = False

    async def handler(_arguments, _context):
        nonlocal invoked
        invoked = True
        return {}

    registry.register(
        ToolSpec(
            tool_id='data.business.query', version='1', name='business',
            description='business', domain='business_data',
            required_permission='business.data.read',
            input_schema=UniversalBusinessDataQueryInput.model_json_schema(),
        ),
        handler,
        input_model=UniversalBusinessDataQueryInput,
    )
    executor = ToolExecutor(
        registry,
        ConfigPolicyProvider.from_yaml(ROOT / 'config' / 'policies.yaml'),
        SimpleNamespace(),
        dataset_catalog=catalog,
    )
    context = ToolExecutionContext(
        request_id='r1', session_id='s1', graph_id='g', graph_version='1',
        node_id='query', allowed_tools={'data.business.query'}, identity=identity(),
    )
    with pytest.raises(NotFoundError) as exc:
        await executor.execute(
            'data.business.query',
            {'dataset_id': 'finance.invoices'},
            context,
        )
    assert exc.value.code == 'UNSUPPORTED_CAPABILITY'
    assert invoked is False


def test_natural_language_procurement_analysis_is_not_downgraded_to_order():
    route = SemanticRoutePlan.model_validate({
        'request_kind': RequestKind.BUSINESS_QUERY,
        'domain': 'procurement', 'operation': 'query_supplier_spending',
        'entity': 'purchase_orders', 'data_needs': ['business_data'],
        'confidence': 0.9, 'required_tools': ['data.business.query'],
        'tool_arguments': {'data.business.query': {}},
        'summary': 'supplier spending',
    }).stabilize_with_question('这个月采购的钱主要花在哪几家供应商？', today=__import__('datetime').date(2026, 8, 6))
    assert route.to_understanding('这个月采购的钱主要花在哪几家供应商？').intent.value == 'analytics'
