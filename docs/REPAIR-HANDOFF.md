# Same-model repair owned by the main agent

From v0.1.13, GeneralRouter selects models only. A failed task returns to the main agent, which decides whether to repair within the user's scope. It starts a fresh subagent of the same model, using the appropriate existing tool and the supported corresponding effort or disclosed default. No Router ticket, prepare-repair call or model rematch is needed.

The new worker receives the original requirements, existing artifacts and a short memento: what was attempted, the observed failure, relevant evidence and the next useful step. The predecessor's failure remains part of the record. Repair success does not prove the original execution passed, and a repair with additional evidence is not a controlled same-input model comparison.

The user proposed the same-model/fresh-worker architecture. The memento concept was referenced from [Anthropic's self-replicating AI article](https://www.anthropic.com/research/self-replicating-ai), Appendix A. This project uses a compact handoff note, not autonomous replication, periodic restarts or unbounded repair loops.

Versions 0.1.8–0.1.12 implemented this through a preparation tool, frozen routes and tickets. v0.1.13 removes that Router enforcement layer; the main-agent workflow retains the same-model repair preference. Historical records remain unchanged. The [October 30 demonstration](demos/2026-10-30.md) exercised an initial recommendation and plugin worker delivery, but did not exercise this repair workflow.
