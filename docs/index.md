---
title: Catalog
hide:
  - navigation
  - toc
---

<div class="home-wrapper">

<div class="dc-hero">
  <span class="dc-hero__badge"><span class="dc-hero__dot"></span>Built on Conductor</span>
  <h1 class="dc-hero__title">Production-grade AI agents,<br><span class="dc-hero__hl">built on Conductor</span></h1>
  <p class="dc-hero__sub">A community catalog of long-running agent harnesses — each a self-contained, runnable project that shows how to build a serious agent on Conductor primitives. Clone one, read it, run it in minutes.</p>
  <div class="dc-actions">
    <a class="dc-btn dc-btn--primary" href="#catalog">Browse ready harnesses →</a>
    <a class="dc-btn dc-btn--ghost" href="https://github.com/conductor-oss/conductor-agents">View on GitHub</a>
  </div>
  <div class="dc-repo-wrap">
    <a class="repo-link" href="https://github.com/conductor-oss/conductor" target="_blank" rel="noopener">
      <svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
      conductor-oss/conductor
      <span class="repo-stats">
        <span class="repo-stat"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2l2.9 6.6 7.1.6-5.4 4.7 1.7 7L12 17.8 5.7 21.5l1.7-7L2 9.8l7.1-.6z"/></svg>32k</span>
        <span class="repo-stat"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="6" cy="5" r="2.5"/><circle cx="18" cy="5" r="2.5"/><circle cx="12" cy="19" r="2.5"/><path d="M6 7.5v3a3 3 0 003 3h6a3 3 0 003-3v-3M12 13.5v3"/></svg>952</span>
      </span>
    </a>
  </div>
</div>

<div class="dc-section" id="catalog">

<div class="dc-section__head">
  <h2 class="dc-section__title">Browse agents</h2>
  <div class="dc-search">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
    <input type="text" id="dc-search" placeholder="Search agents…" autocomplete="off">
  </div>
</div>

<div class="dc-pills" id="dc-pills" role="tablist">
  <button class="dc-pill" data-cat="All" aria-selected="true" role="tab">All</button>
  <button class="dc-pill" data-cat="Security" role="tab">Security</button>
  <button class="dc-pill" data-cat="DevOps" role="tab">DevOps</button>
  <button class="dc-pill" data-cat="Research" role="tab">Research</button>
  <button class="dc-pill" data-cat="Support" role="tab">Support</button>
</div>

<div class="dc-grid" id="dc-grid">
<a class="dc-card dc-card--featured acc-security" href="security-harness/" data-cat="Security" data-text="security harness penetration testing dast sast exploit sarif ssrf idor">
  <span class="dc-card__tag">Security · Ready</span>
  <span class="dc-card__name">Security Harness</span>
  <span class="dc-card__desc">Autonomous web-app &amp; API penetration tester — crawls, reasons about the attack surface, actively exploits, triages false positives, and writes a report + SARIF + attack-graph dossier.</span>
  <span class="dc-card__more">View agent →</span>
</a>
<a class="dc-card acc-devops" href="coding-harness/" data-cat="DevOps" data-text="coding harness github issue pull request parallel worktree code review">
  <span class="dc-card__tag">DevOps · Ready</span>
  <span class="dc-card__name">Coding Harness</span>
  <span class="dc-card__desc">Autonomous coding across local repositories, GitHub issues, and pull requests with parallel worktrees, durable sub-workflows, and human publication gates.</span>
  <span class="dc-card__more">View agent →</span>
</a>
<a class="dc-card acc-research" href="agents/deep-research/" data-cat="Research" data-text="deep research multi-source search read synthesize cite fork join">
  <span class="dc-card__tag">Research · Coming soon</span>
  <span class="dc-card__name">Deep Research</span>
  <span class="dc-card__desc">Multi-source research agent — fan out searches in parallel, read, synthesize, and cite, then verify claims before writing.</span>
  <span class="dc-card__more">Preview →</span>
</a>
<a class="dc-card acc-support" href="agents/customer-support/" data-cat="Support" data-text="customer support triage retrieve act escalate react tool human in the loop">
  <span class="dc-card__tag">Support · Coming soon</span>
  <span class="dc-card__name">Customer Support</span>
  <span class="dc-card__desc">Tool-using support agent — triage, retrieve, act, and escalate cleanly to a human when confidence drops.</span>
  <span class="dc-card__more">Preview →</span>
</a>
</div>

<div class="dc-noresults" id="dc-noresults">No agents match your search. Try a different term or category.</div>

</div>

</div>
