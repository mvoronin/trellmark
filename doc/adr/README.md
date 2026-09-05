# Architecture decision records

An ADR records a consequential choice, its evidence, and the conditions under which it should change. Keep accepted records as historical decisions; a changed decision gets a new record linked to the one it supersedes.

## Records

| Number | Decision date | Status | Decision |
|---|---|---|---|
| 0001 | 2026-09-04 | Accepted | [Keep the framework-free TypeScript frontend](0001-framework-free-frontend.md) |

## Record format

Use the next unused four-digit number and a short lowercase hyphenated name: `NNNN-decision-name.md`. Numbers are stable identifiers, not dates. Use English and ISO dates (`YYYY-MM-DD`). Distinguish the original decision date from the date a record was written or its evidence was measured; never backdate fresh measurements.

Each record contains:

1. **Title and metadata:** number, decision name, status (`Proposed`, `Accepted`, `Rejected`, or `Superseded`), decision date, recorded date, and scope/issue links. A superseded record links its replacement.
2. **Context:** the concrete problem and constraints, with dated measurements where relevant. Include source revision, tool versions, exact metric definitions, and an executable reproduction command. Distinguish measurements, historical reports, and judgments.
3. **Decision:** what is chosen now, what is excluded, and the scope of the commitment.
4. **Alternatives:** credible options with their benefits, costs, and rejection rationale. Verify technical claims against primary sources and record when those sources were checked.
5. **Strongest counterargument:** the best argument against the selected option and the residual cost it exposes. Do not weaken the alternative to make the choice appear inevitable.
6. **Rationale:** why the choice fits present constraints. Separate measured results from expected risks; correct unsupported premises explicitly.
7. **Consequences:** concrete work, ongoing responsibilities, limitations, and links to follow-up issues. Mark future work as future work.
8. **Reconsideration:** observable symptoms and the evidence needed to evaluate them. A trigger opens a decision review; it does not silently authorize a migration. If superseded, record the new decision instead of rewriting history.

Review the finished record for internal link validity, reproducible measurements, fair alternatives, factual provenance, and private material. Documentation-only records use document/file checks and substantive review; they do not require artificial implementation tests.

ADR 0001's three reconsideration symptoms are intentionally specific: full re-rendering loses cursor position or scroll during Markdown editing; a second routed view needs shared state; live search is measurably slow at realistic note counts. Their evidence requirements live in the [record's Reconsideration section](0001-framework-free-frontend.md#reconsideration). They are not feature-count thresholds or authorization to implement those features in Phase 3.
