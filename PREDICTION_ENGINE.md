# Prediction engine for the downstream server

This is a stateless Python 3.11 package. Give it the `/predictions` JSON your
consumer already downloaded. It computes forecasts in memory and returns JSON.
It does not open a browser, contact Forebet or Railway, save history, write a
database, install dependencies, or run background jobs. The Railway service also
uses it to serve `/picks` (see `API.md`). Reader timestamp
normalization is configured separately as described below.

## Use the existing downloaded feed

```python
from prediction_engine import predict_feed

# Inside the downstream poller, after its existing GET:
forecasts = predict_feed(feed_json)
# Return or display forecasts using your downstream application's existing flow.
```

Or process a file or stdin:

```sh
python3 -m prediction_engine downloaded-feed.json
python3 -m prediction_engine < downloaded-feed.json
```

Copy the `prediction_engine/` directory to the downstream Python project. Run
the function once for each new snapshot, rather than once for each user request.
The only computational cost is on the machine calling this function. There is no
additional scraping or feed polling. The Railway service's `/picks` endpoint
runs it once per feed refresh on the in-memory cache and stores nothing, with
`use_live_scores=True`: a game in progress contributes its score so far over
the share of the match played to the goal fit, never to Elo or form records.

The `--as-of` option is for reproducible offline analysis of an old snapshot.
Omit it in production so expired kickoffs and stale feeds are detected using
the real current time. Input timestamps must contain an explicit timezone;
the package respects offsets and normalizes them to UTC.

## What the model does

Each league is modeled separately, because its match duration and player pool
differ. Only rows with `status: played` and a valid `actual_score` enter training.
Future-dated final results and results older than 30 days are excluded. Duplicate
fixtures are counted once; conflicting final scores are excluded and reported.
Player tags are normalized for whitespace/case within a league, never merged
across leagues. Different tags that belong to the same person remain separate.

The scoring model uses up to 20 recent results per player. Weights halve every
10 matches and every seven days. It fits player attack and defense against the
opponents they actually faced, with separate league home/away scoring rates and
strongly shrunk club effects. Player estimates are pulled toward the league
average with 12 prior-game equivalents; club effects use 40. These are initial
fixed choices, not optimized parameters.

An Elo component tracks opponent-adjusted outcomes using the available results.
Ratings move back toward their starting value during inactivity. The outcome
model combines 70% scoring-distribution probabilities and 30% Elo probabilities,
using the scoring model's draw probability for the Elo component.

A negative binomial goal distribution allows more scoring variation than a
plain Poisson distribution. Its dispersion includes a term for small samples.
The resulting score matrix produces winner/draw, total-goal, both-teams-to-score,
and exact-score probabilities. It is rescaled to the final 1X2 ensemble so these
outputs agree with one another.

Forebet's current fixture probabilities are also an input. The player-form
component's weight is `0.75 * support / (support + 8)`, where support is the
smaller of the two players' effective recent sample sizes. Thus sparse history
puts more weight on Forebet. A new player gets Forebet's 1X2 probabilities,
with warnings that the goal estimates have little player evidence. Neither
Forebet's predicted score nor a missing final score is treated as a real result.

The response explains its evidence: recent 5/10/20-game records, goals scored
and conceded, opponent ratings, player ratings, head-to-head results, sample
sizes and the ensemble weights. Head-to-head records are explanatory summaries;
the same games already contribute to fitting and are not counted twice.

## Common opponents and the player network

The attack/defense fit already couples players through their opponents' ratings.
Each forecast now also includes an explicit `evidence.common_opponents` view of
two-step paths: the home player faced C, and the away player also faced C.
This comparison is built in memory from each player's latest 20 available final
results within the current league. The players' direct matches are reported
separately as head-to-head history.

For each shared opponent, the output shows each player's wins/draws/losses,
weighted goals scored and conceded, dates, clubs used, effective sample count,
and attack/defense residual factors. A residual factor compares observed goals
with the existing model's expected goals, which include opponent, venue and club
effects. Older games and comparisons far apart in time receive less weight.
Repeated games against a single opponent have capped influence.

This evidence is always included. Its additional effect on the probability
calculation is an optional experimental setting:

```python
forecasts = predict_feed(feed_json, use_common_opponents=True)
```

The setting defaults to false, because the current model already learned from
these results and additional weighting can duplicate evidence. The candidate
uses shrunk residuals with a small fixed weight and limits each additional goal
multiplier to the range `1/1.1` through `1.1`. This is a conservative experiment,
not a fitted or calibrated claim about how important shared opponents are.

```sh
python3 -m prediction_engine downloaded-feed.json --compare-opponents --limit 60
```

In the 240-game retrospective comparison, 219 targets had shared opponents.
The adjustment gained one correct winner in H2H GG and lost one in Volta; overall
winner accuracy was unchanged. Exact-score log loss worsened slightly in all
four leagues. It therefore remains optional, with the comparison visible by
default. These are exploratory results on the same reused snapshot. They do not
show that common opponents are uninformative: their results already influence
the baseline model, and this tests only one additional weighting formula.

Each player's evidence also includes `attack_strength` and `defensive_strength`.
Both use 1.0 for the league average, with larger values indicating stronger
estimated ability. Before the outcome ensemble, expected home goals are:

```text
league home scoring rate
× home player's attack strength
÷ away player's defensive strength
× home club attack factor
× away club concession factor
```

`evidence.matchup` exposes these club factors and the unblended goal expectations.
They are regularized estimates with limited evidence, not measured intrinsic
skill. Two strong players can still produce many goals if their attacks outweigh
their opponents' defenses. The model does not automatically assign an under
prediction when two highly rated players meet.

## Optional candidate comparison

```sh
python3 -m prediction_engine downloaded-feed.json --compare --limit 60
```

This compares the existing independent negative binomial model, an independent
Poisson model, and a candidate mixture allowing both scores to move with common
match pace. The last candidate uses a Gamma-Poisson joint distribution, weighted
by a strongly shrunk estimate of positive residual score covariance. That
covariance is an indirect statistical signal, not measured possession, tempo,
fatigue or tactical style. It does not represent negative score covariance.

All three use the same fitted attack/defense factors and Elo component. Targets
and training cutoffs match across candidates. Forebet probabilities are excluded.
The comparison reports winner and exact-score log losses, accuracy, total-goal
error, BTTS Brier score, and over/under Brier scores for 2.5, 4.5 and 6.5 goals.
It does not select or activate a candidate automatically.

In the expanded 240-game comparison (60 per league) on the existing snapshot,
the common-pace variant did not consistently improve the metrics. Independent
Poisson had lower exact-score log loss in all four leagues, but exact-score
hit rates improved in only two. Winner hit rates were identical across the
three candidates. These are exploratory comparisons on reused data, not an
independent validation set. The default remains `independent_nb`.

For further research, score-based Bayesian skill models explicitly model offense
and defense as separate uncertain abilities: [Guo et al., Score-based Bayesian
Skill Learning](https://www.microsoft.com/en-us/research/wp-content/uploads/2012/01/sbsl_ecml2012.pdf).
Dynamic models can also allow attacking and defending strengths to change over
time: [Koopman and Lit, dynamic bivariate Poisson football model](https://rss.onlinelibrary.wiley.com/doi/10.1111/rssa.12042).
These motivate future candidates; the current package does not implement either
paper's complete Bayesian inference procedure or claim their results transfer
to these esoccer leagues.

## Output

Top-level fields include `generated_at`, `source_fetched_at`, `stale`,
`model_version`, `validation_status`, `leagues`, and per-league `diagnostics`.
Probabilities are numbers from **0 to 1**, not percentages.

Each future fixture contains:

- `probabilities`: experimental, uncalibrated estimates for `1`, `X`, `2`.
- `most_likely_outcome`: largest probability, retained for statistical evaluation.
- `pick`: null for weak estimates, limited history, or a stale source. Otherwise an experimental lean.
- `winner_assessment`: `no clear winner`, `experimental lean`, or `unavailable: stale source`.
- `expected_goals`: home, away, total.
- `most_likely_score` and five `top_scores`, each with its probability.
- `totals`: over/under probabilities for 1.5 through 8.5 goals.
- `btts`: yes/no probabilities.
- `forebet_probabilities`, `form_model_probabilities`, `ensemble_weights`.
- `evidence`, `data_quality`, and `warnings`.

Only fixtures whose kickoff is strictly in the future are forecast. A past
kickoff without a final score is counted in
`diagnostics[league].past_kickoff_without_result`. A league with no future games
returns an empty array. Forecasts from an old or explicitly stale snapshot carry
`source_feed_stale`. A snapshot is old after six minutes. Missing timestamps
also mark it stale. Staleness is judged per league from `league_fetched_at`
and `league_clock_checks`, so one league's problem does not withhold picks in
the others; the feed-wide `stale` flag is used only when a league has no
fetch time of its own. The top-level `stale` output still reports any league.

`data_quality` describes sample availability; it is not an accuracy score.
`most_likely_score` can still have a small probability. Every forecast is labeled
experimental and uncalibrated until independent evidence supports otherwise.

## Testing accuracy with only the current snapshot

```sh
python3 -m prediction_engine downloaded-feed.json --backtest --limit 30
python3 -m unittest -q test_prediction_engine
```

The optional backtest runs only when requested and retains nothing. It orders
completed games chronologically, predicts each target using earlier games, and
assumes results were available after league match duration plus five minutes.
It excludes simultaneous and still-running matches under that assumption.
The feed does not contain historical result publication times, so this is a
retrospective diagnostic, not a record of forecasts actually made in advance.

Completed-game Forebet probabilities may have changed after kickoff. Consequently
this backtest excludes them: it tests the **form model only**, against a smoothed
league outcome-frequency baseline. It does not validate the live ensemble or
demonstrate that it beats Forebet.

Reported measures include 1X2 accuracy, log loss, multiclass Brier score, mean
absolute total-goal error, correct-score accuracy, and BTTS Brier score. Lower
loss/error scores are better. The initial 120-game diagnostic gave mixed results;
there is no established accuracy advantage. Proving calibration or improvement
would require predictions captured before kickoff and their later results,
which this stateless package does not retain.

## Resource check

On the development Mac, a supplied snapshot with 768 played rows (766 eligible
after excluding two future-dated results) and 43 future fixtures processed in
about **0.04 seconds**. The whole Python process peaked at about **20 MB** RSS.
This is a local measurement, not a Railway resource or cost guarantee. Optional
backtesting of 30 games per league took under a second in the initial run.

No reader code, refresh interval, proxy configuration, Railway variable, volume,
or deployed endpoint was changed for this package.


## Timing and confidence fixes (19 October 2026)

The reader now starts Chrome with an explicit UTC timezone and extracts the
page timezone selector alongside each displayed date. If Forebet's selector
has a fixed offset, that offset is respected; otherwise the browser's offset
is used. `Etc/GMT+8` means UTC minus eight hours, with no daylight saving time.

Twelve live-clock observations in already downloaded snapshots, spanning all
four leagues, showed a residual of 61.6–63.0 minutes between the source kickoff
plus elapsed clock and the fetch time. The Railway setting
`FOREBET_KICKOFF_CORRECTION_MINUTES=60` corrects that observed one-hour drift.
This is an explicit operational correction inferred from live clocks, not an
independently verified official kickoff timestamp. It can be changed or set to
zero without editing code. Do not apply another correction downstream.

Fixtures retain `source_kickoff_utc`, `source_display_time`,
`source_utc_offset_minutes`, `kickoff_correction_minutes`, and
`kickoff_time_status`. The response also includes `league_clock_checks`.
The check compares corrected kickoffs with numeric live clocks without making
requests or retaining history. Clocks at the league's final minute are
ignored, because they stay frozen while Forebet waits to post full time. A
median discrepancy over seven minutes marks the feed stale. No live clocks means unverified, not proof of correct timing.

The package rejects live games as forecast targets. Live observations are
recognized and excluded from training without being reported as malformed.
`live_prediction_status` is `disabled_until_validated`. The improvised Poisson
live adjustment used in conversation is not part of this package.

The display rule requires at least 55% estimated probability, a 15 percentage
point margin over the next outcome, and ten completed games per player before
returning an experimental winner lean. These are conservative presentation
thresholds, not evidence of profitability or calibrated confidence. Weights and
model probabilities have not been tuned to improve this snapshot's results.

`--backtest` now also compares goals MAE, exact scores and over-goals Brier scores
against simple league averages/Poisson totals. It reports winner reliability
bins and the accuracy/coverage of the abstaining display rule. All comparisons
use the same chronological targets and training cutoffs. If an actual
`result_seen_at` exists, training respects it instead of the assumed match
length plus five minutes.

A fair Forebet benchmark requires an already available, separately captured
`prematch_forebet` object on the completed fixture:

```json
{"prematch_forebet": {"observed_at": "2026-10-19T14:55:00Z", "p1": 45, "px": 25, "p2": 30}}
```

Its observation must precede kickoff. Current postgame percentages cannot
substitute for that evidence. The reader does not add storage to obtain it;
when it is absent the comparison explicitly reports
`unavailable_no_prematch_observations`. No network calls or databases are added
by the prediction package or its evaluation commands.
