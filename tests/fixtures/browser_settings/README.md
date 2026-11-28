# Browser settings probe

This opt-in fixture compares a host-owned toolbar and a separate settings window.
It does not add controls to Factory or update a live sandbox. Apply changes a
private JSON fixture; preview uses `Service.preview_settings`.

```sh
PYTHONPATH=.:tests .venv/bin/python tests/browser_settings_probe.py --output /tmp/factory-toolbar
```

Add `--image IMAGE_ID` to exercise the pinned code-server and shipped FrostPi
inside a disposable, offline Podman container. Supply an already cached Factory
stock image; the fixture does not pull or build images. It mounts no host files
and removes its container on exit. Playwright and Chromium are the same test
dependencies used by the existing browser fixtures.

The default view has a small top-centre tab. Clicking it reveals an attached strip
with the sandbox name, current network mode, Settings and Full screen. Returning
to the editor hides the strip. Settings opens a content-sized panel; Escape,
Cancel and Close discard its draft and return focus to the tab. On narrow screens,
the network value stays in Settings and the strip remains one row. The toolbar
uses the supplied channel palette and native font stacks, mirrored in `brand.css`
so it has no dependency on another checkout or external font service.

The probe includes an intentionally unsafe same-origin control. Its successful
token read demonstrates why editor code and Factory controls must not share an
origin. The protected variant uses a separate port, a host-only bearer, strict
request checks and no authority-bearing cookies or message bridge. These are
experimental handlers, not a production HTTP service.

Results and screenshots go to `--output`. Inspect them before drawing conclusions.
The fixture does not prove real Factory updates, credential-container isolation,
all client workflows, or compatibility with other browsers and host platforms.

The layout follows the edge-attached, retractable toolbar shown in the
[Horizon toolbar guide](https://ictsupport.key.org.uk/media/pluklzwa/vdi-v1122.pdf#page=6).
The fixture opens by click so moving across the editor title bar does not reveal
controls unintentionally. The editor keeps its full viewport size.

Production integration would need a shared change plan and revision check used by
CLI, TUI and browser, with execution through existing Factory operations. Network
mode changes require replacement; allowed-destination changes already have a live
operation. The host control service must survive that replacement and retain job
status across page reloads. Pairing, real lifecycle updates and rollback, keyboard
access from the editor, and fullscreen Escape in a headed browser remain separate
implementation checks; this fixture does not certify them.
