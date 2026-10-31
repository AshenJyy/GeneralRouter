# GeneralRouter

**Task-level model selection for DeepSeek Harness · v0.1.13 experimental**

GeneralRouter turns a planner's four-dimensional task weights into a model/effort recommendation with an inspectable ranking. The main agent resolves and invokes its existing native, Codex or Antigravity tool. The Router has no execution guard, ticket, provider lock or repair-preparation workflow.

The author proposed the move from nine dimensions to four: agentic, coding, knowledge/document and scientific reasoning. Artificial Analysis supplies the model evaluation data and the category-weight reference; GeneralRouter implements the data transformation, task-profile process and matching rule. Ordinary task weights are planner estimates using authored semantic anchors, not AA measurements or success probabilities. [Algorithm](CORE-ALGORITHM.md).

The current scorer mixes task weights with a 0.4 composite prior and ranks capability vectors by weighted dot product with explicit missing-data handling. Its 12 configurations are model/effort reference profiles, not proof of 12 callable routes. Cost and latency are recorded when observable but not optimized by the scorer. Quota percentages are reference information rather than execution permission.

The goal is to preserve delivery quality while reducing completion time and pressure on constrained model usage. The saved v0.1.7 translation-extension case did not achieve that goal: B used fewer GPT-route tokens but more total reported tokens, took longer and left navigation untranslated. Eleven candidates were excluded by the then-active native allowlist filter. [Case study](experiments/translation-ab-v2/README.md).

The author subsequently clarified that Router should only select. v0.1.13 removes the accumulated execution-control layer. The main agent still handles a repair using a fresh worker of the same model and a concise memento, without another model match. Optional result recording is separate from execution. Passive usage observation may omit external plugin workers; missing usage remains unknown and report totals are observed subtotals.

A [2026-10-30 demonstration](demos/2026-10-30.md) exercised one v0.1.13 workflow: a coding-task profile, twelve scored configurations, an Argon/high recommendation, one Antigravity worker call and a delivered HTML page. The user checked its fixed example and obtained 2 valid rows, 2 invalid rows and average score 15. Actual worker model/effort and complete usage remained unconfirmed. The same-model repair branch was not exercised, and no new comparative performance experiment was run.

The declared DSH range remains `>=0.2.1-rc.2 <0.2.2-0`, with source comparison through rc.5. That range is not a guarantee that future host interfaces have been verified. The historical experiment records remain unchanged.
