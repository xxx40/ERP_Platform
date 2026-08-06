# jira-defect-orchestrator

面向人类使用者的说明文档。这个 Skill 自身负责缺陷闭环编排；`jira-cli` 仍然使用官方配置机制，但仓库现在提供了一个本地交互式 setup 脚本，用来完成安装、token 写入和默认初始化。

## Quick Setup

```bash
./.harness/skills/jira-defect-orchestrator/scripts/setup-jira-cli.sh
```

如果当前环境里还没有 `jira-cli`，本 Skill 应先停止，而不是继续尝试 `jira version`、`jira me` 或 `jira issue list`。此时直接执行上面的脚本补齐环境。

脚本会按下面顺序执行：

- 通过 Homebrew 安装 `jira-cli`（已安装则跳过）
- 提示访问 Jira 服务器获取 API Token
- 等待输入 token，并把 `JIRA_AUTH_TYPE=bearer`、`JIRA_API_TOKEN=...` 写入 `~/.bashrc`
- 启动 `jira init`，预填 `.jira.yml` 中的 server、project、auth-type
- 运行 `jira me` 和 `jira serverinfo` 作为最小校验

注意：

- `component` 不是 jira-cli 的全局配置项，仍需在命令中显式传 `-C <component>`
- 脚本不会额外实现"登录"逻辑；如果 `jira init` 还需要你补充用户名或邮箱，那是 `jira-cli` 自己的交互流程
- 如果默认配置文件已存在，脚本会先备份再覆盖

## Fixed Context

本 Skill 从项目根目录 `.jira.yml` 读取项目级 Jira 配置：

```yaml
server: https://your-jira-instance.com
project: YOUR_PROJECT_KEY
component: YOUR_COMPONENT
auth_type: bearer
transitions:
  in_progress: "In Progress"
  done: "Done"
```

其中 `server`、`project` 可以写进 `jira-cli` 配置；`component` 不是全局配置项，需要在查询时通过 `-C <component>` 或 JQL 显式过滤。

不同项目只需在根目录放一份 `.jira.yml` 即可复用整个 skill。

## Install jira-cli

优先参考官方安装文档：

- Releases: <https://github.com/ankitpokhrel/jira-cli/releases>
- Installation wiki: <https://github.com/ankitpokhrel/jira-cli/wiki/Installation>

常见安装方式：

### macOS / Homebrew

```bash
brew tap ankitpokhrel/jira-cli
brew install jira-cli
```

### 直接下载官方二进制

从 Releases 页面下载与你的平台匹配的包并放到 `PATH` 中。

### 安装后验证

```bash
jira version
```

## Configure jira-cli

访问 `<server>/secure/ViewProfile.jspa`（server 取自 `.jira.yml`），在这里获取你的 API Token。

如果你不想手动执行下面这些步骤，优先直接运行 `scripts/setup-jira-cli.sh`。

### 配置认证环境变量

如果使用 PAT：

```bash
export JIRA_AUTH_TYPE="bearer"
export JIRA_API_TOKEN="<your-token>"
```

建议把它们放到你的 shell profile 里，而不是提交到仓库。

### 初始化默认 Jira 上下文

token 模式下不需要单独执行什么 "jira login" 命令，直接跑 `jira init` 即可。

```bash
jira init \
  --installation local \
  --server <server> \
  --project <project> \
  --auth-type bearer \
  --force
```

## Verify Configuration

最小验证命令：

```bash
jira me
jira serverinfo
jira issue list -p <project> -C <component> --plain --no-headers --columns key,summary,status
```

## Commands Used By This Skill

### 列出缺陷

```bash
jira issue list -p <project> -C <component> --order-by updated --reverse
```

或：

```bash
jira issue list -p <project> -q 'component = "<component>" ORDER BY updated DESC'
```

### 查看 issue

```bash
jira issue view <ISSUE-KEY>
```

### 追加分析评论

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

### 状态流转

```bash
jira issue move <ISSUE-KEY> "In Progress"
jira issue move <ISSUE-KEY> "Done" --comment "验证通过，准备关闭"
```

## Notes

- `component` 需要在命令里显式带出，不能只依赖配置文件。
- 如果当前 shell 没有 `JIRA_API_TOKEN`，很多命令会直接失败。
- 本 Skill 只约束 Jira 上下文和闭环流程，不替代 `jira-cli` 官方配置文档。
- 若缺陷落在前端用户可感知回归，建议在 `investigation -> fix-plan` 之间先执行一次 E2E 可测性判断。
- 每个门禁在明确通过后，都应先补一条最小 Jira 评论再进入下一阶段。
- `G1 Root Cause` 通过后，除最小评论外，还应把 issue 推进到开发进行中的状态；优先 `处理中`。
