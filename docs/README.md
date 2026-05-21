# Documentation map

> **Audience:** maintainers and AI agents (ProductScribe, TechnicalScribe,
> QuantDev). Not part of the published site — excluded from `mkdocs.yml`.
> Keep this file in sync with the actual `docs/` tree. If the tree changes,
> this file changes in the same commit.

This is the single source of truth for **what each document is for** and
**where new content belongs**. Both scribes must consult this file in Stage 1
and update it in Stage 4 whenever they create, rename, retire, or repurpose
a doc.

---

## Guiding principles

1. **Concepts over code.** Reference docs explain *why a thing exists, what
   contract it guarantees, and what trap to avoid*. They do not enumerate
   what `grep` would already show. If a reader needs the field list, they
   read the source; if they need to understand the model, they read the
   docs. Code links are welcome; code transcription is not.
2. **Extend before create.** A new fact lives in an existing doc by default.
   A new doc is justified only when (a) there is no existing home where the
   content fits without bending its scope, and (b) the content is large
   enough — roughly one screen of prose minimum — that it would dominate
   the host doc.
3. **One home per fact.** Every claim has exactly one canonical location.
   Other docs link to it. Duplication causes silent drift; consolidate on
   sight.
4. **Document behaviour, not aspiration.** If it isn't in `main` and tested,
   it belongs in [project/roadmap.md](project/roadmap.md), not in a
   reference page.
5. **Status honesty.** Partial, deprecated, or pending surfaces are marked
   inline with the same vocabulary the roadmap uses (`Status: PARTIAL /
   PENDING / SUPERSEDED`).

---

## Taxonomy (Diátaxis-adjacent)

Each tier answers a different reader question. Misclassifying a doc is the
most common way the site bloats.

| Tier | Reader question | Voice | Lives in |
|---|---|---|---|
| **Onboarding** | "How do I get started?" | Friendly, second-person, opinionated path | `getting-started/`, `index.md`, `README.md` |
| **How-to (task)** | "How do I do X?" | Imperative, scoped to one goal | `reference/*-guide.md`, `reference/data-extraction.md` |
| **Reference (contract)** | "What does X guarantee?" | Dry, precise, exhaustive on contracts only | `reference/*-architecture.md`, `reference/event-vocabulary.md`, `reference/metrics-*.md`, `reference/llm-gotchas.md` |
| **Explanation (concept)** | "Why does it work this way?" | Discursive, may include trade-offs and history | sections inside reference docs; rarely a standalone file |
| **Status / planning** | "What is shipped? What is next?" | Curated lists, dated | `project/roadmap.md`, `project/release-process.md`, `changelog.md` |
| **Positioning** | "Should I use this?" | Product voice, decision-oriented | `index.md`, `project/one-pager.md`, `README.md` |
| **Ephemeral plans** | "What is currently being built?" | Internal, deleted when done | `active-plans/` (excluded from site) |

---

## File inventory

Update this section in the same commit as any file change under `docs/`.

### `docs/index.md` — site landing page
Audience: prospective adopters. Owns the one-screen pitch and the table of
differentiators. Mirrors the README but is the canonical adopter-facing
front door of the site. **Owner: ProductScribe.**

### `docs/getting-started/`
Audience: a new user with the package installed. Each doc walks one
end-to-end path. Keep short; deep links to reference for anything beyond
the happy path.

- `quickstart.md` — minimum viable simulation, run, inspect output.

**Owner: ProductScribe.** New onboarding paths go here only when the
quickstart cannot accommodate them.

### `docs/reference/`
Audience: an engineer implementing against the codebase. Every page
specifies a contract, an architecture, or a how-to-task. **Concept density
matters more than line count.**

- `config-system.md` — declarative config, `SimulationBuilder`, templates,
  per-agent delays. *How-to + reference.*
- `custom-agent-guide.md` — full adapter pattern for `TradingAgent`
  subclasses. *How-to.*
- `data-extraction.md` — `parse_logs_df`, L1/L2 book history. *How-to.*
- `event-vocabulary.md` — registered `PayloadSchema` entries and
  `BUS_FORMAT_VERSION`. *Reference.*
- `kernel-architecture.md` — kernel lifecycle, scheduling, message
  delivery. *Reference + explanation.*
- `llm-gotchas.md` — canonical catalogue of None/empty traps and safe
  patterns. **All other reference docs link here for gotcha details — do
  not duplicate.** *Reference.*
- `logging-architecture.md` — EventBus, sinks, log layout. *Reference.*
- `metrics-algorithms.md` — formulas, citations, edge cases for each
  metric. *Explanation.*
- `metrics-api.md` — public surface for computing metrics. *How-to +
  reference.*
- `parallel-simulation.md` — multiprocessing, RNG hierarchy, log layout
  under sweeps. *How-to + reference.*

**Owner: TechnicalScribe.** New reference pages require a justification
recorded in this map (see "When to create a new reference doc" below).

### `docs/project/`
Audience: evaluators, governance readers, and contributors deciding
whether and how to engage.

- `one-pager.md` — full positioning, differentiators, target audiences.
- `roadmap.md` — shipped / in-progress / planned, with status vocabulary.
- `release-process.md` — versioning policy, changelog discipline.
- `reproducibility.md` — reproducibility as a positioning claim and the
  guarantees that back it.

**Owner: ProductScribe.**

### `docs/active-plans/`
Audience: QuantDev (and the user reviewing a plan). Ephemeral working
files; deleted when the plan ships. Excluded from `mkdocs.yml`. **Owner:
QuantDev.** Scribes do not write here.

### `docs/changelog.md`
Mirror of the root `CHANGELOG.md` for the published site. **Owner:
ProductScribe.**

---

## When to create a new reference doc

Default to **no**. Extend an existing page first. A new page is justified
only when **all** of the following hold:

1. The topic does not fit inside any existing page without forcing that
   page's scope to expand beyond its title.
2. The content is at least one full screen of substantive prose (rough
   guide: ~150+ lines), not a stub that will sit empty.
3. The topic will be referenced from multiple other docs (i.e. it deserves
   to be a link target).
4. You can write a one-sentence scope statement for the new page that does
   not overlap with any existing page's scope.

If those conditions are not met, find the right section of an existing
page and extend it. Then add a cross-link from the section, not a new file.

When a new doc *is* created:

- Add it to this map (file inventory) in the same commit.
- Add it to `mkdocs.yml` nav under the appropriate section.
- Pick the right tier from the taxonomy table and write in that voice.

## When to consolidate

If two docs cover overlapping ground, the smaller one is folded into the
larger one and a redirect is left if the URL was public. Scribes are
encouraged to flag duplication during Stage 1 orientation and propose a
consolidation in the same change.

---

## `mkdocs.yml` ownership

- **TechnicalScribe** edits the `nav:` entries under *User Guides*,
  *Architecture Reference*, and *For LLM / Agent Builders* when adding,
  moving, or retiring reference pages.
- **ProductScribe** edits everything else in `mkdocs.yml`, including
  *Home*, *Getting Started*, *Operations & Governance*, and any
  structural changes (new top-level section, theme, plugins).
- Either scribe must run `uv run mkdocs build --strict` after touching
  `mkdocs.yml` and resolve every warning before committing.

---

## Anti-patterns

- **A doc that restates the field list of a Pydantic model.** Link to the
  source; explain *what the model is for* and *what invariants it
  enforces* instead.
- **A doc whose only content is a code block.** That belongs in a
  docstring or a notebook, not a reference page.
- **A new file for every new feature.** Most features extend an existing
  contract; extend the existing doc.
- **A file in `docs/reference/` written in marketing voice.** Reference
  prose is dry. Marketing belongs in `index.md` and `project/one-pager.md`.
- **A claim in two places.** One canonical home; the other links.
