---
name: browser-harness
description: "Control an existing browser-pool session through CDP with Rebrowser-patched JavaScript evaluation: inspect pages, click, type, navigate, and handle JS-rendered or bot-protected interfaces. Not for browser/session lifecycle or plain HTTP fetches."
---

# browser-harness

Use Browser Harness to control the browser session already selected by
`browser-pool env`. Browser-pool owns browser creation, profile routing,
isolation, persistence, and cleanup.

Use a plain HTTP client for public pages and APIs that do not require browser
state, JavaScript rendering, or interaction.

## Invocation

Browser-pool exports `BU_NAME` and `BU_CDP_WS`. Preserve them and invoke the
CLI with heredocs; helpers are pre-imported:

```bash
browser-harness <<'PY'
new_tab("https://example.com")
wait_for_load()
print(page_info())
PY
```

- First navigation for a task is `new_tab(url)`. The attached tab persists
  across CLI calls, so do not call `new_tab()` again in every script.
- Before opening another tab, inspect `current_tab()` and `list_tabs()` and
  reuse a matching tab with `switch_tab()`.
- Do not close tabs you did not create. Close task-created tabs when they are no
  longer needed unless that could discard unsaved work or the user needs them
  for a follow-up.
- `new_tab()` and `switch_tab()` operate in the background. Call
  `activate_tab()` only when the user explicitly asks to see the tab.
- A hidden page that pauses is not permission to foreground it. For a genuinely
  focus-gated page, temporarily enable
  `Emulation.setFocusEmulationEnabled`, perform and verify the operation, and
  disable it in a `finally` block.

## Page workflow

- Prefer the accessibility tree:
  `cdp("Accessibility.getFullAXTree")["nodes"]`. Filter it in Python before
  printing.
- Obtain coordinates from an AX node with
  `cdp("DOM.getBoxModel", backendNodeId=...)`; use the center of its content
  quad with `click_at_xy(x, y)`.
- After navigation, call `wait_for_load()`. After rerenders, scrolling, tab or
  frame changes, timeouts, covered elements, or failed actions, reacquire the
  current target and geometry before acting again.
- Use `js(...)` for targeted DOM inspection or extraction when semantic access
  is unavailable or coordinates are the wrong tool. Use screenshots when
  visual layout or imagery determines the action.
- Before submitting a form, verify all entered values and selections. Prefer
  waits tied to observable URL, text, element, network, or load state over fixed
  sleeps.
- Resolve visible blockers instead of forcing stale clicks. Inspect every
  plausible result channel: page state, dialogs, overlays, popups, frames, and
  relevant network responses.
- Verify the requested final visible and backend state. A click, spinner,
  request, popup closure, or DOM mutation alone is not completion.

For unusually long text, use a faster page-appropriate input method and verify
the exact resulting value.

## Authentication and native prompts

Browser-native passkey/WebAuthn choosers are outside the page DOM and are not
JavaScript dialogs. They can intercept coordinate input while the page remains
scriptable. Count an authentication error only when it is visibly rendered or
corroborated by the page or response state.

If a visible password fallback remains populated and one coordinate submit
produces no request, navigation, or visible error, invoke the same validated
submit control once with DOM `click()` or `form.requestSubmit(control)`, then
inspect the result. Never loop credential submissions.

## Iframes and embedded surfaces

Rendered controls may live in different embedding models:

- Same-origin iframe: inspect through `contentDocument` / `contentWindow`.
- Cross-origin iframe: the parent exposes its element and box, not its DOM.
- OOPIF: use `iframe_target()` and `js(..., target_id=...)` when Chrome exposes
  a separate target.
- Nested or isolated component frames: recursively locate the frame that owns
  the rendered control; an outer frame with no inputs proves nothing.
- Popup-producing frame: inspect `list_tabs()` after the action.
- Canvas/compositor-only surface: the control may be absent from DOM and AX.

Progressively expand inspection:

1. Start with AX and DOM, including same-origin frames and attached OOPIFs.
2. If semantic inspection conflicts with what is rendered, use a screenshot to
   establish whether the control exists and locate its outer boundary.
3. Read the current iframe/overlay box with `getBoundingClientRect()` or
   `DOM.getBoxModel`.
4. Use compositor input with `click_at_xy()` when inner DOM access is
   unavailable. Keyboard traversal can be more reliable than guessed points.
5. Reacquire tabs, frame targets, boxes, and coordinates after every transition
   or failed action; never reuse stale geometry.
6. Verify the final page and backend effect, including popups and network state.

## Input and JavaScript stealth

Browser-pool enables `BH_HUMANIZE=1`. In that mode `click_at_xy()`,
`type_text()`, `fill_input()`, `press_key()`, and `scroll()` use Clearcote's
humanized Playwright input layer; supported coordinate clicks use Clearcote's
native browser-engine path.

`js(...)` uses Rebrowser's execution-context path. Do not call `Runtime.enable`
in normal mode. Navigation, screenshots, input, and raw `cdp(...)` remain
available without it.

For an explicitly lower-stealth debugging session, set `BH_RUNTIME_DEBUG=1` in
a fresh Browser Harness process. Native `Runtime.consoleAPICalled` and
`Runtime.exceptionThrown` entries then appear in `drain_events()`; unset it to
return to normal mode.

Raw CDP parameters are keyword arguments:

```python
cdp("Input.insertText", text="hello")
```

The second positional argument is a session ID, not a parameter dictionary. To
target an explicit session, use `session_id="..."` with keyword parameters.

## Specialized mechanics

When an interaction mechanic is unclear, read the relevant reference under
[`interaction-skills/`](https://github.com/browser-use/browser-harness/tree/main/interaction-skills),
especially for dialogs, downloads, drag-and-drop, iframes, network requests,
screenshots, scrolling, shadow DOM, tabs, uploads, or viewport behavior.

For task-specific helper code, use `$BH_AGENT_WORKSPACE/agent_helpers.py`
instead of expanding Browser Harness's core helpers.
