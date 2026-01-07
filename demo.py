import os
import time
import json
from typing import TypedDict, Dict, Any

import httpx
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END

# ================== Basic Setup ==================
# 从 .env 读取环境变量
load_dotenv(override=True)

app = Flask(__name__)

# 模型与 Replicate API 基础配置
MODEL_REF = "meta/meta-llama-3-8b-instruct"
REPLICATE_API_BASE = "https://api.replicate.com/v1"

# 从环境变量读取 Token
TOKEN = (os.getenv("REPLICATE_API_TOKEN") or "").strip()

# 证书路径（服务器环境常见；本地一般不需要改）
CA_BUNDLE = os.getenv("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")


# ================== Prompts ==================
SYSTEM_PROMPT = """# Role: OCI Intelligent Documentation Assistant (OCI IDA)

## Profile
- Language: 中文（默认），必要时可中英双语
- Domain: Oracle Cloud Infrastructure (OCI)
- Description:
你是 OCI 的智能助手，负责解释概念、给出操作步骤，并在需要时提供 OCI CLI / SDK 示例。
你必须避免编造：当缺少关键信息或缺少可靠依据时，先最小追问或明确说明不确定。

## Rules
1. 不要虚构“官方文档链接/段落引用”。如果用户要求链接，你可以建议用户查阅 OCI 官方文档对应服务页面，但不要编造 URL。
2. 如果需要具体操作但信息不足：先在“Missing Info”小节列出最小必要字段（如 compartment_id/region/availability_domain/namespace/bucket_name 等），然后再给下一步建议。
3. 如果上文包含 [CommandTool] JSON（例如 generated_command/missing_fields），优先使用它：
   - generated_command 非空：把它原样放入 “OCI CLI Command” 小节
   - missing_fields 非空：把它合并到 “Missing Info” 小节
4. 输出必须结构化，按以下顺序（没有就写“无”或省略该小节）：
   - Problem Summary
   - Key Concepts (可选，概念题优先)
   - Preconditions
   - Steps
   - Validation
   - OCI CLI Command (如适用)
   - Missing Info (如适用)
   - Notes
5. 所有示例都用占位符（<tenancy_ocid>, <your_compartment_id>），不要输出任何真实密钥/Token。
"""

# Router：判断是否需要调用“命令生成工具”
ROUTER_PROMPT = """# Role: Router

任务：判断用户问题是否需要生成 OCI CLI command（use_command_tool）。

只输出**单行**JSON（不要 Markdown / 不要解释）：
{"use_command_tool": true/false, "missing_fields": [], "reason": ""}

判定规则：
- 需要命令（true）：用户明确要 CLI/command；或问题是资源操作（list/create/delete/update/describe/get），例如 bucket/instance/vcn/volume 等。
- 不需要命令（false）：纯概念解释、对比、原理、术语释义（what is / introduce / difference）。
- missing_fields：只填生成命令所需的最小字段，例如 compartment_id、region、availability_domain、namespace、bucket_name、instance_id。
"""

# Command Tool：把意图转为一条 OCI CLI 命令（如果字段不够就列 missing_fields）
COMMAND_PROMPT = """# Role: OCI CLI Command Generator (Tool)

任务：把用户意图转换为 OCI CLI command。

只输出**单行**JSON（不要 Markdown / 不要解释）：
{"generated_command": "", "missing_fields": [], "notes": ""}

规则：
- generated_command 必须以 'oci ' 开头；只输出一个最相关命令。
- 用户未提供关键字段：用 missing_fields 列出最小必要字段，并将 generated_command 置空。
- 可以使用占位符：
  <your_compartment_id>, <your_region>, <your_availability_domain>, <your_namespace>, <your_bucket_name>, <your_instance_id>
- notes 用一句话提示（例如“需要先提供 compartment_id 才能列出 bucket”）。
"""

# LLaMA 3 Instruct 模型的 chat prompt 模板
LLAMA3_TEMPLATE = (
    "<|begin_of_text|>"
    "<|start_header_id|>system<|end_header_id|>\n\n{system_prompt}<|eot_id|>"
    "<|start_header_id|>user<|end_header_id|>\n\n{prompt}<|eot_id|>"
    "<|start_header_id|>assistant<|end_header_id|>\n\n"
)


# ================== Replicate Client ==================
# 建议开启 trust_env=True，这样在某些环境下 httpx 能自动读代理/证书配置
HTTPX_CLIENT = httpx.Client(verify=CA_BUNDLE, timeout=60, trust_env=True)

def _headers() -> Dict[str, str]:
    return {"Authorization": f"Token {TOKEN}", "Content-Type": "application/json"}

def run_replicate_model(system_prompt: str, prompt: str, max_tokens=200, temperature=0.3) -> str:
    """
    调用 Replicate 的 LLaMA 3 Instruct 模型：
    1) 拉取 latest_version id
    2) 创建 prediction（遇到 429 限流自动等待重试）
    3) 轮询 prediction 状态直到 succeeded
    """
    if not TOKEN:
        # 明确报错：避免“空 token”导致返回 HTML/异常结构后难定位
        raise RuntimeError("REPLICATE_API_TOKEN is empty. Please set it in .env or environment variables.")

    owner, name = MODEL_REF.split("/", 1)

    # 1) get latest version id
    r = HTTPX_CLIENT.get(
        f"{REPLICATE_API_BASE}/models/{owner}/{name}",
        headers=_headers()
    )
    r.raise_for_status()
    vid = r.json()["latest_version"]["id"]

    # 2) create prediction（对 429 做等待重试）
    while True:
        r = HTTPX_CLIENT.post(
            f"{REPLICATE_API_BASE}/predictions",
            headers=_headers(),
            json={
                "version": vid,
                "input": {
                    "system_prompt": system_prompt,
                    "prompt": prompt + "\n",
                    "prompt_template": LLAMA3_TEMPLATE,
                    "max_new_tokens": max_tokens,
                    "temperature": temperature,
                    "stop_sequences": "<|end_of_text|>,<|eot_id|>",
                },
            },
        )

        if r.status_code == 429:
            # Replicate 返回的 retry_after 单位通常是秒
            try:
                retry_after = int(r.json().get("retry_after", 5))
            except Exception:
                retry_after = 5
            time.sleep(retry_after + 1)
            continue

        r.raise_for_status()
        pid = r.json()["id"]
        break

    # 3) poll prediction
    while True:
        j = HTTPX_CLIENT.get(
            f"{REPLICATE_API_BASE}/predictions/{pid}",
            headers=_headers()
        ).json()

        if j["status"] == "succeeded":
            out = j.get("output", "")
            return "".join(out) if isinstance(out, list) else str(out)

        if j["status"] in ("failed", "canceled"):
            raise RuntimeError(f"Replicate prediction {j['status']}: {j}")

        time.sleep(1)


# ================== LangGraph ==================
class State(TypedDict, total=False):
    # 用户输入
    user_input: str
    # Router 的结构化输出
    route: Dict[str, Any]
    # Command Tool 的结构化输出
    command: Dict[str, Any]
    # 最终回答
    reply: str

def router(state: State) -> State:
    """Router：判断是否需要生成 CLI 命令。"""
    raw = run_replicate_model(ROUTER_PROMPT, state["user_input"], 120, 0.2)
    try:
        state["route"] = json.loads(raw)
    except Exception:
        # Router 输出不符合 JSON 时，保守回退：不调用工具
        state["route"] = {
            "use_command_tool": False,
            "missing_fields": [],
            "reason": "Router output not valid JSON"
        }
    return state

def command(state: State) -> State:
    """Command Tool：把意图转换为 OCI CLI command（JSON）。"""
    raw = run_replicate_model(COMMAND_PROMPT, state["user_input"], 200, 0.2)
    try:
        state["command"] = json.loads(raw)
    except Exception:
        # Tool 输出不符合 JSON 时，给空结果，避免后续 500
        state["command"] = {
            "generated_command": "",
            "missing_fields": [],
            "notes": "Command output not valid JSON"
        }
    return state

def answer(state: State) -> State:
    """
    Answer：最终回答节点。
    如果 command 节点跑过，会把 [CommandTool] JSON 附加到 user prompt，便于模型引用工具结果。
    """
    tool_info = ""
    if "command" in state:
        tool_info = f"\n[CommandTool]\n{json.dumps(state['command'], ensure_ascii=False)}\n"

    state["reply"] = run_replicate_model(
        SYSTEM_PROMPT,
        state["user_input"] + tool_info,
        300,
        0.3,
    )
    return state

def route_decision(state: State) -> str:
    """根据 Router 的判断决定走 command 还是直接 answer。"""
    return "command" if state["route"].get("use_command_tool") else "answer"


# 构建 LangGraph 流程
graph = StateGraph(State)
graph.add_node("router", router)
graph.add_node("command", command)
graph.add_node("answer", answer)

graph.set_entry_point("router")
graph.add_conditional_edges("router", route_decision, {"command": "command", "answer": "answer"})
graph.add_edge("command", "answer")
graph.add_edge("answer", END)

CHAT_GRAPH = graph.compile()


# ================== Flask API ==================
@app.route("/api/chat", methods=["POST"])
def chat():
    """
    POST /api/chat
    请求: {"prompt": "..."}
    返回: {"reply": "...", "tool_used": bool, "generated_command": "...", "missing_fields": [...]}
    """
    q = request.json["prompt"]
    out = CHAT_GRAPH.invoke({"user_input": q})

    cmd = out.get("command", {})
    # 更准确的 tool_used：只要生成了命令或提出了缺字段，都算“调用工具并有结果”
    tool_used = bool(cmd.get("generated_command")) or bool(cmd.get("missing_fields"))

    return jsonify({
        "reply": out.get("reply", ""),
        "tool_used": tool_used,
        "generated_command": cmd.get("generated_command", ""),
        "missing_fields": cmd.get("missing_fields", []),
    })


@app.route("/", methods=["GET"])
def home():
    """
    一个最小网页 Demo：直接调用 /api/chat
    说明：这里只是展示，不存储历史对话（每次请求独立）。
    """
    return """
<!doctype html>
<meta charset="utf-8" />
<title>OCI IDA Demo</title>
<style>
  body { font-family: Arial, sans-serif; max-width: 900px; margin: 24px auto; padding: 0 14px; }
  h2 { margin: 0 0 12px; }
  .row { display: flex; gap: 10px; align-items: center; margin: 10px 0; flex-wrap: wrap; }
  textarea { width: 100%; height: 92px; padding: 10px; font-size: 14px; }
  button { padding: 8px 14px; font-size: 14px; cursor: pointer; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 999px; background: #eee; font-size: 12px; }
  pre { background: #f6f6f6; padding: 12px; border-radius: 8px; white-space: pre-wrap; word-break: break-word; }
  .muted { color: #666; font-size: 12px; }
</style>

<h2>OCI IDA Demo</h2>
<div class="muted">调用接口：POST /api/chat</div>

<textarea id="q" placeholder="Ask something... (e.g., List all Object Storage buckets in my compartment using OCI CLI)"></textarea>

<div class="row">
  <button id="askBtn" onclick="ask()">Ask</button>
  <span id="status" class="badge">idle</span>
</div>

<div id="out"></div>

<script>
async function ask() {
  const btn = document.getElementById("askBtn");
  const status = document.getElementById("status");
  const out = document.getElementById("out");
  const q = document.getElementById("q").value.trim();
  if (!q) return;

  btn.disabled = true;
  status.textContent = "loading...";
  out.innerHTML = "";

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({prompt: q})
    });

    if (!resp.ok) {
      const t = await resp.text();
      throw new Error("HTTP " + resp.status + ": " + t);
    }

    const j = await resp.json();
    status.textContent = "done";

    out.innerHTML =
      `<p><span class="badge">tool_used: ${j.tool_used}</span></p>` +
      (j.generated_command ? `<h4>OCI CLI Command</h4><pre>${escapeHtml(j.generated_command)}</pre>` : "") +
      (j.missing_fields && j.missing_fields.length ? `<h4>Missing Info</h4><pre>${escapeHtml(j.missing_fields.join(", "))}</pre>` : "") +
      `<h4>Reply</h4><pre>${escapeHtml(j.reply || "")}</pre>`;

  } catch (e) {
    status.textContent = "error";
    out.innerHTML = `<h4>Error</h4><pre>${escapeHtml(String(e))}</pre>`;
  } finally {
    btn.disabled = false;
  }
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Ctrl+Enter 发送
document.getElementById("q").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") ask();
});
</script>
"""


if __name__ == "__main__":
    # debug=True 仅用于本地开发演示；
    app.run(debug=True, port=5000)
