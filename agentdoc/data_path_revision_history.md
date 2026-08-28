# `data_path` Has No Revision History — Code Evidence and IETF Prior Art

Accurate as of 2026-08-28 against `postgres-migration` (working tree). Prompted by a question about how the schema handles a data path whose `description` or data type changes across model revisions. Short answer: it doesn't retain history — only the *current* (latest-processed) revision's characteristics, as of the fix in §2a. The two columns below originally diverged in opposite, unintentional directions before that fix.

## 1. The schema treats `data_path` as a single global identity, not a per-revision fact

`data_path.machine_id` (`etl/src/schema.sql:106`) is `UNIQUE` across the *entire* table — not scoped to a `data_model`/revision. So a leaf that exists in YANG module revisions `2020-01-01`, `2021-06-01`, and `2023-03-01` is exactly **one row**, even though those are three distinct `data_model` rows (`UNIQUE(name, revision)`, `schema.sql:92`) chained via `parent_id` to mirror the module's `revision` history.

`data_path_source` (`schema.sql:127-133`) is the only table that records *which* `data_model` a path came from, but it's a bare two-column junction (`data_path_id`, `data_model_id`, unused `parse_timestamp`) — no payload columns. It can answer "did this path appear in revision X," never "what did its description/type say in revision X."

## 2. The two mutable columns diverge: first-write-wins vs. last-write-wins

Both bugs are in `add_data_paths_to_dm` (`etl/src/yang/__init__.py:184-236`):

**`description` (and `human_id`/`is_leaf`/`is_configurable`/`parent_id`) — first write wins, silently:**
```sql
INSERT INTO data_path (machine_id, human_id, description, is_leaf, is_configurable, parent_id)
VALUES (...)
ON CONFLICT (machine_id) DO UPDATE SET machine_id = EXCLUDED.machine_id
RETURNING data_path_id
```
(`yang/__init__.py:196-209`) — the `DO UPDATE` is a deliberate no-op, added purely so `RETURNING` still hands back the existing row's id (see the dedup rationale in the comment at `yang/__init__.py:186-192`, and `agentdoc/etl_memory_usage.md` §"Status update" for why it replaced the old `dp_cache` Python-side dedup). Whichever `data_model` revision is processed *first* permanently owns the description text. A later revision's changed description is discarded with no trace that it ever differed.

**`data_type_id` — last write wins, unconditionally:**
```sql
UPDATE data_path SET data_type_id = %s WHERE data_path_id = %s
```
(`yang/__init__.py:231-234`) — no `ON CONFLICT` guard at all, runs every time any module defines that `machine_id`'s type. Whichever revision happens to be processed *last* (order depends on OS/version iteration order) wins.

Same conceptual path, same table, two columns following opposite precedence rules — not a considered versioning policy, just an artifact of how the two statements were independently written.

Not new in this migration: the ArangoDB original had the same shape (`OfDataType` edge explicitly noted as "observed as functionally 1:1," `agentdoc/new_schema.sql:29-31`) — the Postgres migration ported the behavior faithfully rather than introducing it.

## 2a. Fix applied: unify on last-write-wins (2026-08-28)

`add_data_paths_to_dm`'s `ON CONFLICT (machine_id) DO UPDATE` now overwrites `human_id`, `description`, `is_leaf`, and `is_configurable` (and `parent_id`) with `EXCLUDED.*` on every write, matching the last-write-wins behavior `data_type_id` already had. This relies on an ordering guarantee the code already assumed elsewhere (§2's `data_type_id` UPDATE, and the `parent_dm_id` revision chain built in `add_version_modules`): `sorted(revisions.keys())` processes a module's revisions oldest-to-newest, and `os_version_folder_map` (`yang/__init__.py:29-95`) lists each OS's versions oldest-to-newest, so "last processed" coincides with "most recent revision" for a given OS. It does **not** make cross-OS collisions (the same `machine_id` appearing under two different OS module trees) meaningful in revision-date terms — that was already true before this fix and is a separate, much rarer concern.

This makes `data_path` always reflect the *current* (latest-known) state of a path — consistent with what a live device would report via YANG Library (RFC 8525) — but it still does not retain history. A query like "what did this path's description say under IOS XE 16.9.1" is still unanswerable; only "what does it say as of the newest revision parsed" is. §4's `data_path_source`-based fix remains the way to actually retain per-revision snapshots, if that's ever needed (e.g. for diffing what changed between two specific releases).

SNMP is intentionally untouched: `snmp.py`'s `populate_snmp` has no revision dimension in this schema (`data_model.revision` is hardcoded to `''` for SNMP, `snmp.py:332`) — all ~1,650 MIBs are compiled fresh as one flat corpus each run, not organized by OS release the way YANG modules are. Its in-process `oid_cache` dedup (`snmp.py:348-350`) treats a repeat `oid` as a logged error (likely a genuine naming collision), not a legitimate multi-revision evolution — see §3's SNMP/SMIv2 note on why OIDs aren't expected to be redefined in place.

## 3. IETF already has a body of thought on exactly this problem

This isn't a novel question for YANG — NETMOD WG has formal rules for it, and none of them endorse merging across revisions the way `data_path` does.

- **[RFC 7950 §11, "Updating a Module"](https://datatracker.ietf.org/doc/html/rfc7950)** — the foundational backward-compatible (BC) vs. non-backward-compatible (NBC) rules between module revisions.
  - Changing a `description` is BC — YANG expects description text to legitimately drift between revisions, since it doesn't affect wire semantics. First-write-wins throws away exactly the change RFC 7950 sanctions.
  - Changing a leaf's `type` is NBC in essentially all cases (narrow exceptions only, e.g. loosening certain restrictions) — a real type change is meant to be a breaking, revision-bumping event, not something silently overwritten in place by whichever revision parses last.
- **[RFC 8407, "Guidelines for Authors and Reviewers of YANG Data Model Documents"](https://datatracker.ietf.org/doc/html/rfc8407)** — operationalizes §11: how to write `revision` history entries, when to bump `revision-date`, `status: deprecated/obsolete` usage.
- **[RFC 8525, "YANG Library"](https://datatracker.ietf.org/doc/html/rfc8525)** — the actual runtime answer to "what does this path's description/type mean right now": a device advertises the exact set of `(module-name, revision)` pairs it implements, and a client pins to one revision's schema. There is no IETF notion of a path's description/type independent of a revision — the revision is part of the identity.
- **[draft-ietf-netmod-yang-module-versioning](https://datatracker.ietf.org/doc/draft-ietf-netmod-yang-module-versioning/)** (formerly `draft-verdt-...`) — refines §11 further, adds a `rev:non-backwards-compatible` extension so an NBC change is machine-flagged in revision history instead of requiring a text diff to detect.
- **[draft-ietf-netmod-yang-semver](https://datatracker.ietf.org/doc/draft-ietf-netmod-yang-semver/)** (at -24 as of this writing, Standards Track, updates RFCs 7950/8407/8525) — attaches a MAJOR.MINOR.PATCH label to every revision so BC/NBC status is a machine-readable tag, not something inferred from prose.

**The connection back to this schema:** `data_model` already mirrors the IETF revision model correctly (`UNIQUE(name, revision)` + `parent_id` chain = a faithful copy of the RFC 7950 revision-history concept). `data_path` breaks it by deduplicating identity *across* those revisions via a global `machine_id`, discarding exactly the per-revision distinction RFC 7950/8525 treat as load-bearing.

SNMP/SMIv2 side note: RFC 2578 has an analogous `STATUS` clause (`current`/`deprecated`/`obsolete`) but no equivalent BC/NBC diffing framework or library/pinning mechanism. The cultural norm there is "never redefine an OID's type in place, mint a new OID instead" — so this failure mode is structurally rarer (not impossible) in the SNMP-derived half of this data.

## 4. Further fix direction, if per-revision history is ever needed (not implemented — for discussion)

Stop treating `description`/`data_type_id` as attributes of the deduped `data_path` identity row. Move them onto `data_path_source` instead:

```sql
ALTER TABLE data_path_source
    ADD COLUMN description  TEXT,
    ADD COLUMN data_type_id BIGINT REFERENCES data_type (data_type_id);
```

Then `data_path` stays purely the cross-revision identity/tree-structure row (`machine_id`, `human_id`, `parent_id`), and each `(data_path_id, data_model_id)` pair keeps its own revision-scoped description and type — the same pinning principle YANG Library already uses at the module level, applied one level down to the path level. Diffing `data_path_source` rows for the same `data_path_id` across `data_model_id`s would also give a cheap way to surface exactly which paths changed description/type between two OS releases, which the current schema cannot answer at all today.
