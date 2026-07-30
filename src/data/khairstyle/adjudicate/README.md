# Adjudication UI

A standalone, **blinded** labelling app for Experiment 1 (attribute sufficiency /
minimality). It is deliberately separate from the attribute browser in `../ui/` —
that one shows everything; this one hides everything that could bias a verdict.

Raters do two kinds of task:

- **Split (pairs)** — two hairstyles that differ in exactly one attribute: *same
  hairstyle or not?*
- **Merge (groups)** — a group of hairstyles the schema calls identical: *cluster
  the ones that are actually the same hairstyle.*

```bash
cd src/data/khairstyle/adjudicate
python3 serve.py                 # http://127.0.0.1:8770
python3 serve.py --port 9000     # alternative port
```

The server reads its tasks from `experiments/<exp>/frame.json` (produced by
`../analysis/mine_frame.py`) and writes verdicts to the `adjudications` table in
`../data/index.sqlite`, keyed by `(experiment, item_id, rater)`.

---

## What the rater can and cannot see (blinding)

Blinding is enforced **server-side** — the browser never receives the information,
so it cannot leak. For each item the client gets only:

- opaque member slots (`m0`, `m1`, …) in a **per-item randomized order**, and
- three fixed view crops per member.

It never receives source ids, the named category, any salon attribute, the
attribute the pair differs on, or which pool/experiment provenance the item came
from. Faces are removed in the crops, and the same-person guard plus id-blinding
are our defence against a rater matching on the wearer rather than the hairstyle.

Each member is shown with the **same fixed three views** — frontal, near-profile,
and back / three-quarter — so layering and silhouette are comparable and pose is
not a confound.

---

## The screen, section by section

### Header (top bar)

| Element | What it does |
| --- | --- |
| **HairPairs · adjudication** | Brand / title. No function. |
| **experiment** dropdown | Picks which frozen frame you label. Each option shows its label and your progress (`done/total`). Changing it reloads the task list and progress for that experiment; your verdicts are stored per-experiment, so switching never mixes labels across frames. The choice is remembered (in `localStorage`). Defaults to the latest experiment. |
| **Pairs (split)** / **Groups (merge)** tabs | Switch between the two task types. The active tab is highlighted. Switching clears the current item and reloads that type's task list. |
| **rater** input | Your annotator id. **You must set this before labelling** — opening a task without it prompts you to. Verdicts and notes are stored under this id (so inter-/intra-rater agreement can be computed). Remembered across sessions. |

Live progress (`done/total`) is shown inline on the selected **experiment** dropdown option and updates after every save; the sidebar also marks completed items.

### Sidebar (left)

| Element | What it does |
| --- | --- |
| **to do** / **done** / **all** chips | Filter the task list below. `to do` hides items you have already labelled, `done` shows only labelled ones, `all` shows everything. |
| **task list** | Every task in the current tab + filter. Each row is numbered and shows `pair` (split) or `group · N` (merge, where `N` = members). A green **✓** marks completed items; the open item is highlighted. Click a row to open it. |

### Stage (center)

- **Empty state** — "Pick a task from the left, or set your rater id." Shown when
  nothing is open.
- **Work header** — the **title** describes the task (e.g. *"Pair — same hairstyle
  or not?"* or *"Group of N — cluster same-hairstyle photos together"*), and the
  **status** on the right reads `unlabeled` or `saved <timestamp>` if you have
  already submitted a verdict for this item.
- **Cards** — the stimulus. One card per member; each card shows its three view
  crops with a small slot label (`frontal` / `profile` / `back`).
- **Controls** — the task-specific buttons (below).
- **Help line** — the keyboard shortcuts for the current task.
- **Your note** — a free-text box for this item. Type a note and click **Save
  note** (notes are independent of the verdict, so you can leave one without
  deciding, and edit or clear it any time by saving empty). Every other rater's
  note on the same item is listed below yours, tagged with their rater id and
  timestamp — so notes are shared, not private. Typing in the box does not
  trigger keyboard shortcuts.

---

## Split task — "same hairstyle or not?"

Two cards, side by side. Decide whether they are the **same hairstyle** under the
operating definition (same cut geometry, length distribution, layering, fringe,
parting, volume, curl form — invariant to wearer, pose, lighting, and colour).

| Button | Key | Action |
| --- | --- | --- |
| **Same** | `S` | Records `same`, saves, advances to the next to-do item. |
| **Different** | `D` | Records `different`, saves, advances. |

The previously-saved choice (if any) is highlighted when you reopen the item.

---

## Merge task — "cluster the ones that are actually the same"

A group of cards the schema considers identical. Your job is to **partition them
into clusters**, where each cluster is one true hairstyle. A group that you split
into 2+ clusters is a *collision*; leaving it as one cluster says the schema was
right here.

How it works: each card carries a coloured **group N** badge and border showing
which cluster it is currently in (everything starts in one cluster). **Click cards
to select them** (selected cards are outlined), then act on the selection:

| Button | Key | Action |
| --- | --- | --- |
| **Same group** | `G` | Merge the selected cards (≥2) into one cluster. |
| **Separate** | `X` | Split each selected card out into its own new cluster. |
| **All one** | — | Reset: put every card back into a single cluster. |
| **All separate** | — | Put every card in its own cluster (all different). |
| **Submit — N groups** | `Enter` | Save the current clustering (the button shows how many clusters you have), then advance. |

When you reopen a submitted group, your saved clustering is restored.

---

## Keyboard shortcuts (summary)

| Key | Everywhere |
| --- | --- |
| `←` / `→` | Previous / next task in the current list |

| Key | Split | Merge |
| --- | --- | --- |
| `S` | Same | — |
| `D` | Different | — |
| `G` | — | Same group (selected) |
| `X` | — | Separate (selected) |
| `Enter` | — | Submit clustering |

(Shortcuts are ignored while the rater-id input is focused.)

---

## Verdicts & storage

- Saving inserts/updates a row in `adjudications`
  (`experiment, item_id, kind, lens, rater, members, shown_gids, verdict,
  created_at, updated_at`). `members` records the source order and `shown_gids`
  the exact crops shown, so a verdict is fully reconstructable later.
- Re-submitting the same item overwrites your prior verdict (keeps `created_at`).
- Verdicts are stored by **source**, not by display index, so the per-item shuffle
  never corrupts a saved label and the same verdict is reusable under any attribute
  lens that groups those sources together.
- **Notes** live in a separate `item_comments` table, one row per
  `(experiment, item_id, rater)` (`comment`, `created_at`, `updated_at`). They are
  kept apart from verdicts so they never affect progress counts, and `GET
  /api/item` returns every rater's note for the item.

## Files

| File | Role |
| --- | --- |
| `serve.py` | Stdlib HTTP server: scans `experiments/`, serves blinded tasks/crops, writes verdicts. |
| `views.py` | Picks the canonical frontal / profile / back triple per source (after-only). |
| `static/index.html` · `static/app.js` · `static/styles.css` | The single-page UI. |
| `experiments/<exp>/frame.json` | The frozen, pre-registered candidate set for one experiment. |
