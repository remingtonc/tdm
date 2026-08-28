# ETL Memory Usage Assessment — SNMP vs. YANG

Code-level assessment of why `etl/`'s population run uses a large amount of RAM, split by the two data-loading paths it runs sequentially in one process (`main.py:94-99`: `populate_static` → `populate_snmp` → `populate_yang` → optionally `populate_search`). Accurate as of 2026-08-28 against `postgres-migration` (working tree, `etl/src/yang/__init__.py` has one uncommitted whitespace-only diff — `IOS_XE`/`IOS_XR` → `IOS XE`/`IOS XR` — not relevant to memory). This is a static-analysis read of the code plus repo-scale measurements (file counts, checkout sizes); no live profiler was attached in this pass — see "Suggested diagnostics" for how to confirm these against real RSS numbers.

Headline: **YANG was the documented, self-admitted dominant driver.** The module's docstring used to say so directly (`etl/src/yang/__init__.py:15-18`, prior to the fix below): *"Load YANG models in to the database representation of OS/Releases/DataModels/DataPaths/DataTypes. Heavy parsing. Extremely non-optimal. ~10GB memory usage."* SNMP is comparatively lighter but not free of the same anti-patterns (no batching, full-corpus recompilation every run).

**Status update (2026-08-28):** the two biggest YANG-side drivers identified below — whole-OS accumulation before any DB write, and the unbounded `dp_cache`/`dm_dp_cache` module-level caches — have been fixed in this session. `YANGBase.parse_versions()` (`yang_base.py`) is now a generator that yields one version's parsed data at a time instead of building `version_module_map` for an entire OS; `populate_yang()` writes and commits each version immediately, so a version's parsed tree is eligible for GC before the next one is parsed. `dp_cache` and `dm_dp_cache` were removed: `data_path` inserts now use `ON CONFLICT (machine_id) DO UPDATE ... RETURNING data_path_id` (originally a no-op `SET machine_id = EXCLUDED.machine_id` that just let Postgres do the dedup and hand back the existing id; as of 2026-08-28 it also overwrites `human_id`/`description`/`is_leaf`/`is_configurable`/`parent_id` with the incoming row's values, so the latest-processed revision's characteristics win instead of the first — see `agentdoc/data_path_revision_history.md`), and `data_path_source` relies solely on the `ON CONFLICT DO NOTHING` it already had. `dm_cache` and `dt_cache` are unchanged (see the caches bullet below for why they're not part of the problem). The sections below describe the **prior** state that motivated the fix; re-run the diagnostics in "Suggested diagnostics" to confirm the actual RSS improvement.

## Repo-scale numbers (why the multipliers matter)

- SNMP: `etl/cache/extract/mib_repo/v2` has **1,650 `.my` MIB files** compiled together in a single `pysmi` `MibCompiler` run.
- YANG: `etl/cache/extract/yang/vendor/cisco/` (the checkout `populate_yang` parses) is **701MB on disk**, split into per-OS, per-version directories:
  - `nx`: 20 enabled versions (`etl/src/yang/__init__.py:28-50`)
  - `xe`: 15 enabled versions (`:51-67`)
  - `xr`: 11 enabled versions (`:68-93`, most of the 5.3.x/6.0.x/6.1.x/6.2.x range is commented out as "model bugs")
  - Per-version `.yang` file counts scale with release age — sampled `xr/*`: `530`→63 files, `602`→466, `632`→1,246, `701`→1,229, `711`→1,278. The newer/larger XR versions are each parsing well over 1,000 YANG modules individually.

## SNMP load path (`etl/src/snmp.py`)

Entry point `populate_snmp` (`snmp.py:381-396`): `download_mibs` → `transform_mibs_to_json` → `transform_json_to_new` → `parse_json_to_db`, one call each, no chunking across the 1,650-MIB corpus.

1. **`transform_mibs_to_json` (`:145-186`)** hands *all* 1,650 `.my` files to one `MibCompiler` instance with:
   - `noDeps=False` — resolves the full transitive IMPORT graph, so compiling one MIB can pull in and hold many others' parsed state simultaneously.
   - `rebuild=True` — forces full recompilation every run; pysmi's own on-disk compiled-MIB cache is never trusted/reused across ETL runs.
   - `genTexts=True` — retains every `DESCRIPTION` clause (often multi-paragraph prose) on every compiled node, inflating each compiled JSON's in-memory and on-disk size well beyond what's needed structurally.
   - This is a single `mib_compiler.compile(*mib_names, ...)` call for the entire corpus (`:169-178`) — the compiler's internal symbol/dependency state for up to 1,650 modules is live at once.

2. **`build_type_index` (`:188-212`)**, called once per `transform_json_to_new` run (`:234`), `json.load()`s **every** compiled JSON file in `local_json_dir` in a tight loop just to harvest `class in ('type', 'textualconvention')` entries into a flat `{name: parent_name}` dict. Each file's full decoded JSON is transient (freed at the end of each loop iteration), so this is more GC/CPU churn than a sustained peak — but it means the full text of every compiled MIB is parsed into memory at least twice total across the SNMP phase (once by pysmi's compiler, once again here).

3. **`parse_json_to_db` (`:286-379`)** iterates every transformed JSON file and does **one `INSERT ... RETURNING` + `fetchone()` per OID**, no `executemany`/`COPY`, inside a **single transaction** committed only once at the very end (`:378`). `oid_cache`/`dt_cache` are local to this call (freed on return) but grow to one entry per unique OID/base-type across all 1,650 MIBs — likely tens to low hundreds of thousands of small dict entries. The single long-lived transaction also means Postgres itself is buffering a large amount of uncommitted work for the whole SNMP phase, though that's server-side memory, not the ETL process's.

**Net for SNMP:** no structure here is anywhere close to YANG's scale, but `rebuild=True`+`noDeps=False`+`genTexts=True` means every run pays full recompilation cost with no caching, and the total absence of batched inserts means both the Python-side caches and the Postgres-side open transaction scale linearly with total OID count with no relief valve.

## YANG load path (`etl/src/yang/__init__.py`, `yang_base.py`, `yang_parser.py`)

This is the dominant consumer. Walking the call chain from `populate_yang` (`yang/__init__.py:140-159`):

1. Per OS (`nx`/`xe`/`xr`), `YANGBase(...).parse_versions()` (`yang_base.py:54-68`) loops over **every enabled version folder for that OS** and, for each one, calls `yang_parser.parse_repository()`.

2. `parse_repository` (`yang_parser.py:25-40`) builds a `pyang` `FileRepository` + `Context` rooted at that version's directory and calls `context.validate()`. This parses and *semantically validates* **every `.yang` file physically present in that version directory, recursively** (`no_path_recurse=False`) — not just modules actually referenced by a top-level model. As noted above, that's 400-1,300+ files for a single XR version alone. `context.validate()` builds pyang's full semantic layer (`i_children`, typedef resolution, etc.), which is materially heavier than a bare parse tree.

3. `parse_modules` (`yang_parser.py:42-64`) then force-parses (`context.search_module`) every module/revision found and returns a dict of `module_name -> revision -> pyang Module object` — the full AST/semantic graph for every module in that version directory, held at once.

4. `YANGBase.parse_module_oper_attrs` (`yang_base.py:70-92`) recursively walks every module's `i_children` and **rebuilds the entire tree as plain nested Python dicts**, one dict per node, each carrying `machine_id`, `qualified_xpath`, `xpath`, `type`, `primitive_type`, `rw`, `description` (raw text), and a nested `children` dict. For large modules (OpenConfig-style YANG trees run to thousands of leaves/containers) this is thousands of dict allocations per module.

5. **The critical accumulation point:** `parse_versions` (`yang_base.py:54-68`) builds `version_module_map[version] = ...` for **every enabled version of the current OS in one loop**, and only returns once the whole OS is done (`:68`). `populate_yang` (`yang/__init__.py:154-159`) does not start writing to Postgres until this entire per-OS structure exists in memory — nothing is streamed at the version level even though `add_version_modules` (called per-version, `:159`) could structurally support it. So the peak resident structure for one OS is *(all its enabled versions) × (all their modules) × (fully recursive per-node attribute dict)* — for XR, that's 11 versions, several with 1,000+ modules each, each expanded into a full node-attribute tree, all alive simultaneously before the first `INSERT` for that OS's last version even runs.

6. **Unbounded module-level global caches**, called out in the code's own comments (`yang/__init__.py:121-126`: `# TODO: Optimize the caches.` / `# This is the most memory inefficient thing I've ever done.`):
   - `dm_cache`, `dp_cache`, `dt_cache` (`:122-124`) are plain module-level dicts that live for the **entire `populate_yang` call across all three OSes** — never cleared between OS or version iterations.
   - `dp_cache` maps every unique data-path `machine_id` XPath string ever seen (across NX + XE + XR combined) to its DB id — keys are long strings, so this isn't a cheap dict.
   - `dm_dp_cache` (`:126`) is a raw Python `set` of `(dm_id, path_id)` int-tuples, one per (model-revision, path) association ever inserted. Because a `data_model` row is created **per revision** (`add_version_modules`, `:161-184`) and adjacent revisions of the same module largely share the same paths, this set's cardinality tracks total path-insertions across *every revision of every module of every OS* — plausibly millions of tuples for a corpus this size. Python set/tuple overhead (open-addressing table well under capacity, ~56+ bytes per 2-tuple of ints) makes this realistically a multi-hundred-MB-to-GB structure on its own, and it only ever grows for the life of the process.

7. `pyang`'s `Context`/`Module` objects from step 2-3 are local to `parse_repository` and should be reclaimed by refcounting once that function returns for each version — but step 5 means the *extracted* plain-dict trees for every version of the OS are retained regardless, so freeing the pyang objects doesn't touch the actual dominant structure.

## Cross-cutting observations

- **Everything runs in one process, one after another** (`main.py:94-99`). Because glibc/CPython allocators don't reliably return freed arenas to the OS, RSS observed during the YANG phase may already be inflated by fragmentation left over from SNMP's `MibCompiler`/JSON churn — the "incredible amount of RAM" symptom is plausibly SNMP's peak arena footprint *plus* YANG's own peak stacking, even though logically only one phase's data is live at a time.
- **No batching anywhere in either path.** SNMP OIDs and YANG data_path/data_model rows are each inserted one row at a time with an immediate `fetchone()`; SNMP commits once for the whole corpus, YANG commits once per version (`yang/__init__.py:184`). This is a DB-side memory concern more than a Python-process one, but it means neither path has any natural checkpoint to release Python-side cache memory either — the caches are keyed to be valid for the whole run, not just one commit's worth of work.
- Nothing in either path uses `tracemalloc`/RSS instrumentation today — the "~10GB" figure in the YANG docstring is a prior author's empirical observation, not something currently re-verified or logged per run.

## Suggested diagnostics (before optimizing)

1. Add `resource.getrusage(RUSAGE_SELF).ru_maxrss` (or `tracemalloc.get_traced_memory()`) logging before/after each `YANGBase(...).parse_versions()` call and after each `add_version_modules()` call in `populate_yang`, to confirm which OS (likely XR, given version count × file-count) actually produces the peak, and whether the peak lands during pyang parsing (step 2-3 above) or in the retained dict trees (step 4-5).
2. Take a `tracemalloc` snapshot immediately after `parse_versions()` returns for the largest OS, grouped by allocation traceback, to get real byte counts for `dp_cache` / `dm_dp_cache` / the returned `version_module_map` instead of the estimates above.
3. Log RSS at the `populate_snmp` → `populate_yang` boundary in `main.py` to check whether memory actually drops between phases (true liveness) or stays flat (allocator fragmentation/retention).

## Possible remediation directions (not implemented — for discussion)

- **Stream YANG writes per version** instead of accumulating `version_module_map` for a whole OS: `add_version_modules` is already called per-version (`yang/__init__.py:159`), so the structural entry point for streaming already exists — the fix is to not build the full per-OS map before iterating.
- **Replace `dp_cache`/`dm_dp_cache` Python-side dedup with DB-side dedup.** The schema already uses `ON CONFLICT DO NOTHING` for `data_path_source` inserts (`yang/__init__.py:207-210`); a unique constraint on `data_path.machine_id` plus `ON CONFLICT ... RETURNING` would let Postgres do the dedup work instead of an ever-growing in-process set/dict.
- **Scope `Context.validate()` to only the modules a version actually ships**, if the checkout contains more `.yang` files per version directory than are truly needed, rather than recursively validating everything present.
- **Turn off `genTexts=True`** on the SNMP `MibCompiler` if MIB descriptions aren't required, and consider letting `rebuild` default to `False` so pysmi's own compiled-MIB cache is reused across runs instead of always recompiling all 1,650 MIBs from scratch.
- **Run SNMP and YANG (or even each OS within YANG) as separate subprocesses**, so the OS reclaims all memory between phases instead of depending on CPython/glibc to hand it back within one long-lived process.
