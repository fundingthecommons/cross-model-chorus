# Writing your own use case

A use case is one TOML file. It gives every voice in the chorus a role, a "hat", for one kind of document, and it says which sensitivity levels the document may be sent at. The runner loads every `.toml` file in this folder, plus any folder you name with `--use-case-dir DIR` or `CHORUS_USE_CASE_DIR`. Adding a use case needs no code change.

The file name is the use case's name: `board-memo.toml` runs as `--use-case board-memo`. Names are lowercase letters, digits and hyphens.

## A minimal file

```toml
purpose = "a board memo, read as the board will read it"
sensitivity = ["confidential"]

[[hats]]
name = "chair"
title = "board chair"
briefs = ["redteam"]
looks_for = '''
You chair this board. You read for the decision the memo asks for, the risks it plays down,
and what you would need to see before you vote.
'''

[[hats]]
name = "treasurer"
title = "treasurer"
briefs = ["refute"]
looks_for = '''
You are the board's treasurer. You read every figure against the others and against the
plan, and you notice what the memo does not say about money.
'''
```

Save it as `board-memo.toml` in a folder of your own, then:

```bash
python3 chorus.py --use-case-dir ~/chorus-use-cases --list-use-cases
```

```bash
python3 chorus.py --use-case-dir ~/chorus-use-cases --use-case board-memo --file memo.md --dry-run
```

A use case allowed only at `confidential` runs on the TEE voices alone, so even its dry run needs `PHALA_API_KEY` set and an approved upstream for each TEE voice in `CHORUS_CONFIDENTIAL_UPSTREAMS` (`python3 chorus.py --list-models --voices glm` shows the names; see the main README, Upstream pinning). Without them it refuses and sends nothing. For a first try, allow `internal` too and pass `--sensitivity internal` with a synthetic memo.

## Every key

Top-level keys come first in the file, before any `[table]` header: TOML puts a key written after a header inside that table, and the loader will tell you so.

| Key | Required | What it does |
|---|---|---|
| `purpose` | yes | One line saying what the use case reviews. It is printed in the plan and in `--list-use-cases`. |
| `sensitivity` | yes | The levels the document may be sent at: any of `public`, `internal`, `confidential`. A use case allowed only at `confidential` implies it; any other use case makes the operator declare `--sensitivity` every run. |
| `redacted` | no | `true` means the use case runs only on a redacted copy: the operator passes `--redact NAME=ROLE` or confirms `--redacted`. Use it for documents that name counterparties or founders. |
| `shared_role` | no | Text appended to every hat's role, in both rounds. Use it for a rule every hat must follow, such as whose side the reviewers take, or what they may judge against. A rule placed in one brief is easy for a sibling brief to sidestep; a rule in the role is not. |
| `[[hats]]` | yes, one or more | Each hat has a `name` (lowercase, used in `--hats` and in the report), a `title`, the `briefs` it runs, and `looks_for`: the role prompt, written as "You are ... You read for ...". |
| `[briefs.<name>]` | no | A brief of your own: `text` is the instruction, and `reads_criteria = true` marks a brief that reads the funder's published call (`--criteria`). A use case cannot redefine a core brief or name a brief after a top-level key, and every brief it defines must be run by some hat. |
| `[extra_briefs.<level>]` | no | Briefs a hat adds at one sensitivity only, as `hat = ["brief"]`. The shipped copy use case uses it so the fundraiser also reads as the funder's screener at `internal`. |
| `[sensitivity_notes]` | no | A line per level, printed in the plan as a `check:` reminder of what the operator must confirm before sending at that level. |
| `[output]` | no | `verdict`: what each reviewer's two- or three-sentence "overall" must answer (the default is "the verdict a careful recipient would reach"). `rewrites`: the levels at which `--rewrites` may ask for suggested wording; leave it out to keep the use case verifier-only. `guidance`: a paragraph printed in the report's "How to use this" section. |

Multi-line strings fold to single spaces when loaded, so wrap prose however you like. Literal strings (`'''...'''`) need no escaping.

## The core briefs

Any hat may run these, as well as the use case's own briefs.

| Brief | What it asks |
|---|---|
| `refute` | Fact-check every checkable claim, figure, date and name; prefer "unverifiable here" to agreement. |
| `tells` | Find the structural tells of AI-written prose: shape and symmetry, not vocabulary. |
| `redteam` | Read as a skeptical, high-stakes recipient: the strongest objection, ranked by damage. |
| `fit` | Read as the funder's screener against the published call (`--criteria`), or the ask the draft states. |

## How hats are dealt

Every hat is worn on every run. The runner shuffles the hats with a seed and deals them over the voices that will run, so a voice wears several hats when there are more hats than voices. The seed defaults to a hash of the use case, the draft and the local date, which means a `--yes` run on the same day deals exactly as its dry run did, and the deal changes across drafts and days. `--seed` fixes it; `--hats voice=hat` pins a voice's hat. Round 2 keeps the hats: each examiner judges as its own roles.

So think about how many hats you write. At `confidential` only the two TEE voices run, so every extra hat adds a role to one of their answers: the shipped startup use case has four, and one voice wears two or three of them. Three or four is a sensible ceiling there.

## Writing good hats

- **One reader per hat.** A hat is a person with a stake in the document: the counsel, the budget reviewer, the on-call engineer. Say what they read for, in their own terms.
- **Keep hats apart.** If two hats would find the same things, merge them. The strongest signal in a report is the same passage flagged by different families wearing different hats, and that only means something if the hats differ.
- **Ground anything that invites invention.** A model asked about a funder's rules, a market rate or a legal norm will supply one. Tell every hat, in `shared_role`, to judge only by what the document and the published call state, and to say what would need checking.
- **Keep it verifier-only.** Reviewers describe the problem and the kind of fix. Allow `rewrites` only for low-stakes copy.
- **Test it.** A dry run shows the plan and the hat deal and sends nothing. Then try a `--yes` run on a synthetic document before a real one. The offline test suite (`tests/`) shows how to check a use case end to end without a network.

If you write one that others could use, a pull request is welcome (see the main README).
