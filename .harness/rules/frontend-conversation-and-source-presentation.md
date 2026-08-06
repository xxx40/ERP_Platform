---
description: 修改聊天页、会话历史、会话删除、回答元信息或参考来源展示时必须遵守。
globs:
  - "frontend/src/App.vue"
  - "frontend/src/api.ts"
  - "frontend/src/types.ts"
  - "frontend/src/style.css"
  - "frontend/src/ResponseRenderer.vue"
  - "backend/app/api/routes.py"
  - "backend/app/repositories/conversation.py"
  - "backend/app/schemas/chat.py"
  - "backend/tests/test_conversation_repository.py"
---

# 前端会话与来源展示

## 适用范围

适用于 ERP 问答首页左侧历史会话、当前会话消息恢复、会话删除，以及回答底部来源呈现。这里的“会话”以 `session_id` 为聚合单位，一条会话包含多轮 `interactions`，不是一问一条历史记录。

## 强制规则

### 1. 边栏必须一条会话对应一条历史记录

列表数据来自 `ConversationSummary`，稳定键使用 `session_id`，标题、最后问题、轮数和更新时间只是摘要。点击历史项必须通过会话详情接口一次恢复该 `session_id` 下全部 interactions；发送新消息后刷新摘要，但不得把每轮消息插成独立边栏记录。

### 2. 会话访问与删除必须按 owner、tenant、org 隔离

列表、详情、删除都必须经过后端身份解析与 `user_id + tenant_id + org_code` 所有权校验。删除按钮应阻止冒泡，显示确认提示和处理中状态；删除当前会话成功后进入新会话状态。前端隐藏记录不等于删除，必须调用 `DELETE /api/v1/conversations/{session_id}` 并由后端删除依赖内容。

### 3. 普通回答不得展示内部实现元数据

默认回答区域不得显示模型版本、Prompt 版本、Tool ID、工作流 ID、`procurement-metrics-v*`、Mock 聚合标记等内部字段。工作流和追踪信息只能在显式 debug 模式或管理诊断页面展示，不能污染每条回答。

### 4. 来源默认只展示文档标题，片段按需展开

来源列表的第一层只展示标题和来源图标；片段、原文链接等细节放在可展开的 `details`/popover 中。外链只允许 `http`/`https`，并使用新窗口和 `rel="noreferrer"`。不得把全部 excerpt 默认平铺，也不得显示无意义的内部 source ID。

### 5. 交互状态必须避免竞态和误操作

加载历史、发送消息、删除同一会话期间禁用冲突操作；删除按钮必须具备 `title`/`aria-label`；当前项使用 `aria-current`。错误应留在历史区域可见，不能静默丢失或通过刷新页面掩盖。

## 正反例

✅ 正确：按 `session_id` 渲染会话，并将来源片段折叠到标题之下。

```vue
<div v-for="item in conversations" :key="item.session_id">
  <button @click="loadConversation(item.session_id)">
    {{ item.title }} · {{ item.interaction_count }} 轮
  </button>
  <button @click.stop="removeConversation(item)" :aria-label="`删除会话：${item.title}`">
    删除
  </button>
</div>

<details v-for="source in response.sources" :key="source.source_id">
  <summary>{{ source.title }}</summary>
  <p>{{ source.excerpt }}</p>
</details>
```

❌ 错误：把每轮消息作为边栏记录并默认摊开所有来源及内部元数据。

```vue
<li v-for="entry in entries" :key="entry.response.request_id">
  {{ entry.question }}
</li>
<div>{{ response.workflow.plan_summary }} · {{ response.model_version }}</div>
<div v-for="source in response.sources">{{ source.title }} {{ source.excerpt }}</div>
```

## 反模式

- 使用 `request_id` 或消息数组生成历史会话列表。
- 点击历史记录只恢复最后一问，而不是该 `session_id` 的完整 interactions。
- 仅在前端 `filter()` 隐藏记录，不调用后端删除接口。
- 删除接口不校验 owner/tenant/org，或跨会话批量删除未限定范围。
- 每条回答显示工作流、Tool、模型、Mock/聚合版本等开发字段。
- 所有来源片段默认展开，或允许 `javascript:` 等非 HTTP(S) 链接。

## 验证命令

```powershell
cd backend
python -m pytest tests/test_conversation_repository.py

cd ..\frontend
npm run build
```

还需人工快速检查：同一会话连续提问后边栏仍只有一条且轮数增加；点开能恢复全部轮次；删除非当前/当前会话行为正确；来源默认只有标题，展开后才看到片段与安全原文链接；正常模式没有内部工作流元数据。

## 代码依据

- `frontend/src/App.vue`：`conversations`/`entries` 分层、`loadConversation()` 全量恢复、`removeConversation()`、删除状态、`session_id` 列表键、来源 `details` 折叠和安全外链。
- `frontend/src/api.ts`：会话 list/get/delete 接口封装。
- `frontend/src/types.ts`：`ConversationSummary`、`ConversationDetailResponse`、`SourceReference` 和 `ChatResponse` 契约。
- `backend/app/api/routes.py`：会话列表、详情、DELETE 路由及身份范围校验。
- `backend/app/repositories/conversation.py`：会话绑定、所有权断言、按拥有者删除及依赖数据清理。
- `backend/tests/test_conversation_repository.py`：会话范围、聚合列表、级联删除和来源片段持久化回归测试。

## 开源标杆取舍

参考 `garrytan/gstack` 的 design-review：交互审查要覆盖层级、反馈、慢操作状态和实际可用性，而非只看静态样式；参考 GSD verification patterns 的 wired/functional 门禁。本项目不照搬其 UI 技术实现，只保留“关键交互必须可观察并复验”的原则。
