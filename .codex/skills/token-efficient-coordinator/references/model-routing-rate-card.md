# Model Routing Rate Card

Use this reference for quick routing math. Verify current official pricing when exact billing matters.

## Aliases

- `frontier_deep`: `gpt-5.5` + `reasoning.effort: xhigh`
- `frontier_balanced`: `gpt-5.5` + `reasoning.effort: medium`
- `frontier_reviewer`: `gpt-5.5` + `reasoning.effort: high`
- `standard_balanced`: `gpt-5.4` + `reasoning.effort: medium`
- `light_worker`: `gpt-5.4-mini` + `reasoning.effort: low`
- `atomic_worker`: `gpt-5.4-nano` + `reasoning.effort: none or low`
- `realtime_editor`: `gpt-5.3-codex-spark`

## API/Batch Price Hints Per 1M Tokens

| Model | Input | Cached input | Output |
|---|---:|---:|---:|
| `gpt-5.5` | 5.00 | 0.50 | 30.00 |
| `gpt-5.4` | 2.50 | 0.25 | 15.00 |
| `gpt-5.4-mini` | 0.75 | 0.075 | 4.50 |
| `gpt-5.4-nano` | 0.20 | 0.02 | 1.25 |

## Codex Credit Hints Per 1M Tokens

| Model | Input | Cached input | Output |
|---|---:|---:|---:|
| `gpt-5.5` | 125 | 12.5 | 750 |
| `gpt-5.4` | 62.5 | 6.25 | 375 |
| `gpt-5.4-mini` | 18.75 | 1.875 | 113 |

## Math

```text
estimated_cost = input_mtok * input_rate
  + cached_input_mtok * cached_input_rate
  + output_mtok * output_rate
```

Choose a swarm only when:

```text
coordinator_cost + sum(worker_costs) <= 0.85 * monolithic_gpt_5_5_cost
```

Use 0.70 when quality risk is low and the scope is separable. Use 0.85 when the coordinator must spend more synthesis tokens. Override the threshold for security, money movement, auth, migrations, or data-loss risk.
