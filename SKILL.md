---
name: browser-harness
description: "Control a real browser through CDP with Rebrowser-patched JavaScript evaluation: clicking, typing, navigation, logged-in sessions, JS-rendered or bot-protected pages. Not for plain HTTP fetches of public content - use curl for those."
---

# browser-harness

Direct browser control via CDP, with Rebrowser Playwright owning execution-context discovery and `js(...)` evaluation so normal mode never enables the Runtime domain. For task-specific edits, use `agent-workspace/agent_helpers.py`.

## When Not to Use

A basic fetch of public information needs no browser. If a plain HTTP request can read it — a public page, an API, docs — use `curl` or your fetch tool, and leave the browser alone. Use browser-harness when the task needs interaction (click, type, navigate), the user's logged-in session, JS rendering, or a bot-protected page. If a direct fetch fails or returns a shell page, then escalate to the browser.

## Usage

```bash
browser-harness <<'PY'
print(page_info())
PY
```

- Invoke as `browser-harness`. Use heredocs for multi-line commands.
- Helpers are pre-imported. `run.py` calls `ensure_daemon()` before `exec`.
- First navigation for a task is `new_tab(url)`, not `goto_url(url)`. The daemon
  preserves the attached tab across separate CLI invocations, so do not call
  `new_tab()` again in every script.
- Keep one working tab per task/site. Before opening another, inspect
  `current_tab()` and `list_tabs()` and use `switch_tab()` to reuse a matching
  tab. Do not leave duplicate tabs on the same URL or close tabs you did not
  create.
- At task completion, close tabs created for the task that are no longer needed.
  Keep a tab open if the user needs to see it, it is needed for a known follow-up,
  or closing it could discard unsaved work or other important state.
- `new_tab()` and `switch_tab()` attach and move the horse marker without
  changing Chrome's visible tab. Screenshots and normal CDP input work in the
  background. Never call `activate_tab(target)` automatically: it brings Chrome
  to the foreground. Call it only when the user explicitly asks to see or
  visibly switch to that tab. Do not pair `switch_tab()` with `activate_tab()`.
- Set `BH_TAB_MARKER=0` before starting the daemon to leave page titles unchanged.
  The horse marker remains enabled by default.
- A timeout or page that pauses while hidden is not permission to foreground
  Chrome. Keep using background CDP operations. For a focus-gated page,
  temporarily call `cdp("Emulation.setFocusEmulationEnabled", enabled=True)`,
  perform and verify the operation, then disable it in a `finally` block. If
  background control still cannot work, report that limitation instead of
  activating the tab. Do not invent a `Runtime.evaluate` scroll replacement or
  a cross-frame JS walker.

## Page Workflow

- Prefer to find elements with the accessibility tree, not screenshots: `cdp("Accessibility.getFullAXTree")["nodes"]` has every element's role, name, and `backendDOMNodeId` — filter in Python before printing (it is thousands of nodes). Coordinates: `q = cdp("DOM.getBoxModel", backendNodeId=n)["model"]["content"]; x, y = sum(q[0::2])/4, sum(q[1::2])/4` (viewport px, ready for `click_at_xy`; negative/oversized means scroll first).
- Clicking: AX node -> box center -> `click_at_xy(x, y)` -> verify with a targeted `js(...)`/`page_info()` check.
- Fall back to raw HTML via `js(...)` only when the AX tree lacks the element (canvas, exotic widgets); screenshot when layout or imagery matters.
- After navigation, call `wait_for_load()`.
- If the current tab is stale or internal, call `ensure_real_tab()`.
- Use `js(...)` for DOM inspection or extraction when coordinates are the wrong tool.
- When entering unusually long text, avoid slow per-character typing: find a faster page-appropriate input method, then verify the page kept the exact value.
- Before submitting a form, verify every entered value and selection against the intended inputs.
- Prefer waits tied to observable state such as an expected URL, text, element, or load state. Use fixed-duration waits only for animations or when no observable condition exists.
- After a rerender, timeout, covered element, popup, or iframe change, re-inspect the current AX/DOM, tab, and frame state before acting again. Resolve visible blockers instead of forcing a click, and do not blindly retry stale targets.
- Browser-native passkey/WebAuthn choosers are outside the page DOM and are not JavaScript dialogs. Clearcote may leave one pending like a real unanswered prompt; CDP `Escape` may not dismiss it, and it can intercept coordinate input while the page remains scriptable. Count an authentication alert only when it is rendered (`checkVisibility()` and a nonzero layout box) or corroborated by the response/page state. If the site visibly offers password fallback, the verified password remains populated, and one coordinate submit produces no login request, navigation, or visible error, invoke that same validated submit control once with DOM `click()` or `form.requestSubmit(control)`, then inspect the response. Never loop credential submissions.
- Raw CDP is available with `cdp("Domain.method", ...)`.
  Pass CDP parameters as keywords: `cdp("Input.insertText", text="hello")`.
  The second positional argument is a session ID, not a parameters dictionary.
  When targeting an explicit session, use `session_id="..."` alongside the keywords.
- Do not call `Runtime.enable` in normal mode. `js(...)` uses Rebrowser's
  execution-context path; raw input, navigation, screenshots and the `cdp()`
  escape hatch retain Browser Harness's direct-CDP behavior.
### Iframes and embedded surfaces

Rendered pages can combine several embedding models that look identical on screen but require different control paths:

- **Same-origin iframe:** inspect and interact through `contentDocument` / `contentWindow`.
- **Cross-origin iframe:** the parent can expose the iframe element and box but not its inner DOM.
- **Out-of-process iframe (OOPIF):** Chrome may expose the frame as a separate CDP target; use `iframe_target()` and `js(..., target_id=...)` when the target is available.
- **Nested iframe tree:** the visible control may be multiple frames below the outer iframe. Recursively inspect frames and verify which frame owns the rendered control.
- **Isolated component frames:** one visible widget may split fields or controls across separate nested frames. An outer frame reporting zero inputs does not prove the visible controls are unavailable.
- **Popup-producing frame:** an action inside a frame may open a new page target while leaving the original page unchanged.
- **Canvas or compositor-only surface:** a visible control may be absent from DOM and AX trees entirely.

Use this process progressively, expanding inspection only when simpler semantic targeting fails or evidence conflicts:

1. **Start with semantic targeting.** Prefer AX and DOM because they provide stable identities. For same-origin frames use `contentDocument`; for an attached OOPIF use `iframe_target()` with `js(..., target_id=...)`. Recursively inspect nested frames and confirm that the target contains the rendered control.
2. **Use the rendered UI to resolve disagreement.** If AX, DOM, and frame inspection do not explain what is visibly rendered, capture a screenshot and treat the visible UI as authoritative for whether a control exists and where its outer boundary lies.
3. **Locate the outer boundary.** Read the visible iframe, overlay, or composited region's current `getBoundingClientRect()` or `DOM.getBoxModel`.
4. **Fall back to compositor input.** Use `click_at_xy()` inside the current boundary when semantic access is unavailable. Use keyboard traversal such as `Tab`, arrow keys, and Enter when it is more stable than guessing multiple coordinates. Compositor input crosses iframe, shadow, and process boundaries.
5. **Reacquire after every transition.** After navigation, scrolling, rerendering, modal changes, dropdowns, frame swaps, failed actions, or method changes, rediscover the active tab, frame tree, target IDs, element boxes, and coordinates. Never reuse stale frame targets or geometry.
6. **Inspect every outcome channel.** Recheck the current tab, call `list_tabs()` for popups, inspect dialogs and overlays, compare frame inventories, read visible validation state, and inspect relevant network responses. An unchanged parent page does not mean the action did nothing.
7. **Verify the final effect.** Do not infer completion from a click, spinner, popup closure, DOM mutation, or successful intermediate request. Confirm the requested final visible and backend state.

For popup visibility and user handoff, follow the general tab rules above: switch to the intended target and call `activate_tab()` only when the user explicitly asks to see it. Keep the pooled session alive until the handoff is finished.

## References

- For JavaScript dialogs and `beforeunload`, read
  [references/dialogs.md](references/dialogs.md).
- Before deriving coordinates from screenshots or capturing large pages, read
  [references/screenshots.md](references/screenshots.md).
- Before changing window bounds or using mobile-sized layouts, read
  [references/viewport.md](references/viewport.md).

## Design Constraints

- Coordinate clicks default. CDP mouse events pass through iframes/shadow/cross-origin at the compositor level.
- Rebrowser Playwright owns JavaScript execution contexts, frames and workers;
  direct CDP remains the action and escape-hatch transport.
- Core helpers stay short. Put task-specific helper additions in `$BH_AGENT_WORKSPACE/agent_helpers.py`.

## Gotchas

- Omnibox popups are not real work tabs.
- CDP target order is not Chrome's visible tab-strip order.
