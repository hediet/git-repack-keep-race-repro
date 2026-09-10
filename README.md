# Git repack `.keep` race: deterministic object-loss reproduction

[![Demonstrate object loss and workaround](https://github.com/hediet/git-repack-keep-race-repro/actions/workflows/reproduce.yml/badge.svg)](https://github.com/hediet/git-repack-keep-race-repro/actions/workflows/reproduce.yml)

This repository demonstrates a race in **unmodified Git**: geometric repacking
can delete a reachable commit and its tree when a duplicate pack reception
creates a `.keep` file between two scans.

It is relevant to [gitgitgadget/git#2219](https://github.com/gitgitgadget/git/pull/2219).
It uses **only generated, two-commit repositories**, not files or objects from
any real incident. It requires Python 3.10+ and Git (Git for Windows includes the
shell needed by the timing wrapper). No Python packages are required.

## Run

```console
python repro.py
```

The default is three repetitions of all nine cases below. Everything is written
to a **new** `artifacts` directory. The script refuses an existing output
directory; it never opens your working repository for modification and never
cleans up an existing directory. Use a different path to rerun:

```console
python repro.py --output artifacts/second-run --repeat 3
```

To test another Git executable or a patched build:

```console
python repro.py --git /path/to/git --output artifacts/patched --expect fixed
```

The harness ignores inherited `GIT_*` variables and system/global Git
configuration in its synthetic repositories. This does not change your settings.
Repositories and logs are retained for inspection.

## Expected results

Each variant runs a healthy control, the race, and the race with the workaround:

| Variant | No race | Race | Race + `repack.packKeptObjects=true` |
| --- | --- | --- | --- |
| `repack -d -l --geometric=2` | Healthy | Objects lost | Healthy |
| Also `--write-midx` | Healthy | Objects lost | Healthy |
| Also `repack.midxMustContainCruft=false` (follow mode) | Healthy | Objects lost | Healthy |

The follow-mode case verifies that the child actually received
`--stdin-packs=follow`. Every case checks initial `fsck`, final `fsck`, and direct
object lookup **with MIDX lookup disabled**. The vulnerable case must lose the
parent commit and tree and delete their original pack. A healthy result from the
race case is a test failure under `--expect vulnerable`, not silently accepted.

In the vulnerable case, **repack returns zero despite object loss**. It can also
print an `info/refs` update error; this is not a claim of completely silent
success. Both stdout and stderr are retained.

**Green CI means the bug was reproduced AND the controls/workaround passed.**
It does not mean the pinned Git versions are safe. CI tests
Git for Windows **2.55.0.windows.3** and upstream Git **2.55.0**, three times each.
Read the job summary or download the evidence artifact for the outcome table,
object IDs, subprocess arguments, and logs.

Both platforms reproduce the loss on clean GitHub-hosted runners, so this is not
specific to Windows, to a filesystem, or to one machine's configuration.

## Why controlling timing is legitimate

1. Repack sees an existing pack P without a `.keep` and selects it for replacement.
2. Before its `pack-objects` subprocess starts, the wrapper invokes **real Git
   `index-pack --stdin --keep`**, receiving a duplicate of P.
3. The wrapper verifies that Git created P's `.keep`, then runs the real,
   unmodified packing subprocess.
4. On vulnerable Git, `pack-objects --honor-pack-keep` skips P's objects, but the
   parent repack still deletes P based on its earlier selection.
5. Reachable objects are gone.

The wrapper forces a legal scheduling order instead of waiting for a random
overlap. Leaving `.keep` present models the receiving parent not yet reaching its
cleanup step. There is no enforced deadline requiring that cleanup to precede
repacking. No object files are manually deleted during the race.

The fixture setup uses normal `pack-objects` and `prune-packed` to create two
packs, then verifies a healthy repository **before** testing.

This is **not** a full network-fetch or VS Code autofetch test, a replay of a
recorded historical process schedule, or a measurement of how frequently the
race happens naturally. No claim is made that every real-world missing-object
incident has this cause.

## Workaround and upstream verification

Command-scoped `repack.packKeptObjects=true` prevents this reproduced failure.
It makes repacking include objects in kept packs instead of independently
excluding them. It can increase repacking work and storage use, and it cannot
restore objects that were already lost.

This repository tests the workaround, **not the upstream patch**. The
`--expect fixed` mode is provided for maintainers to run against a patched build:
all controls, races, and workaround cases must preserve every input object.

The implementation is in [repro.py](repro.py); the complete timing wrapper is
[race-git.sh](race-git.sh). CI is in
[reproduce.yml](.github/workflows/reproduce.yml).
