# OA 合同评审 AI 自动审批

对接泛微 e-cology E9 开放接口，轮询指定账号的待办流程，读取表单与附件，交给大模型按规则生成评审意见，并自动回填提交。

全程**不修改泛微任何代码**：只使用开放接口、登录接口和下载 servlet，所有状态与日志落在本地。

> ⚠️ 自动审批是代替人做决定，请先以 `DRY_RUN=true` 跑一段时间，确认意见质量后再开自动提交。

---

## 工作原理

```
①取token → ②拉待办 → ③读详情 → ④提fileid → ⑤登录+下载 → ⑥落盘 → ⑦AI评审 → ⑧提交
```

| # | 步骤 | 接口 | 方法 |
|---|---|---|---|
| ① | 获取 token | `/api/ec/dev/auth/applytoken` | POST |
| ② | 拉待办列表 | `/api/workflow/paService/getDoingWorkflowRequestList` | POST |
| ③ | 读流程详情 | `/api/workflow/paService/getWorkflowRequest` | GET |
| ④ | 提取 fileid | 正则解析 `fieldShowValue` | — |
| ⑤ | 登录拿会话 | `/api/hrm/login/checkLogin` | POST |
| ⑥ | 下载附件 | `/weaver/weaver.file.FileDownload` | GET |
| ⑦ | AI 评审 | DeepSeek 文件输入 | — |
| ⑧ | 提交流程 | `/api/workflow/paService/submitRequest` | POST |

业务接口的请求头需要三个字段：`appid` + `token` + `userid`（密文）。下载走的是 JSESSIONID，是另一套凭据。

---

## 目录结构

```
.
├── main.py            轮询主循环
├── config.py          配置（全部从 .env 读）
├── oa.py              泛微接口客户端：token 刷新、会话管理、下载、提交
├── attach.py          附件提取与下载
├── ai.py              DeepSeek 评审
├── store.py           本地状态表（SQLite）
├── docs/方案.md        完整技术方案
└── tools/
    ├── probe_api.py   接口连通性探测
    └── dump_todo.py   待办原始数据导出
```

---

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置
cp .env.example .env
# 编辑 .env，填入 appid / secret / 登录账号 / userid / spk / AI_API_KEY

# 3. 先探测接口是否通
python tools/probe_api.py

# 4. 干跑一轮（不提交）
python main.py --once

# 5. 确认无误后常驻运行
python main.py
```

---

## 关键配置

| 配置 | 说明 |
|---|---|
| `WORKFLOW_WHITELIST` | 只处理这些 workflowId，**必填**，留空会处理所有流程 |
| `OA_USERID` + `OA_SPK` | userid 必须用 spk 公钥 RSA 加密后传，缺一不可 |
| `DRY_RUN` | `true` 只出意见不提交，建议先跑一段时间 |
| `FORCE_APPROVE` | `true` 时不看 AI 结论一律提交，AI 只写意见 |
| `CONFIDENCE_THRESHOLD` | 低于此值转人工 |
| `MAX_RETURN_COUNT` | 被人工退回达到此次数就停止自动处理，防死循环 |

完整清单见 `.env.example` 的注释。

---

## 实现中踩过的坑

这些是实际调试出来的，和网上多数示例不一致，改代码前建议先看一遍。

**1. `userid` 必须是密文**

不是明文用户 ID，而是用注册许可返回的 `spk` 公钥做 RSA（PKCS1 v1.5）加密再 base64。`spk` 是裸 base64 串，需要自己补 `-----BEGIN PUBLIC KEY-----` 头尾。

**2. `Content-Type` 必须和 body 形态匹配**

| body | Content-Type | 结果 |
|---|---|---|
| JSON | `application/json` | ✅ |
| 表单 | `application/x-www-form-urlencoded` | ✅ |
| JSON | `application/x-www-form-urlencoded` | ❌ 返回空响应 |

官方文档写的是 form-urlencoded，但配 JSON body 会直接返回空，`r.json()` 报 `Expecting value: line 1 column 1`。

**3. 接口请求方法不统一**

待办列表 POST、流程详情 GET、提交 POST、下载 GET。代码里用 `METHOD_MAP` 记录，撞到 405 会自动换另一种并记住。

**4. 附件字段里的两个 ID 不是一回事**

表单字段 `fieldValue` 存的是 `docid`，而下载接口要的是 `fieldShowValue` 里链接中的 `fileid`（imagefileid）。直接拿 `fieldValue` 去下载会失败。

**5. DeepSeek 的文件引用是平铺的**

```python
{"type": "file", "file_id": fid}          # ✅
{"type": "file", "file": {"file_id": fid}}  # ❌ file must have a file_id
```

和 OpenAI 的 schema 不一致。另外 `files.create` 的 `purpose` 只支持 `text_recognition` / `rich_layout_recognition` / `user_data`，网上常见的 `assistants`、`file-extract` 都会被拒。

**6. 会话失效时下载接口返回 200**

不是 4xx，而是 `200 + text/html`（登录页）。只判断状态码会把登录页存成附件，必须校验 `Content-Type` 和 `Content-Disposition`。

**7. 意见换行要用 `<br>`**

泛微按 HTML 渲染意见，`\n` 会被当空白吞掉。

---

## 安全与风险

- `.env` 含密钥和账号密码，已在 `.gitignore` 中排除，**不要提交**
- `data/` 存真实合同附件和状态库，同样已排除
- 建议给接口账号单独关闭验证码，并按最小权限授予流程可见范围
- `FORCE_APPROVE=true` 意味着 AI 从「把关者」变成「代笔人」，意见出错也会照常流转，请确保后续环节有人复核
- 同一流程被人工退回多次时应当停止自动处理——连续被退说明 AI 的判断不可靠，继续自动批只会制造噪音

---

## 常见问题

**待办条数和网页上对不上？**
`getDoingWorkflowRequestList` 返回的是「我经手/我发起的在办流程」，比网页的「待我审批」范围宽。跑 `tools/dump_todo.py` 看每条的 `currentNodeName` 和 `creatorName` 就能分辨。

**流程明明有，但什么都没发生？**
看日志里的 `涉及 workflowId`，大概率是 `WORKFLOW_WHITELIST` 配错了。代码里所有跳过路径都会打日志。

**意见被截断了？**
检查 `MAX_REMARK_CHARS`（默认 0 = 不限制），以及 prompt 里的字数要求。

**重复提交同一个流程？**
状态表按 `(request_id, node_id)` 去重，提交后记录审批日志条数；只有日志条数增加（说明被退回重提）才会重新处理。
