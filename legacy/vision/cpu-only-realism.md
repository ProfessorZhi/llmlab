# Can a real image question be answered on this box without the GPU?

**No — not with the weights that exist here.** This is an estimate, made from
readings only. Nothing was started, no model was loaded, no GPU was touched.

The question is worth answering precisely, because two different things get
called "CPU vision" and only one of them is possible:

| step | what it is | CPU-feasible here? |
|---|---|---|
| 1 | picture -> bytes the decoder accepts | **yes**, pure Python (tested) |
| 2 | bytes -> image embeddings | **yes**, `strata-vision` on CPU (measured) |
| 3 | embeddings + a text model -> an answer | **no** — see below |

Steps 1 and 2 are covered by the tests in this directory. Step 3 is the one
that answers a question about a picture, and it is the one that cannot run.

---

## The readings

Measured on `{{LAB_HOSTNAME}}`, 2026-10-13, read-only.

### Memory

```
              total        used        free      shared  buff/cache   available
Mem:          31287       11975        1077        4631       23322       19311
Swap:          8191         465        7726
```

31.3 GiB of RAM, **19.3 GiB available**. 20 cores.

### What is already resident

| process | owner | RSS |
|---|---|---|
| `strata --serve …` (the live engine) | {{LAB_ADMIN_USER}} | **6.23 GiB** |
| `serve/server.py` (its front end) | {{LAB_ADMIN_USER}} | 217 MiB |

The live engine holds 6.2 GiB of the box, and it must keep holding it: it is
serving the production model and this work is not allowed to disturb it.

### The weights on disk

| file | size |
|---|---|
| `Qwen3.8-Flash-Next-…-00001-of-00002.gguf` | **36.53 GB** |
| `Qwen3.8-Flash-Next-…-00002-of-00002.gguf` | **26.82 GB** |
| `mmproj-Qwen3.8-Flash-Next-BF16.gguf` | 0.85 GiB |
| `mtp-q2_0.gguf` | 0.83 GiB |

**63.35 GB of text weights and no smaller alternative.** Everything else that
ends in `.gguf` under `{{STRATA_ROOT}}` is a tokeniser vocabulary stub (4–15 MB),
which is what the vision tool opens with `vocab_only = true` — a vocabulary is
not a model and cannot generate anything.

### Why step 3 is out

The 63.35 GB is the *whole* model, and it must be resident to generate: 63.35 GB
against 19.3 GiB available, on a box whose remaining memory is already holding a
6.2 GiB engine that must not be evicted. Even the smaller shard alone (26.82 GB)
does not fit. There is no quantisation smaller than the one already deployed,
and downloading another is out of scope.

Three further reasons, each sufficient on its own:

1. **There is no small matching model, and the pairing is not interchangeable.**
   `strata-vision` produces embeddings for the *text model it was built for* —
   the projection width has to match. A different small model cannot be dropped
   in behind a Qwen3.8 mmproj and asked a question.
2. **The only thing that consumes those embeddings is the `strata` engine**, via
   its `GENI` protocol, and that binary links CUDA directly:
   `libcudart.so.13`, `libcublas.so.13`, `libcublasLt.so.13`. It has a `--cpu`
   string in it, but it is a GPU build that dlopens CUDA at startup — and it is
   the same binary the production service runs. Starting a second copy is a
   production-shaped action, not a test.
3. **NVML is broken on this host**: kernel module `595.58.03` against library
   `595.99.02`, so `nvidia-smi` fails with *Driver/library version mismatch*.
   Anything that initialises CUDA here is unreliable, and the standing rule is
   that no restart of production Strata happens while that is true.

### What *is* proven on CPU

An earlier round ran the encoder alone under `systemd-run --user --scope` with
`MemoryMax=4G`, `CPUQuota=200%`, `nice -n 19` and no `--gpu`:

```
READY 2560          (projection width)
OK 9 3 3 228        (9 image tokens, 3x3 grid, 228 ms)
peak RSS 981 MiB    wall 1.18 s    exit 0
```

So step 2 is real and cheap. It is also, on its own, useless: an embedding
answers no question until a text model reads it.

---

## What a maintenance window would have to include

For the record — none of this is requested by this round, and none of it can be
done by `{{LAB_USER}}`.

**Prerequisites, in order:**

1. **NVML repaired and verified.** The kernel module and the library must agree,
   and a reboot must come back with `nvidia-smi` working. Until that holds, the
   rule stands: no restart of production Strata, even with approval, because a
   rollback that cannot be relied on is worse than not deploying.
2. **`strata-vision` built for the GPU** (`-DSTRATA_VISION_CUDA=ON`). The build
   in the isolated workspace is CPU-only and byte-identical to nothing on the
   node; a GPU encoder is a different build.
3. **The `mmproj` placed** at `{{STRATA_DATA_ROOT}}/models/IQ2_XS/` (907,543,008 B,
   sha256 `b1a82259…9bd0`). Adding a file does not disturb the running service.
4. **A config change and a restart**, by `{{LAB_ADMIN_USER}}`: a `"vision"` section plus
   `--vision` in the engine's `args`, then a restart. This is the only step that
   interrupts service.
5. **Acceptance that actually tests comprehension**: `/health` reports
   `"images": true`, and then **two different pictures get two different
   answers** on that instance. A single YES is not acceptance — the whole point
   of `consumer_e2e.py`'s control is that a service which cannot see also says
   YES.

**Estimate for step 4's cost**, from the project's own numbers: a GPU encoder
takes roughly 1.2–1.4 GiB of VRAM and is reserved with `--vram-reserve-mib 700`;
the recorded text-throughput cost on this same model is prefill 468 → 458 tok/s,
decode 77.1 → 74.0 tok/s, expert slots 4568 → 3542. Whether that trade is
acceptable is a production decision, not a technical constraint.

**Until step 1, none of the rest can be scheduled.**

---

## What this means for the tests in this directory

`consumer_e2e.py` runs the full protocol against a stand-in, on the CPU, in a
few seconds, and prints `MODEL VISUAL COMPREHENSION: NOT PASSED`. That banner is
not a placeholder to be removed when the tests pass — it is the accurate status,
and it stays until an inference run against a vision-enabled instance produces
two different answers for two different pictures.
