"""泛微 E9 接口客户端。

两套凭据各自维护：
  token      -> /api/... 开放接口
  JSESSIONID -> /weaver/... 老 servlet（下载）
"""
import base64
import json
import re
import threading
import time
from pathlib import Path

import requests

import config as C

# 提交接口的请求方法。默认 POST；若返回 405 就改成 "GET"
SUBMIT_METHOD = "POST"

# 各接口实际可用的请求方法。泛微这套接口方法不统一，先按已知值发，撞到 405 再自动切换。
METHOD_MAP = {
    "/api/workflow/paService/getDoingWorkflowRequestList": "POST",
    "/api/workflow/paService/getWorkflowRequest": "GET",   # 实测：GET
    "/api/workflow/paService/submitRequest": "POST",       # 实测：POST
}


class TokenExpired(Exception):
    pass


class SessionExpired(Exception):
    pass


class OAClient:
    def __init__(self):
        self._token = None
        self._token_exp = 0.0
        self._token_lock = threading.Lock()

        self._sess = None
        self._sess_lock = threading.Lock()

        self._uid_cache = None
        # 记录每个接口实际可用的请求方法（POST / GET）
        self._method_cache: dict[str, str] = {}

    # ================= token =================

    def token(self, force: bool = False) -> str:
        with self._token_lock:
            if not force and self._token and time.time() < self._token_exp:
                return self._token

            r = requests.post(
                f"{C.OA_BASE}/api/ec/dev/auth/applytoken",
                headers={"appid": C.OA_APPID, "secret": C.OA_SECRET},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()

            tok = self._pick_token(data)
            if not tok:
                raise RuntimeError(f"applytoken 未返回 token: {r.text[:300]}")

            self._token = tok
            self._token_exp = time.time() + C.TOKEN_TTL
            return tok

    @staticmethod
    def _pick_token(data):
        if isinstance(data, dict):
            if data.get("token"):
                return data["token"]
            inner = data.get("data")
            if isinstance(inner, dict) and inner.get("token"):
                return inner["token"]
        return None

    @staticmethod
    def _is_token_error(data) -> bool:
        if not isinstance(data, dict):
            return False
        text = json.dumps(data, ensure_ascii=False)
        return any(k in text for k in ("token", "Token", "TOKEN")) and any(
            k in text for k in ("过期", "失效", "无效", "expire", "invalid")
        )

    # ================= 开放接口 =================

    def _headers(self) -> dict:
        """业务接口的请求头：appid + token + userid（密文）。

        - userid 决定"以谁的身份"调用：待办返回谁的数据、提交记在谁名下
        - 泛微要求 userid 是 spk 公钥 RSA 加密后的密文，不是明文
        - Content-Type 必须是 form-urlencoded，但 body 仍是 JSON
        """
        return {
            "appid": C.OA_APPID,
            "token": self.token(),
            "userid": self._encrypted_userid(),
            "Content-Type": C.OA_CONTENT_TYPE,
        }

    def _encrypted_userid(self) -> str:
        """把 userid 用 spk 公钥加密成密文。

        实测泛微要求传密文，明文会被拒（返回空响应）。
        """
        if self._uid_cache is not None:
            return self._uid_cache

        plain = str(C.OA_USERID).strip()
        if not plain:
            raise RuntimeError("OA_USERID 未配置")

        if C.OA_USERID_PLAIN:
            self._uid_cache = plain
            return plain

        if not C.OA_SPK.strip():
            raise RuntimeError(
                "OA_SPK 未配置。userid 必须用 spk 公钥加密成密文才能调用接口。\n"
                "spk 在 /api/ec/dev/auth/regist 的返回里，和 secret 同一次返回，"
                "填进 .env 的 OA_SPK（裸 base64 串，不用加 BEGIN/END）"
            )

        self._uid_cache = rsa_encrypt(C.OA_SPK, plain)
        return self._uid_cache

    def _request(self, method: str, path: str, payload: dict):
        url = f"{C.OA_BASE}{path}"
        headers = self._headers()

        if method == "GET":
            return requests.get(url, params=payload, headers=headers, timeout=30)

        if C.OA_BODY_MODE == "form":
            return requests.post(url, data=payload, headers=headers, timeout=30)

        return requests.post(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            timeout=30,
        )

    def api(self, path: str, payload: dict, retry: int | None = None) -> dict:
        retry = C.MAX_RETRY if retry is None else retry
        last = ""
        # 泛微同一套接口里方法不统一，405 就换一种，并记住这个接口该用哪个
        method = self._method_cache.get(path) or METHOD_MAP.get(path, "POST")
        switched = False

        for _ in range(retry + 1):
            try:
                r = self._request(method, path, payload)
            except Exception as e:
                last = f"网络异常: {e}"
                time.sleep(1)
                continue

            if r.status_code == 405 and not switched:
                method = "GET" if method == "POST" else "POST"
                switched = True
                self._method_cache[path] = method
                continue

            if r.status_code != 200:
                last = (
                    f"HTTP {r.status_code}（{method}） "
                    f"Content-Type={r.headers.get('Content-Type', '')} "
                    f"body={r.text[:500]!r}"
                )
                time.sleep(1)
                continue

            try:
                data = r.json()
            except ValueError:
                # 空响应或 HTML 错误页，这里必须把原文带出来，否则没法排查
                last = (
                    f"返回非 JSON（{method}） HTTP {r.status_code} "
                    f"Content-Type={r.headers.get('Content-Type', '')} "
                    f"body={r.text[:500]!r}"
                )
                time.sleep(1)
                continue

            if self._is_token_error(data):
                self.token(force=True)
                continue

            return data

        raise RuntimeError(f"接口调用失败 {path}: {last}")

    # ================= 业务接口 =================

    def get_doing_list(self) -> list[dict]:
        """拉待办列表。返回里凡是带 requestId 的对象都算一条待办。"""
        data = self.api(
            "/api/workflow/paService/getDoingWorkflowRequestList",
            {"pageNo": 1, "pageSize": 100},
        )
        # 按 requestId 去重，防止嵌套结构里重复收集
        seen, out = set(), []
        for item in _find_all(data, key="requestId"):
            rid = str(item.get("requestId"))
            if rid in seen:
                continue
            seen.add(rid)
            out.append(item)
        return out

    def get_request(self, request_id: str, workflow_id: str) -> dict:
        """读流程详情，返回 data 节点。"""
        payload = {"requestId": request_id}
        if workflow_id:  # 待办列表里可能没有 workflowId，别传空串
            payload["workflowId"] = workflow_id

        data = self.api("/api/workflow/paService/getWorkflowRequest", payload)
        found = _find_all(data, key="workflowMainTableInfo")
        if not found:
            raise RuntimeError(f"流程详情里没找到表单数据: {json.dumps(data, ensure_ascii=False)[:300]}")
        return found[0]

    def submit(self, request_id: str, remark: str) -> dict:
        """提交审批，意见放 remark（走 query string）。405 自动换方法，超长自动改请求体。"""
        path = "/api/workflow/paService/submitRequest"
        url = f"{C.OA_BASE}{path}"

        # 泛微按 HTML 渲染意见，\n 会被吞掉，换成 <br> 才能在页面上换行
        if C.REMARK_HTML:
            remark = remark.replace("\r\n", "\n").replace("\n", "<br>")

        # 中文 URL 编码后约 9 字节/字，太长会顶到 Tomcat 请求行上限
        if len(remark) > 700:
            print(f"[oa] 警告：意见 {len(remark)} 字符，URL 编码后约 {len(remark) * 9} 字节")

        params = {"requestId": request_id, "remark": remark}
        method = self._method_cache.get(path, SUBMIT_METHOD)
        r = requests.request(method, url, params=params, headers=self._headers(), timeout=60)

        if r.status_code == 405:
            method = "GET" if method == "POST" else "POST"
            self._method_cache[path] = method
            r = requests.request(method, url, params=params, headers=self._headers(), timeout=60)

        # 414/431 说明请求行超限，请求根本没到应用，改把 remark 放请求体重发
        if r.status_code in (414, 431):
            print(f"[oa] 请求行超限（{r.status_code}），改用请求体重发")
            r = requests.request(
                method, url,
                params={"requestId": request_id},
                data={"remark": remark},
                headers=self._headers(),
                timeout=60,
            )

        body = r.text.strip()
        if not body:
            return {"status": False, "msg": f"HTTP {r.status_code}（{method}）空响应"}

        try:
            return r.json()
        except ValueError:
            return {"status": False, "msg": f"HTTP {r.status_code}（{method}）非 JSON: {body[:300]}"}

    # ================= 会话 + 下载 =================

    def session(self, force: bool = False) -> requests.Session:
        with self._sess_lock:
            if force or self._sess is None:
                s = requests.Session()
                url = f"{C.OA_BASE}/api/hrm/login/checkLogin"
                form = {"loginid": C.OA_LOGINID, "userpassword": C.OA_USERPASSWORD}

                # POST，参数先放 query string，拿不到 cookie 再换成表单体
                r = s.post(url, params=form, timeout=15)
                if not s.cookies.get("JSESSIONID"):
                    r = s.post(url, data=form, timeout=15)

                if not s.cookies.get("JSESSIONID"):
                    raise RuntimeError(
                        f"登录失败（未拿到 JSESSIONID）: {r.status_code} {r.text[:300]}"
                    )
                self._sess = s
            return self._sess

    def download(self, file_id: str, dest: Path, retry: int | None = None) -> Path:
        """下载单个附件。会话失效时自动重登重试。"""
        retry = C.MAX_RETRY if retry is None else retry
        dest.parent.mkdir(parents=True, exist_ok=True)

        for _ in range(retry + 1):
            sess = self.session()
            with sess.get(
                f"{C.OA_BASE}/weaver/weaver.file.FileDownload",
                params={"fileid": file_id, "download": 1},
                stream=True,
                timeout=(10, 120),
            ) as r:
                ctype = r.headers.get("Content-Type", "")
                disp = r.headers.get("Content-Disposition", "")

                # 会话失效时接口返回 200 + 登录页 HTML，只能靠 Content-Type 判断
                if "text/html" in ctype or not disp:
                    self.session(force=True)
                    continue

                with open(dest, "wb") as f:
                    for chunk in r.iter_content(65536):
                        if chunk:
                            f.write(chunk)

                expect = r.headers.get("Content-Length")
                if expect and dest.stat().st_size != int(expect):
                    continue

            return dest

        raise SessionExpired(f"下载失败 fileid={file_id}")


def rsa_encrypt(spk: str, plain: str) -> str:
    """用注册许可返回的 spk 公钥做 RSA(PKCS1 v1.5) 加密，再 base64。

    spk 一般是裸的 base64 公钥串，需要补上 PEM 头尾才能被解析。
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key_text = spk.strip()
    if "BEGIN" in key_text:
        pem = key_text
    else:
        b64 = re.sub(r"\s+", "", key_text)
        lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
        pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"

    pub = serialization.load_pem_public_key(pem.encode("utf-8"))
    cipher = pub.encrypt(plain.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(cipher).decode("ascii")


def _find_all(obj, key: str) -> list[dict]:
    """在任意嵌套 JSON 里找出所有含指定 key 的 dict。

    泛微各版本返回的包装层不一样（data / result / datas），这样写不用猜结构。
    """
    out: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            if key in node:
                out.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return out


oa = OAClient()
