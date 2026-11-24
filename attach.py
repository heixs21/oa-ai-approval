"""附件处理：从流程详情提 fileid，再从 OA 下载落盘。

解析交给 DeepSeek 服务端（支持 PDF / Word / PPT / 图片），本地不需要 OCR 和 PDF 解析。
"""
import html as html_lib
import re
import urllib.parse
from pathlib import Path

import config as C
from oa import oa

# fieldShowValue 里的下载链接形如：
#   onclick="toURL('/news/download.do?fileid=35991&filename=1.jpg',false);"
# 注意 HTML 里常写成 &amp;，必须先 unescape
ID_RE = re.compile(r"(\d+)")
NAME_RE = re.compile(r"filename=([^&)]+)")
SPLIT_RE = re.compile(r"['\"<>]")


def extract_files(detail: dict) -> list[tuple[str, str]]:
    """遍历详情里所有附件字段，返回 [(imagefileid, 文件名), ...]

    注意 fieldValue 里是 docid，不能拿去下载；只能用 fieldShowValue 里的 fileid。
    """
    seen, out = set(), []

    for field in _iter_fields(detail):
        if str(field.get("fieldHtmlType", "")) not in C.ATTACH_HTML_TYPES:
            continue

        raw = field.get("fieldShowValue") or ""
        if not raw:
            continue

        text = html_lib.unescape(raw)
        # 按 fileid= 切开，每段只取到引号/尖括号为止，避免串到下一个链接
        for chunk in text.split("fileid=")[1:]:
            url_part = SPLIT_RE.split(chunk, 1)[0]

            m = ID_RE.match(url_part.strip())
            if not m:
                continue
            fid = m.group(1)
            if fid in seen:
                continue
            seen.add(fid)

            nm = NAME_RE.search(url_part)
            name = urllib.parse.unquote(nm.group(1)) if nm else f"file_{fid}"
            out.append((fid, name))

    return out


def _iter_fields(node):
    """递归找出所有字段对象（主表 + 明细表都覆盖）。"""
    if isinstance(node, dict):
        if "fieldHtmlType" in node:
            yield node
        for v in node.values():
            yield from _iter_fields(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_fields(v)


def download_all(detail: dict, folder: Path) -> list[Path]:
    """把流程里的附件全部下载到 folder。单个失败不影响其它。"""
    files = extract_files(detail)
    if not files:
        return []

    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for fid, name in files:
        # 用 fileid 做前缀：不同附件同名时不会互相覆盖，重复运行也能识别
        dest = folder / f"{fid}_{clean_name(name)}"
        if dest.exists() and dest.stat().st_size > 0:
            saved.append(dest)
            continue
        try:
            oa.download(fid, dest)
            saved.append(dest)
        except Exception as e:
            log(f"附件下载失败 fileid={fid} name={name}: {e}")
    return saved


def clean_name(name: str, maxlen: int = 80) -> str:
    """清洗成合法文件名。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "").strip()
    s = s.strip(". ")
    return s[:maxlen] or "unnamed"


# ---------------- 兜底：本地抽文本 ----------------

def text_fallback(files: list[Path], limit: int = 10000) -> str:
    """上传给模型失败时，本地抽文本兜底。缺库就跳过，不抛异常。"""
    chunks = []
    for f in files:
        ext = f.suffix.lower()
        text = ""
        try:
            if ext == ".pdf":
                from pypdf import PdfReader
                text = "\n".join((p.extract_text() or "") for p in PdfReader(str(f)).pages)
            elif ext == ".docx":
                from docx import Document
                text = "\n".join(p.text for p in Document(str(f)).paragraphs if p.text.strip())
            elif ext in {".xlsx", ".xlsm"}:
                from openpyxl import load_workbook
                wb = load_workbook(str(f), data_only=True, read_only=True)
                rows = []
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        cells = [str(c) for c in row if c is not None]
                        if cells:
                            rows.append(" | ".join(cells))
                wb.close()
                text = "\n".join(rows)
            elif ext in {".txt", ".csv", ".md", ".log"}:
                text = f.read_text(encoding="utf-8", errors="ignore")
            else:
                text = f"[{ext} 格式无法在本地提取文本]"
        except ImportError:
            text = f"[未安装解析 {ext} 的库，无法提取]"
        except Exception as e:
            text = f"[解析失败: {e}]"

        text = (text or "").strip()
        chunks.append(f"--- {f.name} ---\n{text[:limit] or '[无可提取文本]'}")

    return "\n\n".join(chunks)


def log(msg: str):
    print(f"[attach] {msg}")
