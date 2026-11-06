"""LLM 熔断 → 半开 → 恢复 验证（纯客户端，不启动后端）。

前置：先起带故障注入的服务
    terminal 1: .venv\\Scripts\\python.exe tests\\llm_breaker\\llm_fault_proxy.py
    terminal 2: .venv\\Scripts\\python.exe tests\\llm_breaker\\serve_with_proxy.py

观测手段：
    - 熔断状态读 /metrics 的 codegenx_llm_circuit_state（0=closed 1=half_open 2=open）
    - 故障注入走代理的 /__ctl/break 与 /__ctl/heal，全程不重启服务；
      break/heal 带 ?model=xxx 可只对单个模型生效

预期（threshold=5 / max_attempts=2 / recovery=30s）
    leader 的 model_name 是 N 个模型的 fallback 链，每个模型独立熔断器；
    单次请求对每个模型最多失败 3 次（首发 + 2 次重试），
    因此第 2 个故障请求后 N 个 breaker 全部 OPEN，第 3 个请求应快速失败。

    恢复分两步：
    1）全量恢复：链首 half_open → closed，其余模型因无流量保持 open（fallback 短路）；
    2）全链恢复：逐个把当前链首打回故障，把流量逼到下一个模型，验证链上每个模型
       都能经历 half_open → closed（全部「同时」closed 在该设计下不可达，见 README）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx

_HERE = Path(__file__).resolve().parent
PROXY_BASE = "http://127.0.0.1:8899"
_CONFIG_PATH = _HERE.parents[1] / "config.json"
_STATE_CODE = {"0.0": "closed", "1.0": "half_open", "2.0": "open", "0": "closed", "1": "half_open", "2": "open"}


async def _login(client: httpx.AsyncClient, account: str, password: str) -> str:
    resp = await client.post("/api/user/login", json={"userAccount": account, "userPassword": password})
    resp.raise_for_status()
    token = (resp.json().get("data") or "")
    if not token:
        raise RuntimeError(f"登录失败: {resp.text[:200]}")
    return token


async def _ask(client: httpx.AsyncClient, token: str, app_id: str, question: str) -> tuple[bool, float, str]:
    trace_id = f"cbtest-{uuid.uuid4().hex[:8]}"
    payload = {
        "appId": app_id, "message": question,
        "sessionId": f"cbtestsess-{trace_id}",
        "traceId": trace_id, "requestId": f"cbtestreq-{trace_id}",
        "clientVersion": "circuit-test",
    }
    t0 = time.monotonic()
    text = ""
    try:
        async with client.stream(
            "POST", "/api/ai/chat/gen", json=payload,
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            if resp.status_code != 200:
                return False, time.monotonic() - t0, f"HTTP{resp.status_code}"
            async for chunk in resp.aiter_text():
                if len(text) < 4000:
                    text += chunk
    except Exception as exc:
        return False, time.monotonic() - t0, type(exc).__name__

    dt = time.monotonic() - t0
    # 业务层把 LLM 失败包装成 SSE Error 事件，不再是 HTTP 错误码
    compact = text.replace(" ", "")
    bad = any(k in compact for k in ('"event_type":"Error"', '"state":"failed"', "LLMNoModelAvailable"))
    return (not bad), dt, (text[:120] if bad else "")


async def _circuit_states(client: httpx.AsyncClient) -> dict[str, str]:
    """解析 /metrics 里的熔断器状态；label 形如 dashscope:deepseek-v4-pro-0813。"""
    resp = await client.get("/metrics", timeout=10)
    states: dict[str, str] = {}
    for line in resp.text.splitlines():
        if not line.startswith("codegenx_llm_circuit_state{"):
            continue
        circuit = line.split('circuit="', 1)[-1].split('"', 1)[0]
        value = line.rsplit(" ", 1)[-1].strip()
        states[circuit] = _STATE_CODE.get(value, value)
    return states


def _brief(states: dict[str, str]) -> str:
    if not states:
        return "(未创建)"
    groups: dict[str, int] = {}
    for state in states.values():
        groups[state] = groups.get(state, 0) + 1
    return " / ".join(f"{k}×{v}" for k, v in sorted(groups.items()))


def _by_model(states: dict[str, str]) -> dict[str, str]:
    """熔断键 provider:model → 纯模型名。"""
    return {k.split(":", 1)[-1]: v for k, v in states.items()}


def _leader_chain() -> list[str]:
    """从 backend/config.json 读默认智能体的模型链（按 fallback 顺序）。"""
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    agents = data.get("agents") or []
    agent = next((a for a in agents if a.get("defaults")), agents[0] if agents else {})
    raw = str((agent or {}).get("model_name") or "")
    return [m.strip() for m in raw.split(",") if m.strip()]


async def _ctl(proxy_url: str, action: str, model: str | None = None) -> None:
    params = {"model": model} if model else None
    async with httpx.AsyncClient(base_url=proxy_url, timeout=10, trust_env=False) as c:
        await c.post(f"/__ctl/{action}", params=params)


async def main() -> None:
    parser = argparse.ArgumentParser(description="LLM 熔断/恢复验证")
    parser.add_argument("--base-url", default="http://127.0.0.1:8457")
    parser.add_argument("--proxy-url", default=PROXY_BASE)
    parser.add_argument("--account", default="boluo")
    parser.add_argument("--password", default="boluo123")
    parser.add_argument("--app-id", default="app_0001")
    parser.add_argument("--question", default="只回复两个字：收到")
    parser.add_argument("--recovery-wait", type=float, default=45.0, help="冷却等待，需 > breaker recovery_timeout（默认30s）+ 几秒的 fallback 链延迟余量")
    parser.add_argument("--chain", default="", help="模型链（逗号分隔，按 fallback 顺序）；默认从 backend/config.json 的默认智能体读取")
    parser.add_argument("--skip-full-chain", action="store_true", help="跳过 [4] 全链恢复验证（省约 1 分钟）")
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.base_url, timeout=240, trust_env=False) as client:
        try:
            await client.get("/api/health/", timeout=5)
        except Exception as exc:
            print(f"[ERROR] 无法连接 {args.base_url}：{exc}")
            print("先启动：backend\\.venv\\Scripts\\python.exe backend\\tests\\serve_with_proxy.py")
            return
        token = await _login(client, args.account, args.password)
        rows: list[dict] = []

        async def step(label: str) -> dict:
            ok, dt, note = await _ask(client, token, args.app_id, args.question)
            states = await _circuit_states(client)
            row = {"label": label, "ok": ok, "dt": dt, "state": _brief(states), "note": note}
            rows.append(row)
            print(f"  {label:<20} {'OK  ' if ok else 'FAIL'} {dt:7.2f}s  {row['state']}")
            return {"row": row, "states": states}

        print("[0] 预热：验证代理链路可用")
        warm = await step("预热（正常）")
        if not warm["row"]["ok"]:
            print("[ERROR] 预热失败，先排查代理或服务配置")
            return
        if any(s != "closed" for s in warm["states"].values()):
            print(
                "  [WARN] 起始状态不是全 closed：后端进程残留了上一轮熔断状态，"
                "本轮断言会失真，建议重启 serve_with_proxy.py 后重跑"
            )

        print("[1] 注入上游 503（RETRYABLE → 累计熔断失败）")
        await _ctl(args.proxy_url, "break")
        open_at = None
        for i in range(1, 7):
            res = await step(f"故障请求 #{i}")
            states = res["states"]
            if states and all(s == "open" for s in states.values()):
                open_at = i
                break

        print("[2] 全熔断期：验证快速失败")
        fast = await step("全熔断（快速失败）")

        print(f"[3] 冷却 {args.recovery_wait:.0f}s 后恢复上游，验证 half_open → closed")
        await asyncio.sleep(args.recovery_wait)
        await _ctl(args.proxy_url, "heal")
        for i in range(1, 11):  # 最多 10 次，每个 breaker 至少需要 2 次探测才能从 half_open → closed
            res = await step(f"恢复探测 #{i}")
            states = res["states"]
            if states and all(s == "closed" for s in states.values()):
                break

        # ── [4] 全链恢复：链首恢复后其余模型无流量（fallback 短路），
        #     需逐个把当前链首打回故障，把流量逼到下一个模型，验证其也能 half_open → closed ──
        chain = [m.strip() for m in args.chain.split(",") if m.strip()] or _leader_chain()
        verified: list[str] = []
        if args.skip_full_chain:
            print("[4] 全链恢复：已跳过")
        elif len(chain) < 2:
            print("[4] 全链恢复：模型链不足 2 个，跳过（可用 --chain 指定）")
        else:
            print(f"[4] 全链恢复：链 = {' → '.join(chain)}，逐个验证每个模型都能 half_open → closed")
            first = _by_model(await _circuit_states(client))
            verified = [m for m in chain if first.get(m) == "closed"]
            print(f"    已恢复：{', '.join(verified) or '无'}")
            for idx in range(1, len(chain) + 1):
                states = _by_model(await _circuit_states(client))
                # 待验证目标 = 第一个还没恢复过的模型；全部验证完即结束（不能按固定轮数硬跑，
                # 否则会把已恢复模型一起打回，最后全部 open 变成快速失败）
                pending = [m for m in chain if m not in verified]
                if not pending:
                    print("    ✓ 链上模型已全部验证恢复，结束")
                    break
                target = pending[0]
                # 只让 target 之前的模型让路（open 的天然被跳过，无需干预）
                blocking = [m for m in chain[: chain.index(target)] if states.get(m) != "open"]
                print(
                    f"    → 目标 {target}："
                    + (f"临时故障化 {' + '.join(blocking)}，" if blocking else "前面模型已全部 open，")
                    + "把探测流量让给它"
                )
                for m in blocking:
                    await _ctl(args.proxy_url, "break", m)
                advanced = None
                for j in range(1, 7):  # 被屏蔽模型各需 2 个请求才重新 open，target 需 2 次探测才 closed
                    res = await step(f"推进 {idx}-{j}")
                    cur = _by_model(res["states"])
                    # 记录本轮任何新恢复的模型（target 失败时可能由更后面的模型兜住）
                    new_closed = [m for m in chain if cur.get(m) == "closed" and m not in verified]
                    if new_closed:
                        verified.extend(new_closed)
                        print(f"    ✓ {', '.join(new_closed)} 已 half_open → closed")
                    if target in verified:
                        advanced = target
                        break
                    if cur and all(s == "open" for s in cur.values()):
                        print("    [INFO] 当前全部 open（需等冷却），本轮停止")
                        break
                for m in blocking:
                    await _ctl(args.proxy_url, "heal", m)
                if advanced is None:
                    print(f"    [WARN] {target} 未恢复，停止推进")
                    break
            print(f"[4] 最终验证恢复：{', '.join(verified)}（{len(verified)}/{len(chain)}）")

    _write_report(rows, open_at, fast["row"]["dt"], chain, verified)
    print(f"\n[DONE] 报告已写入 {_HERE / 'circuit_breaker_report.md'}")


def _write_report(
    rows: list[dict],
    open_at: int | None,
    fast_dt: float,
    chain: list[str] | None = None,
    verified: list[str] | None = None,
) -> None:
    normal_dt = rows[0]["dt"] if rows else 0.0
    chain = chain or []
    verified = verified or []
    lines = [
        "# LLM 熔断 / 恢复验证",
        "",
        f"- 正常请求耗时：{normal_dt:.2f}s",
        f"- 熔断触发：第 {open_at} 个故障请求后全部 breaker OPEN" if open_at else "- 熔断触发：未观察到全部 OPEN",
        f"- 全熔断期耗时：{fast_dt:.2f}s（对比正常 {normal_dt:.2f}s，快速失败应低一个量级）",
        f"- 恢复后状态：{rows[-1]['state'] if rows else '-'}",
        f"- 全链已恢复模型：{', '.join(verified) or '未验证'}（{len(verified)}/{len(chain)}）" if chain else "",
        "",
        "| 阶段 | 结果 | 耗时(s) | 熔断器状态 | 备注 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['label']} | {'成功' if r['ok'] else '失败'} | {r['dt']:.2f} "
            f"| {r['state']} | {(r['note'] or '-').replace('|', '/')[:60]} |"
        )
    (_HERE / "circuit_breaker_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
