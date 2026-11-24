"""AI 评审：把流程材料交给 DeepSeek-V4.1-Flash，拿回结构化的审批意见。

附件走 Files API 上传，服务端自动解析 PDF / Word / PPT / 图片（含图文混排），
本地不需要 OCR，也不需要 pypdf / python-docx。

设计原则：AI 是纯函数，只吃材料、只吐 JSON，不碰任何业务接口。
"""
import json
import re
from pathlib import Path

from openai import OpenAI

import config as C
from attach import log

_client = None


def _get_client() -> OpenAI:
    """懒加载，避免没配 API Key 时 import 就报错。"""
    global _client
    if _client is None:
        if not C.AI_API_KEY:
            raise RuntimeError("AI_API_KEY 未配置，请在 .env 里填写")
        _client = OpenAI(base_url=C.AI_BASE_URL, api_key=C.AI_API_KEY)
    return _client

# ============ 审批规则（要调整就改这里） ============

REVIEW_RULES = """你是公司合同评审的审批助手，只负责判断"是否批准"，不负责退回。

【审核要点】逐条核对附件中的合同文本：

1. 形式与要素：合同主体名称、签署方、日期、签章是否完整一致，与申请内容是否吻合
2. 重大错误：金额数字（小写与大写是否一致）、计量单位是否正确，有无明显笔误或前后矛盾
3. 重大风险条款：其他合同当事人的主体资格是否适格、是否存在履约能力的重大瑕疵；有无对我司明显不利、责任严重失衡的条款
4. 条款合理性：产品名称、技术参数、规格、型号、配置、重量等参数是否清晰准确；质量要求、交货期、质保期、包装、检验方式；合同价款及其支付方式、条件、期限；违约责任是否明确、合理
5. 材料完整性：附件是否齐全、内容能否辨认，与申请说明是否一致

【判定规则】
- 上述任一项存在重大错误、重大风险或关键条款缺失 → manual
- 存在轻微瑕疵但可在履约中处理的 → approve，并在意见中明确指出
- 附件无法辨认、与申请内容不符、或信息不足以判断 → manual
"""

AMOUNT_RULE = "- 合同金额超过 {limit} 元 → manual\n"

OPINION_RULES = """
【意见要求】
- 严格控制在 500 字以内，分点论述，**每点单独一行**
- 用 1. 2. 3. 编号，每点一到两句话，先给结论再给依据
- 只写发现的问题和结论，不要复述合同全文
- 未发现问题的方面一句话带过，不要逐条展开
- 不要写成一大段，不要加标题，不要用 markdown 符号
- 语气专业客观，禁止出现"AI""模型""系统判断""人工智能"等字样

正确示例：
1. 合同主体：甲方与乙方名称、签章一致，主体资格无异常。
2. 金额与单位：合同总价 4000 元，大小写一致，计量单位为台/年，无错误。
3. 条款合理性：质保期、付款方式、违约责任约定明确，无失衡条款。
4. 结论：条款齐全、风险可控，同意。
"""

OUTPUT_RULES = """
【输出格式】
只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块：
{"decision":"approve|manual","confidence":0.0,"remark":"","evidence":[]}

字段说明：
- decision：approve 表示同意，manual 表示需人工复核
- confidence：0~1 的小数，表示本次判断的把握
- remark：写进流程的审批意见，分点撰写，不超过 500 字
- evidence：判断依据的要点，字符串数组
"""

FORBIDDEN_WORDS = ("AI", "模型", "系统判断", "人工智能", "机器学习")

# 强制批准模式下，意见为空时用这句兜底
DEFAULT_REMARK = "经核对，合同主体、金额与主要条款无异常，同意。"


def system_prompt() -> str:
    amount = AMOUNT_RULE.format(limit=C.AMOUNT_LIMIT) if C.AMOUNT_LIMIT > 0 else ""
    return REVIEW_RULES + amount + OPINION_RULES + OUTPUT_RULES


# ---------------- 文件上传 ----------------

def _fmt_err(e) -> str:
    """把 OpenAI SDK 的异常展开成可读信息，光看 Error code 没用。"""
    parts = [f"{type(e).__name__}: {e}"]
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            parts.append(f"HTTP {resp.status_code} body={resp.text[:400]!r}")
        except Exception:
            pass
    body = getattr(e, "body", None)
    if body:
        parts.append(f"body={body}")
    return " | ".join(parts)


def upload_files(paths: list[Path]) -> list[str]:
    """上传附件到 DeepSeek，返回 file_id 列表。单个失败不影响其它。"""
    ids: list[str] = []
    for p in paths:
        try:
            size_mb = p.stat().st_size / 1024 / 1024
            if size_mb > C.MAX_UPLOAD_MB:
                log(f"跳过附件 {p.name}：{size_mb:.1f}MB 超过上限 {C.MAX_UPLOAD_MB}MB")
                continue

            fid = _upload_one(p)
            if fid:
                ids.append(fid)
        except Exception as e:
            log(f"上传附件失败 {p.name}: {_fmt_err(e)}")
    return ids


def _upload_one(p: Path) -> str:
    """按几个合法的 purpose 依次尝试。

    实测：text_recognition / rich_layout_recognition 可用；
    user_data 传大文件容易连接中断，放最后。
    """
    purposes = [C.FILE_PURPOSE, "text_recognition", "rich_layout_recognition", "user_data"]
    seen, last = set(), None

    # 本地文件名带 fileid 前缀避免覆盖，但传给模型的仍用原始文件名
    display = p.name.split("_", 1)[1] if "_" in p.name else p.name

    for purpose in purposes:
        if purpose in seen:
            continue
        seen.add(purpose)
        try:
            with open(p, "rb") as f:
                obj = _get_client().files.create(file=(display, f), purpose=purpose)

            log(f"附件上传成功 {p.name} purpose={purpose} id={obj.id}")
            # 把完整返回打出来，方便确认有没有别的 id 字段
            if hasattr(obj, "model_dump"):
                try:
                    log("  上传返回: " + json.dumps(obj.model_dump(), ensure_ascii=False, default=str))
                except Exception:
                    pass
            return obj.id
        except Exception as e:
            last = e

    raise last if last else RuntimeError("上传失败")


def delete_files(file_ids: list[str]):
    for fid in file_ids:
        try:
            _get_client().files.delete(fid)
        except Exception:
            pass


# ---------------- 评审 ----------------

def review(detail: dict, file_ids: list[str], fallback_text: str = "") -> dict:
    material = render_material(detail, bool(file_ids) or bool(fallback_text))

    # 上传失败时用本地提取的文本兜底
    if fallback_text:
        material += "\n\n【附件文本（本地提取）】\n" + fallback_text

    if file_ids:
        content = [{"type": "text", "text": material}]
        for fid in file_ids:
            # 实测：DeepSeek 要平铺的 file_id，不是 OpenAI 那种嵌套 {"file": {...}}
            content.append({"type": "file", "file_id": fid})
    else:
        content = material

    resp = _get_client().chat.completions.create(
        model=C.AI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": content},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        timeout=C.AI_TIMEOUT,
    )

    raw = resp.choices[0].message.content
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log(f"AI 返回非 JSON: {raw[:300]}")
        return _fallback("返回格式异常")

    result = _validate(data)

    # 判定为 manual 时把原始返回打出来，便于确认是规则判的还是格式问题
    if result["decision"] != "approve":
        log(f"AI 判 manual，原始返回: {raw[:600]}")

    # 强制批准：不看 AI 的结论，只用它写的意见
    if C.FORCE_APPROVE:
        if result["decision"] != "approve":
            log("FORCE_APPROVE=true，强制改为 approve")
        result["decision"] = "approve"
        if len(result["remark"]) < 20:
            result["remark"] = DEFAULT_REMARK

    return result


def render_material(detail: dict, has_files: bool) -> str:
    d = detail.get("data", detail)
    lines = [
        f"【流程名称】{d.get('requestName', '')}",
        f"【当前节点】{d.get('currentNodeName', '')}",
        f"【申请人】{d.get('creatorName', '')}",
        f"【提交时间】{d.get('createTime', '')}",
        "",
        "【表单内容】",
    ]

    for field in _iter_fields(d):
        # 附件字段的 fieldShowValue 是一堆 HTML，对模型是噪音，跳过
        if str(field.get("fieldHtmlType", "")) in C.ATTACH_HTML_TYPES:
            continue
        if field.get("view") is False:
            continue

        name = field.get("fieldShowName") or field.get("fieldName") or ""
        value = (field.get("fieldShowValue") or "").strip()
        if not name or not value:
            continue
        lines.append(f"- {name}：{value}")

    logs = d.get("workflowRequestLogs") or []
    if logs:
        lines += ["", "【历史审批记录（最近5条）】"]
        for item in logs[:5]:
            opinion = item.get("remark") or "无意见"
            lines.append(
                f"- {item.get('operatorName', '')}（{item.get('operatorDept', '')}）"
                f"{item.get('operateType', '')}，意见：{opinion}"
            )

    lines += ["", "【附件】" + ("见随附文件，请逐一核对内容" if has_files else "无附件")]
    return "\n".join(lines)


def _iter_fields(node):
    if isinstance(node, dict):
        if "fieldHtmlType" in node:
            yield node
        for v in node.values():
            yield from _iter_fields(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_fields(v)


def _format_remark(text: str) -> str:
    """把挤在一行的分点意见拆成多行，方便在 OA 里阅读。"""
    t = (text or "").strip()
    if not t:
        return t

    t = t.replace("\r\n", "\n").replace("\r", "\n")

    # 序号前补换行：1. / 2、 / 3)   （1.5 这种小数不受影响）
    t = re.sub(r"(?<=\S)[ \t]*(?=(?:[1-9]\d?)[\.、\)](?!\d))", "\n", t)
    # （1）（2） 这种编号
    t = re.sub(r"(?<=\S)[ \t]*(?=（[1-9]\d?）)", "\n", t)

    lines = [ln.strip() for ln in t.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _validate(raw: dict) -> dict:
    decision = str(raw.get("decision", "")).lower()
    if decision not in ("approve", "manual"):
        decision = "manual"

    try:
        confidence = float(raw.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    remark = (raw.get("remark") or "").strip()

    # 格式问题只做修正，不降级决策，也不截断内容
    for w in FORBIDDEN_WORDS:
        if w in remark:
            remark = remark.replace(w, "")
    remark = re.sub(r"[ \t]{2,}", " ", remark).strip()
    remark = _format_remark(remark)

    # 默认不限制长度；只有显式配了 MAX_REMARK_CHARS 才截
    if C.MAX_REMARK_CHARS and len(remark) > C.MAX_REMARK_CHARS:
        log(f"意见 {len(remark)} 字，超过配置上限 {C.MAX_REMARK_CHARS}，截断")
        remark = remark[: C.MAX_REMARK_CHARS]

    return {
        "decision": decision,
        "confidence": confidence,
        "remark": remark,
        "evidence": raw.get("evidence") or [],
    }


def _fallback(reason: str) -> dict:
    log(f"AI 输出不合规，转人工：{reason}")
    return {"decision": "manual", "confidence": 0.0, "remark": "", "evidence": [reason]}


def count_returns(detail: dict) -> int:
    """从历史日志里数人工退回的次数，用于熔断。"""
    d = detail.get("data", detail)
    keywords = ("退回", "驳回", "否决", "不同意")
    return sum(
        1
        for item in (d.get("workflowRequestLogs") or [])
        if any(k in (item.get("operateType") or "") for k in keywords)
    )
