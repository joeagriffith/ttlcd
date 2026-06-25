# 🐞 ISSUES INBOX

Other agents (esp. the ML agent): **append a new block under "OPEN"** when
something goes wrong or you need a change. The lead (Claude) triages these,
fixes, and moves them to "RESOLVED" with a note. Newest at the top of OPEN.

Once the daemon is live you can also file via API:
`curl -XPOST localhost:8770/issue -d '{"title":"...","body":"...","agent":"ml-agent"}'`
(API-filed issues are appended here automatically.)

### Template
```
### [OPEN] <short title>
- **from:** <who>
- **when:** <date/time>
- **severity:** low | medium | high
- **what happened:** ...
- **repro / context:** ...
- **what I want:** ...
```

---

## OPEN

_(none yet)_

---

## RESOLVED

### [RESOLVED] Panel fell off the USB bus during driver smoke-test — needs replug
- **from:** lead (Claude)
- **when:** 2026-06-25 ~00:07
- **severity:** high
- **what happened:** During hardware bring-up I called pyusb `dev.reset()` to recover a stalled init. On this Thermaltake panel that call drops the device off the USB bus and it does not re-enumerate without a power-cycle. It is now absent from `lsusb`.
- **repro / context:** `LcdDriver.reset_usb()` used to call `self.dev.reset()`. The first hardware run (before this) streamed fine, so the driver path itself is proven.
- **resolution:** Panel replugged; the daemon auto-reconnected and resumed streaming within ~2s, 0 errors. Code fixed: `dev.reset()` removed in favor of a safe `USBDEVFS_RESET` ioctl that keeps the device enumerated, so it can't recur.
