# Creative DNA Narrative Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `blog-writer-mcp` Creative DNA so it captures narrative structure and can apply that structure during article generation without changing the existing product role split.

**Architecture:** Keep `CreativeDNAManager` as the single source of truth for writer DNA extraction and persistence, but expand the stored schema with `narrative_dna` and `key_prop_tendency`. Keep `blog_write_article` as the orchestration boundary: it loads Creative DNA, builds one combined `style_prefix`, and passes that to `bots/writer_bot.py`, which remains the final prompt assembler.

**Tech Stack:** Python 3.12, Pydantic, FastMCP, pytest

---

### Task 1: Lock The Contract With Failing Tests

**Files:**
- Modify: `C:\Users\sinmb\workspace\blog-writer-mcp\tests\test_blogwriter_mcp_tools.py`
- Modify: `C:\Users\sinmb\workspace\blog-writer-mcp\tests\test_blogwriter_mcp_server.py`
- Modify: `C:\Users\sinmb\workspace\blog-writer-mcp\tests\test_writer_bot_mcp_integration.py`

- [ ] **Step 1: Write the failing Creative DNA schema test**

Add assertions that a saved DNA payload now includes:
- `narrative_dna.opening_hook`
- `narrative_dna.tension_engine`
- `narrative_dna.signature_move`
- `narrative_dna.resolution_pattern`
- `key_prop_tendency`

- [ ] **Step 2: Run the targeted test to verify it fails**

Run: `cmd /c "set PYTHONPATH=.&& pytest tests\test_blogwriter_mcp_tools.py -q"`
Expected: FAIL because the current `CreativeDNA` model does not expose `narrative_dna` or `key_prop_tendency`.

- [ ] **Step 3: Write the failing server integration test**

Add coverage that `blog_write_article(..., apply_dna=True, apply_narrative=True)` passes a prompt prefix containing narrative structure guidance, not just style guidance.

- [ ] **Step 4: Run the targeted test to verify it fails**

Run: `cmd /c "set PYTHONPATH=.&& pytest tests\test_blogwriter_mcp_server.py -q"`
Expected: FAIL because `WriteArticleInput` does not yet support `apply_narrative` and the generated prefix does not include narrative instructions.

- [ ] **Step 5: Write the failing writer bot integration test**

Add coverage that a narrative-aware `style_prefix` can be prepended without changing persistence behavior.

- [ ] **Step 6: Run the targeted test to verify it fails**

Run: `cmd /c "set PYTHONPATH=.&& pytest tests\test_writer_bot_mcp_integration.py -q"`
Expected: FAIL because the new narrative structure text is not yet represented in the final prompt contract.


### Task 2: Expand The Creative DNA Contract

**Files:**
- Modify: `C:\Users\sinmb\workspace\blog-writer-mcp\blogwriter_mcp\tools\creative_dna.py`
- Modify: `C:\Users\sinmb\workspace\blog-writer-mcp\config\creative_dna.json`

- [ ] **Step 1: Add the new Pydantic types**

Create a focused `NarrativeDNA` model inside `creative_dna.py` with:
- `opening_hook`
- `tension_engine`
- `signature_move`
- `resolution_pattern`

Then add:
- `narrative_dna: NarrativeDNA`
- `key_prop_tendency: str`
to `CreativeDNA`.

- [ ] **Step 2: Update prompt-context rendering**

Extend `CreativeDNA.to_prompt_context()` so it can emit both:
- writer style guidance
- narrative structure guidance

Keep the output as one plain string because `writer_bot.py` already accepts `style_prefix: str`.

- [ ] **Step 3: Extend the extraction prompt**

Update `CreativeDNAManager._system_prompt()` and `_build_prompt()` so the LLM is explicitly asked to return:
- existing style fields
- new `narrative_dna`
- new `key_prop_tendency`

- [ ] **Step 4: Update the sample config file**

Make `config/creative_dna.json` match the new persisted schema so local runtime and tests share the same contract.

- [ ] **Step 5: Run the Creative DNA tests**

Run: `cmd /c "set PYTHONPATH=.&& pytest tests\test_blogwriter_mcp_tools.py -q"`
Expected: PASS


### Task 3: Add Narrative Control To Article Writing

**Files:**
- Modify: `C:\Users\sinmb\workspace\blog-writer-mcp\blogwriter_mcp\server.py`

- [ ] **Step 1: Extend the input model**

Add `apply_narrative: bool = True` to `WriteArticleInput`.

- [ ] **Step 2: Build narrative-aware prompt selection**

Inside `blog_write_article`, keep the existing `apply_dna` behavior, but:
- if `apply_dna=False`, use no Creative DNA prefix
- if `apply_dna=True` and `apply_narrative=False`, use style-only DNA guidance
- if `apply_dna=True` and `apply_narrative=True`, use full style + narrative guidance

- [ ] **Step 3: Preserve response compatibility**

Keep `dna_applied` behavior, and add a clear `narrative_applied` flag in the returned article payload so clients can see whether structural guidance was used.

- [ ] **Step 4: Run the server tests**

Run: `cmd /c "set PYTHONPATH=.&& pytest tests\test_blogwriter_mcp_server.py -q"`
Expected: PASS


### Task 4: Keep Writer Bot As The Final Prompt Assembler

**Files:**
- Modify: `C:\Users\sinmb\workspace\blog-writer-mcp\bots\writer_bot.py`
- Modify: `C:\Users\sinmb\workspace\blog-writer-mcp\tests\test_writer_bot_mcp_integration.py`

- [ ] **Step 1: Confirm prompt-prepend boundary**

Keep `style_prefix` as the single integration surface so `writer_bot.py` does not learn Creative DNA internals.

- [ ] **Step 2: Tighten prompt wording if needed**

If the current `_build_prompt()` text weakens narrative instructions, minimally adjust prompt wording so the prepended DNA block remains authoritative.

- [ ] **Step 3: Run the writer bot tests**

Run: `cmd /c "set PYTHONPATH=.&& pytest tests\test_writer_bot_mcp_integration.py -q"`
Expected: PASS


### Task 5: Verify The Whole Creative DNA Flow

**Files:**
- Verify only

- [ ] **Step 1: Run the focused suite**

Run: `cmd /c "set PYTHONPATH=.&& pytest tests\test_blogwriter_mcp_tools.py tests\test_blogwriter_mcp_server.py tests\test_writer_bot_mcp_integration.py -q"`
Expected: All targeted tests pass.

- [ ] **Step 2: Sanity-check the prompt contract**

Run a short Python snippet or targeted test assertion proving that:
- style-only mode omits narrative structure text
- narrative mode includes hook / tension / signature / resolution guidance

- [ ] **Step 3: Commit**

```bash
git add blogwriter_mcp/tools/creative_dna.py blogwriter_mcp/server.py bots/writer_bot.py config/creative_dna.json tests/test_blogwriter_mcp_tools.py tests/test_blogwriter_mcp_server.py tests/test_writer_bot_mcp_integration.py docs/superpowers/plans/2026-04-03-creative-dna-narrative-extension.md
git commit -m "feat(creative-dna): add narrative DNA structure to article generation"
```
