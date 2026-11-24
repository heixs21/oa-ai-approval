"""接口探测：一次跑完常见组合，直接看出卡在哪一层。

用法（在项目根目录执行）：
    python tools/probe_api.py

不需要改任何配置，读的是 .env。输出里带状态码和响应体，直接看哪一组返回了正常 JSON。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

import config as C
from oa import oa, rsa_encrypt

PATH = "/api/workflow/paService/getDoingWorkflowRequestList"
PAYLOAD = {"pageNo": 1, "pageSize": 10}


def show(tag: str, r):
    ct = r.headers.get("Content-Type", "")
    body = r.text[:400].replace("\r", " ").replace("\n", " ")
    flag = "OK " if r.status_code == 200 and body.lstrip().startswith(("{", "[")) else "!! "
    print(f"\n{flag}[{tag}]")
    print(f"    HTTP {r.status_code}  Content-Type={ct}")
    print(f"    body: {body}")


def main():
    print("=" * 70)
    print("OA 地址 :", C.OA_BASE)
    print("appid   :", (C.OA_APPID[:8] + "...") if C.OA_APPID else "(空)")
    print("userid  :", C.OA_USERID or "(空)", " spk:", "已配置" if C.OA_SPK else "未配置")
    print("=" * 70)

    # ---------- 1. token ----------
    try:
        tok = oa.token()
        print(f"\n[1] applytoken 成功，token 前 8 位: {tok[:8]}...")
    except Exception as e:
        print(f"\n[1] applytoken 失败: {e}")
        return

    # ---------- 2. userid 两种形态 ----------
    uid_variants = {}
    if C.OA_USERID:
        uid_variants["明文"] = str(C.OA_USERID)
    if C.OA_SPK and C.OA_USERID:
        try:
            uid_variants["密文"] = rsa_encrypt(C.OA_SPK, str(C.OA_USERID))
            print(f"[2] userid 加密成功，密文长度 {len(uid_variants['密文'])}")
        except Exception as e:
            print(f"[2] userid 加密失败: {e}")

    if not uid_variants:
        print("[2] OA_USERID 未配置，无法继续")
        return

    # ---------- 3. 组合探测 ----------
    url = f"{C.OA_BASE}{PATH}"
    combos = [
        ("JSON + form-urlencoded",
         json.dumps(PAYLOAD),
         "application/x-www-form-urlencoded; charset=utf-8"),
        ("JSON + application/json",
         json.dumps(PAYLOAD),
         "application/json"),
        ("form + form-urlencoded",
         PAYLOAD,
         "application/x-www-form-urlencoded; charset=utf-8"),
    ]

    for uid_name, uid in uid_variants.items():
        for combo_name, body, ct in combos:
            headers = {
                "appid": C.OA_APPID,
                "token": tok,
                "userid": uid,
                "Content-Type": ct,
            }
            try:
                r = requests.post(url, data=body, headers=headers, timeout=20)
                show(f"userid={uid_name} | {combo_name}", r)
            except Exception as e:
                print(f"\n!! [userid={uid_name} | {combo_name}] 请求异常: {e}")

    print("\n" + "=" * 70)
    print("哪一组出现 OK，就照着那组设置 .env：")
    print("  Content-Type -> OA_CONTENT_TYPE")
    print("  body 形态    -> OA_BODY_MODE  (json 或 form)")
    print("  userid 形态  -> 明文留空 OA_SPK；密文填上 OA_SPK")
    print("=" * 70)


if __name__ == "__main__":
    main()
