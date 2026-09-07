# ADR 0020 — SASRec v2: capacity, sequence length, and the sampling ceiling

**Status:** Proposed
**Date:** 2026-09-05

## Context

[ADR 0016](0016-sasrec-sequential-retrieval.md)'s SASRec is deliberately the 2018 paper's shape:
hidden 64, two blocks, two heads, feed-forward 256, dropout 0.2, the last **50** items, standard
BCE against **32** uniform negatives, batch 512, Adam at 1e-3, two epochs, seed 42, exact FAISS.
That cell was chosen by a 6% pilot to answer one question — does sequence order help at all — and
it answered yes. After O-9's fast-path repair, inference-only run
`528b14513d9a49e098a0525417f23285` reads warm recall@500 **0.5091713455**, cold 0.5262729520,
overall 0.5137688997 over 1,931 warm / 710 cold users under protocol `sha256:b4ed5afa…`, and the
retrieval gate against item-item `4b342e87…` promotes at **+27.59% warm**, one-sided 95% paired
lower bound +24.38%.

Nothing about that cell was tuned. Every hyper-parameter is a 2018 default carried unchanged, and
the two the literature says matter most — capacity and the number of negatives — sit at the bottom
of their useful range. gSASRec (Petrov & Macdonald, 2023) is specifically about the second: SASRec
trained against a handful of sampled negatives is *overconfident*, its scores are miscalibrated
against the full catalog, and raising the negative count is what closes the gap to a full-softmax
objective and to BERT4Rec. This project's own pilot found the opposite at its operating point —
plain BCE beat matched gBCE, 0.3186 to 0.2937 warm recall@500 — which is a result about **32**
negatives, not about the paper's regime. At 34,461 items the question can be settled outright
rather than approximated: a full softmax is affordable here in a way it is not at web scale.

Rung 3 ([ADR 0018](0018-sequence-aware-ranking.md)) has just closed with its stop rule fired — the
encoder's scalar verdict bought +1.86% warm NDCG@10 over a same-frame control, because the score
restates the rank of the retriever's own candidates. That says nothing against the encoder; it says
the next gain is not downstream of it. The remaining levers on this line are a **better encoder**
(this ADR) and **more candidate diversity** (ADR 0019, with the owner).

## The finding that governs the cost of everything below

The trainer builds **one training example per target**. `build_strict_prefix_examples_with_stats`
materialises a `(19,739,546 × max_length)` int32 tensor — one left-padded window per target — and
`fit` runs a full encoder forward for each while using only the final position. Canonical SASRec
predicts *all* positions of a window from one forward pass. So this loop pays for sequence length
twice, once in the per-example forward and once by never amortising it, and the 6 h 44 min 53 s
(24,293.3 s, two epochs, ~8.8 GiB peak resident) that run `a11af5ed…` cost is a property of the
loop rather than of the model.

Write the per-example encoder cost as `A(L,d,b) = 2·b·L·d·(6d + L)` multiply-adds (QKV and output
projections `4Ld²`, attention `2L²d`, feed-forward at `d_ff = 4d` giving `8Ld²`); v1 is
`A(50,64,2) = 5,555,200`. Per-epoch cost is `N · A` under the current loop and `(N/L) · A` plus the
output layer per target under an all-positions loop covering the same targets. Against v1's
measured 12,146.65 s/epoch:

| Cell | hidden / blocks / L | loss | per epoch, current loop | per epoch, all-positions | history tensor |
|---|---|---|---:|---:|---:|
| **0** control | 64 / 2 / 50 | BCE, 32 neg | 3.37 h | **0.07 h** | 3.95 GB → ~0.08 GB |
| **0b** bridge | 64 / 2 / 50 | sampled softmax, 1,024 neg | 3.41 h | **0.11 h** | 3.95 GB → ~0.08 GB |
| **A** anchor | 128 / 2 / 100 | sampled softmax, 1,024 neg | 27.1 h | **0.35 h** | 7.90 GB → ~0.08 GB |
| **B** width | 256 / 2 / 100 | sampled softmax, 1,024 neg | 101.9 h | **1.18 h** | 7.90 GB → ~0.08 GB |
| **C** depth | 128 / 4 / 100 | sampled softmax, 1,024 neg | 54.1 h | **0.62 h** | 7.90 GB → ~0.08 GB |
| **D** length | 128 / 2 / 200 | sampled softmax, 1,024 neg | 60.3 h | **0.38 h** | 15.79 GB → ~0.08 GB |
| **E** ceiling | 128 / 2 / 100 | full softmax, 34,461 items | 29.7 h | **2.95 h** | 7.90 GB → ~0.08 GB |

Seven cells at 3–5 epochs each: **839–1,397 CPU-hours (35–58 days)** under the current loop,
**17–29 hours** under an all-positions one. Cell D is additionally impossible as written — a
15.79 GB history tensor against v1's observed 8.8 GiB peak on a 36 GiB machine.

## Proposal

Two engineering preconditions, then seven predeclared cells, one run each.

**P1 — slice history windows instead of materialising them.** Store each user's item sequence once
and cut the window during batch collation. Memory becomes `O(total interactions)` (~80 MB) instead
of `O(targets × L)`, so sequence length leaves the memory equation. Not a modeling change: under
the same permutation and RNG these are the same examples in the same order, asserted by a 6%
equivalence test rather than argued.

**P2 — predict all positions of a window in one forward pass.** The canonical SASRec objective,
with sliding windows at stride `L` covering the same targets. **This is a modeling change** and is
treated as one: it correlates gradients inside a window, gives early positions a shorter prefix
than the current scheme does, and multiplies the effective batch by `L` — so batch size is
re-expressed in **targets per step**, held at v1's 512–4,096 (`B_seq` = 8–32 at L=100) and logged
as the parameter of record. Its validation is cell 0, not an assertion. P2 also removes FAISS from
the loop, because the per-epoch early-stopping probe scores by exact `torch` matmul and top-k over
the item table — precisely the clean fix W26 names for the OpenMP collision (`OMP_NUM_THREADS=1`
alone moves the abort into the first `build_index()`; `KMP_DUPLICATE_LIB_OK=TRUE` only masks it).
FAISS is then built once, after fitting.

**The cells**, anchored one-factor-at-a-time on **A**, because one run per cell leaves no seed
variance with which to separate an interaction term from noise:

- **0 — control.** v1's exact shape under P1+P2. Run first, at 6% against the recorded pilot (warm
  recall@500 0.3186), then at full data against `528b1451…`. If the loop moved the model that has
  to be explained before any capacity result is attributable. ADR 0018 learned this expensively:
  without a same-frame control, "arm beats incumbent" confounds the change under test with
  everything else that moved.
- **0b — objective bridge.** v1's shape with the sampled softmax at 1,024 negatives, so the loss
  change is separated from the capacity change at the cheapest point on the grid (~0.11 h/epoch)
  rather than inferred from cells that move both at once.
- **A — anchor.** hidden 128, 2 blocks, 4 heads, L=100, sampled softmax with 1,024 negatives.
- **B — width.** A with hidden 256 and 8 heads. **C — depth.** A with 4 blocks. **D — length.**
  A with L=200.
- **E — ceiling.** A with the full softmax over all 34,461 items — the reference that removes the
  sampling question instead of approximating it. Logit memory is `B_seq · L · |V| · 4` bytes, so
  `B_seq` is capped at 16 (≈220 MB) for this cell alone and the cap is a logged parameter.

Feed-forward width stays at 4× hidden, dropout at 0.2, Adam at 1e-3, pre-layer normalisation, seed
42, exact FAISS at evaluation. Where memory forces a smaller physical batch, gradient accumulation
holds the effective targets-per-step fixed and both numbers are logged. Before any full cell runs,
the same correctness battery ADR 0016 used gates it: causal mask, padding, target exclusion,
sampler probability, sampled-versus-full loss agreement on a tiny frame, checkpoint round-trip, and
recall@10 near 1.0 on data small enough to memorise. Those are correctness checks and never quality
evidence.

Epochs 3–5 with early stopping on a held-out slice **of train**, never of the holdout: for a seeded
1% sample of training users, the final training-split target is removed from the example set and
used as the probe, recall@500 is scored on it after each epoch, and training stops when it fails to
improve by ≥0.5% relative. Stopping epoch and dropped-target count are logged — a probe target left
in the training set is leakage.

**Cells dropped, and why.** The declared axes make a 16-cell factorial; eleven are not run. Every
cell changing two axes at once is dropped ahead of the OFAT results, because one run per cell
cannot attribute an interaction. `256 × 4 blocks` is dropped as the most expensive corner with no
evidence either factor alone helps; `L=200 × full softmax` and `4 blocks × L=200` as confounded
combinations of the two costliest axes. A gBCE re-run at 1,024 negatives is dropped because E
answers the calibration question outright. Hidden 512, six blocks and L=500 are outside the
declared axes and outside any costed budget. **One** combination cell may be added, only if two of
B/C/D/E each clear gate 1 independently, combining exactly those two factors.

## Cost, honestly, and what it means for O-3

O-3 was skipped "until the next sequence-model training proposal". This is that proposal, so it
reopens. With P1+P2 the grid is **17–29 hours** on the existing 12-core M3 Pro at one thread —
less than three of v1's runs. Without them it is **35–58 days**, and cell D does not fit in memory.
A single-GPU rental — AWS `g5.xlarge` (A10G 24 GB) or `g6.xlarge` (L4), GCP `g2-standard-4` (L4),
Lambda Labs A10 — lists around **$0.70–1.10/hour** on demand and **$0.20–0.45/hour** spot or
community (list prices to be re-checked at spend time, not taken from this ADR). At an assumed
30–60× effective speedup on these batch shapes the without-P2 grid is 14–46 GPU-hours: **roughly
$10–51 on demand, under $21 on spot.**

The money is therefore not the argument in either direction and should not be allowed to look like
one. What a GPU costs here is the **reproducibility contract** — CUDA kernel non-determinism, a
different BLAS, and a second machine appearing in `docs/results.md` beside numbers whose
comparability is the basis of every gate this project runs. **Recommendation: keep O-3 skipped,
conditional on P2 landing.** Approve the rental only if cell 0's validation fails and P2 is
reverted, at which point the CPU cannot run the grid at all and renting stops being an
optimisation — and then only with a stated numeric tolerance and a CPU re-verification of the
winning cell's artifact before promotion.

## How it is judged

**Preconditions.** O-9's fast-path repair (`torch.backends.mha.set_fastpath_enabled(False)`,
PR #162) must be in force in training, artifact reload, evaluation and serving. It is load-bearing
at a different order of magnitude for v2: at L=50 only 131 of 1,931 warm users are left-padded,
while at L=100 or 200 most of the 1,800-user `50_plus` bucket becomes left-padded too, so a v2 run
against the defect would encode NaN for the majority of its evaluation population. The v2 sequence
build therefore logs the history-length distribution above 50, which has never been measured.
W26's OpenMP fix is the second precondition, discharged by P2.

**Gate 1 — retrieval, against SASRec v1 and nothing else.** Run `528b14513d9a49e098a0525417f23285`
(warm recall@500 0.5091713455, cold 0.5262729520, overall 0.5137688997) at the same protocol hash,
threshold 10, exclusions, K, exact FAISS and the same 1,931 / 710 users. Not against item-item —
that would let v2 bank v1's retrieval gain a second time. Warm recall@500 must move **≥ +3%** with
the one-sided 95% paired user-bootstrap lower bound also ≥ +3%, under the single-seed regime the
owner set for SASRec. Cold is popularity-routed and byte-identical by construction, so a non-zero
cold delta is a defect, not a result. Coverage diagnostics (v1: 30.70% catalog coverage, 10,581
items, mean retrieved popularity rank 3,564.3) are reported beside recall, as ADR 0016 requires.

**Gate 2 — the per-route bundle, under O-1's learned-route reading.** The winner's encoder is
exported, the learned-route booster retrained on its candidates as PR #151 did for v1, and the
recomposed bundle gated against bundle 1b `c1d742c8485d4e54b66746a65f7705d0` (warm NDCG@10
0.101441 / cold 0.549002 / overall 0.221762). With cold frozen the 1,931 warm users carry 33.4% of
the bundle's NDCG mass, so +3% overall requires **+9.0% warm**; the all-routes reading is computed
and recorded beside the learned-route one either way.

**Gate 3 — serving budgets, unchanged.** Encoder p99 < 15 ms and service p99 < 100 ms do not move
for a model. v1 measures **0.285 ms** on the host and **1.005 ms** in the linux/amd64 sidecar image
under 2 CPU / 4 GiB — Rosetta-translated and therefore an upper bound, since the native arm64
container reads 0.599 ms, a 1.68× ratio. Scaling `A(L,d,b)` off the amd64 number projects A at
~8.0 ms, C at ~16.0, D at ~17.9 and **B at ~30.2**; dividing out the translation ratio gives
~4.8 / 9.6 / 10.7 / 18.0 ms. Three of five cells are at or over budget on a linear projection and B
is over on both readings. That projection is a screening tool, never the gate: no cell is promoted
until its exported artifact is measured by the unmodified `src/evaluation/sasrec_latency.py`
(10,000 encodes, 500 warmups, single thread, same 2 CPU / 4 GiB linux/amd64 image) and committed as
its own `docs/experiments/sasrec/` JSON. A cell that wins on recall and misses the budget is
**measured, not promoted**; ONNX export (W20), quantisation and batching are repairs to consider
then, not to assume now.

**One run per cell**, seed 42, one protocol hash across every arm, per the owner's
replication-budget decision on ADR 0016 and O-5.

## Stop rules

1. **Cell 0 fails its 6% equivalence beyond ±3% relative warm recall@500.** P2 is reverted, the
   current-loop cost table applies, no capacity cell runs, and this returns to the owner as a
   GPU-spend decision rather than proceeding on its own.
2. **Cell 0 at full data moves more than ±1% relative against `528b1451…`.** Stop and explain the
   loop first; a v2 gain sitting on an unexplained control shift is not attributable to capacity.
3. **Cell A fails gate 1.** Run exactly one disambiguating cell (128 / 2 / **50**) to separate width
   from length, then stop the sweep. If doubled width and doubled history buy nothing, the axis is
   exhausted on this dataset and v1 stands.
4. **Coverage collapse.** A cell raising warm recall@500 while dropping catalog coverage more than
   5 points below v1's 30.70%, or halving mean retrieved popularity rank, is recorded as a more
   expensive popularity retriever and is not promoted.
5. **Any latency breach is a stop, not a threshold change.**
6. **Gate 2 refuses while gate 1 passes.** Record as retrieval-eligible and stop; do not start
   retraining variants hunting for the difference. That is the Rung 3 lesson, and it cost a rung.
7. **Any cell exceeding its projected per-epoch time by more than 2×** is killed and the whole table
   re-costed from the measurement before the next cell starts.

## Alternatives considered

**(a) Ship v1 and climb Rung 5 instead.** ADR 0019's union of retrievers is with the owner and
attacks candidate *diversity* rather than candidate *quality*. It is cheaper — much of it is
inference-only — and it does not depend on this ADR. It is the right thing first if the owner's
judgement is that one retriever's recall@500 already leaves the ranker room. The two are
complements: a stronger encoder makes the union's sequential arm stronger, and mixing cannot
recover relevance a weak encoder never retrieved. This ADR does not ask to be ranked above 0019,
only to be decided.

**(b) BERT4Rec.** ADR 0016 declined it because gSASRec reports a well-trained SASRec beating it at
lower training cost. Cell E is exactly the test of that premise's first half: if the full softmax
buys a large gain over sampled training, v1 was sampling-limited and BERT4Rec's masked objective
becomes live again. Running it *before* E pays for a bidirectional encoder to answer a question a
loss change answers more cheaply, and adds a second serving implementation the roadmap does not
plan to reuse.

**(c) Longer sequences only, no capacity change.** The cheapest single cell (D, 0.38 h/epoch) and
defensible — MovieLens users are long, v1 truncates, and 2018's defaults were tuned on smaller
catalogs. Not proposed alone because it cannot distinguish "more history helps" from "a
64-dimensional bottleneck cannot hold more history", the exact confound the OFAT design exists to
resolve.

**(d) Keep the current training loop and rent a GPU.** Honest, and cheap in dollars ($10–51).
Rejected as the *first* move because it spends the reproducibility contract to avoid an engineering
fix worth having anyway: P1+P2 make every future sequence-model run on this ladder 10–50× cheaper,
including Rung 7's, while a GPU makes exactly one grid faster.

**(e) Select on a pilot, then run one full cell.** The 6% pilot chose v1's loss and worked. It is
not the selection mechanism here because that slice has ~115 warm users against a measured ~5%
relative warm seed dispersion (O-5, M0-14) — differences between these cells are expected to be of
that size, so the pilot would select on noise. Its role in this ADR is validating P2, where the
effect being checked is large.

## Consequences

- The training loop becomes the thing that changed most, and it changes under a control rather than
  alongside a capacity claim. Every later sequence model on the ladder inherits it.
- `docs/results.md` gains seven rows whose comparability rests on one protocol hash and one machine.
  That property is what the O-3 recommendation protects.
- A promoted v2 carries a wider encoder into a sidecar already pinning CPU torch (O-12): the
  artifact grows (v1 is 9,458,477 bytes and hidden 256 roughly quadruples the item table) and the
  checksum, manifest and drift check follow it without changing shape.
- If B or D wins on recall and loses on latency, this project acquires its first genuine
  quality-versus-SLO trade. Under the non-negotiables the SLO wins and ONNX export (W20) becomes
  urgent rather than optional.
- Early stopping adds a train-internal probe — a small permanent addition to the evaluation surface
  and the first place a future leak would hide, so it is asserted by test rather than by review.

## Risks

- **P2 changes the model and the change is attributed to capacity.** Mitigated by cell 0 at both
  scales and stop rules 1–2, which fire before any capacity cell runs.
- **Effective batch size, not capacity, explains a gain.** P2 multiplies the batch by `L`, so
  targets-per-step is held in v1's range, logged, and used identically by cell 0.
- **Left-padding at longer L.** More of the population is padded at L=100 and 200 than at 50, so
  O-9's repair moves from important to load-bearing; the regression coverage at history lengths 1,
  3, 12, 49, 50 extends to each cell's configured `max_sequence_length`.
- **The full-softmax cell exhausts memory.** `B_seq` is capped at 16 for cell E as a logged
  parameter, not discovered at runtime.
- **Sweep creep.** Seven cells, one run each, one conditional combination cell under a rule written
  before any result exists. A seventh requires a new ADR note and the owner's word.

## How we would know this decision is wrong

- **Cell 0 already wins.** If v1's shape under the new loop beats `528b1451…` materially, the gain
  is the loop and not the architecture, and this ADR should be replaced by a much smaller one that
  lands P1+P2 and stops.
- **Cell E matches cell A within the warm band.** Sampling was never the limitation at 1,024
  negatives, gSASRec's premise does not bind at a 34,461-item catalog, and the loss axis closes for
  good — BERT4Rec included.
- **Cell E beats A substantially.** Then every SASRec number recorded here is a floor set by 32
  negatives rather than a ceiling set by the architecture, ADR 0016's verdict is understated in the
  same direction O-9's was, and the cheapest next model is a better-trained v1, not a wider one.
- **Every cell lands inside ±3% of v1.** MovieLens 25M at this catalog size does not reward capacity
  on this objective, the sequential retrieval line is finished at v1, and the remaining gains are in
  mixing (0019) or in labels (Rung 4).
- **The deterministic sequence-order shuffle ablation reproduces the winner's gain.** Order was not
  the source, and the result is about parameter count rather than sequence modeling.
- **The winner fails gate 2 the way Rung 3 did** — retrieval improves, the bundle does not. Then
  candidate quality has stopped being the binding constraint, which argues for 0019 and against any
  further rung on this axis.

## References

- Wang-Cheng Kang and Julian McAuley, "Self-Attentive Sequential Recommendation," IEEE ICDM 2018,
  DOI 10.1109/ICDM.2018.00035.
- Aleksandr Petrov and Craig Macdonald, "gSASRec: Reducing Overconfidence in Sequential
  Recommendation Trained with Negative Sampling," ACM RecSys 2023, DOI 10.1145/3604915.3608783.
- Fei Sun et al., "BERT4Rec," ACM CIKM 2019, DOI 10.1145/3357384.3357895.
- [ADR 0001](0001-evaluation-protocol.md) (the gate), [ADR 0016](0016-sasrec-sequential-retrieval.md)
  (SASRec v1 and its verdicts), [ADR 0018](0018-sequence-aware-ranking.md) (why the control is not
  optional), [`docs/results.md`](../results.md) and
  [`docs/experiments/sasrec/`](../experiments/sasrec/) (every number quoted above).
