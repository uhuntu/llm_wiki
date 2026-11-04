#!/usr/bin/env python3
"""LLM Wiki 只读 Web 服务：浏览 + 全文检索。

用法: python3 server.py [--port 8899] [--host 0.0.0.0]
环境变量: WIKI_ROOT (默认 /home/hunt/work/dg3sw/wiki)
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

WIKI_ROOT = os.environ.get("WIKI_ROOT", "/home/hunt/work/dg3sw/wiki")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# 允许读取的真实路径根：wiki 目录本身 + 共享盘 _Wiki（符号链接解析后的目标）
ALLOWED_ROOTS = tuple(
    os.path.realpath(p)
    for p in (WIKI_ROOT, "/mnt/img/R000-XMRD/dg3sw/_Wiki")
)

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)

# 共享 _Wiki 的本地同步源：llm_wiki 引擎的 WalkDir 不跟随符号链接，
# wiki/ 必须是真实文件，故每次请求前增量同步（本地 index.md/log.md 受保护不被删）。
WIKI_SYNC_SRC = os.environ.get("WIKI_SYNC_SRC", "/mnt/img/R000-XMRD/dg3sw/_Wiki")
_sync_lock = threading.Lock()
_last_sync = 0.0


def sync_wiki():
    global _last_sync
    with _sync_lock:
        if time.time() - _last_sync < 15:
            return
        try:
            subprocess.run(
                ["rsync", "-a", "--delete",
                 "--exclude", "index.md", "--exclude", "log.md",
                 WIKI_SYNC_SRC.rstrip("/") + "/", WIKI_ROOT.rstrip("/") + "/"],
                check=True, capture_output=True, timeout=60)
            _last_sync = time.time()
        except Exception as e:
            print(f"[sync] wiki 同步失败: {e}", file=sys.stderr)
TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.M)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)


def extract_title(text, fallback):
    m = TITLE_RE.search(text)
    if m:
        return m.group(1).strip("\"'")
    m = H1_RE.search(text)
    if m:
        return re.sub(r"[*_`]", "", m.group(1))
    return fallback


def iter_md_files(root):
    """遍历 wiki 根目录下的 .md 文件（跟随符号链接，带环路保护）。"""
    seen = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".md"):
                full = os.path.join(dirpath, name)
                yield os.path.relpath(full, root)


class WikiIndex:
    """wiki 页面索引：stat 签名变化时才重读文件内容。"""

    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.signature = None
        self.pages = []  # [{path,title,text,mtime,size}]

    def _stat_signature(self):
        sig = []
        for rel in iter_md_files(self.root):
            try:
                st = os.stat(os.path.join(self.root, rel))
                sig.append((rel, st.st_mtime_ns, st.st_size))
            except OSError:
                continue
        return sorted(sig)

    def get(self):
        with self.lock:
            sig = self._stat_signature()
            if sig != self.signature:
                pages = []
                for rel, mtime_ns, size in sig:
                    try:
                        with open(os.path.join(self.root, rel), encoding="utf-8", errors="replace") as f:
                            text = f.read()
                    except OSError:
                        continue
                    pages.append({
                        "path": rel,
                        "title": extract_title(text, os.path.basename(rel)),
                        "text": text,
                        "mtime": mtime_ns // 10 ** 9,
                        "size": size,
                    })
                self.pages = pages
                self.signature = sig
            return self.pages


INDEX = WikiIndex(WIKI_ROOT)


def safe_resolve(rel_path):
    """把相对路径解析为真实文件路径，校验未越界且在允许的根下。"""
    rel = os.path.normpath(rel_path.lstrip("/"))
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    real = os.path.realpath(os.path.join(WIKI_ROOT, rel))
    if not any(real == root or real.startswith(root + os.sep) for root in ALLOWED_ROOTS):
        return None
    return real


def build_tree(pages):
    groups = {}
    for p in pages:
        parts = p["path"].split(os.sep)
        group = parts[0] if len(parts) > 1 else "(root)"
        groups.setdefault(group, []).append({
            "path": p["path"],
            "title": p["title"],
            "mtime": p["mtime"],
        })
    for items in groups.values():
        items.sort(key=lambda x: x["path"])
    return [
        {"name": name, "files": groups[name]}
        for name in sorted(groups)
    ]


def search(pages, query):
    q = query.lower()
    results = []
    for p in pages:
        text_low = p["text"].lower()
        title_hit = q in p["title"].lower() or q in os.path.basename(p["path"]).lower()
        positions = []
        start = 0
        while len(positions) < 8:
            i = text_low.find(q, start)
            if i < 0:
                break
            positions.append(i)
            start = i + max(len(q), 1)
        if not positions and not title_hit:
            continue
        if positions:
            i = positions[0]
            lo = max(0, i - 60)
            hi = min(len(p["text"]), i + len(query) + 60)
            snippet = ("…" if lo > 0 else "") + p["text"][lo:hi] + ("…" if hi < len(p["text"]) else "")
            hits = [[s - lo + (1 if lo > 0 else 0), s - lo + len(query) + (1 if lo > 0 else 0)]
                    for s in positions if lo <= s < hi]
        else:
            snippet, hits = p["text"][:120], []
        results.append({
            "path": p["path"],
            "title": p["title"],
            "snippet": snippet.replace("\n", " ")[:300],
            "hits": hits,
            "occurrences": max(len(positions), 1 if title_hit else 0),
        })
    results.sort(key=lambda r: (-r["occurrences"], r["path"]))
    return results[:50]


# ---------- LLM 问答（检索增强） ----------
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://10.11.97.25:6002/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-oss-120b")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "120"))
APP_STATE = os.path.expanduser("~/.local/share/com.llmwiki.app/app-state.json")
CHUNK_MAX = 900
CHUNK_OVERLAP = 150


def get_llm_api_key():
    """API key 解析：环境变量优先，其次桌面应用设置存储（不落日志）。"""
    k = os.environ.get("LLM_API_KEY", "").strip()
    if k:
        return k
    try:
        with open(APP_STATE, encoding="utf-8") as f:
            return ((json.load(f).get("llmConfig") or {}).get("apiKey") or "").strip()
    except (OSError, ValueError):
        return ""


def build_chunks(pages):
    """按二级标题分节，长节滑窗切块，供检索增强引用。"""
    chunks = []
    for p in pages:
        body = FRONTMATTER_RE.sub("", p["text"], count=1)
        for sec in re.split(r"\n(?=#{1,3}\s)", body):
            sec = sec.strip()
            if not sec:
                continue
            if len(sec) <= CHUNK_MAX:
                pieces = [sec]
            else:
                pieces = [sec[i:i + CHUNK_MAX]
                          for i in range(0, len(sec), CHUNK_MAX - CHUNK_OVERLAP)]
            for piece in pieces:
                chunks.append({"path": p["path"], "title": p["title"], "text": piece})
    return chunks


def query_terms(q):
    """英文按词、中文按二元组拆查询词，兼容中英混排。"""
    terms = [m.lower() for m in re.findall(r"[A-Za-z0-9_.+-]+", q)]
    for run in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def retrieve(question, top_k=6):
    """对问题做分块检索：词频打分 + 标题加权，每页最多 2 块。"""
    terms = query_terms(question)
    scored = []
    for ch in build_chunks(INDEX.get()):
        low = ch["text"].lower()
        s = sum(min(low.count(t), 3) for t in terms)
        if not s:
            continue
        title_low = ch["title"].lower()
        s += sum(2 for t in terms if t in title_low)
        scored.append((s, ch))
    scored.sort(key=lambda x: -x[0])
    out, per_page = [], {}
    for s, ch in scored:
        n = per_page.get(ch["path"], 0)
        if n >= 2:
            continue
        per_page[ch["path"]] = n + 1
        out.append({**ch, "score": s})
        if len(out) >= top_k:
            break
    return out


# ---------- llm_wiki 引擎代理 ----------
ENGINE_BASE = os.environ.get("LLM_WIKI_API", "http://localhost:19828/api/v1")
ENGINE_TOKEN = os.environ.get("LLM_WIKI_API_TOKEN", "")


def ask_engine(question, session_id):
    """调用 llm_wiki 引擎的 chat API（真正的 llm_wiki 大脑：检索/agent/skills）。

    同一 sessionId 的后续消息由引擎自动携带会话历史，天然支持多轮。
    """
    payload = {"message": question, "persistSession": True}
    if session_id:
        payload["sessionId"] = session_id
    req = urllib.request.Request(
        ENGINE_BASE.rstrip("/") + "/projects/current/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-LLM-Wiki-Token": ENGINE_TOKEN},
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
        d = json.load(resp)
    if not d.get("ok"):
        raise RuntimeError(d.get("error") or "engine error")
    sources = []
    for i, r in enumerate(d.get("references") or []):
        sources.append({
            "n": i + 1,
            "path": r.get("path", ""),
            "title": r.get("title") or r.get("path", ""),
            "snippet": (r.get("snippet") or "")[:300],
        })
    return {
        "answer": d.get("message", {}).get("content", ""),
        "sources": sources,
        "sessionId": d.get("sessionId", ""),
        "via": "engine",
    }


def ask_llm(question, contexts):
    refs = "\n\n".join(
        f"[{i + 1}] 来源页面：{c['path']}（{c['title']}）\n{c['text']}"
        for i, c in enumerate(contexts)
    )
    prompt = (
        "你是 dg3sw 项目知识库的问答助手。请仅依据下列参考资料回答问题，"
        "回答中用 [n] 标注引用的资料编号；资料中没有的信息请明说“资料中未提及”，不要编造。\n\n"
        f"参考资料：\n{refs}\n\n问题：{question}"
    )
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "用中文回答，简洁、准确，引用标注 [n]。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 2000,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = get_llm_api_key()
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        LLM_BASE_URL.rstrip("/") + "/chat/completions", data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]


class Handler(BaseHTTPRequestHandler):
    server_version = "LLMWikiWeb/1.0"

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        sync_wiki()
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
            return

        if path == "/api/status":
            engine = False
            try:
                req = urllib.request.Request(
                    ENGINE_BASE.rstrip("/") + "/health",
                    headers={"X-LLM-Wiki-Token": ENGINE_TOKEN},
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    engine = resp.status == 200
            except Exception:
                pass
            self._send_json({"engine": engine})
            return

        if path == "/api/tree":
            self._send_json({"groups": build_tree(INDEX.get())})
            return

        if path == "/api/page":
            rel = (params.get("path") or [""])[0]
            real = safe_resolve(rel)
            if real is None or not os.path.isfile(real):
                self._send_json({"error": "invalid path"}, 400)
                return
            try:
                with open(real, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                self._send_json({"error": str(e)}, 500)
                return
            self._send_json({
                "path": rel,
                "title": extract_title(text, os.path.basename(rel)),
                "content": text,
                "mtime": int(os.stat(real).st_mtime),
            })
            return

        if path == "/api/search":
            q = (params.get("q") or [""])[0].strip()
            if not q:
                self._send_json({"results": []})
                return
            t0 = time.time()
            results = search(INDEX.get(), q)
            self._send_json({"results": results, "elapsed_ms": int((time.time() - t0) * 1000)})
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        sync_wiki()
        if urlparse(self.path).path != "/api/chat":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "bad request"}, 400)
            return
        question = (payload.get("question") or "").strip()
        if not question:
            self._send_json({"error": "question required"}, 400)
            return
        t0 = time.time()
        session_id = (payload.get("sessionId") or "").strip()
        try:
            # 首选：llm_wiki 引擎（真正的 llm_wiki 大脑）
            result = ask_engine(question, session_id)
        except Exception as engine_err:
            # 兜底：引擎不可用时退回本地简化 RAG
            try:
                contexts = retrieve(question)
                if not contexts:
                    self._send_json({"answer": "知识库中没有找到与问题相关的内容。", "sources": []})
                    return
                answer = ask_llm(question, contexts)
            except Exception as e:
                self._send_json({"error": f"引擎不可用（{engine_err}），本地兜底也失败（{e}）"}, 502)
                return
            result = {
                "answer": f"> ⚠️ llm_wiki 引擎当前不可用（{engine_err}），以下为本地简化检索兜底回答：\n\n{answer}",
                "sources": [{"n": i + 1, "path": c["path"], "title": c["title"],
                             "snippet": ""} for i, c in enumerate(contexts)],
                "sessionId": session_id,
                "via": "fallback",
            }
        self._send_json({**result, "elapsed_ms": int((time.time() - t0) * 1000)})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    host, port = "0.0.0.0", 8899
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
        elif a == "--host" and i + 1 < len(args):
            host = args[i + 1]
    INDEX.get()  # 预热索引
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"LLM Wiki web: http://{host}:{port}  root={WIKI_ROOT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
