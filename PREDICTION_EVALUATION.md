# Prediction diagnostic — 19 October 2026

Replayed the latest 60 available completed fixtures per league (240 total)
from the already downloaded 15:01 UTC snapshot. No additional Forebet requests,
feed-history database, or Railway model jobs were used. This is a retrospective
diagnostic on a reused snapshot, not an independent validation set.

Only earlier results enter each prediction; result availability is assumed at
kickoff plus match duration plus five minutes unless an observed publication
time is supplied. Forecast weights were not tuned against these results.

| League | Games | Model winner accuracy | League baseline | Model goals MAE | Baseline goals MAE |
|---|---:|---:|---:|---:|---:|
| esb8 | 60 | 40.0% | 38.3% | 1.897 | 2.038 |
| volta6 | 60 | 30.0% | 43.3% | 2.152 | 2.186 |
| h2hgg | 60 | 35.0% | 21.7% | 1.102 | 1.153 |
| gt12 | 60 | 43.3% | 45.0% | 1.592 | 1.614 |

Winner accuracy did not consistently beat the baseline. Goals MAE was modestly
lower in all four leagues in this snapshot; that alone does not establish a
profitable totals strategy or calibrated probabilities. Lower MAE is better.

The new abstention rule emitted only two experimental form-only leans in this
diagnostic and both lost. It is a presentation safeguard, not demonstrated
predictive improvement. Do not interpret the rule as a recommended betting
strategy. Most targets now correctly present no clear winner.

There were no separately recorded prematch Forebet probabilities. A fair
historical Forebet comparison therefore remains unavailable. Percentages still
visible on completed rows were excluded rather than treated as prematch data.
No live model was evaluated or enabled: a final-score snapshot contains no
historical in-play score/clock sequence needed to validate one.

Reproduce the diagnostic with a snapshot already available to the consumer:

```sh
python3 -m prediction_engine existing-feed.json --backtest --limit 60
```

The JSON output includes reliability bins, winner log loss and Brier score,
over-goals Brier comparisons, and recommendation coverage. No model promotion
or probability recalibration was performed from this diagnostic.
