# Viewport

Viewport size changes affect responsive layout, screenshot dimensions, and coordinate clicks. Read this before relying on pixel coordinates or when a site behaves differently at desktop and mobile sizes.

## Set window bounds

Fetch the real page target first. Calling `Browser.getWindowForTarget` on the wrong target can fail with `No web contents in the target`.

```bash
browser-harness <<'PY'
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

Browser chrome consumes vertical pixels, so viewport height is smaller than window height. A `1280x900` window typically yields a viewport around `1280x813`.

## Mobile-sized window

Use normal window bounds for a narrow layout:

```bash
browser-harness <<'PY'
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

This changes the desktop window size only. It does not emulate a mobile user agent, touch support, or device scale factor. For true mobile emulation, deliberately use `Emulation.setDeviceMetricsOverride` and a user-agent override, then verify with `page_info()` and a screenshot.

## Touch emulation

`maxTouchPoints` is optional, but Chrome validates it as 1–16 even when touch emulation is being disabled. Omit it when disabling:

```python
cdp("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
cdp("Emulation.setTouchEmulationEnabled", enabled=False)
```

Do not pass `maxTouchPoints=0`; Chrome rejects the supplied value before acting on `enabled=False`.
