# Plugin settings write access

`orca.host` is a read-only view of the running slicer: it exists so plugins can analyse,
report and export, and nothing under it mutates the model. `orca.host.edit` is the single
exception. It lets a plugin change print, filament and printer settings, which is what an
assistant-style plugin needs in order to act on its own recommendation rather than describe
it.

Because that is a genuine escalation — third-party code rewriting the settings a print
depends on — the surface is deliberately narrow and gated.

## Consent

Write access is a per-plugin permission, `PluginPermissions::settings_write`, persisted in
the plugin's `.install_state.json` sidecar next to the audit-hook grants.

It differs from those grants in three ways:

- It is **not collected by the audit hook.** The CPython audit hook only sees events the
  interpreter raises; an `orca.host.edit` call crosses a C++ binding directly and raises
  none. `require_settings_write()` in `PluginHostEdit.cpp` therefore performs the check
  itself, on every call.
- It is **a single switch, not a list of targets.** `fs_read` or `network_http` accumulate
  approved paths and hosts; settings access is all-or-nothing, granted from the Plugin Info
  panel of the Plugins dialog.
- It is **revoked on update.** `write_install_state` clears the flag whenever the recorded
  `installed_version` changes: the user consented to specific code, and a new version is
  different code.

The switch defaults to off, and turning it on raises a confirmation naming the plugin. A
plugin without the grant gets `PermissionError`, and can avoid that by checking
`orca.host.edit.can_write()` first.

## Applying a change

`edit.apply({key: serialized_value}, preset_type)` takes values in OrcaSlicer's own
serialized form — `"0.2"`, `"15%"`, `"1,1,1"` — the same strings `full_config_value()`
returns, so a plugin can read a value, adjust it and write it back without a type round trip.

Every key is checked against the target tab's config and its `ConfigOptionDef` before
anything is written: unknown keys, read-only options, invalid enum values, values that fail
deserialization and numbers outside the definition's `min`/`max` are each rejected
individually and reported in the return value. A rejected key is rolled back on its own;
keys validated earlier in the same call stay staged. The result is
`{applied: [...], rejected: {key: reason}, applied_count: n}` — a partially applied batch is
a normal outcome, not an error.

`orca.host.setting_metadata(keys)` exposes the same definitions for reading — label,
category, tooltip, unit, type, bounds and enum values. It carries no write permission
requirement, because knowing an option's bounds is what stops a caller from proposing a
value that could never be applied.

## Where the write lands

The mutation reuses `Tab::load_config`, the path the config importer and "load from project"
already take. That choice is what makes an agent's edit behave like any other edit:

- the preset becomes dirty and the changed fields are visible in the settings tabs;
- the tab's own option interlocks and `update()` run, so dependent options stay consistent;
- a `Plater::take_snapshot` taken first means Ctrl+Z reverts the whole batch.

The alternative — writing `PresetCollection` directly — would bypass all three.

## Threading

Presets are UI-thread state. `apply` marshals its work through `run_on_ui_blocking`
(`PluginHostUiThread.hpp`, shared with the UI bindings), which runs inline when already on
the main thread and otherwise blocks on a `CallAfter` with the GIL released.

This has the same deadlock constraint as the `orca.host.ui` bindings: it must not be called
from a slicing-pipeline hook, since that runs on the slicing worker thread the UI thread may
itself be waiting on. `apply` additionally refuses while `is_background_process_slicing()`
holds, and reschedules the background process once the write succeeds.
