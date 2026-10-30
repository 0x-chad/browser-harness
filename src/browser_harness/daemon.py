"""CDP WS holder + IPC relay (Unix socket on POSIX, TCP loopback on Windows). One daemon per BU_NAME."""
import asyncio, base64, json, os, secrets, socket, sys, time, urllib.error, urllib.request
from urllib.parse import urlparse
from collections import deque
from pathlib import Path

from . import _ipc as ipc
from cdp_use.client import CDPClient
from rebrowser_playwright.async_api import async_playwright


# Rebrowser's default context-discovery mode avoids Runtime.enable while still
# preserving main-world, iframe, worker, console, and page-error support.
os.environ.setdefault("REBROWSER_PATCHES_RUNTIME_FIX_MODE", "addBinding")


def _load_env():
    repo_root = Path(__file__).resolve().parents[2]
    workspace = Path(os.environ.get("BH_AGENT_WORKSPACE", repo_root / "agent-workspace")).expanduser()
    for p in (repo_root / ".env", workspace / ".env"):
        if not p.exists():
            continue
        _load_env_file(p)


def _load_env_file(p):
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

NAME = os.environ.get("BU_NAME", "default")
SOCK = ipc.sock_addr(NAME)
LOG = str(ipc.log_path(NAME))
PID = str(ipc.pid_path(NAME))
BUF = 500
PROFILES = [
    Path.home() / "Library/Application Support/Google/Chrome",
    Path.home() / "Library/Application Support/Google/Chrome Canary",
    Path.home() / "Library/Application Support/Comet",
    Path.home() / "Library/Application Support/Arc/User Data",
    Path.home() / "Library/Application Support/Dia/User Data",
    Path.home() / "Library/Application Support/Microsoft Edge",
    Path.home() / "Library/Application Support/Microsoft Edge Beta",
    Path.home() / "Library/Application Support/Microsoft Edge Dev",
    Path.home() / "Library/Application Support/Microsoft Edge Canary",
    Path.home() / "Library/Application Support/BraveSoftware/Brave-Browser",
    Path.home() / ".config/google-chrome",
    Path.home() / ".config/chromium",
    Path.home() / ".config/chromium-browser",
    Path.home() / ".config/microsoft-edge",
    Path.home() / ".config/microsoft-edge-beta",
    Path.home() / ".config/microsoft-edge-dev",
    Path.home() / ".var/app/org.chromium.Chromium/config/chromium",
    Path.home() / ".var/app/com.google.Chrome/config/google-chrome",
    Path.home() / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
    Path.home() / ".var/app/com.microsoft.Edge/config/microsoft-edge",
    Path.home() / "AppData/Local/Google/Chrome/User Data",
    Path.home() / "AppData/Local/Google/Chrome SxS/User Data",
    Path.home() / "AppData/Local/Chromium/User Data",
    Path.home() / "AppData/Local/Microsoft/Edge/User Data",
    Path.home() / "AppData/Local/Microsoft/Edge Beta/User Data",
    Path.home() / "AppData/Local/Microsoft/Edge Dev/User Data",
    Path.home() / "AppData/Local/Microsoft/Edge SxS/User Data",
    Path.home() / "AppData/Local/BraveSoftware/Brave-Browser/User Data",
]
INTERNAL = ("chrome://", "chrome-untrusted://", "devtools://", "chrome-extension://", "about:")
BU_API = "https://api.browser-use.com/api/v3"
REMOTE_ID = os.environ.get("BU_BROWSER_ID")
API_KEY = os.environ.get("BROWSER_USE_API_KEY")


def log(msg):
    open(LOG, "a").write(f"{msg}\n")


async def _silent(coro):
    try:
        await coro
    except Exception:
        pass


def _ws_from_devtools_active_port(http_url: str) -> str | None:
    """When /json/version returns 404 (Chrome 147+ default profile), match DevToolsActivePort by port."""
    p = urlparse(http_url)
    want_port = str(p.port) if p.port else ""
    if not want_port:
        return None
    host = p.hostname or "127.0.0.1"
    if ":" in host:  # urlparse strips IPv6 brackets; restore them for the ws:// URL
        host = f"[{host}]"
    for base in PROFILES:
        try:
            active = (base / "DevToolsActivePort").read_text().splitlines()
        except (FileNotFoundError, NotADirectoryError):
            continue
        port = active[0].strip() if active else ""
        ws_path = active[1].strip() if len(active) > 1 else ""
        if port == want_port and ws_path:
            return f"ws://{host}:{port}{ws_path}"
    return None


def get_ws_url():
    if url := os.environ.get("BU_CDP_WS"):
        return url
    if url := os.environ.get("BU_CDP_URL"):
        # HTTP DevTools endpoint (e.g. http://127.0.0.1:9333) — resolve to ws via /json/version.
        # Use this for a dedicated automation Chrome on a non-default profile, which avoids the
        # M144 "Allow remote debugging" dialog and the M136 default-profile lockdown.
        deadline = time.time() + 30
        last_err = None
        base_url = url.rstrip("/")
        while time.time() < deadline:
            try:
                return json.loads(urllib.request.urlopen(f"{base_url}/json/version", timeout=5).read())["webSocketDebuggerUrl"]
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 404 and (ws := _ws_from_devtools_active_port(url)):
                    return ws
                time.sleep(1)
            except Exception as e:
                last_err = e
                time.sleep(1)
        raise RuntimeError(f"BU_CDP_URL={url} unreachable after 30s: {last_err} -- is the dedicated automation Chrome running?")
    for base in PROFILES:
        try:
            active = (base / "DevToolsActivePort").read_text().splitlines()
        except (FileNotFoundError, NotADirectoryError):
            continue
        port = active[0].strip() if active else ""
        ws_path = active[1].strip() if len(active) > 1 else ""
        if not port:
            continue
        # Resolve the live WS URL via /json/version instead of trusting the path stored
        # alongside the port in DevToolsActivePort: if Chrome was previously launched
        # with a different --user-data-dir on the same port, that file is left behind
        # with a stale browser UUID and the WS upgrade returns 404.
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read())["webSocketDebuggerUrl"]
            except urllib.error.HTTPError as e:
                # Chrome 147+ disables /json/* HTTP discovery on the default user-data-dir;
                # the ws path Chrome wrote to DevToolsActivePort still works.
                if e.code == 404 and ws_path:
                    return f"ws://127.0.0.1:{port}{ws_path}"
                time.sleep(1)
            except (OSError, KeyError, ValueError):
                time.sleep(1)
        raise RuntimeError(
            f"Chrome's remote-debugging page is open, but DevTools is not live yet on 127.0.0.1:{port} — if Chrome opened a profile picker, choose your normal profile first, then tick the checkbox and click Allow if shown"
        )
    for probe_port in (9222, 9223):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{probe_port}/json/version", timeout=1) as r:
                return json.loads(r.read())["webSocketDebuggerUrl"]
        except (OSError, KeyError, ValueError):
            continue
    raise RuntimeError(f"DevToolsActivePort not found in {[str(p) for p in PROFILES]} — enable chrome://inspect/#remote-debugging, or set BU_CDP_WS for a remote browser")


def stop_remote():
    if not REMOTE_ID or not API_KEY: return
    try:
        req = urllib.request.Request(
            f"{BU_API}/browsers/{REMOTE_ID}",
            data=json.dumps({"action": "stop"}).encode(),
            method="PATCH",
            headers={"X-Browser-Use-API-Key": API_KEY, "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15).read()
        log(f"stopped remote browser {REMOTE_ID}")
    except Exception as e:
        log(f"stop_remote failed ({REMOTE_ID}): {e}")


def is_real_page(t):
    return t["type"] == "page" and not t.get("url", "").startswith(INTERNAL)


class Daemon:
    def __init__(self):
        self.cdp = None
        self.playwright = None
        self.browser = None
        self.page = None
        self.page_targets = {}
        self.bound_pages = set()
        self.telemetry_binding = f"__bh_{secrets.token_hex(8)}"
        self.session = None
        self.target_id = None
        self.events = deque(maxlen=BUF)
        self.dialog = None
        self.stop = None  # asyncio.Event, set inside start()

    async def attach_first_page(self):
        """Attach to a real page (or any page). Sets self.session. Returns attached target or None."""
        targets = (await self.cdp.send_raw("Target.getTargets"))["targetInfos"]
        pages = [t for t in targets if is_real_page(t)]
        if not pages:
            # No real pages — create one instead of attaching to omnibox popup
            tid = (await self.cdp.send_raw("Target.createTarget", {"url": "about:blank"}))["targetId"]
            log(f"no real pages found, created about:blank ({tid})")
            pages = [{"targetId": tid, "url": "about:blank", "type": "page"}]
        self.session = (await self.cdp.send_raw(
            "Target.attachToTarget", {"targetId": pages[0]["targetId"], "flatten": True}
        ))["sessionId"]
        self.target_id = pages[0]["targetId"]
        log(f"attached {pages[0]['targetId']} ({pages[0].get('url','')[:80]}) session={self.session}")
        await self._enable_default_domains(self.session)
        return pages[0]

    async def _enable_default_domains(self, session_id):
        """Enable only the raw CDP domains needed by legacy helpers.

        Used by both initial attach and set_session (called after switch_tab/
        new_tab). Without this, helpers that depend on Network.* events —
        notably wait_for_network_idle() — silently stop receiving events
        after a tab switch, because each fresh CDP session starts with all
        domains disabled.

        Runs the four enables in parallel via gather so the worst-case time is
        bounded by a single CDP round trip rather than four sequential ones —
        important on the set_session path, where the helper's IPC socket has
        a 5s read timeout.
        """
        async def enable_one(d):
            try:
                await asyncio.wait_for(
                    self.cdp.send_raw(f"{d}.enable", session_id=session_id),
                    timeout=4,
                )
            except Exception as e:
                log(f"enable {d} on {session_id}: {e}")
        # Never enable Runtime here. Rebrowser Playwright owns execution-context
        # discovery and evaluation specifically to avoid Runtime.enable's
        # detectable side effects.
        await asyncio.gather(*(enable_one(d) for d in ("Page", "DOM", "Network")))

    def _emit_playwright_event(self, method, params, page=None):
        target_id = next((tid for tid, candidate in self.page_targets.items() if candidate is page), None)
        self.events.append({
            "method": method,
            "params": params,
            "session_id": self.session if page is self.page else None,
            "target_id": target_id,
        })

    def _bind_page_events(self, page):
        if page in self.bound_pages:
            return
        self.bound_pages.add(page)
        page.on("console", lambda msg: self._emit_playwright_event(
            "Playwright.console",
            {"type": msg.type, "text": msg.text, "location": msg.location},
            page,
        ))
        page.on("pageerror", lambda error: self._emit_playwright_event(
            "Playwright.pageError",
            {"message": str(error), "name": getattr(error, "name", "Error"), "stack": getattr(error, "stack", None)},
            page,
        ))
        page.on("worker", lambda worker: self._emit_playwright_event(
            "Playwright.worker", {"url": worker.url}, page
        ))
        page.on("frameattached", lambda frame: self._emit_playwright_event(
            "Playwright.frameAttached", {"url": frame.url, "name": frame.name}, page
        ))
        page.on("framenavigated", lambda frame: self._emit_playwright_event(
            "Playwright.frameNavigated", {"url": frame.url, "name": frame.name}, page
        ))
        page.on("framedetached", lambda frame: self._emit_playwright_event(
            "Playwright.frameDetached", {"url": frame.url, "name": frame.name}, page
        ))

    async def _install_context_telemetry(self, context):
        """Preserve console/page-error events without Runtime.enable.

        Chromium only emits Runtime.consoleAPICalled/exceptionThrown after
        Runtime.enable, so Rebrowser cannot surface those native events in its
        stealth mode. A short init script forwards the same information through
        Runtime.addBinding, which is the patched transport Rebrowser already
        uses for execution-context discovery.
        """
        def receive(source, payload):
            page = source.get("page") if isinstance(source, dict) else None
            method = payload.get("method") if isinstance(payload, dict) else None
            params = payload.get("params", {}) if isinstance(payload, dict) else {}
            if method in ("Playwright.console", "Playwright.pageError"):
                self._emit_playwright_event(method, params, page)

        await context.expose_binding(self.telemetry_binding, receive)
        binding = json.dumps(self.telemetry_binding)
        script = f"""(() => {{
          const emit = globalThis[{binding}];
          try {{ delete globalThis[{binding}]; }} catch (_) {{}}
          if (!emit) return;
          const text = value => {{
            try {{ return typeof value === 'string' ? value : JSON.stringify(value); }}
            catch (_) {{ return String(value); }}
          }};
          for (const type of ['log','debug','info','warn','error','assert']) {{
            const original = console[type];
            if (typeof original !== 'function') continue;
            console[type] = new Proxy(original, {{
              apply(target, thisArg, args) {{
                try {{ void emit({{method:'Playwright.console',params:{{type,text:args.map(text).join(' '),location:{{url:location.href}}}}}}); }} catch (_) {{}}
                return Reflect.apply(target, thisArg, args);
              }}
            }});
          }}
          addEventListener('error', event => {{
            try {{ void emit({{method:'Playwright.pageError',params:{{message:event.message || text(event.error),name:event.error?.name || 'Error',stack:event.error?.stack || null}}}}); }} catch (_) {{}}
          }}, true);
          addEventListener('unhandledrejection', event => {{
            const reason = event.reason;
            try {{ void emit({{method:'Playwright.pageError',params:{{message:reason?.message || text(reason),name:reason?.name || 'UnhandledRejection',stack:reason?.stack || null}}}}); }} catch (_) {{}}
          }});
        }})()"""
        await context.add_init_script(script)
        for page in context.pages:
            try:
                # Rebrowser installs exposed bindings on future documents. Add
                # the JS-side binding wrapper to an already-open document too.
                await page.evaluate(
                    f"globalThis.__playwright__binding__controller__?.addBinding({binding}, false)"
                )
                await page.evaluate(script)
            except Exception:
                pass

    async def _target_id_for_page(self, page):
        session = await page.context.new_cdp_session(page)
        try:
            info = await session.send("Target.getTargetInfo")
            return info["targetInfo"]["targetId"]
        finally:
            await session.detach()

    async def _register_page(self, page):
        self._bind_page_events(page)
        try:
            self.page_targets[await self._target_id_for_page(page)] = page
        except Exception as e:
            log(f"map Playwright page target: {e}")

    async def _refresh_pages(self):
        if not self.browser:
            return
        live_pages = {page for context in self.browser.contexts for page in context.pages}
        self.page_targets = {tid: page for tid, page in self.page_targets.items() if page in live_pages}
        await asyncio.gather(*(self._register_page(page) for page in live_pages))

    async def _select_page(self, target_id):
        if not self.browser:
            return None
        await self._refresh_pages()
        self.page = self.page_targets.get(target_id)
        if self.page:
            await self.page.bring_to_front()
        return self.page

    async def _frame_for_target(self, target_id):
        if not target_id:
            return self.page
        info = (await self.cdp.send_raw("Target.getTargetInfo", {"targetId": target_id}))["targetInfo"]
        url = info.get("url", "")
        if self.page:
            for frame in self.page.frames:
                if frame.url == url:
                    return frame
        raise RuntimeError(f"Playwright frame not found for target {target_id}")

    async def _playwright_action(self, action, params):
        if not self.page:
            await self._select_page(self.target_id)
        if not self.page:
            raise RuntimeError("Playwright is not attached to the active page")
        if action == "evaluate":
            frame = await self._frame_for_target(params.get("target_id"))
            return await frame.evaluate(params["expression"])
        if action == "goto":
            response = await self.page.goto(params["url"], wait_until="commit")
            return {"url": self.page.url, "status": response.status if response else None}
        if action == "mouse_click":
            await self.page.mouse.click(params["x"], params["y"], button=params.get("button", "left"), click_count=params.get("clicks", 1))
            return None
        if action == "insert_text":
            await self.page.keyboard.insert_text(params["text"])
            return None
        if action == "press_key":
            modifiers = params.get("modifiers", 0)
            names = [(1, "Alt"), (2, "Control"), (4, "Meta"), (8, "Shift")]
            chord = "+".join([name for bit, name in names if modifiers & bit] + [params["key"]])
            await self.page.keyboard.press(chord)
            return None
        if action == "scroll":
            await self.page.mouse.move(params["x"], params["y"])
            await self.page.mouse.wheel(params.get("dx", 0), params.get("dy", -300))
            return None
        if action == "screenshot":
            data = await self.page.screenshot(full_page=params.get("full", False))
            return base64.b64encode(data).decode()
        if action == "upload":
            await self.page.locator(params["selector"]).set_input_files(params["files"])
            return None
        raise RuntimeError(f"unknown Playwright action: {action}")

    async def start(self):
        self.stop = asyncio.Event()
        url = get_ws_url()
        parsed_url = urlparse(url)
        safe_url = parsed_url._replace(query="<redacted>").geturl() if parsed_url.query else url
        log(f"connecting to {safe_url}")
        self.cdp = CDPClient(url)
        try:
            await self.cdp.start()
        except Exception as e:
            if os.environ.get("BU_CDP_WS"):
                raise RuntimeError(
                    f"CDP WS handshake failed: {e} -- remote browser WebSocket connection failed. "
                    "This can happen when network policy blocks the connection, the WS URL is wrong or expired, or the remote endpoint is down. "
                    "If you use Browser Use cloud, verify BROWSER_USE_API_KEY and get a fresh URL via start_remote_daemon()."
                )
            raise RuntimeError(f"CDP WS handshake failed: {e} -- click Allow in Chrome if prompted, then retry")
        await self.attach_first_page()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(url)
        for context in self.browser.contexts:
            await self._install_context_telemetry(context)
            context.on("page", lambda page: asyncio.create_task(self._register_page(page)))
        await self._select_page(self.target_id)
        orig = self.cdp._event_registry.handle_event
        async def tap(method, params, session_id=None):
            self.events.append({"method": method, "params": params, "session_id": session_id})
            if method == "Page.javascriptDialogOpening":
                self.dialog = params
            elif method == "Page.javascriptDialogClosed":
                self.dialog = None
            elif method in ("Page.loadEventFired", "Page.domContentEventFired"):
                asyncio.create_task(_silent(asyncio.wait_for(self._playwright_action("evaluate", {
                    "expression": "if(!document.title.startsWith('\U0001F434'))document.title='\U0001F434 '+document.title"
                }), timeout=2)))
            return await orig(method, params, session_id)
        self.cdp._event_registry.handle_event = tap

    async def close(self):
        # Stop the Playwright driver/transport without closing the externally
        # owned browser. browser-pool remains responsible for browser lifetime.
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def handle(self, req):
        # Token guard for Windows TCP loopback: any local process can otherwise
        # connect and issue CDP commands. expected_token() is None on POSIX so
        # this check is a no-op there (AF_UNIX + chmod 600 is the boundary).
        expected = ipc.expected_token()
        if expected is not None and req.get("token") != expected:
            return {"error": "unauthorized"}
        meta = req.get("meta")
        # Liveness probe — lets clients confirm the listener is actually this
        # daemon and not an unrelated process that reused our port post-crash.
        # `pid` lets restart_daemon() verify the live daemon's identity before
        # signaling — protects against SIGTERM-by-stale-pid-file after PID reuse.
        if meta == "ping":        return {"pong": True, "pid": os.getpid()}
        if meta == "drain_events":
            out = list(self.events); self.events.clear()
            return {"events": out}
        if meta == "session":     return {"session_id": self.session}
        if meta == "playwright":
            try:
                return {"result": await self._playwright_action(req["action"], req.get("params") or {})}
            except Exception as e:
                return {"error": str(e)}
        if meta == "current_tab":
            # Resolve the attached page's target info server-side. Helpers can't
            # send Target.getTargetInfo themselves: daemon strips session_id for
            # any Target.* method (browser-level call), and without a targetId
            # Chrome silently returns the *browser* target.
            if not self.target_id:
                return {"error": "not_attached"}
            try:
                info = (await self.cdp.send_raw("Target.getTargetInfo", {"targetId": self.target_id}))["targetInfo"]
            except Exception:
                return {"error": "cdp_disconnected"}
            return {"targetId": info.get("targetId"), "url": info.get("url", ""), "title": info.get("title", "")}
        if meta == "connection_status":
            if not self.target_id:
                return {"error": "not_attached"}
            try:
                info = (await self.cdp.send_raw("Target.getTargetInfo", {"targetId": self.target_id}))["targetInfo"]
            except Exception:
                return {"error": "cdp_disconnected"}
            page = None
            if is_real_page(info):
                page = {
                    "targetId": info.get("targetId"),
                    "title": info.get("title") or "(untitled)",
                    "url": info.get("url") or "",
                }
            return {"target_id": self.target_id, "session_id": self.session, "page": page}
        if meta == "set_session":
            old_session = self.session
            self.session = req.get("session_id")
            self.target_id = req.get("target_id") or self.target_id
            await self._select_page(self.target_id)
            # Run the old-session Network.disable (defense in depth — keeps
            # background-tab traffic out of the global event buffer; the
            # consumer-side filter in wait_for_network_idle is the actual
            # correctness gate) in parallel with the four enables on the new
            # session. Different sessions, independent CDP requests. Keeps
            # the synchronous reply under the helper's 5s IPC read timeout
            # even on a remote daemon — sequentially these would have stacked
            # to ~22s worst case.
            tasks = []
            if old_session and old_session != self.session:
                async def disable_old():
                    try:
                        await asyncio.wait_for(
                            self.cdp.send_raw("Network.disable", session_id=old_session),
                            timeout=2,
                        )
                    except Exception: pass
                tasks.append(disable_old())
            tasks.append(self._enable_default_domains(self.session))
            await asyncio.gather(*tasks)
            # 🐴 tab-marker title prefix is purely cosmetic — fire-and-forget so
            # it doesn't add to the synchronous IPC budget.
            asyncio.create_task(_silent(asyncio.wait_for(self._playwright_action("evaluate", {
                "expression": "if(!document.title.startsWith('\U0001F434'))document.title='\U0001F434 '+document.title"
            }), timeout=2)))
            return {"session_id": self.session}
        if meta == "pending_dialog": return {"dialog": self.dialog}
        if meta == "shutdown":    self.stop.set(); return {"ok": True}

        method = req["method"]
        params = req.get("params") or {}
        # Browser-level Target.* calls must not use a session (stale or otherwise).
        # For everything else, explicit session in req wins; else default.
        sid = None if method.startswith("Target.") else (req.get("session_id") or self.session)
        try:
            return {"result": await self.cdp.send_raw(method, params, session_id=sid)}
        except Exception as e:
            msg = str(e)
            if "Session with given id not found" in msg and sid == self.session and sid:
                log(f"stale session {sid}, re-attaching")
                if await self.attach_first_page():
                    return {"result": await self.cdp.send_raw(method, params, session_id=self.session)}
            return {"error": msg}


async def serve(d):
    async def handler(reader, writer):
        try:
            line = await reader.readline()
            if not line: return
            resp = await d.handle(json.loads(line))
            writer.write((json.dumps(resp, default=str) + "\n").encode())
            await writer.drain()
        except Exception as e:
            log(f"conn: {e}")
            try:
                writer.write((json.dumps({"error": str(e)}) + "\n").encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()

    serve_task = asyncio.create_task(ipc.serve(NAME, handler))
    stop_task = asyncio.create_task(d.stop.wait())
    await asyncio.sleep(0.05)  # let serve() bind so sock_addr() resolves to the live endpoint
    log(f"listening on {ipc.sock_addr(NAME)} (name={NAME}, remote={REMOTE_ID or 'local'})")
    try:
        await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if serve_task.done(): await serve_task  # surfaces a serve crash
    finally:
        for t in (serve_task, stop_task):
            t.cancel()
            try: await t
            except (asyncio.CancelledError, Exception): pass
        ipc.cleanup_endpoint(NAME)


async def main():
    d = Daemon()
    try:
        await d.start()
        await serve(d)
    finally:
        await d.close()


def already_running():
    # Ping handshake (not a bare connect) so a stale .port file + port reuse
    # after a daemon crash doesn't make us mistake an unrelated listener for ours.
    return ipc.ping(NAME, timeout=1.0)


if __name__ == "__main__":
    if already_running():
        print(f"daemon already running on {SOCK}", file=sys.stderr)
        sys.exit(0)
    open(LOG, "w").close()
    open(PID, "w").write(str(os.getpid()))
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"fatal: {e}")
        sys.exit(1)
    finally:
        stop_remote()
        try: os.unlink(PID)
        except FileNotFoundError: pass
