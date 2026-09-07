# Modeling roadmap

**Status:** plan, not commitment. **Last reviewed:** 2026-08-30.

The models are this project's main line of work. This page is the ladder from
the models on `main` today to what a production recommender at Netflix or
YouTube scale runs, rung by rung. Three rules govern it:

1. **Every rung needs approval before work starts.** A rung begins as an ADR
   proposal (decision, alternatives, how we would know it was wrong) and a
   one-line entry in the decision log below marked *proposed*. Nothing is built
   until the owner marks it *approved*. Which rung comes next is a decision
   taken each time, not an order fixed here.
2. **Rungs can be skipped.** Each one names the condition under which it is
   not worth climbing. A skipped rung is recorded as *skipped* with the reason,
   so the omission is a decision rather than an oversight.
3. **Nothing replaces the champion by being newer.** Every candidate is scored
   through `src/evaluation/` on the protocol in [ADR 0001](adr/0001-evaluation-protocol.md)
   — recall@500 for retrieval, NDCG@10 for ranking, warm and cold reported
   separately — and takes over serving only by clearing the promotion gate.
   The current numbers are in [`results.md`](results.md).

The harness around the models — auth, tenancy, the feature store, the latency
gate, the product — is what Phase 3 finishes so that these models are usable by
a real user. Once Phase 3 closes, the remaining phases are re-prioritised and the
ladder resumes as the primary work.

## Where the models are today

| Stage | Model | What it learns | Status |
|---|---|---|---|
| Retrieval | Item-item cosine (`implicit`) | Nothing — cosine over co-occurrence counts. The zero-parameter baseline every learned retriever must beat ([ADR 0004](adr/0004-item-item-before-two-tower.md)). Warm recall@500 **0.4001**. | Champion in the served bundle; warm recall@500 **0.3991** at threshold 10 (2026-08-30) |
| Retrieval | Two-tower (PyTorch + FAISS) | Item embeddings; the user side is a mean-pool over the last 50 items, no user-id embedding; sampled softmax with log-uniform correction ([ADR 0006](adr/0006-two-tower-retrieval-architecture.md)) | Measured, not promoted: best swept configuration warm recall@500 0.0591 vs item-item 0.3991; the sweep diagnosed an unpinned softmax temperature (ADR 0006, 2026-08-30 note) — any Rung 1 work starts there |
| Ranker | LightGBM LambdaRank | A GBDT over eight point-in-time features — counts, popularity windows, item age, genre affinity ([ADR 0005](adr/0005-lightgbm-over-neural-ranker.md)). It sees aggregates of the history, never its order or contents. | Champion; trains on the whole 30-day trailing window (154k positives) and is **promoted over CF/ALS on the gate** — overall NDCG@10 +15.5%, warm +21.2% (2026-08-30) |

Retrieval is similarity in an embedding space; ranking is tabular. That shape is
exactly the two-stage architecture of [ADR 0003](adr/0003-two-stage-architecture.md),
and it is the base the ladder builds on rather than something it replaces.

## The ladder

Each rung: what it is and why the industry took that step, what it needs from
the harness, how it is judged, and when to skip it. Years are the papers that
made each step standard.

### Rung 1 — Two-tower v2: hard negatives and item side features

*What.* The two upgrades every production two-tower carries: hard-negative
mining alongside in-batch negatives (YouTube, 2019–2020), and side features in
the item tower — genres, release year, a text embedding of the overview — so a
never-watched title has an embedding at all.

*Why.* Sampled-softmax with random negatives learns popularity; hard negatives
teach the boundary. Side features are the first honest answer to cold *items*,
which [ADR 0011](adr/0011-cold-start-coverage.md) deliberately deferred.

*Needs.* Text embeddings computed offline and stored in the feature store;
nothing new in serving — the FAISS path is unchanged.

*Judged by.* Recall@500 warm/cold against item-item and two-tower v1; the
ADR 0011 cohort's per-bucket recall for cold users; a new cold-*item* slice.

*Skip if.* The full-dataset two-tower v1 already clears ADR 0004's threshold
and the cold-item slice is not the next thing the product needs. Then go
straight to Rung 2. **As of 2026-08-30 it does not clear it** — warm recall@500
0.0466 against item-item's 0.4001 — so this skip is not available on that
ground. The two-tower's loss flattened after its first epoch, which makes a
training-budget and learning-rate sweep the cheaper experiment to run before any
architectural rung; see [ADR 0004](adr/0004-item-item-before-two-tower.md)'s
2026-08-30 note.

### Rung 2 — Sequential retrieval: SASRec

*What.* A causal transformer over the interaction sequence that predicts the
next item (Kang & McAuley, 2018). It replaces the mean-pool user tower with a
real encoder and serves through the same FAISS index: encode the last *N* items
online, then ANN.

*Why.* This is the workhorse sequential recommender in industry and MovieLens
is its canonical benchmark. It is the first transformer on the ladder, and the
one whose serving cost is known to be small.

*Needs.* Sequence-ordered history in the feature store with point-in-time
correctness (already the standard here); an encoder in the model sidecar with a
latency budget inside the p99 gate; the training-time exclusion question from
PR #64 settled, because a sequence model is sensitive to what it is shown.

*Judged by.* Recall@500 on the same holdout; the cold buckets, where a sequence
model with one item to read is the interesting case; the k6 gate, since the
encoder now runs per request.

*Train it with many sampled negatives.* gSASRec (Petrov & Macdonald, 2023)
shows SASRec trained that way beats BERT4Rec, so BERT4Rec is a comparison to
run if the numbers are close, not a rung of its own.

*Skip if.* Never, realistically — this is the rung the roadmap exists for. It
may be *deferred* behind Rung 1 if cold items are the more urgent product gap.

### Rung 3 — A ranker that reads the sequence: target attention (DIN → TransAct)

*What.* DIN (Alibaba, 2018): attention from the *candidate* item over the
user's history, so the ranker knows which past titles make this one plausible.
Then TransAct (Pinterest, 2023): a transformer over the recent action sequence
scored per candidate.

*Why.* The GBDT ranker lifts warm recall over CF/ALS but its warm NDCG falls —
it reorders on aggregates and cannot tell *which* history made a candidate
relevant. This rung is what fixes that.

*Needs.* The Rung 2 encoder's user embedding and the attention score exposed as
ranker features first; the feature-parity test extended to embedding features;
a neural ranker in the sidecar beside LightGBM.

*Judged by.* NDCG@10 warm — the slice the GBDT regresses on — against the
LightGBM champion under the promotion gate. LightGBM stays champion until
beaten; the first step is adding the sequence features to it, not replacing it.

*Skip if.* Adding the sequence embedding as a GBDT feature already recovers the
warm NDCG. Then the neural ranker is a later rung, not this one.

### Rung 4 — Multi-objective ranking (MMoE / multi-task)

*What.* Predict several targets — will watch, will rate ≥ 4, will finish — with
shared experts (MMoE, Google 2018) and combine them with a tuned utility.

*Why.* Netflix, YouTube and Meta do not rank on one label. MovieLens carries
rating magnitude and timestamps, which is enough to define the targets.

*Needs.* Revisiting [ADR 0002](adr/0002-implicit-feedback-label.md), which
deliberately drops the rating value from the pipeline — this rung brings it back
as a *second* objective, by decision, not by accident. A utility definition in
its own ADR.

*Judged by.* Each objective on its own metric plus the combined ranking on
NDCG@10; the gate reads the slice ADR 0001 says it reads (open decision — see
[`results.md`](results.md)).

*Skip if.* One objective is all the product needs for now. This rung is about
the product's definition of "good", and it should not be climbed before that
definition exists.

### Rung 5 — Multi-retriever mixing and re-ranking

*What.* Union several retrievers — sequential, two-tower, item-item,
popularity-by-context — dedupe, rank, then a re-rank layer for diversity and
calibration (MMR or a DPP) and business rules.

*Why.* Production candidate sets come from many sources; the ranker is what
makes them comparable, and re-ranking is what keeps a ranked list from being
ten near-duplicates.

*Needs.* Almost nothing new — the `candidate_sources` field in the audit row
already records contributions per source; the coordinator's exclusion and
fail-closed sweep already apply after mixing.

*Judged by.* Recall@500 of the union versus its best single source; a diversity
metric that has to be chosen and written down.

*Skip if.* A single retriever's recall@500 leaves no room the ranker can use.

### Rung 6 — Exploration and off-policy evaluation

*What.* Contextual bandits (Thompson sampling) for the featured slot — Netflix's
artwork personalisation is the reference case — and IPS-style off-policy
estimators so a new policy can be evaluated on logged traffic before an A/B.

*Why.* A recommender that only exploits its own history narrows; and offline
NDCG does not predict online outcomes. This rung is what makes Phase 6's A/B
framework more than a coin flip.

*Needs.* Impressions and non-clicks logged (today only decisions are), a
propensity recorded with every served item, and Phase 6's routing layer.

*Judged by.* Off-policy estimates against the A/B outcome once one exists.

*Skip if.* Phase 6 has not landed. This rung cannot precede it.

### Rung 7 — Generative and foundation-style sequence models

*What.* HSTU (Meta, 2024) reframes recommendation as sequential transduction
over the whole event stream; TIGER (Google, 2023) retrieves by generating
semantic item ids; Netflix's foundation model for personalisation (2024–25) is a
large transformer over long member histories whose embeddings feed every
downstream ranker.

*Why.* This is the current frontier at scale: one user-representation model,
many consumers. It is the end state the ladder points at.

*Needs.* Everything above it, a GPU budget this project does not have on a
laptop, and an honest framing about what MovieLens 25M can show at this scale.

*Judged by.* The same protocol — and by whether the embeddings improve the
rankers downstream, not only the retriever.

*Skip if.* Rungs 2–3 already deliver what the product can use. This rung is
allowed to remain a reading list.

## What "like Netflix" needs beyond the models

The system pieces, mapped to the phases that own them:

| Piece | Phase | State |
|---|---|---|
| Retraining pipeline and promotion gate, automated | 4 | Gate documented in ADR 0001, not yet automated |
| Drift detection on features and predictions | 5 | Not started |
| Champion / challenger routing per tenant, shadow mode | 6 | Registry columns landing; routing not built |
| Online feedback loop — impressions, skips, retraining on them | 4 + 6 | Decisions are logged; impressions are not |
| Slate / row optimisation rather than item lists | after 6 | Not started |

## Governance

- **Propose.** An ADR in `docs/adr/` (the flat backend line) stating the rung,
  the decision, the alternatives, and how we would know it was wrong. The
  decision log below gains a row marked *proposed* with the ADR number.
- **Approve.** The owner marks the row *approved*, or *skipped* with a reason.
  No training code for a rung is merged before that row says approved.
- **Build and measure.** Through `src/evaluation/`, logged to MLflow, recorded
  in [`results.md`](results.md) with the run, date, machine and wall-clock.
- **Promote or not.** The served bundle changes only when the candidate clears
  the ADR 0001 gate, which since 2026-08-30 names its slices: overall NDCG@10 at
  +3% relative **and** no regression in the warm or cold slice beyond that
  slice's measured seed-to-seed tolerance (`src/evaluation/gate.py`, `make
  gate`). The outcome is a dated note on the rung's ADR either way.

## Decision log

| Rung | Status | Date | ADR | Note |
|---|---|---|---|---|
| Two-stage architecture | done | 2026-05-31 | 0003 | The base |
| Item-item retrieval | done | 2026-06-01 | 0004 | Warm recall@500 0.4001 on 2026-08-29 |
| Two-tower v1 | done — measured, not promoted | 2026-07-02 | 0006 | Warm recall@500 **0.0466** against item-item's 0.4001 on the full dataset, 2026-08-30 (−88.4%). ADR 0004's gate is not cleared and item-item stays champion; the loss flattened after epoch 1, so this is a verdict on v1 as configured, not on learned retrieval |
| LightGBM LambdaRank | done — promoted over CF/ALS on the gate | 2026-07-02 | 0005 | Warm NDCG@10 **0.0705**, overall 0.1993, seed-averaged over three seeds at the whole trailing window, 2026-08-30. Clears ADR 0001's gate against CF/ALS at **+15.53% overall / +21.21% warm**, and at every seed on its own. (The 0.0554 recorded on 2026-08-29 was one seed of a 20,000-positive sample whose warm slice moved 25% on the seed alone.) |
| 1 — Two-tower v2 | **reopened 2026-09-05 after the FAISS mapping fix; retrieval gate passes, measured not promoted** | 2026-09-06 | 0015 | The 2026-09-04 stop fired on a defective measurement: the recommendation path mistranslated every FAISS row, so the five-arm pilot's 0.0435 was not evidence about the embeddings (#158). One full-data run of the corrected v2 (`2d7f1a49008d4e9d8791d4ca8598d613`, seed 42, 1h54m39s) reads warm recall@500 **0.5113** against item-item's 0.3991 — **+28.14%**, lower bound **+25.28%** population-only on n=1931 — with cold **bit-identical** (both route below-threshold users to the same popularity list) and overall +18.95%. `make gate-retrieval` returns **promote** at both tolerances 0.0, the values SASRec was judged under. **`serving_eligible` is false**: the paired LightGBM NDCG@10 guardrail has not run, and SASRec's step 1 is the cautionary case — a retriever can win recall@500 and still lose end to end once a ranker scores its slate. **This cell is not the original complete-v2 arm** (that used IVF and 4,096 negatives; this uses exact search and 16,384), so none of the difference is attributable to the mapping fix alone. Item-item plus LightGBM remains champion; promotion is the owner's decision under ADR 0001. Uncertainty is one training run, population term only. |
| 2 — SASRec | retrieval promotion eligible; **the retrained pair is measured, not promoted** | 2026-09-05 | 0016 | Retrieval passed (+16.57% warm recall@500; encoder p99 0.285 ms). Under the **fixed** current LightGBM, swapping only the candidate source passed D-002's non-regression check — warm NDCG@10 **+1.67%**, cold **0.00%**, overall **+0.43%** — and that run's `DO NOT PROMOTE` on the +3% overall clause is diagnostic, because the clause governs a *new ranker replacing the old one* rather than a retriever swap under a fixed one. **Retraining the ranker on SASRec's own candidates** (`50d9718802f949d98c5d8d4d6315bb1a` vs the exclusion-matched incumbent `bff5f86e6ae14e6b9c19d9c426e3b6ec`) is worth **+25.96% warm NDCG@10** and **+60.50% warm recall@10** — fifteen times the fixed booster's +1.67% — and the gate refuses it at −32.15% overall / −53.11% cold. That cold loss is a *booster* effect, not a retrieval one: cold users get byte-identical popularity candidates in every arm. Two repair arms then **pass** against the same incumbent — a per-route composition of the two saved boosters (`566f5309767a4076a4f5e8151be16645`, +6.88% overall / +25.96% warm / 0.00% cold, no new training) and a union booster over both training sets (`cf475086bab941aeb6e4519ff021fc94`, +6.30% overall / **+26.69% warm** / −1.05% cold). A passing gate is a measurement; promotion is the owner's decision under ADR 0001. Next: Rung 3a (SASRec embedding + dot-product score as point-in-time ranker features). Item-item plus LightGBM stays the promoted bundle; rolling/tolerance/latency and k6 evidence outstanding for all arms. Exact artifacts, evidence, and logs retained. |
| 2b — SASRec v2 | **proposed** | 2026-09-05 | 0020 | Nothing about v1's cell was tuned: hidden 64, two blocks, 50 items and 32 uniform negatives are 2018 defaults carried unchanged, and the post-O-9 warm recall@500 of 0.5091713455 (`528b1451…`) is what that untuned shape reads. Seven predeclared cells, one run each, one factor at a time from an anchor at hidden 128 / 2 blocks / L=100 / sampled softmax with 1,024 negatives — plus a width cell at 256, a depth cell at 4 blocks, a length cell at L=200, and a full-softmax cell over all 34,461 items that measures the sampling ceiling instead of approximating it. Gated against **v1 and not item-item** (+3% warm recall@500 with the paired lower bound also above +3%), then the per-route bundle against 1b `c1d742c8…` under O-1, with encoder p99 < 15 ms unmoved — a linear projection already puts three of five cells at or over that budget, so measurement in the amd64 sidecar image precedes any promotion. The proposal's own finding is about the trainer rather than the model: it runs one forward pass per *target* instead of per *window*, so the grid costs 35–58 CPU-days as it stands and 17–28 hours once windows are sliced and all positions are predicted together. That is why it **reopens O-3 and recommends keeping the GPU skipped** — the rental is $10–51, and what it would actually spend is the reproducibility contract. Owner approval required before any code |
| 3 — Target attention ranker | approved; **increment 1 measured and refused, increment 2 not built** | 2026-09-05 | 0018 | Approved by the owner 2026-09-05, and measured twice — the first run predated O-9's fast-path repair, which was silently dropping 34,190 of 153,947 learned-route positives, so it was re-run once on the fixed encoder against bundle 1b re-measured on that same encoder (`c1d742c8…`). **The verdict did not change.** The ten-column arm (`bc89411b751f47eab9b098ddd628f306`, booster `0bcc12ba…`) reads warm NDCG@10 **0.104257** against the bundle's 0.101441 (**+2.78%**), cold **0.549002** bit-identical, overall 0.223821 (+0.93%), warm recall@10 −0.29%. **Both gate scopes refuse**: all-routes +0.93% vs +3.00%, learned-route +2.78% vs +3.00%. The decisive number is the ablation — an eight-column control fitted from the *same* frame, groups and labels reads 0.102358, so the two features are worth **+1.86%** and the remaining +0.90% is the retrain rather than the columns. **Stop rule 1 fired, so increment 2 (DIN) is not built.** The diagnosis is ADR 0018's own Risk 1, predicted before either run and sharper after the repair: the two features take **82.3%** of the model's gain while every aggregate falls (`item_popularity_30d` 255,536 -> 37,249), which is what "the dot product re-encodes the retriever's rank" looks like when the candidates *are* SASRec's top-500. What stands is narrower than the rung's premise: the encoder's *scalar verdict* adds little to a ranker scoring the encoder's *own* candidates; whether the history itself would is untouched |
| 4 — Multi-objective | not proposed | — | — | Reopens ADR 0002 by decision |
| 5 — Mixing and re-ranking | not proposed | — | — | |
| 6 — Bandits and OPE | not proposed | — | — | Blocked on Phase 6 |
| 7 — Generative / foundation | not proposed | — | — | May remain a reading list |
| C — Content cold-item retrieval | **approved 2026-09-05** | 2026-09-05 | 0017 | Not a ladder rung but a second candidate source: 3,376 catalog movies have zero ratings and no interaction-derived index can reach them. Genres and release year first; the tag genome covers 0% of cold items. Proves the mechanism — the offline population is deep-catalog obscurity, while the production case is new releases, which MovieLens cannot measure |

## References

- Covington, Adams, Sargin — *Deep Neural Networks for YouTube Recommendations*, 2016
- Yi et al. — *Sampling-Bias-Corrected Neural Modeling for Large Corpus Item Recommendations*, 2019
- Yang et al. — *Mixed Negative Sampling for Learning Two-tower Neural Networks*, 2020
- Kang & McAuley — *Self-Attentive Sequential Recommendation* (SASRec), 2018
- Sun et al. — *BERT4Rec*, 2019
- Petrov & Macdonald — *gSASRec: Reducing Overconfidence in Sequential Recommendation Trained with Negative Sampling*, 2023
- Zhou et al. — *Deep Interest Network for Click-Through Rate Prediction* (DIN), 2018
- Xia et al. — *TransAct: Transformer-based Realtime User Action Model for Recommendation at Pinterest*, 2023
- Ma et al. — *Modeling Task Relationships in Multi-task Learning with Multi-gate Mixture-of-Experts* (MMoE), 2018
- Zhai et al. — *Actions Speak Louder than Words: Trillion-Parameter Sequential Transducers for Generative Recommendations* (HSTU), 2024
- Rajput et al. — *Recommender Systems with Generative Retrieval* (TIGER), 2023
- Li — *Netflix's Foundation Model for Personalized Recommendation*, Netflix TechBlog, 2025
- Chapelle & Li — *An Empirical Evaluation of Thompson Sampling*, 2011; Joachims et al. — *Unbiased Learning-to-Rank with Biased Feedback*, 2017 (off-policy evaluation)
