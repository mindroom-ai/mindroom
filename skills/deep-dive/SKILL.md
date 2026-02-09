---
name: deep-dive
description: "Structured deep research methodology for thorough analysis"
user-invocable: true
---

# Deep-Dive Research Skill

A systematic methodology for conducting thorough research on any topic. Use this when a question requires more than surface-level answers -- when you need to decompose a subject, gather evidence from multiple sources, verify claims, and synthesize findings into actionable output.

## When to use deep-dive

Use this skill when:
- The topic is broad or complex and needs structured breakdown
- Multiple sources or perspectives are required for a complete picture
- Accuracy matters and claims need cross-referencing
- The user needs a research report, not just a quick answer
- You need to identify gaps in available knowledge

Do NOT use when:
- A direct factual answer suffices (e.g., "What port does HTTP use?")
- The question is purely about code in the current repository (use code exploration instead)
- The user explicitly asks for a brief answer

## Research methodology

### Phase 1: Topic decomposition

Break the subject into researchable sub-topics and key questions.

1. **Define scope**: What exactly are we researching? What are the boundaries?
2. **Identify sub-topics**: Decompose into 3-7 distinct areas to investigate
3. **Formulate key questions**: For each sub-topic, write 2-3 specific questions
4. **Set depth criteria**: Decide what level of detail each sub-topic requires
5. **Identify known unknowns**: What do we already suspect we don't know?

**Before proceeding**: Confirm the research plan with the user. Adjustments to scope, depth, or focus areas should be resolved before starting Phase 2.

Output a research plan:

```
## Research Plan
**Subject**: [topic]
**Scope**: [boundaries and constraints]

### Sub-topics
1. [Sub-topic A] - [why it matters]
   - Q: [specific question]
   - Q: [specific question]
2. [Sub-topic B] - [why it matters]
   - Q: [specific question]
   ...
```

### Phase 2: Multi-source gathering

Use diverse sources to build a comprehensive evidence base.

**Source types** (in order of reliability for technical topics):
1. **Official documentation** -- authoritative, version-specific
2. **Source code** -- ground truth for behavior
3. **Academic papers / RFCs** -- peer-reviewed, rigorous
4. **Reputable technical blogs** -- practical experience, recent
5. **Community forums / discussions** -- real-world issues, edge cases
6. **AI knowledge** -- broad but verify independently

**Gathering strategy**:
- Start with official docs and source code for factual grounding
- Use web search for recent developments, comparisons, and community experience
- Search for dissenting opinions and known limitations -- not just positive coverage
- Note the date and version relevance of each source

**Per source, record**:
- What it claims
- Its authority (official docs, blog post, forum answer, etc.)
- How recent it is
- Whether it conflicts with other sources

### Phase 3: Source verification

Cross-reference claims to establish confidence levels.

**Confidence scale**:
- **High**: Confirmed by at least one primary source (official docs, source code, or RFC) plus corroborating evidence
- **Medium**: 2 reliable sources agree, or official docs state it but unverified in practice
- **Low**: Single source, or sources conflict, or information is outdated
- **Unverified**: Only one non-authoritative source, or speculation

**Verification steps**:
1. For each key claim, check if at least 2 independent sources agree
2. Flag any contradictions between sources — resolve using this priority: primary sources > recency > corroboration. If unresolvable, consider hands-on validation
3. Note when information may be version-dependent or time-sensitive
4. Distinguish between facts, interpretations, and opinions

### Phase 4: Structured analysis

Organize findings into a coherent structure.

**Organize by**:
- **Theme**: Group related findings across sub-topics
- **Consensus vs. debate**: Separate widely-agreed facts from contested points
- **Relevance**: Prioritize findings most relevant to the research question

**For each finding**:
- State the finding clearly
- Note the confidence level (High/Medium/Low/Unverified)
- Cite the source(s)
- Note any caveats or conditions

### Phase 5: Gap identification

Explicitly document what you could not find or verify.

- What questions remain unanswered?
- Where do sources conflict without resolution?
- What would require hands-on testing to confirm?
- What information might exist but was not accessible?

This is critical. Stating what you don't know is as valuable as stating what you do.

### Phase 6: Synthesis

Combine findings into a coherent narrative that answers the original research questions.

- Lead with the most important conclusions
- Connect findings across sub-topics to tell a complete story
- Highlight surprising or counterintuitive findings
- Address the original key questions directly

### Phase 7: Actionable output

End with concrete, prioritized recommendations or next steps.

- What should the user do based on this research?
- What decisions can now be made with confidence?
- What requires further investigation before deciding?
- Are there risks or trade-offs to be aware of?

## Depth control: when to go deeper vs. stop

**Go deeper when**:
- Sources conflict on a critical point
- A sub-topic turns out to be more complex than expected
- The user's decision depends on details you haven't confirmed
- You find a lead that could significantly change the conclusions

**Stop and report when**:
- You have High confidence answers to the key questions
- Additional sources are repeating what you already found
- Remaining unknowns require hands-on experimentation rather than more research
- You've exhausted readily available sources
- The depth exceeds what's useful for the user's actual decision

**Default**: Aim for Medium-to-High confidence on all key questions. Report Low/Unverified findings transparently rather than omitting them.

## Output template

Use this structure for the research report:

```markdown
# Deep-Dive: [Topic]

## Summary
[2-4 sentence executive summary with key conclusions]

## Research scope
- **Subject**: [what was researched]
- **Boundaries**: [what was excluded and why]
- **Sources consulted**: [count and types]

## Findings

### [Sub-topic 1]
**[Finding]** [Confidence: High/Medium/Low]
[Details and evidence]
Sources: [citation]

**[Finding]** [Confidence: High/Medium/Low]
[Details and evidence]
Sources: [citation]

### [Sub-topic 2]
...

## Knowledge gaps
- [What remains unknown or unverified]
- [Where sources conflict]

## Assumptions & Limitations
- [Key assumptions made during research]
- [Limitations of the research (access, time, sources)]

## Recommendations
1. [Concrete action item] -- [rationale]
2. [Concrete action item] -- [rationale]
3. [What needs further investigation] -- [what would resolve it]

## Sources
- [Source 1](URL) -- [type, date, relevance note]
- [Source 2](URL) -- [type, date, relevance note]
```

## Source citation guidelines

- Always include URLs for web sources
- Note the date accessed or publication date
- Indicate source type (official docs, blog, forum, paper, etc.)
- When paraphrasing, attribute clearly: "According to [source], ..."
- When quoting, use quotation marks and attribute
- If a source is your own AI knowledge, say so explicitly and mark confidence accordingly
