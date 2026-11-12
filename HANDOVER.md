# HANDOVER — free-llm-hub

## 项目定位
多厂商免费 LLM API 聚合网关：GitHub Actions 每天两次探测筛选可用模型 → 写入 `output/models.json`；
本地/VPS 的 `scripts/gateway.py` 读取该文件，对外只暴露一个 `GATEWAY_KEY`，OpenAI 兼容，失败自动 fallback。

## 目录约定
- `config/providers.yaml`：唯一配置入口（厂商、探测参数、网关参数）
- `scripts/probe.py`：探测筛选，产物 `output/models.json` / `status.json` / `history.json`，并刷新 README 状态块
- `scripts/gateway.py`：网关，`/v1/models`、`/v1/chat/completions`（含 stream）、`/healthz`
- `tests/`：12 个用例，全部 MockTransport，不联网、不花钱

## 已验证（2026-11-12）
- `pytest -q` → 12 passed
- 本地假上游（uvicorn :18099）+ probe 真实 HTTP：4 个模型探测 → 2 个 OK，401/429 正确归类
- 网关真实 HTTP：鉴权 401、`/v1/models`、`auto`、裸模型名、未知模型 fallback、`stream=true` 全部通过
- 真实配置无 Key 跑一次：10 家全部标 ⚪ 跳过，退出码 0（不会把 CI 标红）

## 未完成 / 已知边界
- 未推送到 GitHub（本地只做了 commit）；Actions 定时任务需仓库在 GitHub 上才生效。
- 未接图片/视频端点：Agnes 的 `/images/generations`、`/videos` 目前不在网关路由内，属二期。
- 探测是“单次抽样”，偶发超时会被判死；如误杀多，可把 `probe.timeout_s` 调大或 `keep_degraded: true`。
- GitHub Actions 的 schedule 在仓库 60 天无提交后会被自动停用；每日提交本身可保活。

## 排错顺序
1. `python scripts/probe.py --dry-run` 看清单是否正确
2. `python scripts/probe.py --only <provider>` 单家定位
3. `curl :8080/healthz` 看 `state_source` / `state_age_s`
4. 503 时看返回里 `providers_without_key`：多半是环境变量没设
