from __future__ import annotations

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

from story_agent_observability import (
    load_agent_events,
    load_notifications,
    read_ndjson,
    recovery_log_path,
    redact_text,
)


SnapshotProvider = Callable[[], dict[str, Any]]


def render_dashboard_html(*, poll_seconds: float = 2.0) -> str:
    poll_ms = max(500, int(poll_seconds * 1000))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Story Agent 实时看板</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0a0f18; --panel:#121a27; --line:#26334a;
      --text:#eef4ff; --muted:#97a7bd; --accent:#62d0ff; --ok:#4ee1a0;
      --warn:#ffc766; --bad:#ff6e7a; --stale:#c895ff; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:radial-gradient(circle at 15% 0,#17283d 0,var(--bg) 45%);
      color:var(--text); font:14px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ width:min(1500px,96vw); margin:0 auto; padding:24px 0 48px; }}
    header {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-end; margin-bottom:18px; }}
    h1 {{ margin:0; font-size:26px; letter-spacing:.02em; }}
    h2 {{ font-size:15px; margin:0 0 12px; color:#dbe8fb; }}
    .muted {{ color:var(--muted); }}
    .grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }}
    .panel {{ background:color-mix(in srgb,var(--panel) 94%,transparent); border:1px solid var(--line);
      border-radius:14px; padding:14px; box-shadow:0 14px 40px #0005; overflow:hidden; }}
    .metric {{ font-size:22px; font-weight:750; margin-top:4px; overflow-wrap:anywhere; }}
    .wide {{ grid-column:1/-1; }}
    .half {{ grid-column:span 2; }}
    .banner {{ margin:12px 0; border-left:4px solid var(--warn); }}
    table {{ border-collapse:collapse; width:100%; font-size:12px; }}
    th,td {{ padding:8px 7px; border-bottom:1px solid #213047; text-align:left; vertical-align:top; }}
    th {{ color:var(--muted); position:sticky; top:0; background:var(--panel); }}
    .scroll {{ max-height:520px; overflow:auto; }}
    .badge {{ display:inline-block; padding:2px 7px; border-radius:999px; border:1px solid currentColor;
      font-size:11px; text-transform:uppercase; }}
    .passed,.completed {{ color:var(--ok); }} .running,.reviewing {{ color:var(--accent); }}
    .blocked,.warning,.waiting_for_user {{ color:var(--warn); }}
    .failed,.critical,.terminal_bug {{ color:var(--bad); }} .stale,.retrying {{ color:var(--stale); }}
    .pending,.cancelled {{ color:var(--muted); }}
    progress {{ width:100%; height:8px; accent-color:var(--accent); }}
    .timeline {{ display:grid; gap:8px; }}
    .item {{ border-left:2px solid var(--line); padding:5px 9px; background:#0d1420; border-radius:0 8px 8px 0; }}
    code {{ color:#c7eaff; word-break:break-all; }}
    a {{ color:#8edcff; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
    .error {{ white-space:pre-wrap; color:var(--bad); }}
    button {{ border:1px solid var(--accent); border-radius:10px; padding:9px 13px;
      color:var(--text); background:#173049; cursor:pointer; font-weight:700; }}
    button:hover {{ background:#214767; }} button:disabled {{ opacity:.45; cursor:wait; }}
    @media (max-width:900px) {{ .grid {{ grid-template-columns:1fr 1fr; }} .half {{ grid-column:1/-1; }} }}
    @media (max-width:560px) {{ .grid {{ grid-template-columns:1fr; }} .wide,.half {{ grid-column:1; }} }}
  </style>
</head>
<body>
<main>
  <header><div><h1>Story Agent 实时看板</h1><div id="project" class="muted"></div></div>
    <div><button id="acceptCurrent" type="button">接受当前版本</button>
      <div class="muted"><span id="updated">等待数据</span></div></div></header>
  <section id="error" class="panel error" hidden></section>
  <section class="grid">
    <article class="panel"><div class="muted">Agent</div><div id="agent" class="metric">—</div><div id="stage"></div></article>
    <article class="panel"><div class="muted">Supervisor</div><div id="supervisor" class="metric">—</div><div id="heartbeat"></div></article>
    <article class="panel"><div class="muted">预算</div><div id="budget" class="metric">—</div><progress id="budgetBar" max="100" value="0"></progress></article>
    <article class="panel"><div class="muted">剩余关键路径</div><div id="eta" class="metric">—</div><div class="muted">外部排队不计入</div></article>
    <article id="banner" class="panel wide banner" hidden></article>
    <article class="panel wide"><h2>运行原因与交付状态</h2><div id="runReason" class="timeline"></div></article>
    <article class="panel wide"><h2>精确产物进度</h2><div id="progress" class="grid"></div></article>
    <article class="panel wide"><h2>DAG 与 attempt</h2><div class="scroll"><table>
      <thead><tr><th>分支</th><th>阶段</th><th>依赖</th><th>有效状态</th><th>attempt</th><th>耗时/ETA</th><th>provider / receipt</th><th>成本</th><th>原因 / 重跑范围 / 产物</th></tr></thead>
      <tbody id="stages"></tbody></table></div></article>
    <article class="panel half"><h2>通知</h2><div id="notifications" class="timeline scroll"></div></article>
    <article class="panel half"><h2>事件时间线</h2><div id="events" class="timeline scroll"></div></article>
    <article class="panel wide"><h2>日志与证据</h2><div id="evidence" class="timeline"></div></article>
  </section>
</main>
<script>
const esc = value => String(value == null ? "" : value).replace(/[&<>"']/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}})[ch]);
const badge = value => '<span class="badge '+esc(value)+'">'+esc(value || "pending")+'</span>';
const artifact = path => path ? '<a href="/artifact?path='+encodeURIComponent(path)+'" target="_blank"><code>'+esc(path)+'</code></a>' : "—";
function metricCard(label, data) {{
  const actual = Number(data.actual || 0), expected = Number(data.expected || 0);
  const ratio = expected > 0 ? Math.min(100, actual / expected * 100) : 0;
  const detail = String(data.detail || "");
  return '<article class="panel"><div class="muted">'+esc(label)+'</div><div class="metric">'+actual+' / '+expected+
    '</div><progress max="100" value="'+ratio+'"></progress><div class="muted">'+
    (detail.startsWith("/") ? artifact(detail) : esc(detail))+'</div></article>';
}}
function render(snapshot) {{
  document.getElementById("project").textContent = snapshot.project_dir || "";
  document.getElementById("updated").textContent = new Date().toLocaleTimeString();
  document.getElementById("agent").innerHTML = badge(snapshot.agent_status);
  document.getElementById("stage").textContent = "当前/下一阶段：" + (snapshot.current_stage || snapshot.next_stage || "—");
  const sup = snapshot.supervisor || {{}};
  document.getElementById("supervisor").innerHTML = badge(sup.effective_status || (sup.running ? "running" : "stopped"));
  const lock = ((snapshot.locks || {{}}).job_lock || {{}});
  let supervisorDetail = "PID " + (sup.pid || "—") + " · heartbeat " +
    (sup.heartbeat_age_seconds == null ? "—" : Math.round(sup.heartbeat_age_seconds) + " 秒前")+
    " · job lock " + (lock.owner_alive ? "PID "+(lock.pid || "?") : "free");
  if (sup.backoff_remaining_seconds != null) supervisorDetail += " · 退避剩余 "+sup.backoff_remaining_seconds+"秒";
  document.getElementById("heartbeat").textContent = supervisorDetail;
  const budget = snapshot.budget || {{}}, spent = Number(budget.spent || 0), hard = Number(budget.hard_limit || 0);
  document.getElementById("budget").textContent = "¥" + spent.toFixed(2) + " / ¥" + hard.toFixed(2);
  document.getElementById("budgetBar").value = hard > 0 ? Math.min(100, spent / hard * 100) : 0;
  document.getElementById("eta").textContent = ((snapshot.estimated_remaining_minutes || {{}}).nominal || 0) + " 分钟";
  const timing = snapshot.timing || {{}}, delivery = snapshot.delivery_state || "";
  const usage = snapshot.cost_and_usage || {{}};
  document.getElementById("runReason").innerHTML = [
    '<div class="item"><strong>为什么正在运行</strong><div>'+esc(snapshot.why_running || snapshot.blocked_reason || "等待下一阶段")+'</div></div>',
    '<div class="item"><strong>8 小时时限</strong><div>已用 '+esc(timing.active_elapsed_seconds == null ? "—" : Math.round(timing.active_elapsed_seconds)+"秒")+
      ' · 剩余 '+esc(timing.remaining_deadline_hours == null ? "—" : timing.remaining_deadline_hours+"小时")+'</div></div>',
    '<div class="item"><strong>交付</strong><div>'+esc(delivery || snapshot.agent_status || "—")+'</div></div>',
    '<div class="item"><strong>模型/成本</strong><div>Codex '+esc(usage.codex_calls || 0)+' 次 · '+esc(usage.codex_total_tokens_reported || 0)+
      ' tokens · 供应商费用 ¥'+Number(usage.provider_cost_cny || 0).toFixed(2)+'</div></div>',
    '<div class="item"><strong>渲染与未上报费用</strong><div>正式编码 '+esc(usage.formal_encode_time_seconds || 0)+' 秒 · 总渲染 '+
      esc(usage.render_time_seconds || 0)+' 秒 · ImageGen '+esc(usage.imagegen_cost_status || "未上报")+
      ' · 音乐 '+esc(usage.music_cost_status || "未上报")+'</div></div>'
  ].join("");
  const recovery = snapshot.recovery || {{}}, banner = document.getElementById("banner");
  if (snapshot.blocked_reason || recovery.action) {{
    banner.hidden = false;
    banner.innerHTML = '<strong>'+esc(recovery.action || snapshot.agent_status)+'</strong> · '+
      esc(snapshot.blocked_reason || recovery.reason || "")+
      (recovery.retry_at ? '<div>下次尝试：'+esc(recovery.retry_at)+'</div>' : "")+
      (recovery.required_action ? '<div>需要操作：'+esc(recovery.required_action)+'</div>' : "");
  }} else banner.hidden = true;
  const progress = snapshot.artifact_progress || {{}};
  document.getElementById("progress").innerHTML = Object.entries(progress).map(entry => metricCard(entry[0], entry[1])).join("");
  const rows = snapshot.stage_rows || [];
  const dependencies = {{}};
  (((snapshot.dag || {{}}).edges) || []).forEach(edge => (dependencies[edge.to] ||= []).push(edge.from));
  document.getElementById("stages").innerHTML = rows.map(row => '<tr><td>'+esc(row.branch)+'</td><td>'+esc(row.stage)+
    '</td><td>'+esc((dependencies[row.stage] || []).join(", ") || "—")+'</td><td>'+badge(row.effective_status)+'</td><td>'+esc(row.attempts)+' (I'+esc(row.infrastructure_attempts || 0)+'/Q'+esc(row.quality_attempts || 0)+')</td><td>'+esc(row.duration_seconds == null ? "—" : row.duration_seconds+"s")+
    ' / '+esc(row.estimated_remaining_seconds == null ? "—" : row.estimated_remaining_seconds+"s")+
    '</td><td>'+esc(row.provider || "—")+(row.request_id ? '<br><code>'+esc(row.request_id)+'</code>' : "")+
    '</td><td>¥'+Number(row.actual_cost || 0).toFixed(2)+'</td><td><strong>'+esc(row.why_running || row.message || "")+'</strong>'+
    '<br><span class="muted">retry_scope: '+esc(row.retry_scope || "—")+
    (((row.retry_files || []).length) ? ' · '+esc(row.retry_files.join(", ")) : "")+'</span>'+
    ((row.artifacts || []).length ? '<br>'+artifact(row.artifacts[0]) : "")+'</td></tr>').join("");
  document.getElementById("notifications").innerHTML = (snapshot.notifications || []).slice().reverse().map(item =>
    '<div class="item '+esc(item.severity)+'"><strong>'+esc(item.category)+'</strong> '+badge(item.recovery_mode || item.severity)+
    '<div>'+esc(item.message)+'</div><div class="muted">'+esc(item.created_at)+(item.required_action ? " · "+esc(item.required_action) : "")+'</div></div>').join("") || '<div class="muted">暂无通知</div>';
  document.getElementById("events").innerHTML = (snapshot.events || []).slice().reverse().map(item =>
    '<div class="item"><strong>'+esc(item.stage || item.event_type || item.event)+'</strong> '+badge(item.status)+
    '<div>'+esc(item.summary || item.message || "")+'</div><div class="muted">'+esc(item.timestamp || item.time || "")+'</div></div>').join("") || '<div class="muted">暂无事件</div>';
  const evidence = snapshot.evidence || {{}};
  document.getElementById("evidence").innerHTML = Object.entries(evidence).map(entry =>
    '<div class="item"><strong>'+esc(entry[0])+'</strong><div>'+artifact(entry[1])+'</div></div>').join("") || '<div class="muted">暂无证据路径</div>';
}}
async function refresh() {{
  try {{
    const response = await fetch("/api/status", {{cache:"no-store"}});
    if (!response.ok) throw new Error(await response.text());
    render(await response.json());
    document.getElementById("error").hidden = true;
  }} catch (error) {{
    const target = document.getElementById("error"); target.hidden = false; target.textContent = String(error);
  }}
}}
document.getElementById("acceptCurrent").addEventListener("click", async event => {{
  if (!confirm("接受当前版本将冻结现有有效文件、停止排队中的审美返工并生成轻量交付清单。继续吗？")) return;
  const button = event.currentTarget; button.disabled = true;
  try {{
    const response = await fetch("/api/accept-current", {{method:"POST", headers:{{"Content-Type":"application/json"}}, body:"{{}}"}});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "接受失败");
    alert("已接受当前版本：" + (payload.receipt || ""));
    await refresh();
  }} catch (error) {{ alert(String(error)); }} finally {{ button.disabled = false; }}
}});
refresh(); setInterval(refresh, {poll_ms});
</script>
</body></html>"""


def _path_allowed(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return False
    for root in roots:
        try:
            if os.path.commonpath((str(resolved), str(root))) == str(root):
                return resolved.is_file()
        except ValueError:
            continue
    return False


def build_dashboard_server(
    snapshot_provider: SnapshotProvider,
    *,
    project_dir: Path,
    host: str = "127.0.0.1",
    port: int = 0,
    poll_seconds: float = 2.0,
) -> ThreadingHTTPServer:
    project_root = project_dir.expanduser().resolve()
    roots = (project_root,)
    html = render_dashboard_html(poll_seconds=poll_seconds).encode("utf-8")

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "StoryAgentDashboard/1"

        def _send_security_headers(self, *, dashboard: bool = False) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                (
                    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'none'"
                    if dashboard
                    else "sandbox; default-src 'none'; frame-ancestors 'none'"
                ),
            )

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            try:
                if parsed.path in {"/", "/index.html"}:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.send_header("Cache-Control", "no-store")
                    self._send_security_headers(dashboard=True)
                    self.end_headers()
                    self.wfile.write(html)
                    return
                if parsed.path == "/healthz":
                    self._send_json({"ok": True})
                    return
                if parsed.path == "/api/status":
                    self._send_json(snapshot_provider())
                    return
                if parsed.path == "/api/events":
                    self._send_json(load_agent_events(project_root, limit=500))
                    return
                if parsed.path == "/api/notifications":
                    self._send_json(load_notifications(project_root, limit=200))
                    return
                if parsed.path == "/api/recovery":
                    self._send_json(read_ndjson(recovery_log_path(project_root), limit=100))
                    return
                if parsed.path == "/artifact":
                    raw = parse_qs(parsed.query).get("path", [""])[0]
                    target = Path(raw)
                    if not raw or not _path_allowed(target, roots):
                        self._send_json({"error": "artifact path is missing or outside allowed roots"}, 403)
                        return
                    body = target.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(target.name)}")
                    self._send_security_headers()
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._send_json({"error": "not found"}, 404)
            except Exception as exc:
                self._send_json({"error": redact_text(f"{type(exc).__name__}: {exc}")}, 500)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            try:
                if parsed.path != "/api/accept-current":
                    self._send_json({"error": "not found"}, 404)
                    return
                # This explicit local user action freezes existing files. It
                # never starts final_delivery, doctor, or a production render.
                from story_agent_runtime import accept_current_outputs

                _manifest, receipt = accept_current_outputs(
                    project_root,
                    accepted_by="dashboard_user",
                    notes="用户从 Story Agent Dashboard 接受当前版本",
                )
                self._send_json({"ok": True, "receipt": str(receipt)})
            except Exception as exc:
                self._send_json({"error": redact_text(f"{type(exc).__name__}: {exc}")}, 409)

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    return ThreadingHTTPServer((host, port), DashboardHandler)


def serve_dashboard(
    snapshot_provider: SnapshotProvider,
    *,
    project_dir: Path,
    host: str,
    port: int,
    poll_seconds: float = 2.0,
) -> None:
    server = build_dashboard_server(
        snapshot_provider,
        project_dir=project_dir,
        host=host,
        port=port,
        poll_seconds=poll_seconds,
    )
    actual_host, actual_port = server.server_address[:2]
    print(json.dumps({"dashboard": f"http://{actual_host}:{actual_port}", "project_dir": str(project_dir)}, ensure_ascii=False))
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
