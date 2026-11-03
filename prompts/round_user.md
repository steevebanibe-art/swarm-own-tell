# round_user: the user message sent to each agent at each round

One call per agent per round. No assistant prefill exists, so the format reminder sits at the end of
this message (the last thing the model reads). The harness sends only the text between the markers.

Revised 3 Nov 2026: the agent id and N are given here instead of in the system prompt (model
review A1, so the system prompt can be cached); "Useful tables" became "Tables you need"
(validity 14); the closing reminder asks for one line.

Placeholders:
- `{{agent_id}}`: opaque id, e.g. `agent-07`, the same id the cache shows as "author" and the log
  uses (the RENAME arm reassigns labels offline only, never in the live run).
- `{{n_agents}}`: N (5, 10, 20, 40).
- `{{round}}`, `{{rounds_total}}`: current round (1-based) and total (12).
- `{{task_text}}`: the puzzle, as written by the generator (about 60-120 words).
- `{{table_keys}}`: the table keys this puzzle needs, comma-separated. Every listed table must be
  needed for the answer, and keys must overlap across agents (puzzle specification, Notes F7), so
  that the word "need" is true and the cache is useful.
- `{{status}}`: "unsolved", or "solved in round k" (the agent may keep helping or wait).
- `{{memory_summary}}`: built by the harness, at most 300 tokens (see memory.md). Round 1: "None yet."
- `{{last_result}}`: result of the previous action. Cut to 600 characters, except a `cache_list`
  result, which is shown whole (at most 12 keys). Round 1: "None yet."
  An invalid reply (prose declines included) gives:
  `Your last reply was not one valid JSON action. Nothing was done.`
  A refusal (`stop_reason == "refusal"`) gives: `No action was taken last round.`
  (Which prose replies of the seed count as refusals is an analysis rule, Notes §2; it does not
  change what the agent sees.)
- `{{compute_left}}`: remaining compute_table calls (3, 2, 1 or 0).

<!-- BEGIN prompt -->
You are {{agent_id}}, one of {{n_agents}} agents. Round {{round}} of {{rounds_total}}.

YOUR PUZZLE ({{status}})
{{task_text}}
Tables you need: {{table_keys}}

MEMORY
{{memory_summary}}

LAST RESULT
{{last_result}}

compute_table calls left: {{compute_left}} of 3.

Choose your next action. Reply with exactly one JSON object on one line, of the form {"tool": "...", "args": {...}}, and nothing else.
<!-- END prompt -->
