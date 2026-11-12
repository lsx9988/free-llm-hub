# 🤖 Free LLM Hub

> 把多家免费大模型 API 叠成**一个 Key、一个 OpenAI 兼容入口**，每天自动探测筛选、失败自动切换。
> 纯自用，不转售、不共享。

![update](https://img.shields.io/badge/probe-daily%2009:17%20%2F%2021:17-blue)
![license](https://img.shields.io/badge/use-personal%20only-lightgrey)

---

## ✨ 它是什么

| 组件 | 作用 |
| --- | --- |
| `scripts/probe.py` | 每天把各家免费模型各打一次，按状态/延迟打分，只把活的写进 `output/models.json` |
| `scripts/gateway.py` | 对外提供 `/v1/models`、`/v1/chat/completions`（支持流式），一个 Key、自动选路、自动 fallback |
| `.github/workflows/daily.yml` | 每天两次定时跑探测，把结果和 README 状态块自动提交 |

**它不是额度合并池**：每次请求只走一家上游，总额度是各家免费额度之和，单家限流时自动切下一家。

---

## 🚀 三步跑起来

### 1. 建仓并推送

```bash
git init
git add .
git commit -m "init free-llm-hub"
git branch -M main
git remote add origin https://github.com/<你的用户名>/free-llm-hub.git
git push -u origin main
```

### 2. 配 Secrets（仓库 Settings → Secrets and variables → Actions → New repository secret）

| Secret 名 | 从哪拿 | 说明 |
| --- | --- | --- |
| `AGNES_API_KEY` | agnes-ai.com 控制台 | 免费文本/图片/视频 |
| `SILICONFLOW_API_KEY` | cloud.siliconflow.cn | 国内直连，部分模型免费 |
| `MODELSCOPE_API_KEY` | modelscope.cn → 访问令牌 | 每天 2000 次 |
| `ZHIPU_API_KEY` | open.bigmodel.cn | GLM-4-Flash 永久免费 |
| `OPENROUTER_API_KEY` | openrouter.ai/keys | `:free` 模型 |
| `GROQ_API_KEY` | console.groq.com | 速度快 |
| `GITHUB_MODELS_TOKEN` | GitHub PAT（勾 `models:read`） | GPT-4.1-mini 等 |
| `GEMINI_API_KEY` | aistudio.google.com | 每天 1440 次 |
| `CF_API_TOKEN` / `CF_ACCOUNT_ID` | Cloudflare Dashboard | Workers AI |
| `CEREBRAS_API_KEY` | cloud.cerebras.ai | 高速推理 |
| `GATEWAY_KEY` | 自己随便定，如 `sk-hub-你的随机串` | **网关对外唯一的 Key**，只在本地/VPS 用，不要传到仓库 |

> 只配你有的，没配的厂商会自动标记 ⚪ 跳过，不影响其它厂商。

### 3. 手动触发一次

Actions → `Daily Probe` → `Run workflow`。跑完 README 状态块会自动刷新。

---

## 🖥️ 本机启动网关

```powershell
pip install -r requirements.txt
$env:GATEWAY_KEY = "sk-hub-你的随机串"
python scripts\gateway.py --host 127.0.0.1 --port 8080
```

验证：

```bash
curl http://127.0.0.1:8080/v1/models -H "Authorization: Bearer sk-hub-你的随机串"
curl http://127.0.0.1:8080/v1/chat/completions ^
  -H "Authorization: Bearer sk-hub-你的随机串" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

Python：

```python
from openai import OpenAI
client = OpenAI(api_key="sk-hub-你的随机串", base_url="http://127.0.0.1:8080/v1")
print(client.chat.completions.create(
    model="auto",                       # 或 "agnes/agnes-3.0-flash"
    messages=[{"role": "user", "content": "你好"}],
).choices[0].message.content)
```

任意客户端（Cherry Studio / NextChat / Cline / Cursor）：
`Base URL = http://127.0.0.1:8080/v1`，`API Key = sk-hub-...`，模型填 `auto` 或 `provider/model`。

---

## ☁️ 部署到 VPS / Docker

```bash
docker compose up -d --build
```

`docker-compose.yml` 默认把 `output/` 挂进容器，网关直接读本地探测结果；也可以把 `config/providers.yaml` 里的 `state_url` 指向
`https://cdn.jsdelivr.net/gh/<用户名>/free-llm-hub@main/output/models.json`，让线上网关每 10 分钟自动同步最新可用池。

---

## 🧭 模型名规则

| 写法 | 含义 |
| --- | --- |
| `auto` | 当前分数最高、延迟最低的可用模型 |
| `agnes/agnes-3.0-flash` | 指定厂商 + 模型 |
| `agnes-3.0-flash` | 只写模型名，网关在所有厂商里匹配 |
| 其它 | 池里没有时，自动 fallback 到最优模型（响应头 `X-Gateway-Provider` 告诉你实际用了谁） |

---

## 📁 文件说明

| 文件 | 说明 |
| --- | --- |
| `config/providers.yaml` | 厂商清单、探测参数、网关参数（**唯一需要改的配置**） |
| `output/models.json` | 可用模型池（网关读取） |
| `output/status.json` | 本次探测明细 + 失败原因 |
| `output/history.json` | 最近 30 次可用数趋势 |
| `scripts/common.py` | 配置加载、鉴权头、状态分类、打分 |
| `scripts/probe.py` | 每日探测筛选 |
| `scripts/gateway.py` | 统一网关 |
| `tests/` | 12 个用例，MockTransport 模拟上游，不联网不花钱 |

本地自检：

```bash
pip install -r requirements-dev.txt
pytest -q          # 12 passed
python scripts/probe.py --dry-run
```

---

## ⚠️ 使用边界

- 只聚合各厂商**明确免费**的模型档位，付费模型没有 Key 调不到。
- 免费额度受各家 RPM/RPD 限制，网关只能切换上游，**不能突破**任何一家的限额。
- 各家条款禁止转售/共享 Key，本仓库仅限个人自用。
- 高并发、生产环境请改用付费 API，免费池没有 SLA。

---

<!-- STATUS_START -->
> 🔄 最近刷新：2026-11-12 16:05:18（北京时间） · 在线 **0/10** 家 · 可用模型 **0** 个（探测 0 个）

> ⚠️ 当前没有可用模型：请在仓库 Settings → Secrets 中配置厂商 API Key，然后到 Actions 页手动跑一次 `Daily Probe`。

| 厂商 | 状态 | 可用/探测 | 最快延迟 | 备注 |
| --- | :---: | :---: | :---: | --- |
| Agnes AI | ⚪ | 0/0 | — | 未配置 Key |
| 硅基流动 | ⚪ | 0/0 | — | 未配置 Key |
| 魔搭 ModelScope | ⚪ | 0/0 | — | 未配置 Key |
| 智谱 GLM | ⚪ | 0/0 | — | 未配置 Key |
| OpenRouter | ⚪ | 0/0 | — | 未配置 Key |
| Groq | ⚪ | 0/0 | — | 未配置 Key |
| GitHub Models | ⚪ | 0/0 | — | 未配置 Key |
| Google AI Studio | ⚪ | 0/0 | — | 未配置 Key |
| Cloudflare Workers AI | ⚪ | 0/0 | — | 未配置 Key |
| Cerebras | ⚪ | 0/0 | — | 未配置 Key |
<!-- STATUS_END -->

---

*个人自用项目，与各厂商无关联。*
