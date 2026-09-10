# Git repack `.keep` race: deterministic object-loss reproduction

[![Demonstrate object loss and workaround](https://github.com/hediet/git-repack-keep-race-repro/actions/workflows/reproduce.yml/badge.svg)](https://github.com/hediet/git-repack-keep-race-repro/actions/workflows/reproduce.yml)

**Unmodified Git can delete a reachable commit.** If a fetch lands while
`git repack --geometric` is running, repack can delete a pack whose objects the
new pack deliberately left out. `repack` still exits `0`; the loss only shows up
later, when something tries to read the missing object.

This reproduces it on demand, with synthetic repositories, in about two seconds.
Related to [gitgitgadget/git#2219](https://github.com/gitgitgadget/git/pull/2219).

## What goes wrong

Start with a repository holding two packs and a clean `git fsck`:

```
pack P  →  parent commit (+ its tree and blob)
pack Q  →  tip commit    (+ its tree and blob)
```

A `.keep` file next to a pack means *"leave this pack alone, someone is using it"*.
Neither pack has one yet. Now run `git repack --geometric=2 -d`:

| Step | What happens |
| --- | --- |
| 1 | **repack scans the packs.** Neither P nor Q has a `.keep`, so it plans to combine both into one new pack and then delete both. |
| 2 | **A fetch finishes right now.** `index-pack --keep` writes the pack it received — a duplicate of P, so the same objects and the same pack name — and creates `P.keep`. |
| 3 | **repack starts its `pack-objects` child, which scans the packs again.** This time `P.keep` exists, so `--honor-pack-keep` skips every object in P. The new pack gets only the tip commit. |
| 4 | **repack deletes P and Q**, carrying out the plan it made in step 1. Nothing rechecks the `.keep` that appeared in between. |
| 5 | **The parent commit, tree and blob are gone.** `repack` exits `0`. `git fsck` now reports a broken link to a missing commit. |

The bug is that steps 1 and 3 use **two different snapshots of the `.keep` files**.
Step 3 excludes objects on the assumption that P will survive; step 1 already
decided that P will not.

Nothing here is exotic: a fetch and an automatic repack overlapping is ordinary
behaviour on a busy repository, and Git offers no guarantee that a fetch's `.keep`
cleanup happens before a concurrent repack looks.

## Run it

```console
python repro.py
```

Needs Python 3.10+ and Git; no packages to install. On Windows, Git for Windows
supplies the shell used by the timing wrapper.

Everything lands in a **new** `artifacts` directory — the script refuses to reuse
an existing one, never touches your own repositories, and ignores your global and
system Git configuration. Output looks like this:

```
01-geometric-control:     fsck 0 -> 0; repack exit 0; missing []; PASS
01-geometric-race:        fsck 0 -> 2; repack exit 0; missing ['parent_blob', 'parent_tree', 'parent_commit']; PASS
01-geometric-workaround:  fsck 0 -> 0; repack exit 0; missing []; PASS
```

`PASS` means *the observed outcome matched the expectation*. On the race line it
confirms the bug happened — it does not mean Git is fine.

Useful options:

```console
python repro.py --output artifacts/second-run   # rerun without deleting evidence
python repro.py --git /path/to/git --expect fixed   # check a patched build
```

## What is checked

Three repack variants, each run as a healthy control, as the race, and as the
race with the workaround, repeated three times (27 cases):

| Variant | No race | Race | Race + `repack.packKeptObjects=true` |
| --- | --- | --- | --- |
| `repack -d -l --geometric=2` | Healthy | Objects lost | Healthy |
| Also `--write-midx` | Healthy | Objects lost | Healthy |
| Also `repack.midxMustContainCruft=false` (follow mode) | Healthy | Objects lost | Healthy |

Every case checks `fsck` before, `fsck` after, and direct object lookup **with
MIDX lookup disabled**, so a stale multi-pack index cannot disguise a present or
absent object. The race case must lose the parent commit and tree *and* delete
their original pack; a healthy result there is reported as a failure, not quietly
accepted. The follow-mode case additionally verifies that the child really
received `--stdin-packs=follow`.

In the race cases repack returns zero despite the loss. It may also print an
`info/refs` update error, so this is not a claim of completely silent success.
Both streams are kept in the evidence artifact.

## Is it fixed upstream?

CI builds two Git trees from source and runs the same harness against both:

| Build | Expectation |
| --- | --- |
| `gitgitgadget/git` master, unpatched | must still lose objects |
| `refs/pull/2219/head` (the proposed fix) | must preserve every object |

Both expectations currently hold. With an identical injection on the same runner,
unpatched master `b8242b0` ends at `fsck` exit 2 with the parent commit, tree and
blob gone, while patched `a18e354` ends at `fsck` exit 0 with every object intact
across all 27 cases — so **the proposed fix resolves exactly this failure**.

This is what makes the badge meaningful: it fails if the bug stops reproducing on
unpatched Git *or* if the proposed fix stops preventing it. The exact commits
tested are printed in each run's job summary. Released builds are covered too —
Git for Windows **2.55.0.windows.3** and upstream Git **2.55.0** — and both lose
objects on clean GitHub-hosted runners, so this is not specific to Windows, to a
filesystem, or to one machine's configuration.

## Workaround

Command-scoped `repack.packKeptObjects=true` prevents every failure reproduced
here. It makes repacking include objects from kept packs rather than excluding
them, which removes the inconsistency between the two scans. It can increase
repacking work and storage use, and it cannot restore objects already lost.

## How the timing is forced

A wrapper named `git`, placed first in `GIT_EXEC_PATH`, intercepts the moment
repack launches `pack-objects`. It then runs **real, unmodified Git**
`index-pack --stdin --keep` on a duplicate of pack P, checks that Git created
`P.keep`, and finally execs the real `pack-objects`. That is the whole trick; it
is [21 lines](race-git.sh).

No pack, `.keep` file or object is ever created or deleted by hand, no Git source
is modified, and the fixture is built with ordinary `pack-objects` and
`prune-packed` and verified healthy before each test. The wrapper only forces a
legal ordering instead of waiting for a coincidence, and leaving `.keep` in place
models a fetch that has not yet reached its cleanup step.

## Scope and limits

- This is a controlled interleaving, **not** an end-to-end network fetch test and
  **not** a replay of any recorded incident.
- It does not measure how often the race occurs naturally.
- It makes no claim that any particular real-world missing-object report has this
  cause.
- The repositories are generated two-commit fixtures. No data from any real
  repository is included.

## Files

- [repro.py](repro.py) — the harness
- [race-git.sh](race-git.sh) — the timing wrapper
- [.github/workflows/reproduce.yml](.github/workflows/reproduce.yml) — CI
