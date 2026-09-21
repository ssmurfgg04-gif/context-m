# cortexm memory export

user_id: `anatomy-arcade`
exported_at: 2026-09-21T11:00:46.936615+00:00
facts: 4
chunks: 6

## Layout

- `facts/<subject>__<relation>__<id8>.md` — one file per fact,
  YAML frontmatter carries every bi-temporal field.
- `chunks/chunk__<id8>.md` — raw source text, hash-verified.

## Round-trip

  cortexm import --markdown <dir> --user-id <user>

Re-importing re-verifies BLAKE3 hashes; mismatches land in the
audit log. Human edits to the `value:` field are picked up and
tagged with `source: user_override` provenance.
