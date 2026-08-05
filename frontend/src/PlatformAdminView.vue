<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import mermaid from "mermaid";
import {
  CheckCircle2,
  Database,
  History,
  GitBranch,
  Layers3,
  LoaderCircle,
  KeyRound,
  Plug,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  TableProperties,
  Trash2,
  Upload,
  Wrench,
} from "@lucide/vue";
import {
  configurePlatformConnectors,
  configurePlatformDatasets,
  configurePlatformHttpTools,
  createPlatformSecret,
  deletePlatformSecret,
  getPlatformDatasets,
  getPlatformHttpTools,
  getPlatformProviders,
  getPlatformConfigStatus,
  getPlatformHarness,
  getPlatformConnectors,
  introspectPlatformConnector,
  listPlatformCapabilities,
  listPlatformConfigVersions,
  listPlatformGraphs,
  listPlatformEvaluations,
  listPlatformModules,
  listPlatformSecrets,
  previewPlatformDataset,
  publishPlatformConfig,
  rollbackPlatformConfig,
  testPlatformConnector,
  validatePlatformConfig,
  createGovernedDataSource,
  createSemanticModel,
  createSemanticModelVersion,
  dataSourceAction,
  introspectGovernedDataSource,
  listGovernedDataSources,
  listSemanticModels,
  listSemanticModelVersions,
  rotateGovernedDataSourceSecret,
  semanticModelAction,
} from "./api";
import SemanticModeler from "./SemanticModeler.vue";
import type {
  ConnectorSnapshot,
  PlatformConfigStatus,
  PlatformConfigVersion,
  PlatformModule,
  PlatformCapability,
  GraphTopology,
  HarnessStatus,
  EvaluationRun,
  BusinessDatasetSnapshot,
  HttpToolCatalogSnapshot,
  PlatformProviderStatus,
  PlatformSecret,
  GovernedDataSource,
  SemanticModelRecord,
  SemanticModelVersion,
} from "./types";

const props = defineProps<{
  permissions: Record<string, boolean>;
  currentUserId?: string;
}>();

const can = (permission: string) => props.permissions[permission] === true;
const canViewStatus = computed(() => can("platform.status.read"));
const canConfigurePlatform = computed(() => can("platform.config.manage"));
const canManageConnectors = computed(() => can("platform.connector.manage"));
const canManageDatasets = computed(() => can("platform.dataset.manage"));
const canManageTools = computed(() => can("platform.tool.manage"));
const canManageProviders = computed(() => can("platform.provider.manage"));
const canCreateDataSource = computed(() => can("platform.data_source.create"));
const canReviewDataSource = computed(() => can("platform.data_source.review"));
const canAdminDataSource = computed(() => can("platform.data_source.admin"));
const canManageSemanticModel = computed(() => can("platform.semantic_model.manage"));
const canUseSelfService = computed(() => (
  canCreateDataSource.value || canReviewDataSource.value || canManageSemanticModel.value
));
const canUseIntegrations = computed(() => (
  canUseSelfService.value
  || canManageConnectors.value
  || canManageDatasets.value
  || canManageTools.value
  || canManageProviders.value
));

mermaid.initialize({
  startOnLoad: false,
  securityLevel: "strict",
  theme: "neutral",
  flowchart: { curve: "linear", htmlLabels: false, useMaxWidth: true },
});

const modules = ref<PlatformModule[]>([]);
const capabilities = ref<PlatformCapability[]>([]);
const connectors = ref<ConnectorSnapshot | null>(null);
const status = ref<PlatformConfigStatus | null>(null);
const versions = ref<PlatformConfigVersion[]>([]);
const graphs = ref<GraphTopology[]>([]);
const harness = ref<HarnessStatus | null>(null);
const evaluations = ref<EvaluationRun[]>([]);
const selectedGraphId = ref("");
const graphMode = ref<"orchestrator" | "retrieval">("orchestrator");
const graphHost = ref<HTMLElement | null>(null);
const graphError = ref("");
let graphRenderSequence = 0;
const draftEnabled = ref<Record<string, boolean>>({});
const loading = ref(false);
const actionLoading = ref("");
const notice = ref("");
const error = ref("");
const connectorTesting = ref("");
const datasets = ref<BusinessDatasetSnapshot | null>(null);
const providers = ref<PlatformProviderStatus[]>([]);
const secrets = ref<PlatformSecret[]>([]);
const httpTools = ref<HttpToolCatalogSnapshot | null>(null);
const integrationTab = ref<"selfservice" | "connectors" | "datasets" | "tools" | "providers">("selfservice");
const governedSources = ref<GovernedDataSource[]>([]);
const dataSourceDraft = ref({ connector_id: "", display_name: "", dialect: "postgresql", scope: "personal", host: "", port: 5432, database_name: "", username: "", password: "", base_url: "", api_token: "", tls_required: true });
const governedIntrospection = ref<Record<string, unknown> | null>(null);
const activeGovernedConnector = ref("");
const semanticModel = ref<SemanticModelRecord | null>(null);
const semanticModels = ref<SemanticModelRecord[]>([]);
const semanticVersions = ref<SemanticModelVersion[]>([]);
const selectedSemanticModelId = ref("");
const rotatingSource = ref<GovernedDataSource | null>(null);
const rotationSecret = ref("");
const secretDraft = ref({ name: "", value: "" });
const connectorDraft = ref({
  id: "",
  type: "database" as "database" | "data_http",
  secretId: "",
  apiKeySecretId: "",
  tenantId: "tenant-demo",
  orgCode: "ORG-DEMO-001",
});
const datasetCatalogDraft = ref("");
const selectedDatasetId = ref("");
const datasetPreview = ref<Record<string, unknown> | null>(null);
const connectorIntrospection = ref<Record<string, unknown> | null>(null);
const httpToolDraft = ref({
  id: "",
  name: "",
  domain: "general",
  permission: "business.data.read",
  baseUrl: "",
  path: "/api/query",
});

const hasChanges = computed(() => modules.value.some(
  (module) => draftEnabled.value[module.id] !== module.enabled,
));
const secretProviderConfigured = computed(() => (
  providers.value.find((provider) => provider.kind === "secret")?.configured ?? false
));
const orchestratorGraphs = computed(() => graphs.value.filter((graph) => graph.type === "orchestrator"));
const selectedOrchestrator = computed(() => orchestratorGraphs.value.find(
  (graph) => graph.graph_id === selectedGraphId.value,
) ?? orchestratorGraphs.value[0] ?? null);
const hasRetrievalSubgraph = computed(() => selectedOrchestrator.value?.related_graph_ids.includes("knowledge.retrieval") ?? false);
const activeGraph = computed(() => {
  if (graphMode.value === "retrieval" && hasRetrievalSubgraph.value) {
    return graphs.value.find((graph) => graph.graph_id === "knowledge.retrieval") ?? null;
  }
  return selectedOrchestrator.value;
});
const governedTables = computed(() => (
  Array.isArray(governedIntrospection.value?.tables)
    ? governedIntrospection.value?.tables as Array<{ schema?: string | null; name: string; columns: Array<{ name: string; type?: string; nullable?: boolean }> }>
    : []
));
const activeSemanticModels = computed(() => semanticModels.value.filter(
  (model) => model.connector_id === activeGovernedConnector.value,
));

async function refresh() {
  loading.value = true;
  error.value = "";
  try {
    if (canViewStatus.value) {
      const [moduleResult, capabilityResult, currentStatus, graphResult, harnessResult, evaluationResult] = await Promise.all([
        listPlatformModules(),
        listPlatformCapabilities(),
        getPlatformConfigStatus(),
        listPlatformGraphs(),
        getPlatformHarness(),
        listPlatformEvaluations(),
      ]);
      modules.value = moduleResult.items;
      capabilities.value = capabilityResult.items;
      status.value = currentStatus;
      graphs.value = graphResult.items;
      harness.value = harnessResult;
      evaluations.value = evaluationResult.items;
      if (!selectedGraphId.value || !graphResult.items.some((item) => item.graph_id === selectedGraphId.value)) {
        selectedGraphId.value = graphResult.items.find((item) => item.type === "orchestrator")?.graph_id ?? "";
      }
      draftEnabled.value = Object.fromEntries(
        moduleResult.items.map((module) => [module.id, module.enabled]),
      );
      connectors.value = await getPlatformConnectors();
    }
    if (canConfigurePlatform.value) {
      versions.value = (await listPlatformConfigVersions()).items;
    }
    await refreshIntegrations();
    if (canUseSelfService.value) {
      const [sourceResult, modelResult] = await Promise.all([
        listGovernedDataSources(),
        listSemanticModels(),
      ]);
      governedSources.value = sourceResult.items;
      semanticModels.value = modelResult.items;
      if (activeGovernedConnector.value) {
        const current = semanticModels.value.find(
          (item) => item.model_id === selectedSemanticModelId.value,
        ) ?? semanticModels.value.find(
          (item) => item.connector_id === activeGovernedConnector.value,
        ) ?? null;
        semanticModel.value = current;
        selectedSemanticModelId.value = current?.model_id ?? "";
      }
    }
  } catch (loadError) {
    error.value = loadError instanceof Error ? loadError.message : "平台配置加载失败";
  } finally {
    loading.value = false;
  }
}

async function addGovernedSource() {
  actionLoading.value = "data-source-create";
  try {
    const payload = dataSourceDraft.value.dialect === "http"
      ? { connector_id: dataSourceDraft.value.connector_id, display_name: dataSourceDraft.value.display_name, dialect: "http", scope: dataSourceDraft.value.scope, base_url: dataSourceDraft.value.base_url, api_token: dataSourceDraft.value.api_token || null, tls_required: true }
      : { ...dataSourceDraft.value, base_url: undefined, api_token: undefined };
    await createGovernedDataSource(payload);
    governedSources.value = (await listGovernedDataSources()).items;
    notice.value = "个人数据源草稿已创建；密码已直接写入 Secret Provider。";
  } catch (cause) { error.value = cause instanceof Error ? cause.message : "创建数据源失败"; }
  finally {
    dataSourceDraft.value.password = "";
    dataSourceDraft.value.api_token = "";
    actionLoading.value = "";
  }
}

async function runGovernedAction(connectorId: string, action: "test" | "submit" | "approve" | "reject" | "disable") {
  actionLoading.value = `${action}:${connectorId}`;
  try {
    await dataSourceAction(connectorId, action, action === "approve" || action === "reject" ? { reason: "网页审核" } : undefined);
    governedSources.value = (await listGovernedDataSources()).items;
    notice.value = `数据源操作 ${action} 已完成。`;
  } catch (cause) { error.value = cause instanceof Error ? cause.message : "数据源操作失败"; }
  finally { actionLoading.value = ""; }
}

async function openModeler(connectorId: string) {
  actionLoading.value = `introspect:${connectorId}`;
  try {
    governedIntrospection.value = await introspectGovernedDataSource(connectorId);
    activeGovernedConnector.value = connectorId;
    const existing = semanticModels.value.find((item) => item.connector_id === connectorId) ?? null;
    semanticModel.value = existing;
    selectedSemanticModelId.value = existing?.model_id ?? "";
    semanticVersions.value = existing
      ? (await listSemanticModelVersions(existing.model_id)).items
      : [];
  } catch (cause) { error.value = cause instanceof Error ? cause.message : "内省失败"; }
  finally { actionLoading.value = ""; }
}

async function saveSemanticModel(payload: Record<string, unknown>) {
  actionLoading.value = "semantic-create";
  try {
    const modelId = String(payload.model_id ?? "");
    const existing = semanticModels.value.find((item) => item.model_id === modelId);
    semanticModel.value = existing
      ? await createSemanticModelVersion(
        modelId,
        payload.logical_model as Record<string, unknown>,
      )
      : await createSemanticModel(payload);
    semanticModels.value = [
      semanticModel.value,
      ...semanticModels.value.filter((item) => item.model_id !== modelId),
    ];
    selectedSemanticModelId.value = modelId;
    const validation = await semanticModelAction(semanticModel.value.model_id, "validate");
    semanticVersions.value = (
      await listSemanticModelVersions(semanticModel.value.model_id)
    ).items;
    notice.value = (validation.valid ? "语义模型校验通过。" : `校验失败：${(validation.errors as string[]).join("；")}`);
  } catch (cause) { error.value = cause instanceof Error ? cause.message : "创建语义模型失败"; }
  finally { actionLoading.value = ""; }
}

async function publishCurrentSemanticModel() {
  if (!semanticModel.value) return;
  actionLoading.value = "semantic-publish";
  try {
    const result = await semanticModelAction(semanticModel.value.model_id, "publish");
    semanticVersions.value = (
      await listSemanticModelVersions(semanticModel.value.model_id)
    ).items;
    notice.value = `已发布 ${(result.tool_id as string) || semanticModel.value.model_id}，Agent 可直接发现。`;
    await refresh();
  } catch (cause) { error.value = cause instanceof Error ? cause.message : "发布语义模型失败"; }
  finally { actionLoading.value = ""; }
}

async function rollbackSemanticModel(version: number) {
  if (!semanticModel.value) return;
  actionLoading.value = `semantic-rollback:${version}`;
  try {
    await semanticModelAction(semanticModel.value.model_id, "rollback", { version });
    semanticModel.value = {
      ...semanticModel.value,
      current_version: version,
      status: "published",
    };
    semanticVersions.value = (
      await listSemanticModelVersions(semanticModel.value.model_id)
    ).items;
    notice.value = `Semantic Model 已回滚并重新发布到版本 ${version}`;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "语义模型回滚失败";
  } finally {
    actionLoading.value = "";
  }
}

async function rotateSourceSecret(source: GovernedDataSource) {
  rotatingSource.value = source;
  rotationSecret.value = "";
}

async function confirmRotateSourceSecret() {
  const source = rotatingSource.value;
  const value = rotationSecret.value;
  if (!source || !value) return;
  actionLoading.value = `rotate:${source.connector_id}`;
  try {
    await rotateGovernedDataSourceSecret(
      source.connector_id,
      source.dialect === "http" ? { api_token: value } : { password: value },
    );
    governedSources.value = (await listGovernedDataSources()).items;
    notice.value = "Secret 已验证并轮换；Dataset 和 Agent Tool 无需重建";
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Secret 轮换失败";
  } finally {
    rotationSecret.value = "";
    rotatingSource.value = null;
    actionLoading.value = "";
  }
}

async function selectSemanticModel(modelId: string) {
  selectedSemanticModelId.value = modelId;
  semanticModel.value = semanticModels.value.find((item) => item.model_id === modelId) ?? null;
  semanticVersions.value = semanticModel.value
    ? (await listSemanticModelVersions(semanticModel.value.model_id)).items
    : [];
}

function onSemanticModelSelected(event: Event) {
  void selectSemanticModel((event.target as HTMLSelectElement).value);
}

async function refreshIntegrations() {
  if (canManageDatasets.value) {
    datasets.value = await getPlatformDatasets();
    datasetCatalogDraft.value = JSON.stringify({
      version: datasets.value.snapshot.version,
      datasets: datasets.value.items,
    }, null, 2);
    if (!selectedDatasetId.value) {
      selectedDatasetId.value = datasets.value.items[0]?.id ?? "";
    }
  }
  if (canManageProviders.value) {
    const [providerResult, secretResult] = await Promise.allSettled([
      getPlatformProviders(),
      listPlatformSecrets(),
    ]);
    if (providerResult.status === "fulfilled") {
      providers.value = providerResult.value.items;
    }
    secrets.value = secretResult.status === "fulfilled" ? secretResult.value.items : [];
  }
  if (canManageTools.value) httpTools.value = await getPlatformHttpTools();
}

async function renderActiveGraph() {
  const sequence = ++graphRenderSequence;
  graphError.value = "";
  await nextTick();
  if (!graphHost.value || !activeGraph.value) return;
  try {
    const result = await mermaid.render(
      `platform-graph-${sequence}`,
      activeGraph.value.mermaid,
    );
    if (sequence === graphRenderSequence && graphHost.value) {
      graphHost.value.innerHTML = result.svg;
      result.bindFunctions?.(graphHost.value);
    }
  } catch (renderError) {
    if (sequence === graphRenderSequence) {
      graphHost.value.innerHTML = "";
      graphError.value = renderError instanceof Error ? renderError.message : "Graph 拓扑渲染失败";
    }
  }
}

watch(activeGraph, renderActiveGraph, { flush: "post" });
watch(hasRetrievalSubgraph, (available) => {
  if (!available && graphMode.value === "retrieval") graphMode.value = "orchestrator";
});

async function validate() {
  await runAction("validate", async () => {
    const result = await validatePlatformConfig(draftEnabled.value);
    notice.value = `校验通过：${result.capability_count} 个 Capability，${result.graph_count} 个 Graph，${result.tool_count} 个 Tool。`;
  });
}

async function publish() {
  await runAction("publish", async () => {
    await publishPlatformConfig(draftEnabled.value);
    notice.value = "配置已原子发布，新请求将使用新快照。";
    await refresh();
  });
}

async function rollback() {
  await runAction("rollback", async () => {
    await rollbackPlatformConfig();
    notice.value = "已回滚到上一运行快照。";
    await refresh();
  });
}

async function runAction(name: string, action: () => Promise<void>) {
  actionLoading.value = name;
  notice.value = "";
  error.value = "";
  try {
    await action();
  } catch (actionError) {
    error.value = actionError instanceof Error ? actionError.message : "配置操作失败";
  } finally {
    actionLoading.value = "";
  }
}

async function testConnector(connectorId: string) {
  connectorTesting.value = connectorId;
  error.value = "";
  try {
    const result = await testPlatformConnector(connectorId);
    notice.value = result.ready ? `${connectorId} 健康检查通过。` : `${connectorId} 健康检查失败。`;
  } catch (testError) {
    error.value = testError instanceof Error ? testError.message : "连接器测试失败";
  } finally {
    connectorTesting.value = "";
  }
}

async function addSecret() {
  await runAction("secret", async () => {
    const created = await createPlatformSecret(secretDraft.value.name, secretDraft.value.value);
    connectorDraft.value.secretId = created.secret_id;
    secretDraft.value = { name: "", value: "" };
    await refreshIntegrations();
    notice.value = "密钥已加密保存，页面仅保留引用 ID。";
  });
}

async function removeSecret(secretId: string) {
  await runAction("secret-delete", async () => {
    await deletePlatformSecret(secretId);
    await refreshIntegrations();
    notice.value = "密钥引用已删除。";
  });
}

async function publishConnector() {
  await runAction("connector-publish", async () => {
    if (!connectors.value?.catalog) throw new Error("Connector Catalog 尚未加载");
    const draft = connectorDraft.value;
    const connector: Record<string, unknown> = {
      id: draft.id,
      type: draft.type,
      enabled: true,
      default: false,
      routes: draft.tenantId && draft.orgCode
        ? [{ tenant_id: draft.tenantId, org_code: draft.orgCode }]
        : [],
    };
    if (draft.type === "database") connector.dsn_secret_id = draft.secretId;
    else {
      connector.base_url_secret_id = draft.secretId;
      if (draft.apiKeySecretId) connector.api_key_secret_id = draft.apiKeySecretId;
    }
    const catalog = {
      version: `${Date.now()}`,
      connectors: [...connectors.value.catalog.connectors, connector],
    };
    await configurePlatformConnectors("validate", catalog);
    await configurePlatformConnectors("publish", catalog);
    connectorDraft.value.id = "";
    await refresh();
    notice.value = "连接器已校验并发布，可继续内省和映射 Dataset。";
  });
}

async function introspectConnector(connectorId: string) {
  await runAction("introspect", async () => {
    connectorIntrospection.value = await introspectPlatformConnector(connectorId);
    integrationTab.value = "datasets";
  });
}

async function validateDatasetCatalog() {
  await runAction("dataset-validate", async () => {
    await configurePlatformDatasets("validate", JSON.parse(datasetCatalogDraft.value));
    notice.value = "Dataset 字段、指标和来源列校验通过。";
  });
}

async function publishDatasetCatalog() {
  await runAction("dataset-publish", async () => {
    await configurePlatformDatasets("publish", JSON.parse(datasetCatalogDraft.value));
    await refresh();
    notice.value = "Dataset 已发布并同步生成 Agent 虚拟 Tool。";
  });
}

async function previewDataset() {
  if (!selectedDatasetId.value) return;
  await runAction("dataset-preview", async () => {
    datasetPreview.value = await previewPlatformDataset(selectedDatasetId.value);
  });
}

async function publishHttpTool() {
  await runAction("http-tool-publish", async () => {
    const draft = httpToolDraft.value;
    const payload = {
      version: `${Date.now()}`,
      tools: [...(httpTools.value?.items ?? []), {
        id: draft.id,
        version: "1.0.0",
        name: draft.name,
        description: `Read-only ${draft.name}`,
        domain: draft.domain,
        required_permission: draft.permission,
        tags: [draft.domain],
        input_schema: { type: "object", properties: {} },
        output_schema: { type: "object" },
        transport: {
          type: "http",
          base_url: draft.baseUrl,
          path: draft.path,
          method: "GET",
          allowed_hosts: [new URL(draft.baseUrl).hostname],
        },
      }],
    };
    await configurePlatformHttpTools("validate", payload);
    await configurePlatformHttpTools("publish", payload);
    await refresh();
    notice.value = "HTTP Tool 已发布，Agent 无需新增 Workflow 即可发现。";
  });
}

onMounted(() => {
  integrationTab.value = canUseSelfService.value
    ? "selfservice"
    : canManageConnectors.value
      ? "connectors"
      : canManageDatasets.value
        ? "datasets"
        : canManageTools.value
          ? "tools"
          : "providers";
  void refresh();
});
</script>

<template>
  <main class="platform-admin">
    <header class="admin-heading">
      <div><span>平台与数据治理</span><h1>模块、能力与数据连接</h1><p>页面仅加载当前身份获准的功能；所有操作仍由后端策略中心逐项校验。</p></div>
      <div class="admin-actions">
        <button type="button" title="刷新目录" :disabled="loading" @click="refresh"><RefreshCw :size="16" :class="loading && 'spin'" /></button>
        <button v-if="canConfigurePlatform" type="button" :disabled="Boolean(actionLoading)" @click="validate"><CheckCircle2 :size="16" />校验</button>
        <button v-if="canConfigurePlatform" class="primary" type="button" :disabled="Boolean(actionLoading) || !hasChanges" @click="publish"><Upload :size="16" />发布</button>
        <button v-if="canConfigurePlatform" type="button" :disabled="Boolean(actionLoading) || !status?.rollback_available" @click="rollback"><RotateCcw :size="16" />回滚</button>
      </div>
    </header>

    <div v-if="notice" class="admin-notice success"><CheckCircle2 :size="16" />{{ notice }}</div>
    <div v-if="error" class="admin-notice error"><ShieldCheck :size="16" />{{ error }}</div>

    <section v-if="canViewStatus" class="admin-summary" aria-label="当前平台快照">
      <div><span>运行快照</span><strong>{{ status?.snapshot.version ?? "-" }}</strong><small>{{ status?.snapshot.content_hash.slice(0, 12) ?? "-" }}</small></div>
      <div><span>模块</span><strong>{{ status?.plugin_count ?? 0 }}</strong><small>Python + 声明式</small></div>
      <div><span>Capabilities</span><strong>{{ status?.capability_count ?? 0 }}</strong><small>由 Tool 元数据自动生成</small></div>
      <div><span>Graphs / Tools</span><strong>{{ status?.graph_count ?? 0 }} / {{ status?.tool_count ?? 0 }}</strong><small>LangGraph / ToolExecutor</small></div>
    </section>

    <section v-if="canViewStatus" class="admin-section harness-section">
      <header><div><ShieldCheck :size="18" /><span><strong>Harness 与发布门禁</strong><small>预算、验证、保留策略和离线评测状态</small></span></div><em>{{ evaluations.length }}</em></header>
      <div class="admin-summary">
        <div><span>执行边界</span><strong>{{ harness?.runtime.graph_engine ?? "-" }}</strong><small>{{ harness?.runtime.tool_boundary ?? "-" }} · 最多修复 {{ harness?.runtime.max_repair_attempts ?? 0 }} 次</small></div>
        <div><span>Prompt 版本</span><strong>{{ harness?.prompts.length ?? 0 }}</strong><small>已纳入平台快照哈希</small></div>
        <div><span>数据保留</span><strong>{{ harness?.retention.conversations_days ?? 0 }} / {{ harness?.retention.trace_and_evidence_days ?? 0 }} 天</strong><small>会话 / Trace 与证据</small></div>
        <div><span>最近评测</span><strong>{{ evaluations[0]?.release_gate.passed ? "通过" : evaluations.length ? "未通过" : "未运行" }}</strong><small v-if="evaluations[0]">{{ Math.round((evaluations[0].metrics.overall_pass_rate ?? 0) * 100) }}% · P95 {{ Math.round(evaluations[0].metrics.p95_latency_ms ?? 0) }} ms</small><small v-else>运行离线评测后写入 SQL</small></div>
      </div>
    </section>

    <section v-if="canViewStatus" class="admin-section graph-section">
      <header>
        <div><GitBranch :size="18" /><span><strong>Graph 实际拓扑</strong><small>主平台只有一个通用 Orchestrator Graph</small></span></div>
        <em>{{ activeGraph?.nodes.length ?? 0 }}</em>
      </header>
      <div class="graph-toolbar">
        <label><span>Orchestrator Graph</span><select v-model="selectedGraphId"><option v-for="graph in orchestratorGraphs" :key="graph.graph_id" :value="graph.graph_id">{{ graph.name }} · {{ graph.graph_id }}</option></select></label>
        <div class="graph-segments" aria-label="拓扑层级">
          <button type="button" :class="graphMode === 'orchestrator' && 'active'" @click="graphMode = 'orchestrator'">主编排图</button>
          <button type="button" :class="graphMode === 'retrieval' && 'active'" :disabled="!hasRetrievalSubgraph" @click="graphMode = 'retrieval'">知识检索子图</button>
        </div>
        <span v-if="activeGraph" class="graph-meta">{{ activeGraph.type === "orchestrator" ? "Orchestrator" : "Subgraph" }} · v{{ activeGraph.version }} · {{ activeGraph.edges.length }} 条边</span>
      </div>
      <div v-if="graphError" class="graph-render-error">{{ graphError }}</div>
      <div ref="graphHost" class="graph-canvas" aria-live="polite"></div>
    </section>

    <div v-if="canViewStatus" class="admin-grid">
      <section class="admin-section module-section">
        <header><div><Layers3 :size="18" /><span><strong>模块目录</strong><small>开关变更先校验，再原子发布</small></span></div><em>{{ modules.length }}</em></header>
        <div class="module-list">
          <label v-for="module in modules" :key="module.id" class="module-row">
            <input v-model="draftEnabled[module.id]" type="checkbox" :disabled="!canConfigurePlatform" />
            <span><strong>{{ module.name }}</strong><code>{{ module.id }}@{{ module.version }}</code></span>
            <em>{{ module.type === "python" ? "Python 插件" : "声明式插件" }}</em>
            <small>{{ module.capability_count }} Capability · {{ module.tool_count }} Tool</small>
          </label>
        </div>
      </section>

      <section class="admin-section connector-section">
        <header><div><Database :size="18" /><span><strong>业务数据连接器</strong><small>问答平台只依赖统一 API 契约</small></span></div><em>{{ connectors?.connectors.length ?? 0 }}</em></header>
        <div v-if="connectors" class="connector-list">
          <div v-for="connector in connectors.connectors" :key="connector.source_id" class="connector-row">
            <span :class="['connector-state', connector.ready && 'ready']"></span>
            <div><strong>{{ connector.source_id }}</strong><small>{{ connector.default ? "默认路由" : `${connector.route_count} 个 tenant/org 路由` }}</small></div>
            <button v-if="canManageConnectors" type="button" :disabled="connectorTesting === connector.source_id" @click="testConnector(connector.source_id)">
              <LoaderCircle v-if="connectorTesting === connector.source_id" :size="14" class="spin" /><Plug v-else :size="14" />测试
            </button>
          </div>
          <p class="connector-revision">Connector snapshot {{ connectors.revision }} · {{ connectors.version }}</p>
        </div>
        <p v-else class="empty-admin">启动统一采购数据 API 后可查看连接器状态。</p>
      </section>
    </div>

    <section v-if="canUseIntegrations" class="admin-section integration-section">
      <header>
        <div><Wrench :size="18" /><span><strong>企业集成配置</strong><small>Connector、Dataset、HTTP Tool 与 Provider</small></span></div>
        <em>{{ (connectors?.connectors.length ?? 0) + (datasets?.count ?? 0) + (httpTools?.count ?? 0) }}</em>
      </header>
      <nav class="integration-tabs" aria-label="企业集成配置">
        <button v-if="canUseSelfService" type="button" :class="integrationTab === 'selfservice' && 'active'" @click="integrationTab = 'selfservice'"><Plus :size="15" />个人数据源 / BI 建模</button>
        <button v-if="canManageConnectors" type="button" :class="integrationTab === 'connectors' && 'active'" @click="integrationTab = 'connectors'"><Database :size="15" />数据连接</button>
        <button v-if="canManageDatasets" type="button" :class="integrationTab === 'datasets' && 'active'" @click="integrationTab = 'datasets'"><TableProperties :size="15" />Dataset</button>
        <button v-if="canManageTools" type="button" :class="integrationTab === 'tools' && 'active'" @click="integrationTab = 'tools'"><Plug :size="15" />HTTP Tool</button>
        <button v-if="canManageProviders" type="button" :class="integrationTab === 'providers' && 'active'" @click="integrationTab = 'providers'"><KeyRound :size="15" />Provider / Secret</button>
      </nav>

      <div v-if="integrationTab === 'selfservice'" class="integration-pane">
        <div v-if="canCreateDataSource" class="config-form-grid">
          <label><span>连接 ID</span><input v-model.trim="dataSourceDraft.connector_id" placeholder="my-finance-db" /></label>
          <label><span>显示名称</span><input v-model.trim="dataSourceDraft.display_name" placeholder="我的财务只读库" /></label>
          <label><span>类型</span><select v-model="dataSourceDraft.dialect"><option value="postgresql">PostgreSQL</option><option value="mysql">MySQL</option><option value="sqlserver">SQL Server</option><option value="oracle">Oracle</option><option value="http">只读 HTTP API</option></select></label>
          <label><span>范围</span><select v-model="dataSourceDraft.scope"><option value="personal">个人</option><option value="team">团队（需审批）</option><option value="tenant">租户（管理员）</option></select></label>
          <template v-if="dataSourceDraft.dialect !== 'http'">
            <label><span>Host</span><input v-model.trim="dataSourceDraft.host" placeholder="db.company.com" /></label>
            <label><span>Port</span><input v-model.number="dataSourceDraft.port" type="number" /></label>
            <label><span>Database</span><input v-model.trim="dataSourceDraft.database_name" /></label>
            <label><span>只读账号</span><input v-model.trim="dataSourceDraft.username" /></label>
            <label><span>密码</span><input v-model="dataSourceDraft.password" type="password" autocomplete="new-password" /></label>
          </template>
          <template v-else>
            <label><span>HTTPS Base URL</span><input v-model.trim="dataSourceDraft.base_url" placeholder="https://data-api.company.com" /></label>
            <label><span>访问 Token</span><input v-model="dataSourceDraft.api_token" type="password" /></label>
          </template>
        </div>
        <div v-if="canCreateDataSource" class="config-actions"><button class="primary" type="button" :disabled="!dataSourceDraft.connector_id || !dataSourceDraft.display_name || Boolean(actionLoading)" @click="addGovernedSource"><Plus :size="15" />创建安全草稿</button></div>
        <div class="integration-list">
          <div v-for="source in governedSources" :key="source.connector_id">
            <button v-if="canCreateDataSource && source.owner_user_id === currentUserId && source.status !== 'disabled'" type="button" @click="rotateSourceSecret(source)"><KeyRound :size="14" />轮换 Secret</button>
            <span :class="['connector-state', ['ready','approved','published'].includes(source.status) && 'ready']"></span>
            <strong>{{ source.display_name }}</strong><code>{{ source.connector_id }} · {{ source.dialect }}</code><span>{{ source.scope }} / {{ source.status }} / {{ source.host_masked }}</span>
            <button v-if="canCreateDataSource && source.owner_user_id === currentUserId && ['draft','rejected'].includes(source.status)" type="button" @click="runGovernedAction(source.connector_id, 'test')">隔离测试</button>
            <button v-if="((canManageSemanticModel && source.owner_user_id === currentUserId) || canReviewDataSource || canManageDatasets) && ['ready','submitted','approved','published'].includes(source.status)" type="button" @click="openModeler(source.connector_id)">内省 / 建模审批</button>
            <button v-if="canCreateDataSource && source.owner_user_id === currentUserId && source.status === 'ready'" type="button" @click="runGovernedAction(source.connector_id, 'submit')">提交审批</button>
            <button v-if="canReviewDataSource && source.status === 'submitted'" type="button" @click="runGovernedAction(source.connector_id, 'approve')">批准</button>
            <button v-if="canReviewDataSource && source.status === 'submitted'" type="button" @click="runGovernedAction(source.connector_id, 'reject')">拒绝</button>
            <button v-if="canAdminDataSource && ['approved','published'].includes(source.status)" type="button" @click="runGovernedAction(source.connector_id, 'disable')">停用</button>
          </div>
        </div>
        <div v-if="activeSemanticModels.length" class="config-form-grid">
          <label><span>当前语义模型</span><select :value="selectedSemanticModelId" @change="onSemanticModelSelected"><option v-for="model in activeSemanticModels" :key="model.model_id" :value="model.model_id">{{ model.name }} · {{ model.model_id }} · v{{ model.current_version }}</option></select></label>
        </div>
        <SemanticModeler
          v-if="canManageSemanticModel && activeGovernedConnector && governedTables.length"
          :key="`${activeGovernedConnector}:${semanticModel?.model_id ?? 'new'}:${semanticModel?.current_version ?? 0}`"
          :connector-id="activeGovernedConnector"
          :tables="governedTables"
          :initial-model="semanticModel?.logical_model ?? null"
          @create="saveSemanticModel"
        />
        <div v-if="semanticVersions.length" class="integration-list">
          <div v-for="version in semanticVersions" :key="version.version">
            <strong>版本 {{ version.version }}</strong>
            <span>{{ version.status }} · {{ version.created_by }}</span>
            <button v-if="semanticModel && version.version !== semanticModel.current_version" type="button" @click="rollbackSemanticModel(version.version)"><RotateCcw :size="14" />回滚并发布</button>
          </div>
        </div>
        <div v-if="semanticModel" class="config-actions"><span>模型 {{ semanticModel.model_id }} · {{ semanticModel.status }}</span><button v-if="canManageDatasets" class="primary" type="button" @click="publishCurrentSemanticModel">审批后发布为 data.{{ semanticModel.model_id }}.query</button></div>
      </div>

      <div v-else-if="integrationTab === 'connectors'" class="integration-pane">
        <div class="config-form-grid">
          <label><span>Connector ID</span><input v-model.trim="connectorDraft.id" placeholder="hr-primary" /></label>
          <label><span>类型</span><select v-model="connectorDraft.type"><option value="database">Database</option><option value="data_http">Data HTTP</option></select></label>
          <label><span>{{ connectorDraft.type === 'database' ? 'DSN Secret' : 'Base URL Secret' }}</span><select v-model="connectorDraft.secretId"><option value="">选择 Secret</option><option v-for="secret in secrets" :key="secret.secret_id" :value="secret.secret_id">{{ secret.name }} · {{ secret.masked }}</option></select></label>
          <label v-if="connectorDraft.type === 'data_http'"><span>API Key Secret</span><select v-model="connectorDraft.apiKeySecretId"><option value="">无</option><option v-for="secret in secrets" :key="secret.secret_id" :value="secret.secret_id">{{ secret.name }}</option></select></label>
          <label><span>Tenant</span><input v-model.trim="connectorDraft.tenantId" /></label>
          <label><span>Organization</span><input v-model.trim="connectorDraft.orgCode" /></label>
        </div>
        <div class="config-actions"><button class="primary" type="button" :disabled="!connectorDraft.id || !connectorDraft.secretId || Boolean(actionLoading)" @click="publishConnector"><Plus :size="15" />校验并发布</button></div>
        <div class="integration-list">
          <div v-for="connector in connectors?.connectors ?? []" :key="connector.source_id">
            <span :class="['connector-state', connector.ready && 'ready']"></span>
            <strong>{{ connector.source_id }}</strong><code>{{ connector.type ?? 'legacy' }}</code>
            <button type="button" @click="testConnector(connector.source_id)">测试</button>
            <button type="button" @click="introspectConnector(connector.source_id)">内省</button>
          </div>
        </div>
      </div>

      <div v-else-if="integrationTab === 'datasets'" class="integration-pane dataset-pane">
        <div class="dataset-toolbar">
          <label><span>预览 Dataset</span><select v-model="selectedDatasetId"><option v-for="dataset in datasets?.items ?? []" :key="dataset.id" :value="dataset.id">{{ dataset.name }} · {{ dataset.id }}</option></select></label>
          <button type="button" :disabled="!selectedDatasetId" @click="previewDataset">预览 20 行</button>
          <button type="button" @click="validateDatasetCatalog">校验</button>
          <button class="primary" type="button" @click="publishDatasetCatalog">发布</button>
        </div>
        <div class="dataset-editor-grid">
          <label><span>Dataset Catalog JSON</span><textarea v-model="datasetCatalogDraft" spellcheck="false"></textarea></label>
          <div><span>Connector 内省 / Dataset 预览</span><pre>{{ JSON.stringify(datasetPreview ?? connectorIntrospection ?? {}, null, 2) }}</pre></div>
        </div>
      </div>

      <div v-else-if="integrationTab === 'tools'" class="integration-pane">
        <div class="config-form-grid">
          <label><span>Tool ID</span><input v-model.trim="httpToolDraft.id" placeholder="hr.leave.balance" /></label>
          <label><span>名称</span><input v-model.trim="httpToolDraft.name" placeholder="假期余额" /></label>
          <label><span>业务域</span><input v-model.trim="httpToolDraft.domain" /></label>
          <label><span>权限</span><input v-model.trim="httpToolDraft.permission" /></label>
          <label><span>Base URL</span><input v-model.trim="httpToolDraft.baseUrl" placeholder="https://api.example.com" /></label>
          <label><span>Path</span><input v-model.trim="httpToolDraft.path" /></label>
        </div>
        <div class="config-actions"><button class="primary" type="button" :disabled="!httpToolDraft.id || !httpToolDraft.name || !httpToolDraft.baseUrl" @click="publishHttpTool"><Plus :size="15" />校验并发布</button></div>
        <div class="integration-list"><div v-for="tool in httpTools?.items ?? []" :key="tool.id"><Plug :size="14" /><strong>{{ tool.name }}</strong><code>{{ tool.id }}</code><span>{{ tool.domain }}</span></div></div>
      </div>

      <div v-else class="integration-pane provider-pane">
        <div class="provider-status-list"><div v-for="provider in providers" :key="provider.kind"><span :class="['connector-state', provider.configured && provider.ready !== false && 'ready']"></span><strong>{{ provider.kind }}</strong><code>{{ provider.provider }}</code><span>{{ provider.configured ? '已配置' : '未配置' }}</span></div></div>
        <p v-if="!secretProviderConfigured" class="integration-warning">Secret Provider 尚未启用。请在根目录 .env 配置 LOCAL_SECRET_MASTER_KEY（至少 16 个字符）并重启后端；密钥不会写入平台数据库或审计记录。</p>
        <div class="secret-create-row"><label><span>Secret 名称</span><input v-model.trim="secretDraft.name" :disabled="!secretProviderConfigured" /></label><label><span>Secret 值</span><input v-model="secretDraft.value" type="password" autocomplete="new-password" :disabled="!secretProviderConfigured" /></label><button class="primary" type="button" :disabled="!secretProviderConfigured || !secretDraft.name || !secretDraft.value" @click="addSecret"><Plus :size="15" />保存</button></div>
        <div class="integration-list"><div v-for="secret in secrets" :key="secret.secret_id"><KeyRound :size="14" /><strong>{{ secret.name }}</strong><code>{{ secret.secret_id }}</code><span>{{ secret.masked }}</span><button type="button" title="删除 Secret" @click="removeSecret(secret.secret_id)"><Trash2 :size="14" /></button></div></div>
      </div>
    </section>

    <section v-if="canViewStatus" class="admin-section capability-section">
      <header><div><Plug :size="18" /><span><strong>Capability 能力目录</strong><small>Module → Capability → Tool；不参与运行时路由</small></span></div><em>{{ capabilities.length }}</em></header>
      <div class="capability-table-wrap"><table><thead><tr><th>Capability</th><th>所属模块 / 领域</th><th>原子 Tool</th><th>权限 / 风险</th></tr></thead>
        <tbody><tr v-for="capability in capabilities" :key="capability.id"><td><strong>{{ capability.name }}</strong><code>{{ capability.id }}</code><small>{{ capability.description }}</small></td><td><code v-for="moduleId in capability.module_ids" :key="moduleId">{{ moduleId }}</code><small>{{ capability.domains.join('、') }}</small></td><td><code v-for="tool in capability.tool_ids" :key="tool">{{ tool }}</code></td><td><code v-for="permission in capability.required_permissions" :key="permission">{{ permission }}</code><small>{{ capability.risk_levels.join('、') }}</small></td></tr></tbody>
      </table></div>
    </section>

    <section v-if="canConfigurePlatform" class="admin-section version-section">
      <header><div><History :size="18" /><span><strong>发布审计</strong><small>保存在平台 SQLAlchemy 数据库</small></span></div><em>{{ versions.length }}</em></header>
      <div v-if="versions.length" class="version-list"><div v-for="version in versions" :key="version.id"><strong>{{ version.action === 'publish' ? '发布' : '回滚' }}</strong><code>{{ version.content_hash.slice(0, 12) }}</code><span>{{ version.actor_user_id }}</span><time>{{ new Date(version.created_at).toLocaleString("zh-CN") }}</time></div></div>
      <p v-else class="empty-admin">尚无发布记录。首次发布后会在此形成审计版本。</p>
    </section>
    <div v-if="rotatingSource" class="secret-modal-backdrop" role="presentation" @click.self="rotatingSource = null; rotationSecret = ''">
      <form class="secret-modal" @submit.prevent="confirmRotateSourceSecret">
        <header><KeyRound :size="18" /><strong>轮换 {{ rotatingSource.display_name }} 的凭据</strong></header>
        <label><span>{{ rotatingSource.dialect === 'http' ? '新 API Token' : '新的只读账号密码' }}</span><input v-model="rotationSecret" type="password" autocomplete="new-password" autofocus /></label>
        <div class="config-actions"><button type="button" @click="rotatingSource = null; rotationSecret = ''">取消</button><button class="primary" type="submit" :disabled="!rotationSecret || Boolean(actionLoading)">验证并轮换</button></div>
      </form>
    </div>
  </main>
</template>
