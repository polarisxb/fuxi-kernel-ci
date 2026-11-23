# Workflow and Windows client implementation

1. Trigger verification on pull requests and builds on trusted push/manual runs.
   Keep read-only permissions by default and signing material out of caches.
2. Check hosted runner disk capacity before creating 32 GiB swap, then invoke
   the existing pinned Full LTO build interfaces without weakening protection.
3. Collect named kernel/native artifacts; only publish the current default
   branch SHA. Upload a draft release completely before marking it latest.
4. Provide standard-library Python clients for immutable release downloads,
   SHA256/source-commit checks, and on-demand upstream source reads/imports.
5. Test clients with mocked network responses and real payload hashes, including
   corrupt payloads, ambiguous assets, unsafe paths and Windows reserved names.
6. Document native Windows development and anonymous agent download commands.
