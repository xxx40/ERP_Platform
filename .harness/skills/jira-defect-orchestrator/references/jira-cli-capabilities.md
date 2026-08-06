# Jira CLI Capabilities

## 固定上下文

本 skill 从项目根目录 `.jira.yml` 读取以下配置：

- `server` — Jira 服务器地址
- `project` — 默认项目 KEY
- `component` — 默认组件过滤
- `auth_type` — 认证方式
- `transitions` — 状态流转映射

## 认证与配置

优先依赖：

- `JIRA_CONFIG_FILE`
- `JIRA_API_TOKEN`
- `JIRA_AUTH_TYPE`

安装与配置说明统一见 `README.md`。

关键点：

- 运行本 skill 时，先做 `command -v jira` 前置检查；若 `jira-cli` 不存在，应立即中断并引导用户执行 `scripts/setup-jira-cli.sh`
- 使用 `jira init` 生成默认配置，或用 `-c/--config` 维护专用配置文件
- token 模式下不需要单独的 "jira login" 命令
- 使用 `JIRA_CONFIG_FILE` 或 `jira -c <file>` 切换专用配置
- `component` 不是 jira-cli 的配置项，必须在查询时显式添加

## 推荐命令

### 列出 component 下的缺陷

```bash
jira issue list -p <project> -C <component> --order-by updated --reverse
```

### 查看单个 issue

```bash
jira issue view <ISSUE-KEY>
```

### 追加评论

```bash
python ./.harness/skills/jira-defect-orchestrator/scripts/render_jira_comment.py \
  --stage investigation \
  --fact "已确认复现路径稳定" \
  --hypothesis "问题由状态同步竞态触发" \
  --unknown "尚未拿到线上完整请求体" \
  --next-action "补充防抖与幂等保护" \
  --output /tmp/jira-comment.md

jira issue comment add <ISSUE-KEY> --template /tmp/jira-comment.md
```

### Gate Pass 最小同步

```bash
python ./.harness/skills/jira-defect-orchestrator/scripts/render_jira_comment.py \
  --stage gate-pass-sync \
  --compact \
  --compact-title 根因分析 \
  --fact "已确认当前首选根因成立" \
  --fact "issue 将进入 fix plan，并在需要时流转到处理中" \
  --output /tmp/jira-g1-comment.md

jira issue comment add <ISSUE-KEY> --template /tmp/jira-g1-comment.md
jira issue move <ISSUE-KEY> "处理中"
```

### 状态流转

```bash
jira issue move <ISSUE-KEY> "In Progress"
jira issue move <ISSUE-KEY> "Done" --comment "验证通过，准备关闭"
```

## 适用边界

本 skill 默认围绕以下操作：

- `issue list`
- `issue view`
- `issue comment add`
- `issue move`
- `issue assign`

Gate 通过后的最小评论默认使用 `--compact`；其中 `G1` 评论后的 `处理中` 流转是固定同步动作。

通常不默认使用：

- `issue delete`
- `issue create`
- 大范围跨 project 查询

## 查询建议

- 单 issue 优先直接 `view`
- 多 issue 场景优先 `list` 后缩小范围
- 除非用户明确要求，不要脱离固定 `project + component` 上下文
