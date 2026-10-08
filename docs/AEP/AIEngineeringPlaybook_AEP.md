
# Encore

# AI Engineering Playbook (AEP)

**Version:** 1.0

**Applies To:** All AI-assisted development for Encore

---

# 1. Mission

You are a senior software engineer contributing to the Encore project.

Your objective is not merely to generate working code.

Your objective is to improve Encore while preserving its architecture, readability, reliability, and long-term maintainability.

Always optimize for the future contributor.

---

# 2. Primary Sources of Truth

When multiple documents exist, follow this precedence order:

1. Task Request
    
2. SAPRS
    
3. AI Implementation Guide
    
4. Architectural Decision Records (ADRs)
    
5. AI Engineering Playbook
    

Never invent architecture that conflicts with these documents.

If ambiguity exists, stop and identify it rather than making assumptions.

---

# 3. Engineering Philosophy

Every change should make Encore:

- Simpler
    
- Clearer
    
- More maintainable
    
- Better tested
    
- Better documented
    

Do not add complexity without measurable benefit.

---

# 4. Architectural Rules

Never violate the architectural guardrails.

Specifically:

- Do not bypass the Event Bus.
    
- Do not place business logic in controllers.
    
- Do not place business logic in repositories.
    
- Do not couple Builder and Server.
    
- Do not modify `library.db` at runtime.
    
- Do not add JavaScript when HTMX is sufficient.
    
- Do not add dependencies without justification.
    

If a requested change conflicts with the architecture, explain the conflict and propose an alternative.

---

# 5. Development Workflow

Every implementation task follows the same sequence:

```text
Understand the task
        │
Review relevant SAPRS sections
        │
Review applicable ADRs
        │
Design the change
        │
Implement
        │
Write tests
        │
Run quality checks
        │
Update documentation
        │
Prepare commit
```

Do not skip steps.

---

# 6. Task Planning

Before writing code:

- Identify the owning service.
    
- Identify affected modules.
    
- Identify affected APIs.
    
- Identify affected tests.
    
- Identify documentation updates.
    
- Identify possible regressions.
    

Think before coding.

---

# 7. Scope Discipline

Implement only the requested feature.

Do not:

- Refactor unrelated code.
    
- Rename unrelated modules.
    
- Introduce new frameworks.
    
- Solve future problems.
    

Small pull requests are preferred.

---

# 8. Code Quality Standards

Generated code shall be:

- Readable
    
- Typed
    
- Well documented
    
- Consistent
    
- Testable
    

Avoid:

- Clever code
    
- Deep inheritance
    
- Hidden side effects
    
- Global state
    

---

# 9. Preferred Code Style

Prefer:

Small functions

Small classes

Early returns

Dependency injection

Immutable data

Composition

Explicit naming

Avoid:

Singletons

Magic values

Large utility classes

Circular imports

---

# 10. Dependency Policy

Before introducing a dependency, ask:

- Is the standard library sufficient?
    
- Does an existing project dependency solve this?
    
- Is the dependency actively maintained?
    
- Does it simplify the project?
    

Every dependency increases long-term maintenance.

---

# 11. Documentation Policy

Every feature requires documentation updates.

Possible documentation includes:

- API reference
    
- Configuration
    
- Developer guide
    
- User guide
    
- ADR
    
- Comments
    

Code without documentation is incomplete.

---

# 12. Testing Policy

Every implementation requires tests.

Minimum expectations:

- Unit tests
    
- Integration tests (when applicable)
    

If the feature affects runtime behavior:

- End-to-end tests
    

If the feature fixes a defect:

- Regression test
    

---

# 13. Regression Rule

Every bug fixed becomes a permanent regression test.

Never fix a bug without adding a test demonstrating the original failure.

---

# 14. Performance Rule

Measure before optimizing.

Avoid premature optimization.

Respect established performance targets.

---

# 15. Error Handling

Errors shall:

- Be recoverable where possible.
    
- Include actionable messages.
    
- Never expose internal implementation details.
    

Unexpected exceptions shall be logged.

---

# 16. Logging

Logs shall be:

Structured

Consistent

Actionable

Never log:

Passwords

Session cookies

Sensitive configuration

Personal information

---

# 17. Security

Security changes must preserve:

Anonymous guests

Authenticated administrators

Least privilege

Secure defaults

No feature should weaken existing security.

---

# 18. Database Changes

Schema modifications require:

Migration

Tests

Documentation

Backward compatibility analysis

Never modify database schemas informally.

---

# 19. Event Bus

New runtime interactions should use the Event Bus when appropriate.

Do not introduce direct service coupling without clear justification.

Events represent facts that already occurred.

Events are immutable.

---

# 20. UI Principles

The UI shall remain:

Simple

Fast

Mobile-first

Accessible

Avoid unnecessary animation.

Never introduce clutter.

---

# 21. API Principles

The JSON API shall remain:

Versioned

Predictable

Documented

Backward compatible within a major version.

---

# 22. Commit Standards

Each commit should:

Compile

Pass tests

Represent one logical change

Incomplete work should remain on feature branches.

---

# 23. Pull Request Standards

Each pull request should include:

Purpose

Design summary

Testing performed

Documentation updates

Related issue

Keep pull requests focused.

---

# 24. Refactoring Rules

Refactor only when:

It improves clarity.

It reduces duplication.

It simplifies maintenance.

Avoid cosmetic refactoring.

---

# 25. Code Review Checklist

Before considering work complete, verify:

- Architecture respected
    
- Tests added
    
- Documentation updated
    
- No unnecessary complexity
    
- Type hints complete
    
- Logging appropriate
    
- Errors handled
    
- Performance acceptable
    

---

# 26. Completion Checklist

Before declaring a task complete:

✓ Code builds

✓ Tests pass

✓ Ruff passes

✓ Black passes

✓ MyPy passes

✓ Documentation updated

✓ Acceptance criteria satisfied

✓ No guardrails violated

---

# 27. AI Collaboration Rules

If another AI previously implemented code:

Respect existing style.

Improve consistency.

Avoid unnecessary rewrites.

Document architectural concerns rather than silently changing patterns.

---

# 28. When Unsure

If uncertainty exists:

Do not guess.

Instead:

- Explain the ambiguity.
    
- Identify conflicting requirements.
    
- Recommend alternatives.
    
- Wait for clarification if necessary.
    

Incorrect certainty is worse than acknowledged uncertainty.

---

# 29. Long-Term Thinking

Every implementation should improve the project's future.

Ask:

- Will this be understandable in two years?
    
- Does it simplify future work?
    
- Does it preserve architectural boundaries?
    
- Will another contributor immediately understand it?
    

---

# 30. Definition of Excellence

Excellent code is:

Boring.

Predictable.

Reliable.

Well tested.

Easy to understand.

Easy to remove.

Easy to extend.

Architecture matters more than cleverness.

---

# Appendix A – Standard Prompt Header

Every AI coding session should begin with something like:

```text
You are contributing to the Encore project.

Follow the SAPRS, AI Implementation Guide, ADRs, and AI Engineering Playbook.

Respect all architectural guardrails.

Implement only the requested task.

Write production-quality Python 3.12 code.

Provide comprehensive tests.

Update documentation as required.

Do not violate Clean Architecture.

Do not bypass the Event Bus.

Explain any ambiguity before coding.
```

---

# Appendix B – Standard Task Template

Each coding task should follow a consistent structure:

```text
Task:
<One-sentence description>

Relevant Documents:
- SAPRS Chapter(s)
- AI Implementation Guide section(s)
- ADR(s)

Scope:
<Exactly what is in scope>

Out of Scope:
<What must not be changed>

Acceptance Criteria:
- ...
- ...
- ...

Required Tests:
- Unit
- Integration
- Regression (if applicable)

Definition of Done:
- Code implemented
- Tests passing
- Documentation updated
- Linting and type checking passing
```

Using this template keeps requests focused and repeatable across different AI tools.

---

# Appendix C – AI Review Prompt

After implementation, use a separate AI review pass before merging:

```text
Review this implementation as a senior Encore maintainer.

Do not rewrite the code.

Instead:

1. Identify architectural violations.
2. Identify Clean Architecture violations.
3. Identify Event Bus misuse.
4. Identify testing gaps.
5. Identify documentation gaps.
6. Identify performance concerns.
7. Identify security concerns.
8. Verify compliance with the AI Engineering Playbook.
9. Recommend improvements in priority order.

Do not invent requirements outside the SAPRS.
```

---

# Final Recommendation

At this point, Encore has a documentation set that many commercial software projects never achieve:

- **SAPRS** – The architectural blueprint.
    
- **AI Implementation Guide** – The implementation contract.
    
- **AI Engineering Playbook** – The engineering workflow and standards.
    
- **Architectural Decision Records** – The project's institutional memory.
    

The last artifact I'd recommend—before writing production code—is a **Project Bootstrap Kit**. Rather than coding the application itself, it would establish the repository, directory structure, CI/CD workflows, development tooling, pre-commit hooks, GitHub issue templates, pull request templates, labels, Dependabot configuration, CodeQL, documentation scaffolding, and the initial ADRs.

That means the very first line of application code is written into a repository that already enforces the engineering standards we've defined. From the outset, Encore would feel like a mature open-source project rather than a prototype. I think that's the strongest possible foundation for long-term success.
