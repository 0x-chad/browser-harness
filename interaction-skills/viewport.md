# Viewport

Viewport size changes affect responsive layout, screenshot dimensions, and coordinate clicks. Use this skill before relying on pixel coordinates or when the site behaves differently on desktop vs mobile.

## Browser Use cloud resizing

For Browser Use cloud sessions, decide whether you need resizing before starting the daemon. `Browser.setWindowBounds` only worked reliably when the session was created with `allowResizing=True`.

```bash
BU_NAME=existing-remote browser-harness <<'PY'
start_remote_daemon(
    "resize-test",
    proxyCountryCode=None,
    allowResizing=True,
    browserScreenWidth=1280,
    browserScreenHeight=900,
    timeout=30,
)
PY
```

If `allowResizing=False`, the provider may accept `Browser.setWindowBounds` and return `{}` but keep the old bounds. In one observed session, a requested `1280x900` stayed clamped at `1512x857`.

## Set window bounds

Fetch the real page target first. Calling `Browser.getWindowForTarget` on the wrong target can fail with `No web contents in the target`.

```bash
BU_NAME=resize-test browser-harness <<'PY'
targets = cdp("Target.getTargets").get("targetInfos", [])
target = next(
    t for t in targets
    if t.get("type") == "page" and "example.com" in (t.get("url") or "")
)
window = cdp("Browser.getWindowForTarget", targetId=target["targetId"])
cdp("Browser.setWindowBounds", windowId=window["windowId"], bounds={
    "left": 10,
    "top": 10,
    "width": 1280,
    "height": 900,
    "windowState": "normal",
})
print(cdp("Browser.getWindowBounds", windowId=window["windowId"]))
print(page_info())
PY
```

The browser chrome consumes vertical pixels, so viewport height is smaller than window height. A `1280x900` window yielded `page_info()` around `w=1280, h=813`.

## Mobile-sized window

Use normal window bounds for a narrow layout:

```bash
BU_NAME=resize-test browser-harness <<'PY'
targets = cdp("Target.getTargets").get("targetInfos", [])
target = next(t for t in targets if t.get("type") == "page")
window = cdp("Browser.getWindowForTarget", targetId=target["targetId"])
cdp("Browser.setWindowBounds", windowId=window["windowId"], bounds={
    "left": 10,
    "top": 10,
    "width": 390,
    "height": 844,
    "windowState": "normal",
})
print(page_info())
PY
```

In the observed cloud session, `390x844` window bounds produced a `390x757` viewport.

This changes the desktop window size only. It does not by itself emulate a mobile user agent, touch support, or device scale factor. If the task requires true mobile emulation, use CDP `Emulation.setDeviceMetricsOverride` and user-agent override deliberately, then verify with `page_info()` and a screenshot.

## Touch emulation

`maxTouchPoints` is optional, but Chrome validates it as 1–16 even when touch
emulation is being disabled. Omit it when disabling:

```python
cdp("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
cdp("Emulation.setTouchEmulationEnabled", enabled=False)
```

Do not pass `maxTouchPoints=0`; Chrome rejects the supplied value before acting
on `enabled=False`.
