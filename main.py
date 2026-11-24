"""主循环：轮询待办 → 读详情 → 下载附件 → AI 评审 → 提交。

用法：
    python main.py            # 常驻轮询
    python main.py --once     # 只跑一轮，用于调试

设计原则：每一条待办最终去了哪里，日志里必须说清楚。不允许静默跳过。
"""
import sys
import time
import traceback
from datetime import datetime

import ai
import attach
import config as C
import store
from oa import oa


def log(msg: str):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(C.LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def handle(todo: dict, doing_ids: set[str], seen_wids: dict) -> str:
    """处理一条待办，返回结果标签（用于本轮统计）。"""
    rid = str(todo.get("requestId") or "")
    if not rid:
        log("跳过：待办项里没有 requestId")
        return "NO_ID"

    # 待办列表里不一定带 workflowId，先从详情里取权威值再判白名单
    wid_hint = str(todo.get("workflowId") or "")
    detail = oa.get_request(rid, wid_hint)
    d = detail.get("data", detail)

    wid = str((d.get("workflowBaseInfo") or {}).get("workflowId") or wid_hint)
    node_id = str(d.get("currentNodeId") or "")
    node_name = d.get("currentNodeName") or ""
    name = d.get("requestName") or ""

    seen_wids[wid] = seen_wids.get(wid, 0) + 1

    if C.WORKFLOW_WHITELIST and wid not in C.WORKFLOW_WHITELIST:
        log(f"跳过 {rid}「{name}」：workflowId={wid} 不在白名单 {C.WORKFLOW_WHITELIST}")
        return "SKIP_WID"

    if C.NODE_WHITELIST and node_id not in C.NODE_WHITELIST:
        log(f"跳过 {rid}「{name}」：节点 {node_id}({node_name}) 不在白名单 {C.NODE_WHITELIST}")
        return "SKIP_NODE"

    # 用审批日志条数判断"提交之后有没有新动作"，退回重提靠这个识别
    log_count = len(d.get("workflowRequestLogs") or [])

    rec = store.get(rid, node_id)
    if rec and rec.get("status") == "SUBMITTED":
        prev = rec.get("log_count") or 0
        if prev == 0:
            # 老记录没存日志条数，先补记再跳过，避免把已提交的流程重提一遍
            store.save(rid, node_id, "SUBMITTED", workflow_id=wid,
                       request_name=name, log_count=log_count)
            log(f"跳过 {rid}「{name}」：已提交过。记录当前审批日志 {log_count} 条作为基准，"
                f"以后超过 {log_count} 条才说明有新操作")
            return "SKIP_DONE"
        if log_count <= prev:
            log(f"跳过 {rid}「{name}」：该节点已于 {rec.get('update_time')} 提交，"
                f"之后没有新操作")
            return "SKIP_DONE"
        log(f"{rid}「{name}」：提交后又产生 {log_count - prev} 条操作，重新评审")

    # 退回熔断：AI 只批不退，人退多次说明 AI 判断不可信，停止自动处理
    returns = ai.count_returns(detail)
    if C.MAX_RETURN_COUNT > 0 and returns >= C.MAX_RETURN_COUNT:
        store.save(rid, node_id, "MANUAL", workflow_id=wid, request_name=name,
                   err_msg=f"人工退回 {returns} 次，达到上限 {C.MAX_RETURN_COUNT}")
        log(f"跳过 {rid}「{name}」：已被人退回 {returns} 次，达到上限 "
            f"{C.MAX_RETURN_COUNT}，转人工")
        return "CIRCUIT"

    # 附件落盘（多个附件字段会全部下载）
    folder = C.STORAGE_ROOT / f"{rid}_{attach.clean_name(name, 60)}"
    files = attach.download_all(detail, folder)
    total_mb = sum(f.stat().st_size for f in files) / 1024 / 1024 if files else 0
    log(f"附件 {len(files)} 个，合计 {total_mb:.1f}MB -> {folder}")

    # 上传给模型（服务端解析 PDF/Word/PPT/图片）
    file_ids = ai.upload_files(files) if files else []

    # 全部上传失败时，退回本地抽文本，别让整条链路断掉
    fallback = ""
    if files and not file_ids:
        fallback = attach.text_fallback(files)
        log("附件上传失败，改用本地提取文本")

    try:
        result = ai.review(detail, file_ids, fallback)
    finally:
        if C.DELETE_FILE_AFTER_REVIEW and file_ids:
            ai.delete_files(file_ids)

    log(f"AI {rid}「{name}」-> {result['decision']} "
        f"conf={result['confidence']:.2f} remark={result['remark'][:80]}")

    common = dict(workflow_id=wid, request_name=name, decision=result["decision"],
                  confidence=result["confidence"], remark=result["remark"],
                  attach_count=len(files), folder=str(folder), log_count=log_count)

    if result["decision"] != "approve":
        store.save(rid, node_id, "MANUAL", err_msg="AI 建议人工复核", **common)
        log(f"不提交 {rid}：AI 建议人工复核")
        return "MANUAL"

    if not C.FORCE_APPROVE and result["confidence"] < C.CONFIDENCE_THRESHOLD:
        store.save(rid, node_id, "MANUAL", err_msg="置信度不足", **common)
        log(f"不提交 {rid}：置信度 {result['confidence']:.2f} "
            f"< 阈值 {C.CONFIDENCE_THRESHOLD}")
        return "LOW_CONF"

    # 建议模式：只出意见不提交
    if C.DRY_RUN:
        store.save(rid, node_id, "AI_REVIEWED", **common)
        log(f"不提交 {rid}：DRY_RUN=true，只出意见不提交")
        return "DRY_RUN"

    # 提交前二次确认，防止人工已经处理过
    if rid not in doing_ids:
        log(f"不提交 {rid}：已不在待办列表（可能已被人工处理）")
        return "GONE"

    resp = oa.submit(rid, result["remark"])
    log(f"提交返回 {rid}: {str(resp)[:300]}")

    if _submit_ok(resp):
        store.save(rid, node_id, "SUBMITTED", **common)
        log(f"提交成功 {rid}")
        return "SUBMITTED"

    retry = store.bump_retry(rid, node_id, str(resp)[:500])
    status = "MANUAL" if retry >= C.MAX_RETRY else "FAILED"
    store.save(rid, node_id, status, err_msg=f"提交失败: {str(resp)[:500]}", **common)
    log(f"提交失败 {rid} 第 {retry} 次")
    return "SUBMIT_FAIL"


def _submit_ok(resp) -> bool:
    if not isinstance(resp, dict):
        return False
    if str(resp.get("code", "")).upper() == "SUCCESS":
        return True
    if resp.get("status") is True:
        return True
    return False


def run_once():
    todos = oa.get_doing_list()
    doing_ids = {str(t.get("requestId")) for t in todos}
    log(f"待办 {len(todos)} 条 | DRY_RUN={C.DRY_RUN} 白名单={C.WORKFLOW_WHITELIST} "
        f"置信度阈值={C.CONFIDENCE_THRESHOLD} 金额阈值={C.AMOUNT_LIMIT}")

    if not todos:
        log("待办为空，本轮无事可做")
        return

    stats: dict[str, int] = {}
    seen_wids: dict[str, int] = {}

    for todo in todos:
        try:
            tag = handle(todo, doing_ids, seen_wids)
        except Exception as e:
            tag = "ERROR"
            log(f"处理失败 {todo.get('requestId')}: {e}")
            traceback.print_exc()
        stats[tag] = stats.get(tag, 0) + 1

    log(f"本轮结果: {stats}")
    log(f"涉及 workflowId: {seen_wids}")
    if C.WORKFLOW_WHITELIST and not (set(seen_wids) & set(C.WORKFLOW_WHITELIST)):
        log(f"注意：本轮待办里没有任何白名单流程 {C.WORKFLOW_WHITELIST}，"
            f"检查 WORKFLOW_WHITELIST 是否配错")


def main():
    once = "--once" in sys.argv
    log(f"启动 DRY_RUN={C.DRY_RUN} 白名单={C.WORKFLOW_WHITELIST} 模型={C.AI_MODEL}")

    while True:
        try:
            run_once()
        except Exception as e:
            log(f"轮询异常: {e}")
            traceback.print_exc()

        if once:
            break
        time.sleep(C.POLL_INTERVAL)


if __name__ == "__main__":
    main()
