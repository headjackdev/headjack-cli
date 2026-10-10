"""Tank's two static skills, installed once into the skills root.

`install_gateway` writes `/tank`, a user-invocable entry point that costs zero
resident tokens (`disable-model-invocation`). `/tank <query>` forwards to
`skill_search`; `/tank save ...` dispatches to the save flow — both verbs live in
this one gateway file (`P3-10`/`P3-09`), not split across two, since the save verb
belongs on the same human-typed entry point the search verb already uses.

`install_workflow_skill` writes `tank-workflow`, a different kind of skill: meant
to be read *by the model*, not typed by a human, teaching the decision process for
when to proactively ask Tank for knowledge and when to proactively hand new
knowledge back — the autonomous case `/tank` cannot cover, since `/tank` only
fires when a human remembers to type it. Unlike the gateway it has no
`disable-model-invocation`, so it does cost resident tokens — a deliberate
trade-off against the Tier-0 budget, accepted because the autonomous trigger
is the whole point of asking for it.

Named `tank`, matching the role Tank already has everywhere else
(Trinity asks, Tank loads, Headjack holds). A human talks to Tank the same way
Neo does in the chair — "Tank, I need a pilot program for a B-212 helicopter" —
and Tank is who goes and loads it from Headjack's catalog, or carries new
knowledge back in. The `headjack` CLI (`headjack init`/`search`/`publish`/...)
keeps the Headjack name on purpose: that is operator/platform tooling, a
different audience from this skill.

Tank *activates* what it loads under the `tank-<scope>-<name>` prefix (`P3-04`),
so the thing a human asks and the things it hands back share one namespace, not
two. The gateway shipped briefly under the name `headjack` before moving back to
`tank` — `install_gateway` removes that old `headjack/` directory so a fresh
install doesn't leave a stale, duplicate skill behind.

The gateway is a deliberately scoped-down slice of `P3-10`/`P3-09`'s full design:
it installs the forwarding/dispatching skill and nothing else. The
`manual_invocation` telemetry (needs `P4-04`), manifest/eviction integration
(needs `P3-05`/`P3-06`), and the activated-skill-prefix-coexistence guarantee
(needs `P3-04`, itself still a stub) are deferred until those land — none of them
are buildable honestly today, and a gateway that silently claims to do them would
be worse than one that doesn't try.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_SKILL_NAME = "tank"
_OLD_SKILL_NAMES = ("headjack",)  # removed on install so the reversal doesn't leave a duplicate

_SKILL_MD = """\
---
name: tank
description: >
  Loads specialist knowledge from Headjack on request, and saves new or
  corrected knowledge back.
disable-model-invocation: true
---

# /tank — ask Tank directly

Invoked as `/tank <query>` or `/tank save ...`.

## Asking: `/tank <query>`

Call the `skill_search` MCP tool with `<query>` as the `query` argument, then
present its results to the user exactly as returned — do not summarize,
re-rank, or filter them yourself.

## Saving: `/tank save ...`

When the argument starts with `save`, the user wants to hand new or corrected
knowledge back to Headjack — treat the rest of the argument, plus whatever was
already discussed this session, as the material to save.

1. **Draft the artifact**: a `name` (short, matches how someone would search
   for this), a `kind` (`skill` for something installable and procedural,
   `runbook` for reference-only operational detail), and a `body` (a clean
   writeup — headed sections, like any other artifact in the catalog — not the
   raw back-and-forth that produced it).
2. **Strip anything that shouldn't travel**: secrets, tokens, credentials,
   anything private to this session that wouldn't be true or appropriate for
   whoever searches this next.
3. **Show the user the exact draft and wait for them to say yes.** Tank never
   writes to the shared catalog unconfirmed — the same way you wouldn't publish
   on someone's behalf without showing them first.
4. Once confirmed, call Tank's artifact-save tool with `name`, `kind` and
   `body`. If it reports the artifact already exists, it hands back the current
   hash — pass that back as `supersedes_hash` to confirm you mean to replace
   it, confirming the replacement with the user the same way, and never retry
   blind.
5. Relay the tool's result to the user exactly as returned — it already names
   Tank as the one who saved or refused.

This skill exists so a user can ask or tell Tank by hand, on demand, without the
model having to notice first. There is no other logic here: if this needs to do
more than forward to search or the save tool, that is a different issue, not an
addition to this file.
"""


def install_gateway(skills_root: Path) -> Path:
    """Write the `/tank` gateway skill into `skills_root`. Idempotent:
    re-running overwrites the same file rather than duplicating it. Also removes
    any skill directory under `_OLD_SKILL_NAMES` — cleanup for the `headjack` ->
    `tank` reversal, so a fresh install never leaves both behind."""
    for old_name in _OLD_SKILL_NAMES:
        old_dir = skills_root / old_name
        if old_dir.exists():
            shutil.rmtree(old_dir)

    skill_dir = skills_root / _SKILL_NAME
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_SKILL_MD)
    return skill_path


_WORKFLOW_SKILL_NAME = "tank-workflow"

_WORKFLOW_SKILL_MD = """\
---
name: tank-workflow
description: >
  Check Tank before starting work, whatever the work is. Tank is the user's own
  skill and knowledge database — the procedures, conventions and specialists
  they have accumulated across everything they work on, not one subject area.
  Look for what already covers this task before deciding how to do it, ask the
  user when the catalog has nothing, and save back what you worked out. Read
  this at the start of a task, not once you are stuck.
---

# Working with Tank

Tank is the operator who loads the user's own knowledge on request — their
procedures, their conventions, the way they want a recurring kind of work
done, prior fixes: anything that isn't common knowledge and lives in
Headjack's catalog instead of a loaded skill. Over time the catalog
accumulates all of it, in the exact form the user wants — it is a shortcut for
work you would otherwise redo from first principles every session. Tank can
also take knowledge back from you: what you and the user just worked out, so
next time it is a lookup instead of a rediscovery.

**Whatever the topic.** Runbooks and incident fixes are the catalog's most
obvious inhabitants, so it is easy to read this skill as being about infra
work. It is not. Headjack holds whatever the user has accumulated about how
their work gets done, across every domain they work in; the catalog's scope is
the user, not a subject area. So do not decide from a task's flavour that Tank
is unlikely to have anything — "this one is just engineering", "this isn't
ops", "this is a writing task" are the same mistake wearing different clothes,
and the check is cheap enough that the guess is never worth making. A task
touching code does not make it exempt either.

**Check first, not last.** A model does not usually notice it "doesn't know"
how the user wants a thing done — it investigates and improvises a way
through, quietly, without ever surfacing an explicit gap to check against.
That means a trigger based on noticing ignorance will rarely fire. Treat
checking Tank as a default step before starting, not as a fallback for when
you are stuck: the user's own way of doing this already exists more often than
a prompt to "ask when unsure" would suggest, and working it out from scratch
when Tank already had it throws away the reason the catalog exists. Check
before you start exploring, not after.

## Before you start: check Tank

1. **Check what's already loaded.** If a loaded skill already covers this,
   use it — don't ask Tank for something you already have.
2. **Cheap check first: `skill_probe`.** One call, many terms — pass every
   identifier, error string, or keyword relevant to the task at hand, all at
   once:

       skill_probe(["BackoffLimitExceeded", "cassandra hinted handoff"])

   Each answer is a count plus names. A `0` means nothing nearby — believe
   it, but it's lexical-only (no paraphrase matching) and can be a few hours
   stale, so a near-miss phrasing can still be worth a real search.
3. **Escalate to `skill_search`** when probe signals something, or the task is
   specific enough that the catalog plausibly already has it. This is the
   expensive, semantic, ranked step — real content, a snippet, and whether the
   result is `installable`.
4. **`skill_install` an installable result** before relying on it, so it's
   verified and active rather than quoted from a search result, then follow it
   rather than reconstructing your own approach alongside it.
5. **Once Tank genuinely has nothing** — not merely "probe returned a low
   count" — say so plainly and ask the user before going solo: "Tank has
   nothing for this — do you have a specialist or an existing process for it,
   or should I proceed on general judgment?" The catalog lags what the user
   knows, and the answer is often a person or a convention that was never
   written down. Skip the question only if the user has already said, in this
   session or a remembered one, that nothing exists for this kind of work.
6. **Then work it out yourself**, with the user as needed. That is the
   uncommon path, not the default one.
7. **If Tank is unreachable**, the tools say so plainly — don't retry
   silently; fall back to what's already installed and say search is
   degraded.

## After you finish: give Tank what you learned

Whenever step 6 above was needed — Tank had nothing, and you and the user
worked it out the hard way — that outcome should not evaporate at the end of
the session. This is not conditional on noticing something was wrong; not
having had a procedure for this task at all is itself the trigger.

1. **Draft the artifact**, don't just save the raw transcript: a `name`
   (short, matches how someone would search for this), a `kind` (`skill` for
   something installable and procedural, `runbook` for reference-only
   operational detail), and a `body` (the clean writeup — headed sections,
   like any other artifact in the catalog — not the back-and-forth that
   produced it).
2. **Strip anything that shouldn't travel**: secrets, tokens, credentials,
   anything private to this session that wouldn't be true or appropriate for
   whoever searches this next.
3. **Show the user the draft and ask before saving.** Tank never writes
   immediately, for real, into the catalog everyone searches — never call the
   save tool unconfirmed, the same way you wouldn't publish on someone's
   behalf without showing them first.
4. **Call Tank's artifact-save tool** once they say yes. If it already exists,
   Tank tells you the current hash — pass it back as `supersedes_hash` to
   confirm you mean to replace it, never retry blind.

## When to try any of this

Before starting a task, every time, whatever the task is — not only when you
would otherwise say "I don't know how to do that." The exception is narrow:
a mechanical one-off with no approach to choose needs no lookup. Everything
where you are about to decide *how* to go about it is a check, because that
decision is exactly what the user may already have made once and stored.
Checking first is the point: Tank existing is pointless if you keep
re-deriving what it already has.
"""


def install_workflow_skill(skills_root: Path) -> Path:
    """Write the model-facing `tank-workflow` skill into `skills_root`. Unlike
    the gateway, this one is meant to be read by the model — no
    `disable-model-invocation`, so its description competes for triggering
    relevance the same way any other skill's does. Idempotent, same as
    `install_gateway`."""
    skill_dir = skills_root / _WORKFLOW_SKILL_NAME
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_WORKFLOW_SKILL_MD)
    return skill_path
