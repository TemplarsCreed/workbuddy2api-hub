usage.jsonl is the active ledger. Starting with the segmented ledger, the
gateway seals a complete file once it reaches WB_USAGE_ROTATE_BYTES
(default 8388608, or 8 MiB). Sealed records live in usage.jsonl.segments/.
The gateway compresses sealed segments in independently seekable gzip blocks
and builds timestamp indexes, including for a large pre-existing ledger.
The adjacent .rollup.gz summaries are derived data. They let the gateway
rebuild all-time totals without replaying valid sealed segments. A missing or
invalid summary falls back to reading its own segment and can be regenerated.

Back up the whole usage directory, including usage.jsonl.segments/. Keep the
archives with the active file, and let the gateway handle rotation. Set
WB_USAGE_ROTATE_BYTES=0 to stop creating segments; existing ones remain
readable. Before installing a gateway version that only reads usage.jsonl,
stop the service and export the complete ledger to a new file:

    python -m wb_usage_io export usage/usage.jsonl usage/usage-export.jsonl

Use the exported file as usage.jsonl in the older installation's data folder.
The export preserves the record order, and the destination must not exist.

usage-aggregate-cache-<kind>.json files are a restart accelerator: each one
stores one kind of the dashboard's incremental aggregates (snapshot,
by_account, analytics, series, remaining) together with the log offset it
covers, so the first refresh after a restart only reads the rows appended
since. One file per kind: a process only parses the kinds it actually serves,
and only rewrites the kinds that advanced. They are validated against the log
(offset, device/inode, tail signature) and against the price tables and realm
assignments when loaded; anything that does not match is ignored and the full
scan happens instead, so deleting these files is always safe. Set
WB_USAGE_CACHE=0 to turn them off entirely.

usage-aggregate-cache.json (no <kind>) is the single-file layout older builds
wrote. It is still read as a fallback for any kind that has no file of its
own, but it is never written or deleted; once every kind has been written once
it is simply ignored, and you can delete it safely.

usage-summary.json is a leftover from older builds, which rewrote it on every
request even though nothing ever read it back. New versions append to
usage.jsonl only; you can delete the old summary file safely.
