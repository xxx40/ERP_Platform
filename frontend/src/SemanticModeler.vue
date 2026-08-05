<script setup lang="ts">
import { computed, ref } from "vue";
import { GitMerge, GripVertical, Plus, Trash2 } from "@lucide/vue";

interface ColumnInfo { name: string; type?: string; nullable?: boolean }
interface TableInfo { schema?: string | null; name: string; columns: ColumnInfo[] }
interface CanvasTable extends TableInfo { alias: string; x: number; y: number }
interface JoinEdge {
  left_source: string;
  left_column: string;
  right_source: string;
  right_column: string;
  join_type: "left" | "inner";
  cardinality: "one_to_one" | "one_to_many" | "many_to_one";
}
type Aggregation = "sum" | "count" | "count_distinct" | "avg" | "min" | "max";
interface MetricDraft {
  id: string;
  label: string;
  description: string;
  aliases: string;
  aggregation: Aggregation;
  sourceField: string;
  unit: string;
  restrictDimensions: boolean;
  allowedDimensions: string[];
}
interface FieldOption {
  qualified: string;
  name: string;
  label: string;
  dataType: "string" | "integer" | "number" | "boolean" | "date" | "datetime";
  numeric: boolean;
}

const props = defineProps<{
  connectorId: string;
  tables: TableInfo[];
  initialModel?: Record<string, unknown> | null;
}>();
const emit = defineEmits<{ create: [payload: Record<string, unknown>] }>();

const modelId = ref("");
const modelName = ref("");
const description = ref("");
const domain = ref("general");
const scope = ref<"personal" | "team" | "tenant">("personal");
const canvasTables = ref<CanvasTable[]>([]);
const joins = ref<JoinEdge[]>([]);
const selectedFields = ref<Record<string, boolean>>({});
const grainFields = ref<Record<string, boolean>>({});
const metrics = ref<MetricDraft[]>([]);
const security = ref({ tenant: "", org: "", owner: "", accessScope: "" });
const pendingJoin = ref<{ source: string; column: string } | null>(null);
const drag = ref<{ alias: string; dx: number; dy: number } | null>(null);

function normalizeIdentifier(value: string) {
  const normalized = value.replace(/[^a-zA-Z0-9_]/g, "_").replace(/_+/g, "_").toLowerCase();
  return /^[a-zA-Z_]/.test(normalized) ? normalized : `field_${normalized}`;
}

function fieldName(source: string, column: string) {
  return normalizeIdentifier(`${source}_${column}`).slice(0, 128);
}

function columnDataType(type = ""): FieldOption["dataType"] {
  if (/bool|bit/i.test(type)) return "boolean";
  if (/timestamp|datetime/i.test(type)) return "datetime";
  if (/date/i.test(type)) return "date";
  if (/tinyint|smallint|bigint|\bint(?:eger)?\b/i.test(type)) return "integer";
  if (/decimal|numeric|float|double|real|money|number/i.test(type)) return "number";
  return "string";
}

const availableFields = computed<FieldOption[]>(() => canvasTables.value.flatMap((table) => table.columns
  .filter((column) => selectedFields.value[`${table.alias}.${column.name}`])
  .map((column) => {
    const dataType = columnDataType(column.type);
    return {
      qualified: `${table.alias}.${column.name}`,
      name: fieldName(table.alias, column.name),
      label: `${table.alias}.${column.name}`,
      dataType,
      numeric: dataType === "integer" || dataType === "number",
    };
  })));

const availableFieldMap = computed(() => new Map(availableFields.value.map((field) => [field.name, field])));

const metricErrors = computed(() => {
  const errors: string[] = [];
  const ids = metrics.value.map((metric) => metric.id.trim());
  if (new Set(ids).size !== ids.length) errors.push("指标 ID 不能重复。");
  metrics.value.forEach((metric, index) => {
    const title = metric.label.trim() || `第 ${index + 1} 个指标`;
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(metric.id.trim())) {
      errors.push(`${title} 的指标 ID 只能包含字母、数字和下划线，且不能以数字开头。`);
    }
    if (!metric.label.trim()) errors.push(`第 ${index + 1} 个指标缺少展示名称。`);
    if (metric.aggregation !== "count" && !metric.sourceField) {
      errors.push(`${title} 必须选择来源字段。`);
    }
    const field = availableFieldMap.value.get(metric.sourceField);
    if (metric.sourceField && !field) errors.push(`${title} 引用了未选择的字段。`);
    if ((metric.aggregation === "sum" || metric.aggregation === "avg") && field && !field.numeric) {
      errors.push(`${title} 的 ${metric.aggregation} 来源字段必须是数字。`);
    }
    if (metric.restrictDimensions && !metric.allowedDimensions.length) {
      errors.push(`${title} 已启用分组维度限制，但尚未选择维度。`);
    }
    if (metric.allowedDimensions.some((name) => !availableFieldMap.value.has(name))) {
      errors.push(`${title} 包含已失效的分组维度。`);
    }
  });
  return [...new Set(errors)];
});

const securityReady = computed(() => {
  if (scope.value === "personal" && (!security.value.owner || !security.value.accessScope)) return false;
  if ((scope.value === "team" || scope.value === "tenant" || canvasTables.value.length > 1) && !security.value.tenant) return false;
  return true;
});

const canCreate = computed(() => Boolean(
  modelId.value
  && modelName.value
  && description.value
  && canvasTables.value.length
  && availableFields.value.length
  && Object.values(grainFields.value).some(Boolean)
  && securityReady.value
  && metricErrors.value.length === 0,
));

function addTable(table: TableInfo) {
  const base = normalizeIdentifier(table.name);
  let alias = base;
  let index = 2;
  while (canvasTables.value.some((item) => item.alias === alias)) alias = `${base}_${index++}`;
  canvasTables.value.push({
    ...table,
    alias,
    x: 28 + canvasTables.value.length * 36,
    y: 28 + canvasTables.value.length * 28,
  });
}

function removeTable(alias: string) {
  const removedFieldNames = new Set(
    canvasTables.value
      .find((item) => item.alias === alias)
      ?.columns.map((column) => fieldName(alias, column.name)) ?? [],
  );
  canvasTables.value = canvasTables.value.filter((item) => item.alias !== alias);
  joins.value = joins.value.filter((item) => item.left_source !== alias && item.right_source !== alias);
  for (const key of Object.keys(selectedFields.value)) {
    if (key.startsWith(`${alias}.`)) delete selectedFields.value[key];
  }
  for (const key of Object.keys(grainFields.value)) {
    if (key.startsWith(`${alias}.`)) delete grainFields.value[key];
  }
  for (const key of Object.keys(security.value) as Array<keyof typeof security.value>) {
    if (security.value[key].startsWith(`${alias}.`)) security.value[key] = "";
  }
  metrics.value = metrics.value
    .filter((metric) => !removedFieldNames.has(metric.sourceField))
    .map((metric) => ({
      ...metric,
      allowedDimensions: metric.allowedDimensions.filter((name) => !removedFieldNames.has(name)),
    }));
}

function onFieldSelectionChanged(qualified: string) {
  if (selectedFields.value[qualified]) return;
  delete grainFields.value[qualified];
  for (const key of Object.keys(security.value) as Array<keyof typeof security.value>) {
    if (security.value[key] === qualified) security.value[key] = "";
  }
  const [source, column] = qualified.split(".");
  const removedName = fieldName(source, column);
  metrics.value = metrics.value
    .filter((metric) => metric.sourceField !== removedName)
    .map((metric) => ({
      ...metric,
      allowedDimensions: metric.allowedDimensions.filter((name) => name !== removedName),
    }));
}

function startMove(event: PointerEvent, table: CanvasTable) {
  const target = event.currentTarget as HTMLElement;
  target.setPointerCapture(event.pointerId);
  drag.value = { alias: table.alias, dx: event.clientX - table.x, dy: event.clientY - table.y };
}

function move(event: PointerEvent) {
  if (!drag.value) return;
  const table = canvasTables.value.find((item) => item.alias === drag.value?.alias);
  if (table) {
    table.x = Math.max(0, event.clientX - drag.value.dx);
    table.y = Math.max(0, event.clientY - drag.value.dy);
  }
}

function chooseJoin(source: string, column: string) {
  if (!pendingJoin.value) {
    pendingJoin.value = { source, column };
    return;
  }
  if (pendingJoin.value.source !== source) {
    const duplicate = joins.value.some((join) => (
      join.left_source === pendingJoin.value?.source
      && join.left_column === pendingJoin.value?.column
      && join.right_source === source
      && join.right_column === column
    ));
    if (!duplicate) {
      joins.value.push({
        left_source: pendingJoin.value.source,
        left_column: pendingJoin.value.column,
        right_source: source,
        right_column: column,
        join_type: "left",
        cardinality: "many_to_one",
      });
    }
  }
  pendingJoin.value = null;
}

function addMetric() {
  const source = availableFields.value.find((field) => field.numeric) ?? availableFields.value[0];
  if (!source) return;
  const aggregation: Aggregation = source.numeric ? "sum" : "count_distinct";
  const base = normalizeIdentifier(`${aggregation}_${source.name}`).slice(0, 120);
  let id = base;
  let index = 2;
  while (metrics.value.some((metric) => metric.id === id)) id = `${base}_${index++}`;
  metrics.value.push({
    id,
    label: `${source.label} ${aggregation}`,
    description: "",
    aliases: "",
    aggregation,
    sourceField: source.name,
    unit: "",
    restrictDimensions: false,
    allowedDimensions: [],
  });
}

function objectRows(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    : [];
}

function hydrateInitialModel() {
  const initial = props.initialModel;
  if (!initial) return;
  modelId.value = String(initial.id ?? "");
  modelName.value = String(initial.name ?? "");
  description.value = String(initial.description ?? "");
  domain.value = String(initial.domain ?? "general");
  if (["personal", "team", "tenant"].includes(String(initial.scope))) {
    scope.value = String(initial.scope) as typeof scope.value;
  }

  const persistedFields = objectRows(initial.fields);
  const qualifiedByName = new Map<string, string>();
  for (const field of persistedFields) {
    const source = String(field.source ?? "");
    const column = String(field.source_column ?? "");
    const name = String(field.name ?? "");
    if (!source || !column || !name) continue;
    const qualified = `${source}.${column}`;
    qualifiedByName.set(name, qualified);
    selectedFields.value[qualified] = true;
  }

  canvasTables.value = objectRows(initial.sources).map((source, index) => {
    const alias = String(source.alias ?? "");
    const name = String(source.table ?? "");
    const schema = source.schema == null ? null : String(source.schema);
    const introspected = props.tables.find(
      (table) => table.name === name && (table.schema ?? null) === schema,
    ) ?? props.tables.find((table) => table.name === name);
    const columns = introspected?.columns ?? persistedFields
      .filter((field) => String(field.source ?? "") === alias)
      .map((field) => ({
        name: String(field.source_column ?? ""),
        type: String(field.data_type ?? ""),
      }))
      .filter((column) => column.name);
    return {
      schema,
      name,
      columns,
      alias,
      x: 28 + index * 36,
      y: 28 + index * 28,
    };
  }).filter((table) => table.alias && table.name);

  const grain = new Set(
    Array.isArray(initial.grain) ? initial.grain.map((item) => String(item)) : [],
  );
  for (const name of grain) {
    const qualified = qualifiedByName.get(name);
    if (qualified) grainFields.value[qualified] = true;
  }
  joins.value = objectRows(initial.relationships).map((item) => ({
    left_source: String(item.left_source ?? ""),
    left_column: String(item.left_column ?? ""),
    right_source: String(item.right_source ?? ""),
    right_column: String(item.right_column ?? ""),
    join_type: item.join_type === "inner" ? "inner" : "left",
    cardinality: ["one_to_one", "one_to_many", "many_to_one"].includes(String(item.cardinality))
      ? String(item.cardinality) as JoinEdge["cardinality"]
      : "many_to_one",
  }));
  const allowedAggregations: Aggregation[] = ["sum", "count", "count_distinct", "avg", "min", "max"];
  metrics.value = objectRows(initial.metrics).map((item) => {
    const aggregation = allowedAggregations.includes(item.aggregation as Aggregation)
      ? item.aggregation as Aggregation
      : "count";
    const allowedDimensions = Array.isArray(item.allowed_dimensions)
      ? item.allowed_dimensions.map((value) => String(value))
      : [];
    return {
      id: String(item.name ?? ""),
      label: String(item.label ?? item.name ?? ""),
      description: String(item.description ?? ""),
      aliases: Array.isArray(item.aliases) ? item.aliases.map((value) => String(value)).join("，") : "",
      aggregation,
      sourceField: item.field == null ? "" : String(item.field),
      unit: String(item.unit ?? ""),
      restrictDimensions: allowedDimensions.length > 0,
      allowedDimensions,
    };
  });
  security.value = {
    tenant: qualifiedByName.get(String(initial.tenant_field ?? "")) ?? "",
    org: qualifiedByName.get(String(initial.org_field ?? "")) ?? "",
    owner: qualifiedByName.get(String(initial.owner_field ?? "")) ?? "",
    accessScope: qualifiedByName.get(String(initial.access_scope_field ?? "")) ?? "",
  };
}

function createModel() {
  if (!canCreate.value) return;
  const fields = canvasTables.value.flatMap((table) => table.columns
    .filter((column) => selectedFields.value[`${table.alias}.${column.name}`])
    .map((column) => {
      const dataType = columnDataType(column.type);
      const isComparable = dataType === "integer" || dataType === "number" || dataType === "date" || dataType === "datetime";
      return {
        name: fieldName(table.alias, column.name),
        source: table.alias,
        source_column: column.name,
        data_type: dataType,
        label: column.name,
        description: "",
        semantic_type: "dimension",
        sensitivity: "internal",
        selectable: true,
        allowed_operators: isComparable
          ? ["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "between"]
          : ["eq", "ne", "in", "not_in", "contains"],
      };
    }));
  const grain = fields
    .filter((field) => grainFields.value[`${field.source}.${field.source_column}`])
    .map((field) => field.name);
  const resolveFieldName = (qualified: string) => {
    const [source, column] = qualified.split(".");
    return fields.find((field) => field.source === source && field.source_column === column)?.name || null;
  };
  const metricPayload = metrics.value.map((metric) => ({
    name: metric.id.trim(),
    label: metric.label.trim(),
    description: metric.description.trim(),
    aliases: metric.aliases.split(/[，,]/).map((value) => value.trim()).filter(Boolean),
    aggregation: metric.aggregation,
    field: metric.aggregation === "count" && !metric.sourceField ? null : metric.sourceField,
    allowed_dimensions: metric.restrictDimensions ? metric.allowedDimensions : [],
    unit: metric.unit.trim(),
  }));
  emit("create", {
    model_id: modelId.value,
    connector_id: props.connectorId,
    name: modelName.value,
    description: description.value,
    domain: domain.value,
    scope: scope.value,
    logical_model: {
      version: "1.0.0",
      scope: scope.value,
      grain,
      sources: canvasTables.value.map((table) => ({
        alias: table.alias,
        table: table.name,
        schema: table.schema || null,
      })),
      relationships: joins.value,
      fields,
      metrics: metricPayload,
      tenant_field: resolveFieldName(security.value.tenant),
      org_field: resolveFieldName(security.value.org),
      owner_field: resolveFieldName(security.value.owner),
      access_scope_field: resolveFieldName(security.value.accessScope),
      max_rows: 500,
      required_permission: "business.data.read",
      tags: [domain.value],
      examples: [],
    },
  });
}

hydrateInitialModel();
</script>

<template>
  <div class="semantic-modeler">
    <div class="modeler-meta">
      <label><span>模型 ID</span><input v-model.trim="modelId" placeholder="finance.invoice_model" :disabled="Boolean(initialModel)" /></label>
      <label><span>模型名称</span><input v-model.trim="modelName" /></label>
      <label><span>领域</span><input v-model.trim="domain" /></label>
      <label><span>范围</span><select v-model="scope"><option value="personal">个人</option><option value="team">团队</option><option value="tenant">租户</option></select></label>
      <label class="wide"><span>说明</span><input v-model.trim="description" /></label>
    </div>

    <div class="modeler-layout">
      <aside>
        <strong>内省数据表</strong>
        <button v-for="table in tables" :key="`${table.schema}.${table.name}`" type="button" @click="addTable(table)">
          <Plus :size="13" />{{ table.schema ? `${table.schema}.` : "" }}{{ table.name }}
        </button>
      </aside>
      <div class="model-canvas" @pointermove="move" @pointerup="drag = null" @pointercancel="drag = null">
        <p v-if="!canvasTables.length" class="canvas-help">从左侧添加表。点击两个不同表的字段即可创建 Join；拖动表头可以布局。</p>
        <article v-for="table in canvasTables" :key="table.alias" class="table-node" :style="{ left: `${table.x}px`, top: `${table.y}px` }">
          <header @pointerdown="startMove($event, table)">
            <GripVertical :size="14" /><strong>{{ table.alias }}</strong><small>{{ table.name }}</small>
            <button type="button" title="删除数据表" @pointerdown.stop @click="removeTable(table.alias)"><Trash2 :size="12" /></button>
          </header>
          <label v-for="column in table.columns" :key="column.name" :class="pendingJoin?.source === table.alias && pendingJoin?.column === column.name && 'join-pending'">
            <input v-model="selectedFields[`${table.alias}.${column.name}`]" type="checkbox" title="选择字段" @change="onFieldSelectionChanged(`${table.alias}.${column.name}`)" />
            <button type="button" title="选择 Join 字段" @click="chooseJoin(table.alias, column.name)"><GitMerge :size="11" /></button>
            <span>{{ column.name }}</span><small>{{ column.type }}</small>
            <input v-if="selectedFields[`${table.alias}.${column.name}`]" v-model="grainFields[`${table.alias}.${column.name}`]" type="checkbox" title="设为 Grain（唯一粒度）" />
          </label>
        </article>
      </div>
    </div>

    <section class="join-list section-card">
      <div class="section-title"><strong>多表关系</strong><small>Join 必须形成一棵连通树，指标不允许经过 1:N fan-out。</small></div>
      <p v-if="!joins.length" class="empty-tip">单表模型不需要 Join；多表模型请在画布中依次点击两个表的关联字段。</p>
      <div v-for="(join, index) in joins" :key="index" class="join-row">
        <code>{{ join.left_source }}.{{ join.left_column }}</code>
        <select v-model="join.join_type"><option value="left">LEFT JOIN</option><option value="inner">INNER JOIN</option></select>
        <code>{{ join.right_source }}.{{ join.right_column }}</code>
        <select v-model="join.cardinality"><option value="one_to_one">1:1</option><option value="one_to_many">1:N</option><option value="many_to_one">N:1</option></select>
        <button type="button" title="删除 Join" @click="joins.splice(index, 1)"><Trash2 :size="12" /></button>
      </div>
    </section>

    <section class="metric-editor section-card">
      <div class="section-title">
        <div><strong>指标 / Measure</strong><small>指标是由受控聚合函数计算的数字，不允许填写 SQL 或任意表达式。</small></div>
        <button type="button" :disabled="!availableFields.length" @click="addMetric"><Plus :size="13" />添加指标</button>
      </div>
      <p v-if="!metrics.length" class="empty-tip">可选：先勾选字段，再添加“总金额、订单数、平均单价”等指标。</p>
      <div v-for="(metric, index) in metrics" :key="index" class="metric-row">
        <label><span>指标 ID</span><input v-model.trim="metric.id" placeholder="total_amount" /></label>
        <label><span>展示名称</span><input v-model.trim="metric.label" placeholder="总金额" /></label>
        <label><span>口径说明</span><input v-model.trim="metric.description" placeholder="已审批订单含税金额合计" /></label>
        <label><span>业务别名</span><input v-model.trim="metric.aliases" placeholder="采购额，订单金额" /></label>
        <label><span>聚合</span><select v-model="metric.aggregation"><option value="sum">求和 sum</option><option value="count">计数 count</option><option value="count_distinct">去重计数</option><option value="avg">平均 avg</option><option value="min">最小 min</option><option value="max">最大 max</option></select></label>
        <label><span>来源字段</span><select v-model="metric.sourceField"><option v-if="metric.aggregation === 'count'" value="">按行计数</option><option v-for="field in availableFields" :key="field.name" :value="field.name">{{ field.label }} · {{ field.dataType }}</option></select></label>
        <label><span>单位/格式</span><input v-model.trim="metric.unit" placeholder="元、%、件（可选）" /></label>
        <label class="dimension-toggle"><span>分组维度</span><span><input v-model="metric.restrictDimensions" type="checkbox" />限制维度</span></label>
        <label v-if="metric.restrictDimensions" class="dimension-select"><span>允许分组</span><select v-model="metric.allowedDimensions" multiple><option v-for="field in availableFields" :key="field.name" :value="field.name">{{ field.label }}</option></select></label>
        <button type="button" title="删除指标" @click="metrics.splice(index, 1)"><Trash2 :size="14" /></button>
      </div>
      <ul v-if="metricErrors.length" class="validation-errors"><li v-for="item in metricErrors" :key="item">{{ item }}</li></ul>
    </section>

    <section class="security-fields section-card">
      <div class="section-title"><strong>数据权限字段</strong><small>这些字段由服务端注入权限过滤，Agent 和前端不能覆盖。</small></div>
      <div class="security-grid">
        <label><span>tenant 字段</span><select v-model="security.tenant"><option value="">未设置</option><option v-for="field in availableFields" :key="field.qualified" :value="field.qualified">{{ field.label }}</option></select></label>
        <label><span>org 字段</span><select v-model="security.org"><option value="">未设置</option><option v-for="field in availableFields" :key="field.qualified" :value="field.qualified">{{ field.label }}</option></select></label>
        <label><span>owner 字段</span><select v-model="security.owner"><option value="">未设置</option><option v-for="field in availableFields" :key="field.qualified" :value="field.qualified">{{ field.label }}</option></select></label>
        <label><span>access_scope 字段</span><select v-model="security.accessScope"><option value="">未设置</option><option v-for="field in availableFields" :key="field.qualified" :value="field.qualified">{{ field.label }}</option></select></label>
      </div>
      <p v-if="!securityReady" class="validation-hint">
        {{ scope === "personal" ? "个人模型必须选择 owner 和 access_scope 字段。" : "团队/租户/多表模型必须选择 tenant 字段。" }}
      </p>
    </section>

    <button class="primary" type="button" :disabled="!canCreate" @click="createModel">{{ initialModel ? "保存并校验新版本" : "创建并校验语义模型" }}</button>
  </div>
</template>

<style scoped>
.semantic-modeler{display:grid;gap:12px}.modeler-meta{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.modeler-meta label,.security-grid label,.metric-row label{display:grid;gap:4px}.modeler-meta .wide{grid-column:span 2}.modeler-layout{display:grid;grid-template-columns:190px minmax(0,1fr);min-height:480px;border:1px solid #dfe5ec;border-radius:12px;overflow:hidden}.modeler-layout aside{padding:12px;border-right:1px solid #dfe5ec;background:#f8fafc;display:flex;flex-direction:column;gap:7px}.modeler-layout aside button,.section-title button{display:flex;gap:5px;align-items:center;text-align:left}.model-canvas{position:relative;overflow:auto;background-image:radial-gradient(#d7dee8 1px,transparent 1px);background-size:18px 18px;min-height:480px}.canvas-help{padding:24px;color:#64748b}.table-node{position:absolute;width:260px;background:white;border:1px solid #b8c5d6;border-radius:9px;box-shadow:0 8px 20px #0f172a18;overflow:hidden}.table-node header{display:flex;align-items:center;gap:5px;padding:8px;background:#eef4fb;cursor:move}.table-node header small{margin-left:auto}.table-node header button,.table-node>label button{border:0;background:transparent}.table-node>label{display:grid;grid-template-columns:18px 24px 1fr auto 18px;align-items:center;gap:4px;padding:5px 8px;border-top:1px solid #edf1f5}.table-node>label.join-pending{background:#fff7d6}.section-card{padding:12px;border:1px solid #dfe5ec;border-radius:10px;background:#fff}.section-title{display:flex;align-items:center;justify-content:space-between;gap:12px}.section-title>div,.section-title>strong{display:grid;gap:3px}.section-title small,.empty-tip{color:#64748b}.join-list,.metric-editor{display:grid;gap:8px}.join-row{display:flex;align-items:center;gap:8px}.metric-row{display:grid;grid-template-columns:1fr 1.2fr 150px 1.25fr 1fr 36px;gap:8px;align-items:end;padding-top:8px;border-top:1px solid #edf1f5}.metric-row>button{height:34px}.security-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:10px}.validation-errors,.validation-hint{margin:0;color:#b42318;background:#fff3f1;border-radius:7px;padding:8px 12px}.validation-errors{padding-left:28px}.primary:disabled{cursor:not-allowed;opacity:.5}@media(max-width:1100px){.metric-row{grid-template-columns:1fr 1fr 1fr}.metric-row>button{align-self:end}}@media(max-width:900px){.modeler-layout{grid-template-columns:1fr}.modeler-layout aside{border-right:0;border-bottom:1px solid #dfe5ec}.modeler-meta,.security-grid{grid-template-columns:1fr 1fr}.metric-row{grid-template-columns:1fr 1fr}}
.modeler-layout,.table-node,.section-card{border-radius:8px}.metric-row{grid-template-columns:repeat(4,minmax(0,1fr))}.dimension-toggle>span:last-child{display:flex;align-items:center;gap:6px;min-height:34px}.dimension-select{grid-column:1/-2}.dimension-select select{min-height:76px}
@media(max-width:900px){.dimension-select{grid-column:1/-1}}
</style>
