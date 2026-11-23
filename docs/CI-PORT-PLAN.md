# CI workspace portability pass

Reuse the already tested r2 workspace scripts and their regression assertions.

1. Replace host-specific and sibling paths with hydrated workspace `inputs/`,
   `downloads/` and `kernel/prebuilts/` paths; take the external signing directory
   from `SIGNING_KEY_DIR` and verify its certificate against the same baseline.
2. Keep source freeze/provenance, configuration, ELF/CRC/import/namespace,
   signature, UAPI and clean source replay gates unchanged.
3. Divide the existing regression runner into pre-build and post-build groups;
   the output-dependent build/hook checks use this build's generated output.
4. Add the CI driver: bootstrap, pre-build tests, actual Full LTO build,
   post-build tests, complete collection, two clean delivery verifications.
5. Export only verified ZIPs and public metadata/checksums to `dist`. Signing
   key files remain temporary and are removed after build completion. Do not
   cache or upload complete build output trees.

No new project dependency, signing policy relaxation, device operation or
change to existing kernel/modular behavior is part of this pass.
