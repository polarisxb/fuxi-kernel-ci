# Fuxi R28 local adaptation — local2
Based on sidex15/susfs4ksu-module v1.5.2+_R28, original asset SHA256 abcd6bf456ea8189298d9e496faa440201d90e4f0bd93b83c0527589001c9376.
Requires the already installed driver40900 and SUSFS2.3.0. This is a local adaptation, not an unchanged official release.
The official bundled ARM64 helper is retained byte-for-byte and pinned to SHA256 8b9fa0174ab9bc95910258f935628dfb9b551ae9b5074ba0eb1fa85e06590641; it passed show version/enabled_features/variant on this phone.
Installer always copies and verifies that helper; installation and action/update scripts no longer download executables. Module updateJson removed to avoid silently replacing the reviewed local changes. Manual updates require a reviewed package.
Deprecated sus_su disabled for SUSFS2.x. Other upstream defaults, including suspicious mount hiding and visible boot-property changes, remain as upstream. Does not actually relock the bootloader or sign AVB. Zygisk is not required.

## local2 changes (2026-11-10)

- WebUI commands reuse the existing counter-based callback allocator. Rejections and synchronous bridge exceptions release the callback; dependent initialization and uname updates await completion.
- The logging stage explicitly applies either `enable_log 0` or `enable_log 1`; invalid configuration falls back to the existing enabled default.
- Uname saving uses one shared operation for manual values and the stock build date. Shell arguments, shell configuration values and sed replacements are escaped independently. NUL and line breaks are rejected, while quotes, slashes, ampersands, backslashes and shell metacharacters roundtrip as data. The WebUI config reader decodes this canonical shell quoting and boot-time arguments remain quoted.
- Cached values and displayed settings are updated only after successful application/persistence. The save buttons are disabled while saving; command failures produce an error message. Upstream UI layout and feature defaults are unchanged.
- `module.prop` uses `v1.5.2-R28-fuxi230-local2` / versionCode `105002029`; the existing boot-completed suffix rule retains `local2` after the kernel version is shown.

The new tests run on the host with temporary files and a simulated WebUI bridge; they do not establish on-device behavior for this candidate. Test sources are outside the installable module in `tests/module/`. The helper binary and executable-update restrictions remain unchanged.
