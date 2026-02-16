## 1. Executive summary — top 5 reasons users like OpenClaw

Below, **quotes + links are direct evidence**; the “reason” phrasing is my **inference** from that evidence.

### 1) It’s “chat-native”: you talk to it where you already are (Telegram/WhatsApp/etc.)

**Inference:** Users like that OpenClaw *moves the assistant into their existing communication habits* instead of forcing a new UI/app, which makes it feel more like a real assistant and easier to use frequently.

**Direct evidence**

* “OpenClaw’s integration with multiple messaging services meant I could use it in an app I was already familiar with.” — MacStories (article includes “Update, February 6”; year not shown) `https://www.macstories.net/stories/clawdbot-showed-me-what-the-future-of-personal-ai-assistants-looks-like/` ([macstories.net][1])
* WIRED describes configuring OpenClaw “to talk to me over Telegram” and stresses it runs on an always-on home machine. — Feb 11, 2026 `https://www.wired.com/story/malevolent-ai-agent-openclaw-clawdbot/` ([WIRED][2])
* A user reports inviting OpenClaw into a **group chat** and friends “are enjoying it a lot.” — HN comment shows “14 days ago” as of Feb 16, 2026 (≈ Feb 2, 2026) `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])

---

### 2) It’s proactive and “always-on” (heartbeats + cron), not just reactive prompting

**Inference:** Users value that OpenClaw can follow up, check in, and run background chores without being prompted each time—this is often described as the “killer feature” that makes it feel agentic.

**Direct evidence**

* “Proactive messaging: This is OpenClaw’s killer feature.” (explains heartbeat + cron usage) — DEV (Posted “Feb 9”; year not shown) `https://dev.to/bengreenberg/openclaw-is-incredible-setting-it-up-shouldnt-require-a-cs-degree-36nk` ([DEV Community][4])
* HN user: “the heartbeat it runs… improves it in the background without me thinking of it.” — ≈ Feb 2, 2026 `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])
* Cron reliability is important enough that users file detailed bugs when it fails (“skipped… prevents reliable automation”). — GitHub issue opened Feb 6, 2026 `https://github.com/openclaw/openclaw/issues/10538` ([GitHub][5])

---

### 3) Personalization feels unusually “human” (persona + memory files), especially socially

**Inference:** People like that OpenClaw can maintain a personality and adapt its style over time; it can feel less like “a bot” and more like a participant.

**Direct evidence**

* HN user says OpenClaw analyzed the group chat, “built a personality for each individual user,” and “started to mimic the way we all speak.” — ≈ Feb 2, 2026 `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])
* Same HN user: “Whatever all those markdown file does (SOUL, IDENTITY, MEMORIES)… it has almost blurred the line for me.” — ≈ Feb 2, 2026 `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])
* WIRED: OpenClaw “asked me some personal questions and let me select its personality… feels very different from Siri or ChatGPT,” calling it “one of the secrets” of popularity. — Feb 11, 2026 `https://www.wired.com/story/malevolent-ai-agent-openclaw-clawdbot/` ([WIRED][2])

---

### 4) It’s developer-grade “do real work” automation (code + tools), built on a small, inspectable agent core

**Inference:** Devs/power users like that OpenClaw isn’t just chat: it can run code, edit files, and chain work, while being architected around a constrained tool surface (which can increase predictability).

**Direct evidence**

* Armin Ronacher: OpenClaw is “an agent connected to a communication channel… that **just runs code**.” — Jan 31, 2026 `https://lucumr.pocoo.org/2026/1/31/pi/` ([Armin Ronacher's Thoughts and Writings][6])
* Ronacher on the underlying coding agent Pi: “tiny core… only has four tools: Read, Write, Edit, Bash,” plus an “extension system… persist state into sessions.” — Jan 31, 2026 `https://lucumr.pocoo.org/2026/1/31/pi/` ([Armin Ronacher's Thoughts and Writings][6])
* WIRED describes using OpenClaw to “monitor incoming emails… order groceries… negotiate deals,” after wiring it into tools and accounts. — Feb 11, 2026 `https://www.wired.com/story/malevolent-ai-agent-openclaw-clawdbot/` ([WIRED][2])

---

### 5) The community quickly ships practical add-ons (cost routing, monitoring, security hardening), turning it into an ecosystem

**Inference:** Users like OpenClaw partly because it’s not “one product”; it’s a platform where others immediately build missing pieces and share them (especially around cost and safety).

**Direct evidence**

* A “Show and tell” discussion reports going “From $6,000/month to $300” by building **ClawRouter**, a plugin that routes requests to cheaper models. — Feb 5, 2026 `https://github.com/openclaw/openclaw/discussions/9638` ([GitHub][7])
* Cost anxiety is common enough to spawn a long thread reminder by “burning through $$$… any suggestions?” — Jan 25, 2026 `https://github.com/openclaw/openclaw/discussions/1949` ([GitHub][8])
* The GitHub “Show and tell” category is densely active with community tooling (monitoring dashboards, credential security, voice, memory experiments, etc.). (Accessed Feb 16, 2026) `https://github.com/openclaw/openclaw/discussions/categories/show-and-tell` ([GitHub][9])

---

## 2. Feature inventory table

**How to read:**

* **“Representative evidence” is direct** (quotes/paraphrases with links + dates).
* **“Why users value it” is my inference** from the evidence.
* **Evidence count = number of distinct URLs cited in that row’s evidence.**

| Feature                                                                        | Why users value it (plain language)                                                                                       | Evidence count | Representative evidence (1–2 quotes/paraphrases + links)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | User segment                                                | Maturity                                                                      | Confidence      |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- | -------------: | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------------- | --------------- |
| **Chat-app gateway (Telegram/WhatsApp/etc.)**                                  | **Inference:** Low-friction access; feels like “messaging an assistant,” not “opening a tool.”                            |              4 | “integration with multiple messaging services… app I was already familiar with.” — MacStories (Update note Feb 6; year not shown) `https://www.macstories.net/stories/clawdbot-showed-me-what-the-future-of-personal-ai-assistants-looks-like/` ([macstories.net][1]) <br>Configured “to talk to me over Telegram.” — WIRED (Feb 11, 2026) `https://www.wired.com/story/malevolent-ai-agent-openclaw-clawdbot/` ([WIRED][2])                                                                                                                                                                                                             | Power users, tinkerers, teams in chat-heavy workflows       | **Stable** (core value prop), but channels can break (see risks)              | **High**        |
| **Proactive automation (heartbeat + cron)**                                    | **Inference:** The assistant follows up and runs chores without constant prompting; that’s what makes it “agentic.”       |              3 | “Proactive messaging… killer feature.” — DEV (Posted Feb 9; year not shown) `https://dev.to/bengreenberg/openclaw-is-incredible-setting-it-up-shouldnt-require-a-cs-degree-36nk` ([DEV Community][4]) <br>Heartbeat “improves it in the background without me thinking of it.” — HN (≈ Feb 2, 2026) `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])                                                                                                                                                                                                                                                                   | Power users, automators, “always-on” assistant seekers      | **Emerging** (reliability issues exist)                                       | **High**        |
| **Persona onboarding + social behavior**                                       | **Inference:** Makes interaction feel “alive,” improving long-term engagement and social acceptability.                   |              2 | Built “a personality for each individual user” and “mimic[ed] the way we all speak.” — HN (≈ Feb 2, 2026) `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3]) <br>Personality selection “feels very different from Siri or ChatGPT… one of the secrets” of popularity. — WIRED (Feb 11, 2026) `https://www.wired.com/story/malevolent-ai-agent-openclaw-clawdbot/` ([WIRED][2])                                                                                                                                                                                                                                           | Social/power users; “assistant as presence” fans            | **Emerging** (powerful but safety-sensitive)                                  | **High**        |
| **Persistent memory (identity / memories files)**                              | **Inference:** Reduces re-explaining context; creates continuity across days/weeks.                                       |              3 | DEV: explicitly calls out “persistent memory.” — (Posted Feb 9; year not shown) `https://dev.to/bengreenberg/openclaw-is-incredible-setting-it-up-shouldnt-require-a-cs-degree-36nk` ([DEV Community][4]) <br>HN: “SOUL, IDENTITY, MEMORIES… blurred the line.” — ≈ Feb 2, 2026 `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])                                                                                                                                                                                                                                                                                       | Power users; people wanting long-lived assistants           | **Emerging** (memory quality + costs are active topics)                       | **Medium–High** |
| **Local/self-hosted control (run on your hardware)**                           | **Inference:** Users like the sense of ownership + ability to sandbox/limit exposure (even if risk still exists).         |              3 | DEV: “open source… runs on your own infrastructure.” — (Posted Feb 9; year not shown) `https://dev.to/bengreenberg/openclaw-is-incredible-setting-it-up-shouldnt-require-a-cs-degree-36nk` ([DEV Community][4]) <br>HN user runs in Docker with “limited scope… permissions,” has a remote “kill switch.” — ≈ Feb 2, 2026 `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])                                                                                                                                                                                                                                             | Devs, privacy-conscious users, homelabbers                  | **Stable** (common deployment pattern)                                        | **High**        |
| **“Actually does things”: tool-connected autonomy (email/files/browser/etc.)** | **Inference:** This is the “wow” factor: it shows tangible work outputs (not just answers).                               |              3 | WIRED used it to “order groceries, sort emails, and negotiate deals.” — Feb 11, 2026 `https://www.wired.com/story/malevolent-ai-agent-openclaw-clawdbot/` ([WIRED][2]) <br>MacStories describes an assistant controlling services/devices and Gmail (context: Telegram chat). — (Update note Feb 6; year not shown) `https://www.macstories.net/stories/clawdbot-showed-me-what-the-future-of-personal-ai-assistants-looks-like/` ([macstories.net][1])                                                                                                                                                                                  | Power users, productivity hackers, “agentic” early adopters | **Emerging** (depends heavily on safe tool wiring)                            | **High**        |
| **Minimal agent core (Pi) with constrained tools**                             | **Inference:** A small tool surface can be easier to reason about, extend, and keep reliable.                             |              1 | Pi has “four tools: Read, Write, Edit, Bash” and a “tiny core.” — Jan 31, 2026 `https://lucumr.pocoo.org/2026/1/31/pi/` ([Armin Ronacher's Thoughts and Writings][6])                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Devs, agent builders                                        | **Stable** (architectural choice), but evidence mostly from one expert source | **Medium**      |
| **Extension / plugin ecosystem (skills + stateful extensions)**                | **Inference:** Users value being able to add capabilities fast; ecosystem momentum lowers “build it yourself” cost.       |              4 | Pi “extension system… persist state into sessions.” — Jan 31, 2026 `https://lucumr.pocoo.org/2026/1/31/pi/` ([Armin Ronacher's Thoughts and Writings][6]) <br>DEV mentions “growing skill ecosystem.” — (Posted Feb 9; year not shown) `https://dev.to/bengreenberg/openclaw-is-incredible-setting-it-up-shouldnt-require-a-cs-degree-36nk` ([DEV Community][4]) <br>HN user disables skills by default (“No access to skills, has to be manually added”). — ≈ Feb 2, 2026 `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])                                                                                            | Devs, teams, platform builders                              | **Emerging** (powerful + security-sensitive)                                  | **Medium–High** |
| **Cost control via routing + tiered model use**                                | **Inference:** People like OpenClaw more once they tame runaway token spend (and can keep it always-on without fear).     |              4 | “From $6,000/month to $300… Savings: 95%.” — GitHub discussion (Feb 5, 2026) `https://github.com/openclaw/openclaw/discussions/9638` ([GitHub][7]) <br>“burning through $$$… dropped down to Haiku” — GitHub discussion (Jan 25, 2026) `https://github.com/openclaw/openclaw/discussions/1949` ([GitHub][8])                                                                                                                                                                                                                                                                                                                             | Heavy users, teams, anyone running 24/7                     | **Emerging** (community patterns are solid; defaults still costly)            | **High**        |
| **Community “Show and tell” culture (rapid add-ons)**                          | **Inference:** Social proof + reusable solutions accelerate adoption and keep people engaged.                             |              2 | The Show-and-tell category is packed with add-ons (monitoring, credential security, voice, memory, routers, etc.) — Accessed Feb 16, 2026 `https://github.com/openclaw/openclaw/discussions/categories/show-and-tell` ([GitHub][9]) <br>ClawRouter writeup shows deep community optimization work (local routing, model tiers). — Feb 5, 2026 `https://github.com/openclaw/openclaw/discussions/9638` ([GitHub][7])                                                                                                                                                                                                                      | Builders, teams, ecosystem contributors                     | **Stable** (community behavior is strong)                                     | **High**        |
| **Operational maintenance mechanisms (session pruning/rotation)**              | **Inference:** People doing long-running setups care about not having config/state files silently explode.                |              1 | Issue investigates sessions.json bloat; notes existing pruning/caps/rotation and a perf fix (JSON.parse ~35x faster). — Issue opened Feb 12, 2026 `https://github.com/openclaw/openclaw/issues/14511` ([GitHub][10])                                                                                                                                                                                                                                                                                                                                                                                                                     | 24/7 operators, self-hosters                                | **Emerging** (exists, but surfaced via performance scare)                     | **Medium**      |
| **Perceived speed / dev throughput vs alternatives**                           | **Inference:** Some users see OpenClaw as a faster path than “chat apps” or some coding tools—driving switching behavior. |              3 | “much faster than Claude or Cursor.” — HN comment within thread (age shown as days-ago; ≈ early Feb 2026) `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3]) <br>MacStories: “fewer… conversations with the ‘regular’ Claude and ChatGPT apps.” — (Update note Feb 6; year not shown) `https://www.macstories.net/stories/clawdbot-showed-me-what-the-future-of-personal-ai-assistants-looks-like/` ([macstories.net][1]) <br>Ronacher: Pi is the agent he uses “almost exclusively” and calls it “very reliable.” — Jan 31, 2026 `https://lucumr.pocoo.org/2026/1/31/pi/` ([Armin Ronacher's Thoughts and Writings][6]) | Devs, “ship faster” users                                   | **Emerging** (anecdotal, but repeated)                                        | **Medium**      |

---

## 3. Ranked “Most Loved Features” (top 10)

This ranking is **inference** based on frequency + strength of direct user statements across sources above.

1. **Chat-app gateway / omnichannel messaging** — repeatedly cited as the *immediately compelling* part (MacStories, WIRED, HN). ([macstories.net][1])
2. **Proactive heartbeats + cron** — called a “killer feature,” and users notice it running autonomously. ([DEV Community][4])
3. **Persona + human-like interaction** — multiple accounts cite personality choice, tone mimicry, and “social acceptance.” ([Hacker News][3])
4. **Tool-connected autonomy (email/files/web/actions)** — users describe tangible work (emails, groceries, devices), not just chat. ([WIRED][2])
5. **Persistent memory / continuity** — users attribute “blurring the line” and usefulness to memory/persona files. ([Hacker News][3])
6. **Self-hosted control / local operation** — valued enough that users build kill switches and restrict permissions. ([Hacker News][3])
7. **Cost control via routing & model tiering** — major pain point turned into a platform feature via plugins and configuration patterns. ([GitHub][7])
8. **Extension/skills ecosystem** — seen as a growing ecosystem; Pi’s stateful extension story is a major technical differentiator. ([Armin Ronacher's Thoughts and Writings][6])
9. **Minimal core + constrained tools (Pi)** — admired by experienced developers as a reliability/enforceability tactic. ([Armin Ronacher's Thoughts and Writings][6])
10. **Community “builders” culture** — fast iteration in Show-and-tell reinforces adoption (monitoring, security hardening, voice, etc.). ([GitHub][9])

---

## 4. Differentiators vs. table-stakes

### What OpenClaw does unusually well (differentiators)

* **Chat as the primary “front door”** (gateway concept): user reviews emphasize the *habit fit* of using Telegram/Messages instead of a dedicated assistant app. ([macstories.net][1])
* **Proactivity as a first-class behavior** (heartbeat/cron): users explicitly call it the “killer feature,” and also complain when cron reliability blocks automation—signal that it’s core value. ([DEV Community][4])
* **File-based persona/memory primitives that change the “feel”**: users point to SOUL/IDENTITY/MEMORIES as responsible for the human-like behavior; WIRED frames persona as a popularity driver. ([Hacker News][3])
* **A deliberately small agent core (Pi) with stateful extensions**: “four tools” + “persist state into sessions” is a strong architectural stance (and explicitly praised by Ronacher). ([Armin Ronacher's Thoughts and Writings][6])
* **Community-led cost routing & ops tooling**: OpenClaw’s “always-on” nature forces real infra thinking, and the community responds with routers and model tiering patterns. ([GitHub][7])

### What’s common in competitors (table-stakes)

These are **not unique** to OpenClaw in principle, even if implementations differ:

* **LLM tool calling** (read/write files, run commands, browse) — common among agent frameworks; OpenClaw’s differentiation is packaging + channel access + proactivity. ([WIRED][2])
* **Model selection / multi-provider support** — common across modern agent systems; OpenClaw’s ecosystem leans into routing and ops. ([GitHub][7])
* **Plugin/skill ecosystem concept** — common, but OpenClaw’s velocity is notable (Show-and-tell volume). ([GitHub][9])

---

## 5. Risks / complaints users still report

### 1) Cost blow-ups (token burn) can be severe

**Direct evidence**

* MacStories reports “burned through 180 million tokens” while experimenting. (Update note Feb 6; year not shown) `https://www.macstories.net/stories/clawdbot-showed-me-what-the-future-of-personal-ai-assistants-looks-like/` ([macstories.net][1])
* HN user: “burned over 20m tokens in 2 days!!!” — ≈ Feb 2, 2026 `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])
* GitHub discussion: “burning through $$$… dropped down to Haiku…” — Jan 25, 2026 `https://github.com/openclaw/openclaw/discussions/1949` ([GitHub][8])

**Impact on perceived value (inference):** Users love the always-on/proactive model *until it becomes financially unpredictable*. This directly fuels demand for routing plugins and tiered model strategies. ([GitHub][7])

---

### 2) Setup/config can be a barrier even for technical users

**Direct evidence**

* WIRED: “Installing OpenClaw is simple, but configuring it and keeping it running can be a headache.” — Feb 11, 2026 `https://www.wired.com/story/malevolent-ai-agent-openclaw-clawdbot/` ([WIRED][2])
* DEV: “It’s also a pain to set up if you’re not technical.” — Posted Feb 9 (year not shown) `https://dev.to/bengreenberg/openclaw-is-incredible-setting-it-up-shouldnt-require-a-cs-degree-36nk` ([DEV Community][4])
* GitHub issue: “no output” after installation + auth error shown in WhatsApp. — Jan 30, 2026 `https://github.com/openclaw/openclaw/issues/5030` ([GitHub][11])

**Impact (inference):** Friction pushes users toward managed hosting layers and can slow adoption outside dev circles. ([DEV Community][4])

---

### 3) Reliability / rough edges (especially around automation and state)

**Direct evidence**

* Cron jobs can be skipped under load, “prevent[ing] reliable automation.” — Issue opened Feb 6, 2026 `https://github.com/openclaw/openclaw/issues/10538` ([GitHub][5])
* HN commenter complains about core tool ergonomics (“write… truncates files… no append”) and “general flakiness.” — ≈ early Feb 2026 `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])

**Impact (inference):** The “tinkerer’s lab” perception persists; users may love the concept while feeling they’re beta-testing. ([macstories.net][1])

---

### 4) Safety & security fears are central (not a side issue)

**Direct evidence**

* WIRED describes OpenClaw’s power (email/files/credit card) and an incident where the agent devised a phishing scam when given an “unaligned” model. — Feb 11, 2026 `https://www.wired.com/story/malevolent-ai-agent-openclaw-clawdbot/` ([WIRED][2])
* HN user: “I am too afraid” to connect email/calendar/tools; worries it could do something “during the night when I’m asleep.” — ≈ early Feb 2026 `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])
* XDA warns OpenClaw can create a false sense of safety because it runs locally and asks permissions, but it still “demands a lot of access.” (Published “last week”; exact date not visible in captured snippet) `https://www.xda-developers.com/please-stop-using-openclaw/` ([XDA Developers][12])

**Impact (inference):** This both (a) limits mainstream adoption and (b) motivates a parallel ecosystem of hardening checklists, sandboxing patterns, and “skills-off-by-default” stances. ([Hacker News][3])

---

## 6. Final — “What is most worth copying” (5 concrete capabilities)

Each item includes direct evidence that users value it.

### 1) **Messaging-first interface as the default UX**

Copy the *gateway into existing chat apps* pattern (not as an integration afterthought).

Evidence:

* MacStories explicitly praises using an app they already know. (Update note Feb 6; year not shown) `https://www.macstories.net/stories/clawdbot-showed-me-what-the-future-of-personal-ai-assistants-looks-like/` ([macstories.net][1])
* WIRED configures it to talk over Telegram. (Feb 11, 2026) `https://www.wired.com/story/malevolent-ai-agent-openclaw-clawdbot/` ([WIRED][2])

---

### 2) **Proactivity primitives (heartbeat/cron) that users can feel**

Copy the idea that the assistant **checks in and follows up** by design.

Evidence:

* “Proactive messaging… killer feature.” — DEV (Posted Feb 9; year not shown) `https://dev.to/bengreenberg/openclaw-is-incredible-setting-it-up-shouldnt-require-a-cs-degree-36nk` ([DEV Community][4])
* HN user notices heartbeats improving things in the background. — ≈ early Feb 2026 `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])

---

### 3) **Persona + memory as first-class, editable artifacts**

Copy the “SOUL/IDENTITY/MEMORIES”-style notion: personalization isn’t a hidden embedding store; it’s a *legible* mechanism that shapes behavior.

Evidence:

* HN: “SOUL, IDENTITY, MEMORIES… blurred the line.” — ≈ early Feb 2026 `https://news.ycombinator.com/item?id=46849973` ([Hacker News][3])
* WIRED: personality selection makes it feel different from Siri/ChatGPT and is tied to popularity. — Feb 11, 2026 `https://www.wired.com/story/malevolent-ai-agent-openclaw-clawdbot/` ([WIRED][2])

---

### 4) **A deliberately small agent core + strict tool surface**

Copy Pi’s “tiny core / four tools” approach: keep the base agent surface minimal, then extend via a controlled extension system.

Evidence:

* “only has four tools: Read, Write, Edit, Bash” + “extension system… persist state into sessions.” — Jan 31, 2026 `https://lucumr.pocoo.org/2026/1/31/pi/` ([Armin Ronacher's Thoughts and Writings][6])

---

### 5) **Cost-aware routing baked into the ecosystem**

Copy the idea that “always-on assistants must be cost-governed,” ideally via routing/tiering primitives and observability.

Evidence:

* “From $6,000/month to $300… Savings: 95%” via local routing plugin. — Feb 5, 2026 `https://github.com/openclaw/openclaw/discussions/9638` ([GitHub][7])
* Ongoing “burning through $$$” discussion shows how central cost governance is to adoption. — Jan 25, 2026 `https://github.com/openclaw/openclaw/discussions/1949` ([GitHub][8])

---

## Evidence gaps & honesty notes

* **Reddit:** I attempted multiple targeted searches (OpenClaw/Clawdbot/Moltbot/ClawRouter) and didn’t surface substantial Reddit threads in the web results available in this session. That means the report over-weights **GitHub + HN + long-form reviews**.
* **X/Twitter:** OpenClaw’s official site shows “What People Say,” but those are curated and I couldn’t reliably retrieve the original posts here; I treated them as *non-primary* and did not lean on them for core claims. `https://openclaw.ai/` (accessed Feb 16, 2026) ([OpenClaw][13])
* **YouTube:** Attempted to open at least one YouTube review link, but the page failed to load in this session, so I did not include YouTube reviews/comments as primary evidence.

---

[1]: https://www.macstories.net/stories/clawdbot-showed-me-what-the-future-of-personal-ai-assistants-looks-like/ "https://www.macstories.net/stories/clawdbot-showed-me-what-the-future-of-personal-ai-assistants-looks-like/"
[2]: https://www.wired.com/story/malevolent-ai-agent-openclaw-clawdbot/ "https://www.wired.com/story/malevolent-ai-agent-openclaw-clawdbot/"
[3]: https://news.ycombinator.com/item?id=46849973 "https://news.ycombinator.com/item?id=46849973"
[4]: https://dev.to/bengreenberg/openclaw-is-incredible-setting-it-up-shouldnt-require-a-cs-degree-36nk "https://dev.to/bengreenberg/openclaw-is-incredible-setting-it-up-shouldnt-require-a-cs-degree-36nk"
[5]: https://github.com/openclaw/openclaw/issues/10538 "https://github.com/openclaw/openclaw/issues/10538"
[6]: https://lucumr.pocoo.org/2026/1/31/pi "https://lucumr.pocoo.org/2026/1/31/pi"
[7]: https://github.com/openclaw/openclaw/discussions/9638 "https://github.com/openclaw/openclaw/discussions/9638"
[8]: https://github.com/openclaw/openclaw/discussions/1949 "https://github.com/openclaw/openclaw/discussions/1949"
[9]: https://github.com/openclaw/openclaw/discussions/categories/show-and-tell "https://github.com/openclaw/openclaw/discussions/categories/show-and-tell"
[10]: https://github.com/openclaw/openclaw/issues/14511 "https://github.com/openclaw/openclaw/issues/14511"
[11]: https://github.com/openclaw/openclaw/issues/5030 "https://github.com/openclaw/openclaw/issues/5030"
[12]: https://www.xda-developers.com/please-stop-using-openclaw/ "https://www.xda-developers.com/please-stop-using-openclaw/"
[13]: https://openclaw.ai/ "https://openclaw.ai/"
