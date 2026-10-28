# Research: LangGraph `MemorySaver` internals used by `BoundedMemorySaver`

Resolves issue #6. Researched 2026-10-28.

## Question

How are `InMemorySaver.storage`, `.writes` and `.blobs` laid out in the installed
langgraph version? Does `BoundedMemorySaver` (in `src/resilient_triage/graph/builder.py`)
fully and safely evict a thread by popping `storage[thread_id]` and `writes[thread_id]`?
Is eviction FIFO or LRU? Is `put` the only write path? Is there a public delete API?

## Versions examined

- `pyproject.toml` pins only `langgraph>=0.2.0`. No upper bound and no lockfile.
- Installed in `.venv` (`pip show`): **langgraph 1.2.16**, **langgraph-checkpoint 4.3.0**.
- Primary source: `.venv/lib/python3.12/site-packages/langgraph/checkpoint/memory/__init__.py`
  (upstream repo `langchain-ai/langgraph`, path `libs/checkpoint/langgraph/checkpoint/memory/__init__.py`).
  Line numbers below refer to the installed 4.3.0 file.

## Findings

### 1. Data layout (memory/__init__.py L69-95)

`MemorySaver` is an alias, `MemorySaver = InMemorySaver` (L634).

| Attribute | Key | Value | Built by |
|---|---|---|---|
| `storage` | `thread_id` -> `checkpoint_ns` -> `checkpoint_id` (nested) | `(serialized checkpoint, serialized metadata, parent_checkpoint_id)` | `defaultdict(lambda: defaultdict(dict))` |
| `writes` | **flat tuple** `(thread_id, checkpoint_ns, checkpoint_id)` | `{(task_id, write_idx): (task_id, channel, serialized value, task_path)}` | `defaultdict(dict)` |
| `blobs` | **flat tuple** `(thread_id, checkpoint_ns, channel, version)` | `(type, bytes)` | `defaultdict()` |

`put` (L430-474) writes one `blobs` entry per channel in `new_versions`, plus one `storage` entry.
Checkpoint `channel_values` are not stored inline. They live in `blobs`.
`put_writes` (L476-512) keys `writes` by `(thread_id, checkpoint_ns, checkpoint_id)` (L500).

### 2. The current eviction leaks `writes` and `blobs`

`BoundedMemorySaver.put` calls `self.writes.pop(oldest_thread, None)`. `writes` is keyed by a
3-tuple, so a bare `thread_id` string never matches and the pop does nothing. The code needs
`# type: ignore[call-overload]` to get past mypy, which hints at this. `blobs` is never touched.
The bulk of the state lives in `blobs`: the serialized channel values for every version.

### 3. Defect: eviction never fires during real graph execution

This is worse than the leak. Under real `invoke`/`ainvoke`, **nothing is ever evicted**.

- Before a run starts, Pregel loads the thread's latest checkpoint with `get_tuple`/`aget_tuple`.
- `get_tuple` reads `self.storage[thread_id][checkpoint_ns]` (L256, L285). `storage` is a
  `defaultdict`, so that **read inserts `thread_id`** as an empty entry.
  Verified: on a fresh saver, `get_tuple(...)` returns `None` and leaves
  `storage == {'new': {'': {}}}`.
- When `put` runs later, `thread_id not in self.storage` is already `False`, so the capacity
  check is skipped every time.

The existing unit test `tests/test_graph.py::test_bounded_memory_saver_eviction` passes only
because it calls `saver.put(...)` directly and never calls `get_tuple` first. It does not test
the path the app actually uses.

Empirical repro: a scratch script compiled a 2-node graph with `BoundedMemorySaver(max_threads=2)`
and called `await app.ainvoke(...)` for 50 distinct thread_ids. Result:

```
after t2 (cap=2): storage=['t0', 't1', 't2'] ...
after 50 threads: storage=50 write-threads=50 blob-threads=50 n_writes=153 n_blobs=459
```

Production impact: `api/app.py:225` builds a **unique** thread id for every request
(`f"{thread_id}_{uuid4().hex[:8]}"`), and `triage_graph` is a module-level singleton
(`builder.py:188`, cap hardcoded to 1000 and not in `config.py`). The checkpointer therefore
grows without bound for the life of the process. This is an unbounded memory leak, which goes
against the "never stall" promise.

### 4. FIFO, not LRU

`next(iter(self.storage))` returns the key that was inserted first, because dicts keep insertion
order. Re-using a thread never moves it to the end. Eviction is pure FIFO, so the docstring's
"FIFO/LRU" claim is wrong. Since the app never reuses a thread_id (see §3), FIFO and LRU behave
the same here today.

### 5. `aput` delegates to `put`, so the override is reached

- `AsyncPregelLoop` calls `await checkpointer.aput(...)` (`langgraph/pregel/_loop.py` L1975).
  The sync loop calls `.put(...)` (L1719).
- `InMemorySaver.aput` is `return self.put(...)` (memory L568-586), so the override is
  dispatched in both modes. `aput_writes` -> `put_writes` and `aget_tuple` -> `get_tuple`
  delegate the same way (L588-609, L532-544).
- The override is reached. It just never triggers (§3).
- Caveat: this delegation is an implementation detail of InMemorySaver. Overriding `aput` too
  (or hooking a shared helper) would be more robust.

### 6. A public deletion API exists: `delete_thread` / `adelete_thread`

- `BaseCheckpointSaver` declares `delete_thread`, `adelete_thread`, `delete_for_runs`,
  `copy_thread`, `prune`/`aprune` (`checkpoint/base/__init__.py` L344, L536, L398, L585).
- `InMemorySaver.delete_thread` (memory L514-530) removes `storage[thread_id]` and **every**
  `writes` and `blobs` key whose `k[0] == thread_id`. `adelete_thread` delegates to it (L611-620).
  Verified: after one run the counts were storage/writes/blobs = 1/3/9. After `adelete_thread`
  they were 0/0/0.
- InMemorySaver does **not** implement `prune` (the base class raises `NotImplementedError`).
- Cost: `delete_thread` scans every key in `writes` and `blobs`, so it is O(total entries).
  With cap 1000 that is acceptable per eviction, but not free.

## Recommendation (for an implementation ticket; no source was changed here)

1. Replace the hand-rolled pops with `self.delete_thread(oldest)`. This frees storage, writes
   and blobs through the public API.
2. Fix the trigger. Track live threads in a separate structure that the saver owns, for example
   an `OrderedDict[str, None]` updated in `put`/`put_writes`. Bound that instead of testing
   `thread_id not in self.storage`, which `get_tuple` pollutes. Use `move_to_end` on access if
   LRU is wanted. Otherwise fix the docstring to say FIFO.
3. Guard with a lock, or keep the logic synchronous with no awaits between check and delete.
   The current sync path is atomic on the event loop. Keep it that way.
4. Replace or extend the unit test so it drives eviction through `ainvoke` on a compiled graph,
   asserting that `len(storage)`, the thread-ids in `writes` and the thread-ids in `blobs` all
   stay `<= max_threads`.
5. Consider whether per-request checkpointing is needed at all. Every request gets a fresh
   thread_id, so no state is ever resumed, and `X-Thread-ID` never resumes a prior run either.
   If the checkpointer only serves the cyclic refine/repair loop within one run, the app could
   call `adelete_thread(execution_thread_id)` in a `finally` after `ainvoke`. That bounds memory
   by concurrency rather than by a cap.
6. Move the cap into `config.py` and `.env.example` per project conventions. Consider adding an
   upper bound to the langgraph pin, since this relies on InMemorySaver internals.

## Sources

- `.venv/.../langgraph/checkpoint/memory/__init__.py` (langgraph-checkpoint 4.3.0): L69-95 layout,
  L239-288 get_tuple, L430-512 put/put_writes, L514-530 delete_thread, L568-620 async wrappers, L634 alias.
- `.venv/.../langgraph/checkpoint/base/__init__.py`: L344 delete_thread, L398 prune, L536 adelete_thread, L585 aprune.
- `.venv/.../langgraph/pregel/_loop.py` (langgraph 1.2.16): L1719 `.put`, L1975 `.aput`.
- Repo: `src/resilient_triage/graph/builder.py` L28-51, L183-188; `src/resilient_triage/api/app.py` L225-226;
  `tests/test_graph.py` L387-398.
- Empirical probe script (throwaway, run against the `.venv`, not committed).
