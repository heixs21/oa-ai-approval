"""全局配置，全部从 .env 读取。"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _list(key: str, default: str = "") -> list[str]:
    raw = os.getenv(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# ---------- 泛微 OA ----------
OA_BASE = os.getenv("OA_BASE_URL", "http://10.0.0.10:8080").rstrip("/")
OA_APPID = os.getenv("OA_APPID", "")
OA_SECRET = os.getenv("OA_SECRET", "")

# 下载 servlet 用的会话账号
OA_LOGINID = os.getenv("OA_LOGINID", "")
OA_USERPASSWORD = os.getenv("OA_USERPASSWORD", "")

# 业务接口请求头里的 userid：以哪个用户身份调用。
# 决定待办列表返回谁的数据、提交记在谁名下，必填。
OA_USERID = os.getenv("OA_USERID", "")

# 注册许可接口返回的 spk 公钥，用于加密 userid（泛微要求传密文）。
# 留空则按明文传，仅适用于部分未强制加密的环境。
OA_SPK = os.getenv("OA_SPK", "")

# 业务接口的 Content-Type。
# 实测：JSON body 配 application/json 正常；配 form-urlencoded 会返回空响应。
OA_CONTENT_TYPE = os.getenv("OA_CONTENT_TYPE", "application/json")

# 请求体形态：json = 发 JSON 字符串；form = 按表单参数发
OA_BODY_MODE = os.getenv("OA_BODY_MODE", "json")

# userid 是否按明文传。实测泛微要求密文，默认 false。
OA_USERID_PLAIN = os.getenv("OA_USERID_PLAIN", "false").lower() == "true"

# token 有效期（秒）。泛微一般是 30 分钟，提前刷新留余量
TOKEN_TTL = int(os.getenv("TOKEN_TTL", "1500"))

# ---------- 处理范围 ----------
# 只处理这些流程，留空表示全部（不建议）
WORKFLOW_WHITELIST = _list("WORKFLOW_WHITELIST", "264")
# 只处理这些节点（nodeId），留空表示不限
NODE_WHITELIST = _list("NODE_WHITELIST", "")

# 附件字段的 fieldHtmlType，6=附件上传
ATTACH_HTML_TYPES = set(_list("ATTACH_HTML_TYPES", "6"))

# ---------- AI ----------
# 全家桶 API 入口带 /v1
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.deepseek.com/v1")
AI_API_KEY = os.getenv("AI_API_KEY", "")
# DeepSeek-V4.1-Flash：支持文件输入（PDF / Word / PPT / 图片）
AI_MODEL = os.getenv("AI_MODEL", "deepseek-flash")
AI_TIMEOUT = int(os.getenv("AI_TIMEOUT", "180"))

# 附件上传用的 purpose。
# 实测可用：text_recognition / rich_layout_recognition；user_data 传大文件易断
# 文档里的 assistants、file-extract 都会被拒（550 / 400）
FILE_PURPOSE = os.getenv("FILE_PURPOSE", "text_recognition")
# 单文件上传上限（MB），超了直接跳过
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
# 评审完是否删除已上传的文件（避免服务端堆积）
DELETE_FILE_AFTER_REVIEW = os.getenv("DELETE_FILE_AFTER_REVIEW", "true").lower() == "true"

# 低于这个置信度不自动提交，转人工
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.75"))

# 合同金额超过这个数就转人工；填 0 表示不启用这条规则
AMOUNT_LIMIT = int(os.getenv("AMOUNT_LIMIT", "0"))

# 强制批准：不看 AI 的结论，一律提交，只用它写的意见。
# 开了之后 AI 只负责写意见，不再把关，风险由人工看意见时承担。
FORCE_APPROVE = os.getenv("FORCE_APPROVE", "false").lower() == "true"

# 审批意见的最大字数。0 表示不限制（默认）。
# 注意：意见走 URL query string 传，中文 URL 编码后约 9 字节/字，
# Tomcat 请求行上限默认 8KB，所以超过 800 字左右可能顶到上限。
MAX_REMARK_CHARS = int(os.getenv("MAX_REMARK_CHARS", "0"))

# 提交时是否把换行转成 <br>。
# 实测泛微按 HTML 渲染意见，\n 会被当空白吞掉，必须用 <br> 才能换行。
REMARK_HTML = os.getenv("REMARK_HTML", "true").lower() == "true"

# 同一流程被人工退回超过这个次数，停止自动处理；填 0 表示不启用熔断
MAX_RETURN_COUNT = int(os.getenv("MAX_RETURN_COUNT", "2"))

# ---------- 运行 ----------
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
MAX_RETRY = int(os.getenv("MAX_RETRY", "2"))

# true = 只生成意见写日志，不提交
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", str(BASE_DIR / "data" / "attach")))
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "state.db")))
LOG_PATH = Path(os.getenv("LOG_PATH", str(BASE_DIR / "data" / "run.log")))

for _p in (STORAGE_ROOT, DB_PATH.parent, LOG_PATH.parent):
    _p.mkdir(parents=True, exist_ok=True)
