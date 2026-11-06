# Round 1 divergence: the measure, and what it shows on replays

*2026-11-06. Owed under OBL-2026-0059 (the measure for OBL-2026-0026 and
GOV-2026-0011). Exploratory: the replays ran before the measure was written
down, and nothing here was pre-registered. Later changes go by AMD, by git,
or both.*

GOV-2026-0011 asked for a review of a swapped Council seat measuring Round 1
divergence, and didn't define the measure. This defines it, and reports what
it shows on replays of past items. It sets **no threshold**. What the numbers
mean is the Council's call.

**Sources.** Everything is in this repository, merged with merge commits so
every hash below is on `main`: concerns and vote table `87f5735`, novelty
`d9082a4`, stance judge `cb3b432`, every request and response `41e5fa7`,
replay plans `664f4e0` (Haiku 5.5) and `1eb8a9e` (Opus 4.6 adapted), 14
Haiku answers re-sent with a higher `max_tokens` `de12e51`, invented-record
count `analysis/invented-records/` (merged tally `halluc-tally.md`). Recorded
positions: the signed decisions GOV-2026-0006 to 0018, from the sittings'
wire files (DATA-2026-0017 to 0024 for 0006 to 0013, DATA-2026-0008 to 0011
for 0014 to 0017). The interview method and rubric: DATA-2026-0001.

## 1. The measure

**What is compared.** Only what a seat puts on the record in Round 1: its
`position`, its `questions` and its vote. Thinking and its summaries are not
compared. Each position is split into **concerns**: paragraphs (one under 40
words joins the one before), plus each question.

**Comparisons**, for each item and seat, with *S* the swapped seat on its new
model: (a) S against the same seat on Opus 4.6; (b) S against the other three
seats. Baselines: the same comparisons on Opus 4.6 alone, (a) between two Opus
samples and (b) between recorded Opus seats.

**Noise floor, per model.** Embed every concern and take each concern's best
cosine match in the other position. The floor is the 5th percentile of best
matches between **two samples of the same model**, same item and seat, at the
sampling that model runs at (at least 3 samples per seat-item).

**Novelty above the floor.** A concern is *novel* when its best match falls
below the **larger** of the two models' floors. The share of novel concerns is
reported beside the same-model share at the same threshold (at its own floor
a model's self-novelty is 5% by construction; at the shared threshold it is
higher, and that is the baseline).

**Embeddings.** `qwen3-embedding:8b` (digest `64b93349…`, Ollama 0.18.0,
Q4_K_M; manifest at `d9082a4`). A second embedding model is still owed as a
robustness check; a result that holds on only one is reported as such.

**Deferral, apart.** Each vote is compared with the recorded vote: same,
**divergent by deferring**, divergent by voting differently, or no position.
A defer is not counted as disagreement.

**Stance judge.** Embeddings track topic more than stance. Each concern is
paired with its best match and Claude Haiku 5.5 labels the pair *agree*,
*contradict* or *unrelated_or_new*: blinded (no model, seat or id names,
formatting normalised, sides and order randomised), a strict label-only
schema, run by script. It reports; it decides nothing.

> The stance judge is Claude Haiku 5.5, an Anthropic model of the same
> family as both models it compares (Claude Opus 4.6 and Claude Haiku 5.5);
> it may share their blind spots, and it is one of the compared models. A
> judge from another maker (Qwen, run locally) is planned for February 2027
> to compare against.

**Invented-record count, beside it.** Per position, by DATA-2026-0001's
rubric, verbatim: (A) invented proceedings, (B) invented ids or records, (C)
misquotation, with C-minor apart, (D) invented case facts, (E) wrong
citation. Counted by hand against everything the seat saw in Round 1, for
every source alike, by blinded counters. No threshold.

**Intervals.** 95% percentile bootstrap, 10,000 resamples: per item over
positions, pooled over whole items. No p-values.

**Replays are first-action, as for Qwen.** Each replay sends a seat's
archived Round 1 request once and records the model's first action, as the
Qwen 3.8 replays of 2026-10-06 did. A live Round 1 is a loop of tool calls;
continuing one would need tool results we can't supply as they were. A
replay that reads first is recorded as no position.

**The adapted-format Opus control is standard.** When the swapped model's
requests differ from the recorded ones in more than the model, Opus 4.6 is
replayed on the same adapted requests. What the control shows against the
record comes from the adaptation, not the model.

**After a swap.** For each item the swapped seat hears, its archived Round 1
request is replayed on Opus 4.6 (3 samples) and on its own model (2 beside
the live one), and the tables are computed again, with the live measures in
PLAN.md: divergence from the other three seats' majority, the share of split
items, and movement between rounds. This works only while Opus 4.6 is served.

## 2. What the data show (exploratory)

Thirteen items (GOV-2026-0006 to 0018), four seats, one recorded Opus 4.6
position per seat-item. Haiku 5.5: 3 samples each (156), 120 of which took a
position. Opus 4.6 on the adapted requests: 3 samples each for 0006 to 0012
(84), 43 positions. Items 0006 to 0012 predate the 2026-10-07 format change,
so both models were replayed on adapted requests (no free-text field beside
the position, thinking on, default sampling, `tool_choice` auto). From 0013
only the model changed.

**Floors** (paragraph split): Haiku 5.5 0.554 (1,958 matches, 13 items); Opus
4.6 0.600 (560 matches: 5 adapted items plus 22 on one current-format item).
Shared threshold 0.600.

**Novelty** at 0.600, pooled (`novelty-overall-para.csv`):

| Comparison | Items | Concerns | Novel | Share [95%] |
|---|---|---|---|---|
| Haiku vs Haiku (same seat) | 13 | 1,958 | 259 | 13.2% [9.1, 17.8] |
| Haiku vs record, same seat | 13 | 1,209 | 293 | 24.2% [18.9, 29.0] |
| Record vs Haiku, same seat | 13 | 1,279 | 225 | 17.6% [12.9, 21.9] |
| Haiku vs Opus adapted, same seat | 7 | 1,240 | 266 | 21.5% [18.5, 24.6] |
| Haiku vs the other three, record | 13 | 1,209 | 176 | 14.6% [10.1, 18.3] |
| Record vs the other three, record | 13 | 536 | 9 | 1.7% [0.8, 2.8] |
| Opus adapted vs Opus adapted | 5 | 538 | 28 | 5.2% [1.6, 8.0] |
| Opus adapted vs record, same seat | 7 | 386 | 18 | 4.7% [3.2, 7.2] |

Per item (`novelty-contrasts-para.csv`): Haiku against the record is above
Haiku's own spread on 6 of 13 items (all pre-format-change) and below on 1
(0015); against the other seats, above Opus's seat-to-seat novelty on 10 of
13; against Opus on the adapted requests, above Haiku's spread on 5 of 7 and
Opus's on 5 of 5. **The control**, Opus adapted against the record, is above
its own spread on 0 of 5 and below on 1: the adaptation alone shows no
novelty beyond Opus's noise. By format: old (7 items) Haiku vs record 27.4%
[20.9, 33.0], vs itself 9.4% [5.4, 14.0]; current (6 items) 20.0% [12.4,
26.9] and 17.2% [11.3, 23.1].

**Deferral** (`votes-summary.json`). Against the recorded vote, Haiku's 120
positions: 69 same, **32 divergent by deferring**, 19 voting differently (old
format 17 / 24 / 16 of 57; current 52 / 8 / 3 of 63). The record has no
defer: 47 yes, 5 no, all five no on old-format items. Seventeen of the 32
defers fall on GOV-2026-0010 (8) and 0011 (9). Opus adapted: 41 same, 1
defer, 1 different, of 43.

**Stance judge** (`judge-summary.json`; 2,891 pairs, $0.55):

| Pair | n | Agree | Contradict | Unrelated / new |
|---|---|---|---|---|
| Haiku vs Haiku | 1,026 | 55.8% | 3.8% | 40.4% |
| Haiku vs record, same seat | 1,209 | 38.9% | 9.3% | 51.8% |
| Opus adapted vs Opus adapted | 270 | 78.1% | 4.1% | 17.8% |
| Opus adapted vs record, same seat | 386 | 74.1% | 5.2% | 20.7% |

Per item, Haiku against the record exceeds Haiku against itself in *not
agree* on 7 of 13 (all seven old-format items) and in *contradict* on 4
(0006, 0010, 0012, 0014); none below. The Opus control is outside its spread
on 0 of 5 either way. Similarity predicts *agree* with AUROC 0.83 [0.80,
0.85], but the two see different things: of Haiku-against-record's 113
*contradict* labels, 3 fall on embedding-novel pairs and 110 on the rest.
Novelty finds new concerns; contradictions sit among concerns on the same
topic.

### Invented records

Nine Opus 5.5 counters, blinded to source, counted 215 positions in whole
seat-item bundles (`analysis/invented-records/halluc-tally.md`). Share of
positions with at least one item, C-minor excluded; intervals resample items:

| Source | Items | n | >=1 item [95%] | A | B | C (minor) | D | E |
|---|---|---|---|---|---|---|---|---|
| Haiku 5.5 replay | all | 120 | 21% [15, 27] | 0 | 3 | 27 (23) | 17 | 5 |
| Haiku 5.5 replay | old | 57 | 16% [12, 20] | 0 | 1 | 21 (21) | 6 | 2 |
| Haiku 5.5 replay | current | 63 | 25% [15, 36] | 0 | 2 | 6 (2) | 11 | 3 |
| Opus 4.6 adapted replay | old | 43 | 19% [7, 37] | 0 | 0 | 4 (3) | 7 | 1 |
| Opus 4.6 recorded | all | 52 | 6% [0, 15] | 0 | 0 | 8 (8) | 4 | 0 |

No invented proceedings (A) anywhere. Most items are D (facts about the case
not in the inputs: wrong intervals, positions given to the wrong commenter)
and C-minor. For scale, DATA-2026-0001 found Opus 4.6 at 15 of 42 and 14 of
45 answers, and Qwen 3.8 at 36 of 42 and 27 of 45, on a different task.

- **Like for like is Haiku against Opus adapted, old format:** 16% [12, 20]
  against 19% [7, 37]. The intervals overlap. Recorded positions saw their
  whole Round 1 loop (reads) and replays only the request, so the record's
  6% is not comparable with either.
- **The current-format Haiku rate is higher** than old-format, with a wide
  interval and no Opus replay to compare it with.
- **One echo call matters.** Eleven GOV-2026-0010 answers repeat the Clerk's
  misreading of the item's scope limitation; under the echo rule they are not
  counted. Counted as D, Haiku is 23% [16, 31] and Opus adapted 30% [8, 61].
- **Known limits.** The counters are the same family as both models. Groups
  adopted each other's calls, with one stricter case (G8-7 against G2-3, one
  case). The mechanical candidates missed `Article X.N` shorthand, Protocol
  sections read as Constitution sections, single-quoted text, and
  "P"-prefixed, OBL, AMD and TEST ids; the counters caught these by hand.
  GOV-2026-0018's four recorded positions come from the Council's request
  dumps, which are unpublished (hashes in `manifest.json`).

## 3. Where the measure is weak

- **First action only, by method.** 36 of 156 Haiku samples and 41 of 84
  Opus samples read first and took no position. Recorded positions came at
  the end of a live loop, after the seat had read. The Opus control holds the
  method fixed on the seven old-format items; on the other six there is no
  control.
- **Thin Opus floor:** five old-format items plus one current-format item.
- **Format and item can't be separated.** The model effect shows mainly on
  old-format items, which also hold every recorded *no*; the current-format
  items were unanimous *yes* on the record (24 of 24).
- **The split matters on the current format.** Twenty-one of Haiku's 120
  positions have no line breaks, so each is one long concern. With sentence
  chunks, Haiku against the record on the current format is 19.7% [11.7,
  26.4] against itself at 11.8% [8.2, 15.2], above its spread on 4 of 6
  items (`novelty-contrasts-chunk.csv`). Pooled, the result holds on both
  splits: 24.5% [18.8, 29.8] against 10.6% [7.8, 13.3].
- **One recorded sample per seat-item**, one embedding model, a same-family
  judge, same-family counters, and 13 items.

## 4. Still open

- **Round 2 uptake**, from the mixed rehearsal (two Haiku 5.5 seats beside
  two Opus 4.6 seats): of each Haiku Round 1 concern novel against both Opus
  seats, does an Opus seat's Round 2 response contain a match above the floor
  (judge: *agree* taken up, *contradict* engaged against)? Do votes move
  toward the Haiku seats' Round 1 position or away? Baseline: the same for
  one Opus seat among Opus-only sittings. One rehearsal is one sample.
- The Qwen judges (February 2027) and the second embedding model.
- Questions on which comment is welcome: the floor and baseline; paragraphs
  or sentence chunks as primary; whether reading first should be reported as
  a measure of its own; whether a *defer* is divergence, caution or neither;
  splitting *unrelated_or_new*; whether the Haiku judge's labels should reach
  the Council before a second judge exists; splitting by how contested an
  item was on the record.
