<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  Check,
  ChevronDown,
  CircleHelp,
  Clipboard,
  Clock3,
  Database,
  ExternalLink,
  FileSearch,
  GitBranch,
  LoaderCircle,
  LogIn,
  LogOut,
  Menu,
  MessageSquare,
  MessageSquarePlus,
  PackageSearch,
  Paperclip,
  RefreshCw,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  X,
} from "@lucide/vue";
import {
  authenticationMode,
  developmentIdentities,
  getDevelopmentIdentity,
  initializeAuthentication,
  setDevelopmentIdentity,
  startLogin,
  startLogout,
} from "./auth";
import {
  API_BASE_URL,
  deleteConversation,
  getConversation,
  getAuthorizationContext,
  getHealth,
  getTrace,
  getWorkflowRun,
  listConversations,
  sendChat,
  submitFeedback,
} from "./api";
import type {
  ChatEntry,
  ConversationSummary,
  FeedbackRating,
  FeedbackReason,
  HealthResponse,
  AuthorizationContext,
  OrderCard,
  Understanding,
} from "./types";

const AnalyticsPanel = defineAsyncComponent(() => import("./AnalyticsPanel.vue"));
const ResponseRenderer = defineAsyncComponent(() => import("./ResponseRenderer.vue"));

const suggestionActions = [
  {
    label: "订单进度",
    question: "PO202607001 当前状态",
  },
  {
    label: "经营概览",
    question: "本季度采购经营概览",
  },
  {
    label: "制度查询",
    question: "采购订单审核后如何收料？",
  },
  {
    label: "流程说明",
    question: "供应商准入流程需要提交哪些资料？",
  },
];

const feedbackReasons: Array<{ code: FeedbackReason; label: string }> = [
  { code: "incorrect", label: "事实错误" },
  { code: "incomplete", label: "回答不完整" },
  { code: "citation_issue", label: "引用问题" },
  { code: "outdated", label: "内容过期" },
  { code: "hard_to_understand", label: "表达不清" },
  { code: "other", label: "其他" },
];

const SESSION_STORAGE_KEY = "erp-assistant-session";
const entries = ref<ChatEntry[]>([]);
const input = ref("");
const sending = ref(false);
const sessionId = ref(localStorage.getItem(SESSION_STORAGE_KEY) ?? "");
const health = ref<HealthResponse | null>(null);
const authContext = ref<AuthorizationContext | null>(null);
const identityError = ref("");
const networkError = ref("");
const conversationElement = ref<HTMLElement | null>(null);
const searchQuery = ref("");
const sidebarOpen = ref(false);
const conversations = ref<ConversationSummary[]>([]);
const historyLoading = ref(false);
const historyError = ref("");
const deletingSessionIds = ref<Set<string>>(new Set());
const debugRequested = new URLSearchParams(window.location.search).get("debug") === "1";
const debugMode = computed(() => (
  import.meta.env.DEV
  && debugRequested
  && (authContext.value?.roles ?? []).includes("platform_admin")
));
const authReady = ref(false);
const loginRequired = ref(false);
const authenticationError = ref("");
const profileMenuOpen = ref(false);
const debugIdentityId = ref(getDevelopmentIdentity().id);

const serviceLabel = computed(() => {
  if (!health.value) return "正在连接";
  return health.value.status === "ok" ? "服务正常" : "部分服务异常";
});
const roleLabels: Record<string, string> = {
  employee: "普通员工",
  procurement_specialist: "采购专员",
  procurement_manager: "采购经理",
  data_source_reviewer: "数据源审批员",
  data_source_admin: "数据源管理员",
  platform_admin: "平台管理员",
};
const profileName = computed(() => {
  if (authenticationMode === "development" && getDevelopmentIdentity().id === "configured") {
    return "王主管";
  }
  return authContext.value?.display_name || "王主管";
});
const profileRole = computed(() => {
  const roles = authContext.value?.roles ?? [];
  const businessRoles = roles.filter((role) => !["platform_admin", "data_source_admin", "data_source_reviewer"].includes(role));
  return (businessRoles.length ? businessRoles : roles).map((role) => roleLabels[role] ?? role).join("、") || "未分配角色";
});
const profileInitials = computed(() => {
  const value = profileName.value.trim() || "--";
  return /[\u3400-\u9fff]/.test(value) ? value.slice(0, 1) : value.slice(0, 2).toUpperCase();
});
const safeExternalUrl = (value: string | null) => {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? value : null;
  } catch {
    return null;
  }
};
const formatGeneralAnswer = (value: string) => value
  .replace(/\r\n/g, "\n")
  .replace(/^\s{0,3}#{1,6}\s+/gm, "")
  .replace(/\*\*([^*]+)\*\*/g, "$1")
  .replace(/__([^_]+)__/g, "$1")
  .replace(/^\s*[-*+]\s+/gm, "- ")
  .replace(/`([^`]+)`/g, "$1")
  .trim();
const activeConversation = computed(() =>
  conversations.value.find((item) => item.session_id === sessionId.value),
);
const conversationTitle = computed(() => activeConversation.value?.title ?? entries.value[0]?.question ?? "新对话");
const recentConversations = computed(() => {
  const keyword = searchQuery.value.trim().toLowerCase();
  return conversations.value.filter((item) => (
    !keyword
    || item.title.toLowerCase().includes(keyword)
    || item.last_question.toLowerCase().includes(keyword)
  ));
});

const intentLabels: Record<Understanding["intent"], string> = {
  document: "制度检索",
  order: "订单查询",
  mixed: "业务诊断",
  analytics: "数据分析",
  composite: "复合任务",
  business: "业务查询",
  general: "通用问答",
  clarify: "信息补充",
  reject: "安全拦截",
};

async function loadApplication() {
  const [healthResult, identityResult] = await Promise.allSettled([
    getHealth(),
    getAuthorizationContext(),
  ]);
  if (healthResult.status === "fulfilled") {
    health.value = healthResult.value;
  } else {
    networkError.value = `后端服务未连接，请确认 ${API_BASE_URL} 已启动。`;
  }
  if (identityResult.status === "fulfilled") {
    authContext.value = identityResult.value;
  } else {
    identityError.value = identityResult.reason instanceof Error
      ? identityResult.reason.message
      : "身份与权限加载失败";
  }
  await refreshConversations();
  if (sessionId.value) {
    await loadConversation(sessionId.value);
  }
}

function requireLogin() {
  loginRequired.value = true;
  authContext.value = null;
  profileMenuOpen.value = false;
}

onMounted(async () => {
  window.addEventListener("erp-authentication-required", requireLogin);
  const state = await initializeAuthentication();
  authReady.value = true;
  loginRequired.value = state.loginRequired;
  authenticationError.value = state.error;
  if (state.authenticated) await loadApplication();
});

onBeforeUnmount(() => {
  window.removeEventListener("erp-authentication-required", requireLogin);
});

async function login() {
  authenticationError.value = "";
  try {
    await startLogin();
  } catch (cause) {
    authenticationError.value = cause instanceof Error ? cause.message : "无法跳转企业登录";
  }
}

async function logout() {
  profileMenuOpen.value = false;
  await startLogout();
}

function switchDevelopmentIdentity() {
  if (debugIdentityId.value === "configured") {
    sessionStorage.removeItem("erp-assistant-development-identity");
  } else {
    setDevelopmentIdentity(debugIdentityId.value);
  }
  localStorage.removeItem(SESSION_STORAGE_KEY);
  window.location.reload();
}

async function refreshConversations() {
  historyError.value = "";
  try {
    const pageSize = 100;
    const loaded: ConversationSummary[] = [];
    let offset = 0;
    let total = 0;
    do {
      const result = await listConversations(pageSize, offset);
      loaded.push(...result.items);
      total = result.count;
      offset += result.items.length;
      if (!result.items.length) break;
    } while (loaded.length < total);
    conversations.value = loaded;
    if (sessionId.value && !loaded.some((item) => item.session_id === sessionId.value)) {
      startNewSession();
    }
    return true;
  } catch (error) {
    historyError.value = error instanceof Error ? error.message : "会话记录加载失败";
    return false;
  }
}

async function loadConversation(targetSessionId: string) {
  if (
    !targetSessionId
    || sending.value
    || historyLoading.value
    || deletingSessionIds.value.has(targetSessionId)
  ) return;
  historyLoading.value = true;
  historyError.value = "";
  try {
    const detail = await getConversation(targetSessionId);
    entries.value = detail.interactions.map((interaction) => ({
      entryId: interaction.request_id,
      question: interaction.question,
      createdAt: interaction.created_at,
      response: interaction.response,
      feedback: interaction.feedback ?? undefined,
    }));
    sessionId.value = detail.session_id;
    localStorage.setItem(SESSION_STORAGE_KEY, detail.session_id);
    sidebarOpen.value = false;
    await scrollToBottom();
  } catch (error) {
    historyError.value = error instanceof Error ? error.message : "会话记录加载失败";
  } finally {
    historyLoading.value = false;
  }
}

async function removeConversation(item: ConversationSummary) {
  if (
    sending.value
    || historyLoading.value
    || deletingSessionIds.value.has(item.session_id)
  ) return;

  const confirmed = window.confirm(`确定删除会话“${item.title}”吗？删除后无法恢复。`);
  if (!confirmed) return;

  deletingSessionIds.value = new Set([...deletingSessionIds.value, item.session_id]);
  historyError.value = "";
  try {
    await deleteConversation(item.session_id);
    conversations.value = conversations.value.filter(
      (conversation) => conversation.session_id !== item.session_id,
    );
    if (sessionId.value === item.session_id) {
      startNewSession();
    }
  } catch (error) {
    historyError.value = error instanceof Error ? error.message : "会话删除失败";
  } finally {
    const nextDeletingSessionIds = new Set(deletingSessionIds.value);
    nextDeletingSessionIds.delete(item.session_id);
    deletingSessionIds.value = nextDeletingSessionIds;
  }
}

async function submit(message = input.value) {
  const value = message.trim();
  if (!value || sending.value) return;
  input.value = "";
  networkError.value = "";
  sidebarOpen.value = false;
  const entry: ChatEntry = {
    entryId: crypto.randomUUID(),
    question: value,
    createdAt: new Date().toISOString(),
    pending: true,
  };
  entries.value.push(entry);
  sending.value = true;
  await scrollToBottom();

  try {
    const response = await sendChat(value, sessionId.value || undefined);
    entry.response = response;
    entry.pending = false;
    sessionId.value = response.session_id;
    localStorage.setItem(SESSION_STORAGE_KEY, response.session_id);
    await refreshConversations();
  } catch (error) {
    entry.pending = false;
    entry.error = error instanceof Error ? error.message : "请求失败，请稍后重试";
  } finally {
    sending.value = false;
    await scrollToBottom();
  }
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    void submit();
  }
}

async function scrollToBottom() {
  await nextTick();
  conversationElement.value?.scrollTo({
    top: conversationElement.value.scrollHeight,
    behavior: "smooth",
  });
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatDate(value: string | null) {
  return value ? new Intl.DateTimeFormat("zh-CN").format(new Date(value)) : "暂无数据";
}

function formatAmount(value: number | null, currency: string | null) {
  if (value === null) return "暂无数据";
  const symbol = currency === "CNY" ? "¥" : currency ?? "";
  return `${symbol} ${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)}`.trim();
}

function orderProgress(order: OrderCard) {
  const ordered = order.line_items.reduce((sum, line) => sum + line.ordered_qty, 0);
  const received = order.line_items.reduce((sum, line) => sum + line.received_qty, 0);
  return ordered > 0 ? Math.round((received / ordered) * 100) : 0;
}

function orderSummary(order: OrderCard) {
  const progress = orderProgress(order);
  return `${order.order_number} 当前${order.business_status ?? "状态待确认"}，收料进度 ${progress}%，${order.inbound_status ?? "入库状态待确认"}。`;
}

function startNewSession() {
  entries.value = [];
  sessionId.value = "";
  localStorage.removeItem(SESSION_STORAGE_KEY);
  input.value = "";
  sidebarOpen.value = false;
}

async function loadTrace(entry: ChatEntry) {
  if (!entry.response || entry.traceLoading) return;
  entry.traceLoading = true;
  entry.traceError = "";
  try {
    [entry.workflowRun, entry.trace] = await Promise.all([
      getWorkflowRun(entry.response.request_id),
      getTrace(entry.response.request_id),
    ]);
  } catch (error) {
    entry.traceError = error instanceof Error ? error.message : "Trace 加载失败";
  } finally {
    entry.traceLoading = false;
  }
}

async function copyAnswer(entry: ChatEntry) {
  const response = entry.response;
  if (!response) return;
  const text = response.analytics_card?.summary
    ?? response.document_answer?.conclusion
    ?? (response.order_card ? orderSummary(response.order_card) : "");
  if (text) await navigator.clipboard.writeText(text);
}

async function persistFeedback(
  entry: ChatEntry,
  rating: FeedbackRating,
  reasonCodes: FeedbackReason[] = [],
  comment: string | null = null,
) {
  if (!entry.response || entry.feedbackLoading) return;
  entry.feedbackLoading = true;
  entry.feedbackError = "";
  entry.feedbackNotice = "";
  try {
    entry.feedback = await submitFeedback(entry.response.request_id, {
      rating,
      reason_codes: reasonCodes,
      comment,
    });
    entry.feedbackPanelOpen = false;
    entry.feedbackNotice = "反馈已记录";
  } catch (error) {
    entry.feedbackError = error instanceof Error ? error.message : "反馈保存失败";
  } finally {
    entry.feedbackLoading = false;
  }
}

async function openNegativeFeedback(entry: ChatEntry) {
  entry.feedbackError = "";
  entry.feedbackNotice = "";
  entry.feedbackDraftReasons = entry.feedback?.rating === "not_helpful"
    ? [...entry.feedback.reason_codes]
    : [];
  entry.feedbackDraftComment = entry.feedback?.rating === "not_helpful"
    ? entry.feedback.comment ?? ""
    : "";
  entry.feedbackPanelOpen = true;
  await scrollToBottom();
}

function toggleFeedbackReason(entry: ChatEntry, reason: FeedbackReason) {
  entry.feedbackDraftReasons ??= [];
  entry.feedbackDraftReasons = entry.feedbackDraftReasons.includes(reason)
    ? entry.feedbackDraftReasons.filter((item) => item !== reason)
    : [...entry.feedbackDraftReasons, reason];
}

async function submitNegativeFeedback(entry: ChatEntry) {
  await persistFeedback(
    entry,
    "not_helpful",
    entry.feedbackDraftReasons ?? [],
    entry.feedbackDraftComment?.trim() || null,
  );
}
</script>

<template>
  <main v-if="!authReady" class="authentication-shell" aria-live="polite">
    <div class="authentication-mark"><LoaderCircle :size="24" class="spin" /></div>
    <h1>正在验证企业身份</h1>
    <p>正在恢复安全会话并加载授权范围。</p>
  </main>

  <main v-else-if="loginRequired" class="authentication-shell">
    <div class="authentication-mark"><ShieldCheck :size="26" /></div>
    <h1>ERP 智能问答助手</h1>
    <p>{{ authenticationError || "使用企业账号登录后继续。" }}</p>
    <button v-if="authenticationMode === 'oidc'" class="authentication-button" type="button" @click="login">
      <LogIn :size="17" />企业账号登录
    </button>
    <div v-else class="authentication-configuration-error">
      生产环境必须配置 OIDC；开发环境请通过 Vite dev server 启动。
    </div>
  </main>

  <div v-else class="app-shell">
    <button class="mobile-menu" type="button" title="打开导航" @click="sidebarOpen = true">
      <Menu :size="20" />
    </button>
    <div v-if="sidebarOpen" class="sidebar-scrim" @click="sidebarOpen = false"></div>

    <aside :class="['sidebar', sidebarOpen && 'sidebar--open']">
      <div class="brand-block">
        <div class="brand-mark"><BarChart3 :size="18" /></div>
        <strong>ERP智能问答助手</strong>
        <button class="sidebar-close" type="button" title="关闭导航" @click="sidebarOpen = false"><X :size="18" /></button>
      </div>

      <button class="new-session-button" type="button" @click="startNewSession">
        <MessageSquarePlus :size="16" />发起新对话
      </button>

      <label class="search-box">
        <Search :size="15" />
        <input v-model="searchQuery" type="search" placeholder="搜索对话" aria-label="搜索对话" />
      </label>

      <nav class="recent-list" aria-label="最近对话">
        <span class="section-label">
          最近对话
          <LoaderCircle v-if="historyLoading" :size="13" class="spin" />
        </span>
        <div
          v-for="item in recentConversations"
          :key="item.session_id"
          :class="['conversation-item', { active: item.session_id === sessionId }]"
        >
          <button
            class="conversation-open"
            type="button"
            :aria-current="item.session_id === sessionId ? 'page' : undefined"
            :title="item.title"
            :disabled="historyLoading || sending || deletingSessionIds.has(item.session_id)"
            @click="loadConversation(item.session_id)"
          >
            <MessageSquare :size="14" />
            <span class="conversation-copy">
              <strong>{{ item.title }}</strong>
              <small>{{ item.interaction_count }} 轮 · {{ formatTime(item.updated_at) }}</small>
            </span>
          </button>
          <button
            class="conversation-delete"
            type="button"
            :title="`删除会话：${item.title}`"
            :aria-label="`删除会话：${item.title}`"
            :disabled="historyLoading || sending || deletingSessionIds.has(item.session_id)"
            @click.stop="removeConversation(item)"
          >
            <LoaderCircle v-if="deletingSessionIds.has(item.session_id)" :size="14" class="spin" />
            <Trash2 v-else :size="14" aria-hidden="true" />
          </button>
        </div>
        <p v-if="historyError" class="history-error">{{ historyError }}</p>
        <p v-else-if="!historyLoading && recentConversations.length === 0">
          {{ conversations.length ? "没有匹配的对话" : "还没有历史对话" }}
        </p>
      </nav>

      <label v-if="debugMode && authenticationMode === 'development'" class="development-identity">
        <span>测试身份</span>
        <select v-model="debugIdentityId" @change="switchDevelopmentIdentity">
          <option value="configured">本地配置身份</option>
          <option v-for="identity in developmentIdentities" :key="identity.id" :value="identity.id">
            {{ identity.label }}
          </option>
        </select>
        <small>仅开发模式可用</small>
      </label>

      <div class="sidebar-profile">
        <div class="avatar">{{ profileInitials }}</div>
        <div>
          <strong>{{ profileName }}</strong>
          <span>{{ profileRole }}</span>
          <small v-if="identityError" class="profile-error">{{ identityError }}</small>
        </div>
        <ShieldCheck :size="17" />
      </div>
    </aside>

    <section class="workspace">
      <header class="workspace-header">
        <div class="header-title">{{ conversationTitle }}</div>
        <div class="header-status">
          <span :class="['status-dot', health?.status === 'ok' && 'active']"></span>{{ serviceLabel }}
        </div>
        <div class="profile-menu-wrap">
          <button class="header-profile" type="button" :aria-expanded="profileMenuOpen" @click="profileMenuOpen = !profileMenuOpen">
            <span class="avatar avatar--small">{{ profileInitials }}</span>
            <span><strong>{{ profileName }}</strong><small>{{ profileRole }}</small></span>
            <ChevronDown :size="14" />
          </button>
          <div v-if="profileMenuOpen" class="profile-popover">
            <strong>{{ profileName }}</strong>
            <span>{{ profileRole }}</span>
            <small v-if="authContext?.email">{{ authContext.email }}</small>
            <small>{{ authContext?.trusted ? "企业身份已验证" : "本地演示身份" }}</small>
            <button v-if="authenticationMode === 'oidc'" type="button" @click="logout"><LogOut :size="15" />退出登录</button>
          </div>
        </div>
      </header>

      <main ref="conversationElement" :class="['conversation', entries.length === 0 && 'conversation--empty']" aria-live="polite">
        <section v-if="entries.length === 0" class="welcome-state">
          <div class="welcome-mark"><Sparkles :size="22" /></div>
          <h1>{{ profileName }}，您好</h1>
          <p>今天想了解哪项采购业务？</p>
        </section>

        <article v-for="entry in entries" :key="entry.entryId" class="exchange">
          <div class="user-row">
            <div class="user-message">{{ entry.question }}</div>
            <div class="avatar avatar--message">{{ profileInitials }}</div>
          </div>

          <div v-if="entry.pending" class="assistant-loading">
            <div class="assistant-mark"><Bot :size="16" /></div>
            <LoaderCircle :size="17" class="spin" />正在查询并校验业务数据
          </div>

          <div v-else-if="entry.error" class="error-message request-error">
            <AlertTriangle :size="18" />
            <strong>{{ entry.error }}</strong>
            <button type="button" @click="submit(entry.question)"><RefreshCw :size="14" />重试</button>
          </div>

          <div v-else-if="entry.response" class="assistant-response">
            <AnalyticsPanel v-if="entry.response.analytics_card" :card="entry.response.analytics_card" />

            <template v-if="entry.response.order_card">
              <div class="response-summary">
                <div class="assistant-mark"><Bot :size="16" /></div>
                <p>{{ orderSummary(entry.response.order_card) }}</p>
              </div>
              <section class="order-card">
                <header><span><PackageSearch :size="17" />订单信息</span></header>
                <div class="order-facts">
                  <div><span>订单号</span><strong>{{ entry.response.order_card.order_number }}</strong></div>
                  <div><span>当前状态</span><strong>{{ entry.response.order_card.receipt_status }} · {{ entry.response.order_card.inbound_status }}</strong></div>
                  <div><span>供应商</span><strong>{{ entry.response.order_card.supplier_name ?? "暂无数据" }}</strong></div>
                  <div><span>已收 / 应收</span><strong>{{ orderProgress(entry.response.order_card) }}%</strong></div>
                  <div><span>订单金额</span><strong>{{ formatAmount(entry.response.order_card.total_amount, entry.response.order_card.currency) }}</strong></div>
                  <div><span>计划日期</span><strong>{{ formatDate(entry.response.order_card.line_items[0]?.planned_receive_date ?? null) }}</strong></div>
                </div>
                <div v-if="entry.response.order_card.status_reason" class="status-reason">
                  <CircleHelp :size="15" /><p>{{ entry.response.order_card.status_reason }}</p>
                </div>
                <details v-if="entry.response.order_card.line_items.length" class="line-items">
                  <summary>查看 {{ entry.response.order_card.line_items.length }} 条物料明细</summary>
                  <div class="line-table-wrap">
                    <table>
                      <thead><tr><th>行</th><th>物料</th><th>订购</th><th>已收</th><th>已入库</th><th>含税单价</th><th>计划收货</th></tr></thead>
                      <tbody><tr v-for="line in entry.response.order_card.line_items" :key="line.line_no">
                        <td>{{ line.line_no }}</td><td><strong>{{ line.material_name }}</strong><small>{{ line.material_code }}</small></td>
                        <td>{{ line.ordered_qty }} {{ line.unit }}</td><td>{{ line.received_qty }} {{ line.unit }}</td><td>{{ line.inbound_qty }} {{ line.unit }}</td>
                        <td>{{ formatAmount(line.tax_inclusive_unit_price, entry.response.order_card.currency) }}</td><td>{{ formatDate(line.planned_receive_date) }}</td>
                      </tr></tbody>
                    </table>
                  </div>
                </details>
              </section>
            </template>

            <section v-if="entry.response.document_answer" class="answer-content">
              <div v-if="!entry.response.order_card" class="response-summary">
                <div class="assistant-mark"><Bot :size="16" /></div>
                <p v-if="entry.response.understanding.intent === 'general'" class="general-answer">
                  {{ formatGeneralAnswer(entry.response.document_answer.conclusion) }}
                </p>
                <h3 v-else>{{ entry.response.document_answer.conclusion }}</h3>
              </div>
              <div
                v-for="section in entry.response.document_answer.sections ?? []"
                :key="section.title"
                class="answer-section answer-topic"
              >
                <h4><FileSearch :size="16" />{{ section.title }}</h4>
                <p v-if="section.summary" class="answer-topic-summary">{{ section.summary }}</p>
                <ul v-if="section.items.length">
                  <li v-for="item in section.items" :key="item">{{ item }}</li>
                </ul>
              </div>
              <div v-if="!entry.response.document_answer.sections?.length && entry.response.document_answer.confirmed_facts.length" class="answer-section">
                <h4><Check :size="16" />已确认事实</h4><p v-for="fact in entry.response.document_answer.confirmed_facts" :key="fact">{{ fact }}</p>
              </div>
              <div v-if="!entry.response.document_answer.sections?.length && entry.response.document_answer.details.length" class="answer-section">
                <h4><FileSearch :size="16" />说明</h4><p v-for="detail in entry.response.document_answer.details" :key="detail">{{ detail }}</p>
              </div>
              <div v-if="entry.response.document_answer.steps.length" class="answer-section action-section">
                <h4><GitBranch :size="16" />建议动作</h4><ol><li v-for="step in entry.response.document_answer.steps" :key="step">{{ step }}</li></ol>
              </div>
              <div v-if="entry.response.document_answer.unknowns.length || entry.response.document_answer.cautions.length" class="answer-section warning-section">
                <h4><AlertTriangle :size="16" />待确认</h4>
                <p v-for="item in [...entry.response.document_answer.unknowns, ...entry.response.document_answer.cautions]" :key="item">{{ item }}</p>
              </div>
            </section>

            <ResponseRenderer
              v-if="entry.response.presentation?.length"
              :blocks="entry.response.presentation"
            />

            <div v-if="entry.response.error" class="error-message">
              <AlertTriangle :size="17" /><strong>{{ entry.response.error.message }}</strong>
            </div>

            <section v-if="entry.response.sources.length" class="sources-section">
              <h4><Database :size="16" />参考来源</h4>
              <ol>
                <li v-for="source in entry.response.sources" :key="source.source_id">
                  <details class="source-item">
                    <summary class="source-row">
                      <FileSearch :size="16" />
                      <strong>{{ source.title }}</strong>
                      <ChevronDown :size="16" class="source-chevron" />
                    </summary>
                    <div class="source-detail">
                      <p v-if="source.excerpt.trim()" class="source-excerpt">{{ source.excerpt }}</p>
                      <p v-else class="source-excerpt source-excerpt--empty">暂无可展示的相关片段</p>
                      <a
                        v-if="safeExternalUrl(source.url)"
                        :href="safeExternalUrl(source.url) ?? undefined"
                        target="_blank"
                        rel="noreferrer"
                        title="打开原始文档"
                      ><ExternalLink :size="14" />打开原始文档</a>
                    </div>
                  </details>
                </li>
              </ol>
            </section>

            <details v-if="debugMode && entry.response.workflow" class="workflow-trace">
              <summary><Activity :size="14" />执行与诊断 <span>{{ intentLabels[entry.response.understanding.intent] }}</span></summary>
              <p>{{ entry.response.workflow.plan_summary }}</p>
              <ol><li v-for="(step, index) in entry.response.workflow.steps" :key="`${step.stage}-${index}`"><b>{{ step.stage }}</b>{{ step.detail }}</li></ol>
              <button type="button" @click="loadTrace(entry)">
                <LoaderCircle v-if="entry.traceLoading" :size="14" class="spin" /><Activity v-else :size="14" />加载实际运行记录
              </button>
              <p v-if="entry.traceError" class="trace-error">{{ entry.traceError }}</p>
              <div v-if="entry.workflowRun" class="workflow-runtime">
                <header>
                  <div><span>Workflow</span><strong>{{ entry.workflowRun.workflow_id }}</strong></div>
                  <div><span>版本</span><strong>{{ entry.workflowRun.workflow_version }}</strong></div>
                  <div><span>状态</span><strong>{{ entry.workflowRun.status }}</strong></div>
                </header>
                <section>
                  <h5>节点执行</h5>
                  <div v-for="node in entry.workflowRun.nodes" :key="node.execution_id" class="runtime-row">
                    <code>{{ node.graph_id }} / {{ node.node_id }} <small>第 {{ node.attempt }} 次<span v-if="node.parent_node_id"> · 父节点 {{ node.parent_node_id }}</span></small></code><span>{{ node.kind }}</span><em>{{ node.error_code ? `${node.status} · ${node.error_code}` : node.status }}</em><b>{{ node.duration_ms?.toFixed(1) ?? "-" }} ms</b>
                  </div>
                </section>
                <section v-if="entry.workflowRun.tool_calls.length">
                  <h5>标准工具与连接器</h5>
                  <div v-for="call in entry.workflowRun.tool_calls" :key="call.call_id" class="runtime-item">
                    <strong>{{ call.tool_id }}@{{ call.tool_version }}</strong>
                    <span>{{ call.connector_id ?? "internal" }} · {{ call.status }} · {{ call.duration_ms?.toFixed(1) ?? "-" }} ms</span>
                  </div>
                </section>
                <section v-if="entry.workflowRun.policy_decisions.length">
                  <h5>权限决策</h5>
                  <div v-for="(decision, index) in entry.workflowRun.policy_decisions" :key="`${decision.node_id}-${index}`" class="runtime-item">
                    <strong>{{ decision.action }} · {{ decision.allowed ? "允许" : "拒绝" }}</strong>
                    <span>{{ decision.reason }} · {{ decision.policy_id }}@{{ decision.policy_version }}</span>
                  </div>
                </section>
              </div>
              <div v-if="entry.trace" class="runtime-spans">
                <div v-for="span in entry.trace.spans" :key="span.span_id"><strong>{{ span.name }}</strong><span>{{ span.kind }}</span><em>{{ span.error_code ? `${span.status} · ${span.error_code}` : span.status }}</em><code>{{ span.duration_ms.toFixed(1) }} ms</code></div>
              </div>
            </details>

            <footer class="response-actions">
              <button type="button" title="复制回答" @click="copyAnswer(entry)"><Clipboard :size="15" /></button>
              <button
                type="button"
                title="回答有帮助"
                :aria-pressed="entry.feedback?.rating === 'helpful'"
                :class="{ 'feedback-helpful--active': entry.feedback?.rating === 'helpful' }"
                :disabled="entry.feedbackLoading"
                @click="persistFeedback(entry, 'helpful')"
              ><ThumbsUp :size="15" /></button>
              <button
                type="button"
                title="回答需改进"
                :aria-pressed="entry.feedback?.rating === 'not_helpful'"
                :class="{ 'feedback-negative--active': entry.feedback?.rating === 'not_helpful' }"
                :disabled="entry.feedbackLoading"
                @click="openNegativeFeedback(entry)"
              ><ThumbsDown :size="15" /></button>
              <button type="button" title="重新回答" @click="submit(entry.question)"><RefreshCw :size="15" /></button>
              <small v-if="entry.feedbackNotice" class="feedback-notice"><Check :size="13" />{{ entry.feedbackNotice }}</small>
              <small v-else-if="entry.feedbackError" class="feedback-error">{{ entry.feedbackError }}</small>
              <span><Clock3 :size="13" />{{ formatTime(entry.createdAt ?? entry.response.analytics_card?.queried_at ?? entry.response.order_card?.queried_at ?? new Date().toISOString()) }}</span>
            </footer>
            <section v-if="entry.feedbackPanelOpen" class="feedback-panel" aria-label="回答改进反馈">
              <header>
                <strong>回答需要改进的原因</strong>
                <button type="button" title="关闭反馈" @click="entry.feedbackPanelOpen = false"><X :size="15" /></button>
              </header>
              <div class="feedback-reasons">
                <label v-for="reason in feedbackReasons" :key="reason.code">
                  <input
                    type="checkbox"
                    :checked="entry.feedbackDraftReasons?.includes(reason.code)"
                    @change="toggleFeedbackReason(entry, reason.code)"
                  />
                  <span>{{ reason.label }}</span>
                </label>
              </div>
              <textarea
                v-model="entry.feedbackDraftComment"
                rows="2"
                maxlength="1000"
                placeholder="补充说明（可选）"
                aria-label="反馈补充说明"
              ></textarea>
              <footer>
                <button type="button" @click="entry.feedbackPanelOpen = false">取消</button>
                <button type="button" class="feedback-submit" :disabled="entry.feedbackLoading" @click="submitNegativeFeedback(entry)">
                  <LoaderCircle v-if="entry.feedbackLoading" :size="14" class="spin" />提交反馈
                </button>
              </footer>
            </section>
          </div>
        </article>
      </main>
      <div v-if="networkError" class="network-error"><AlertTriangle :size="15" />{{ networkError }}</div>
      <footer class="composer-zone">
        <div class="composer-suggestions" aria-label="常用问题">
          <button
            v-for="action in suggestionActions"
            :key="action.label"
            type="button"
            :title="action.question"
            :disabled="sending"
            @click="submit(action.question)"
          >
            {{ action.question }}
          </button>
        </div>
        <form class="composer" @submit.prevent="submit()">
          <textarea v-model="input" rows="2" maxlength="2000" placeholder="向问答小助手提问..." aria-label="向问答小助手提问" @keydown="handleKeydown"></textarea>
          <button class="attach-button" type="button" title="添加附件"><Paperclip :size="18" /></button>
          <button class="send-button" type="submit" :disabled="sending || !input.trim()" title="发送">
            <LoaderCircle v-if="sending" :size="18" class="spin" /><Send v-else :size="18" />
          </button>
        </form>
        <p>AI 生成内容仅供参考，业务事实以 ERP 系统为准。</p>
      </footer>
    </section>
  </div>
</template>
