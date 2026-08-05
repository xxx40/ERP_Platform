export type ResponseStatus =
  | "success"
  | "needs_clarification"
  | "rejected"
  | "not_found"
  | "unauthorized"
  | "timeout"
  | "service_error";

export interface Understanding {
  intent: "document" | "order" | "mixed" | "analytics" | "composite" | "business" | "general" | "clarify" | "reject";
  order_type: string | null;
  order_number: string | null;
  user_goal: string;
  missing_fields: string[];
  summary: string;
  analytics_period: string | null;
  analytics_comparison: string | null;
  analytics_dimension: string | null;
  required_tools: string[];
  workflow_id: string | null;
  route_confidence: number | null;
  routing_mode: string | null;
  route_arguments: Record<string, unknown>;
  request_kind: string | null;
  domain: string | null;
  operation: string | null;
  entity: string | null;
  data_needs: string[];
  evidence_need: boolean | null;
}

export interface PresentationBlock {
  type: "markdown" | "key_value" | "table" | "metric" | "chart";
  title: string | null;
  text: string | null;
  items: Array<Record<string, unknown>>;
  columns: string[];
  rows: unknown[][];
  chart_type: "line" | "bar" | "pie" | null;
  x_axis: string[];
  series: Array<{ name?: string; data?: number[]; [key: string]: unknown }>;
}

export interface DocumentAnswer {
  conclusion: string;
  confirmed_facts: string[];
  unknowns: string[];
  details: string[];
  steps: string[];
  cautions: string[];
  sections?: DocumentAnswerSection[];
  source_ids: string[];
}

export interface DocumentAnswerSection {
  title: string;
  summary: string | null;
  items: string[];
  source_ids: string[];
}

export interface OrderLineFact {
  line_no: number;
  material_code: string;
  material_name: string;
  ordered_qty: number;
  received_qty: number;
  inbound_qty: number;
  unit: string;
  unit_price: number | null;
  tax_inclusive_unit_price: number | null;
  line_amount: number | null;
  warehouse_name: string | null;
  planned_receive_date: string | null;
  delivery_date: string | null;
  promised_date: string | null;
}

export interface OrderCard {
  order_number: string;
  order_type: string;
  business_status: string | null;
  audit_status: string | null;
  change_status: string | null;
  receipt_status: string | null;
  inbound_status: string | null;
  status_reason: string | null;
  supplier_name: string | null;
  buyer_name: string | null;
  purchase_org_name: string | null;
  order_date: string | null;
  currency: string | null;
  total_amount: number | null;
  line_items: OrderLineFact[];
  related_documents: string[];
  queried_at: string;
  data_source: string;
  data_connector_id: string | null;
  data_route_key: string | null;
  data_schema_version: string | null;
  data_source_tables: string[];
  mock_data: boolean;
}

export interface AnalyticsMetric {
  key: string;
  label: string;
  value: number;
  unit: string;
  comparison_value: number | null;
  change_value: number | null;
  change_rate: number | null;
  trend: "up" | "down" | "flat";
}

export interface AnalyticsTrendPoint {
  period: string;
  label: string;
  metric_key?: string;
  value?: number;
  purchase_amount?: number;
  order_count?: number;
  [key: string]: string | number | undefined;
}

export interface AnalyticsDimensionItem {
  key: string;
  label: string;
  value: number;
  share: number;
  comparison_value: number | null;
  change_rate: number | null;
}

export interface AnalyticsMetricDefinition {
  key: string;
  label: string;
  unit: string;
  definition: string;
  formula: string;
  allowed_dimensions: string[];
}

export interface AnalyticsCard {
  analysis_type: string;
  period_type: string;
  comparison_mode: string;
  breakdown_dimension: string;
  title: string;
  summary: string;
  scope_label: string;
  period_label: string;
  comparison_label: string;
  comparison_basis: string;
  currency: string;
  trend_metric_key?: string;
  breakdown_metric_key?: string;
  breakdown_chart_type?: "bar" | "pie";
  metrics: AnalyticsMetric[];
  trend: AnalyticsTrendPoint[];
  breakdown_title: string;
  breakdown: AnalyticsDimensionItem[];
  insights: string[];
  recommendations: string[];
  cautions: string[];
  metric_version: string;
  metric_definitions: AnalyticsMetricDefinition[];
  data_as_of: string;
  queried_at: string;
  data_source: string;
  data_connector_id: string | null;
  data_route_key: string | null;
  data_schema_version: string | null;
  data_source_tables: string[];
  mock_data: boolean;
}

export interface SourceReference {
  source_id: string;
  title: string;
  source_system: string;
  authority_level: string;
  filename: string | null;
  url: string | null;
  excerpt: string;
  score: number | null;
  updated_at: string | null;
}

export interface WorkflowStep {
  stage: string;
  status: string;
  detail: string;
  attempt: number;
  tools: string[];
}

export interface WorkflowTrace {
  plan_summary: string;
  allowed_tools: string[];
  steps: WorkflowStep[];
  retrieval_rounds: number;
  evaluation: string | null;
  final_state: string;
}

export interface OrderListItem {
  order_number: string;
  supplier_name: string;
  order_date: string | null;
  currency: string | null;
  total_amount: number | null;
  ordered_qty: number;
  received_qty: number;
  inbound_qty: number;
  receipt_status: string;
  inbound_status: string;
}

export interface OrderListResult {
  items: OrderListItem[];
  total_count: number;
  returned_count: number;
  truncated: boolean;
  inbound_state: "not_inbound" | "incomplete";
  queried_at: string;
  data_source: string;
  data_connector_id: string | null;
  data_route_key: string | null;
  data_schema_version: string | null;
  data_source_tables: string[];
  mock_data: boolean;
}

export interface ChatResponse {
  request_id: string;
  session_id: string;
  status: ResponseStatus;
  understanding: Understanding;
  document_answer: DocumentAnswer | null;
  order_card: OrderCard | null;
  order_list: OrderListResult | null;
  analytics_card: AnalyticsCard | null;
  presentation: PresentationBlock[];
  sources: SourceReference[];
  workflow: WorkflowTrace | null;
  error: { code: string; message: string } | null;
}

export type FeedbackRating = "helpful" | "not_helpful";
export type FeedbackReason =
  | "incorrect"
  | "incomplete"
  | "citation_issue"
  | "outdated"
  | "hard_to_understand"
  | "other";

export interface AnswerFeedback {
  request_id: string;
  rating: FeedbackRating;
  reason_codes: FeedbackReason[];
  comment: string | null;
  created_at: string;
  updated_at: string;
}

export interface FeedbackPayload {
  rating: FeedbackRating;
  reason_codes?: FeedbackReason[];
  comment?: string | null;
}

export interface ChatEntry {
  entryId: string;
  question: string;
  createdAt?: string;
  response?: ChatResponse;
  pending?: boolean;
  error?: string;
  trace?: TraceResponse;
  workflowRun?: WorkflowRun;
  traceLoading?: boolean;
  traceError?: string;
  feedback?: AnswerFeedback;
  feedbackLoading?: boolean;
  feedbackError?: string;
  feedbackNotice?: string;
  feedbackPanelOpen?: boolean;
  feedbackDraftReasons?: FeedbackReason[];
  feedbackDraftComment?: string;
}

export interface ConversationSummary {
  session_id: string;
  title: string;
  last_question: string;
  interaction_count: number;
  created_at: string;
  updated_at: string;
}

export interface ConversationInteraction {
  request_id: string;
  question: string;
  response: ChatResponse;
  created_at: string;
  feedback: AnswerFeedback | null;
}

export interface ConversationListResponse {
  count: number;
  items: ConversationSummary[];
}

export interface ConversationDetailResponse {
  session_id: string;
  interactions: ConversationInteraction[];
}

export interface TraceSpan {
  span_id: string;
  name: string;
  kind: string;
  status: string;
  started_at: string;
  ended_at: string;
  duration_ms: number;
  attributes: Record<string, unknown>;
  error_code: string | null;
}

export interface TraceResponse {
  request_id: string;
  session_id: string;
  spans: TraceSpan[];
}

export interface WorkflowRunNode {
  execution_id: string;
  graph_id: string;
  parent_node_id: string | null;
  attempt: number;
  node_id: string;
  kind: string;
  handler: string;
  status: string;
  duration_ms: number | null;
  error_code: string | null;
}

export interface GraphTopologyNode {
  node_id: string;
  name: string;
  kind: "start" | "end" | "node";
}

export interface GraphTopologyEdge {
  source: string;
  target: string;
  condition: string | null;
  conditional: boolean;
}

export interface GraphTopology {
  graph_id: string;
  type: "orchestrator" | "retrieval_subgraph";
  name: string;
  version: string;
  nodes: GraphTopologyNode[];
  edges: GraphTopologyEdge[];
  mermaid: string;
  related_graph_ids: string[];
}

export interface WorkflowToolCall {
  call_id: string;
  node_id: string;
  tool_id: string;
  tool_version: string;
  connector_id: string | null;
  arguments: Record<string, unknown>;
  status: string;
  duration_ms: number | null;
  error_code: string | null;
  attempt_count: number;
  retry_history: Array<Record<string, unknown>>;
}

export interface WorkflowPolicyDecision {
  node_id: string;
  tool_id: string;
  action: string;
  resource: string;
  allowed: boolean;
  reason: string;
  policy_id: string;
  policy_version: string;
}

export interface WorkflowRun {
  request_id: string;
  session_id: string;
  workflow_id: string;
  workflow_version: string;
  identity_scope: {
    user_id: string;
    tenant_id: string;
    org_code: string;
  };
  status: string;
  started_at: string;
  ended_at: string | null;
  error_code: string | null;
  snapshot_version: string | null;
  snapshot_hash: string | null;
  skill_id: string | null;
  operation_id: string | null;
  prompt_version: string | null;
  nodes: WorkflowRunNode[];
  tool_calls: WorkflowToolCall[];
  policy_decisions: WorkflowPolicyDecision[];
  verification_runs: Array<{
    id: number;
    verifier_version: string;
    passed: boolean;
    deterministic_passed: boolean;
    semantic_status: string;
    issues: string[];
    repair_attempt: number;
    skipped_reason: string | null;
    created_at: string;
  }>;
}

export interface HealthResponse {
  status: string;
  environment: string;
  capabilities: {
    wise: boolean;
    ima: boolean;
    model: boolean;
    purchase_order: string;
    trace: boolean;
    langfuse: boolean;
    graph_runtime: string;
    graph_definitions: number;
    registered_tools: number;
    registered_capabilities: number;
    registered_plugins: number;
    platform_snapshot: string;
  };
  dependencies: Record<string, { configured: boolean; ready: boolean | null }>;
}

export interface AuthorizationContext {
  user_id: string;
  display_name: string | null;
  email: string | null;
  tenant_id: string;
  org_code: string;
  roles: string[];
  auth_source: string;
  trusted: boolean;
  permissions: Record<string, boolean>;
  policy: { id: string; version: string };
  full_platform_access: boolean;
}

export interface PlatformModule {
  id: string;
  name: string;
  version: string;
  enabled: boolean;
  type: "python" | "declarative";
  capability_count: number;
  tool_count: number;
}

export interface PlatformCapability {
  id: string;
  name: string;
  description: string;
  domains: string[];
  module_ids: string[];
  tool_ids: string[];
  required_permissions: string[];
  risk_levels: string[];
  examples: string[];
  tags: string[];
}

export interface ConnectorStatus {
  source_id: string;
  connector_id?: string;
  type?: string;
  route_count: number;
  routes?: Array<{ tenant_id: string; org_code: string }>;
  default: boolean;
  ready: boolean;
}

export interface ConnectorSnapshot {
  revision: string;
  version: string;
  connectors: ConnectorStatus[];
  catalog: {
    version: string;
    connectors: Array<Record<string, unknown>>;
  };
  rollback_available: boolean;
}

export interface BusinessDatasetSnapshot {
  count: number;
  items: Array<Record<string, unknown> & {
    id: string;
    name: string;
    domain: string;
    connector_id: string;
    fields: Array<Record<string, unknown>>;
    metrics: Array<Record<string, unknown>>;
  }>;
  snapshot: {
    revision: string;
    version: string;
    rollback_available: boolean;
  };
}

export interface PlatformProviderStatus {
  kind: string;
  provider: string;
  configured: boolean;
  ready?: boolean;
}

export interface PlatformSecret {
  secret_id: string;
  name: string;
  masked: string;
  provider: string;
}

export interface GovernedDataSource {
  connector_id: string;
  owner_user_id: string;
  tenant_id: string;
  org_code: string;
  display_name: string;
  dialect: "postgresql" | "mysql" | "sqlserver" | "oracle" | "http";
  host_masked: string;
  database_name: string | null;
  scope: "personal" | "team" | "tenant";
  status: "draft" | "testing" | "ready" | "submitted" | "approved" | "rejected" | "published" | "disabled";
  approved_by: string | null;
  approved_at: string | null;
  version: number;
  secret: { secret_id: string; masked: string };
}

export interface SemanticModelRecord {
  model_id: string;
  connector_id: string;
  owner_user_id: string;
  tenant_id: string;
  org_code: string;
  name: string;
  description: string;
  domain: string;
  scope: "personal" | "team" | "tenant";
  status: string;
  current_version: number;
  logical_model: Record<string, unknown>;
  validation_result: { valid?: boolean; errors?: string[] };
  version_status?: string;
  published_at?: string | null;
}

export interface SemanticModelVersion {
  model_id: string;
  version: number;
  logical_model: Record<string, unknown>;
  validation_result: { valid?: boolean; errors?: string[] };
  status: string;
  created_by: string;
  created_at: string;
  published_at: string | null;
}

export interface HttpToolCatalogSnapshot {
  version: string;
  revision: string;
  count: number;
  items: Array<Record<string, unknown> & { id: string; name: string; domain: string }>;
  rollback_available?: boolean;
}

export interface PlatformConfigStatus {
  valid: boolean;
  snapshot: { version: string; content_hash: string; loaded_at: string };
  plugin_enabled: Record<string, boolean>;
  plugin_count: number;
  capability_count: number;
  graph_count: number;
  tool_count: number;
  rollback_available?: boolean;
  release_gate_enforced?: boolean;
}

export interface HarnessStatus {
  snapshot: { version: string; content_hash: string; loaded_at: string };
  runtime: {
    graph_engine: string;
    tool_boundary: string;
    retry_policy: { max_attempts: number; retryable: string[]; non_retryable: string[] };
    answer_verifier: string;
    max_repair_attempts: number;
    memory_turn_limit: number;
  };
  retention: { conversations_days: number; trace_and_evidence_days: number };
  release_gate_enforced: boolean;
  prompts: Array<{ id: string; version: string; content_hash: string }>;
}

export interface EvaluationRun {
  run_id: string;
  snapshot_version: string | null;
  dataset: string;
  metrics: {
    case_count?: number;
    overall_pass_rate?: number;
    security_pass_rate?: number;
    p95_latency_ms?: number;
  };
  release_gate: { passed?: boolean; checks?: Record<string, boolean>; reasons?: string[] };
  result_path: string | null;
  created_at: string;
}

export interface PlatformConfigVersion {
  id: number;
  action: string;
  snapshot_version: string;
  content_hash: string;
  config: { plugin_enabled: Record<string, boolean>; note?: string | null };
  actor_user_id: string;
  created_at: string;
}
