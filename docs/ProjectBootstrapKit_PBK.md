
# Encore

# Project Bootstrap Kit (PBK)

**Version:** 1.0

**Purpose:** Initialize the Encore repository as a production-quality open source project before application development begins.

---

# 1. Objectives

The Bootstrap Kit shall:

- Create the repository structure.
    
- Configure development tooling.
    
- Configure quality gates.
    
- Configure CI/CD.
    
- Configure documentation.
    
- Configure issue tracking.
    
- Configure release automation.
    
- Configure testing infrastructure.
    
- Configure AI development support.
    

No application functionality is implemented during this phase.

---

# 2. Repository Layout

The initial repository shall be organized as follows:

```text
encore/
├── .github/
│   ├── ISSUE_TEMPLATE/
│   ├── workflows/
│   ├── CODEOWNERS
│   ├── dependabot.yml
│   ├── pull_request_template.md
│   └── FUNDING.yml (optional)
│
├── apps/
│   ├── builder/
│   └── server/
│
├── encore/
│   ├── api/
│   ├── config/
│   ├── controllers/
│   ├── domain/
│   ├── events/
│   ├── playback/
│   ├── repositories/
│   ├── search/
│   ├── services/
│   ├── templates/
│   ├── static/
│   └── utilities/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── regression/
│   ├── performance/
│   └── party_simulation/
│
├── docs/
│   ├── SAPRS/
│   ├── AIG/
│   ├── AEP/
│   ├── Developer/
│   ├── adr/
│   ├── api/
│   └── images/
│
├── scripts/
├── tools/
├── assets/
└── examples/
```

This layout is fixed unless changed by an approved ADR.

---

# 3. Python Project Initialization

The repository shall use:

- Python 3.12+
    
- `uv` for dependency management
    
- `pyproject.toml` as the single project configuration
    
- Editable installation for development
    

Required initial files:

- `pyproject.toml`
    
- `uv.lock`
    
- `.python-version`
    

---

# 4. Code Quality Tooling

Configure the following tools from the beginning:

- Ruff
    
- Black (or Ruff formatter if adopted consistently)
    
- MyPy
    
- pytest
    
- Coverage.py
    
- pre-commit
    

Developers should be unable to commit code that fails basic quality checks.

---

# 5. Pre-Commit Hooks

Every commit should automatically execute:

- Formatting
    
- Linting
    
- Import organization
    
- Trailing whitespace removal
    
- End-of-file normalization
    
- YAML validation
    
- TOML validation
    
- Markdown linting (where applicable)
    

Fast feedback should happen before code reaches CI.

---

# 6. Continuous Integration

GitHub Actions should include separate workflows for:

### Validate

- Install dependencies
    
- Run Ruff
    
- Run formatter verification
    
- Run MyPy
    
- Run unit tests
    

---

### Test

- Integration tests
    
- Coverage reporting
    

---

### Documentation

- Verify Markdown
    
- Check internal links (excluding generated docs)
    
- Build documentation (if applicable)
    

---

### Security

- CodeQL
    
- Dependency vulnerability scan
    
- Secret scanning where supported
    

---

### Release

Triggered only from tagged releases.

---

# 7. GitHub Repository Configuration

Enable:

- Branch protection
    
- Required reviews
    
- Required status checks
    
- Signed commits (optional but recommended)
    
- Auto-delete merged branches
    
- Merge queue (optional for larger contributor bases)
    

---

# 8. Issue Templates

Create templates for:

- Bug Report
    
- Feature Request
    
- Documentation
    
- Performance Issue
    
- Security Concern
    
- Refactoring Proposal
    
- Question
    

Each template should collect the information needed to reproduce or evaluate the request.

---

# 9. Pull Request Template

Every PR should include:

- Summary
    
- Motivation
    
- Testing performed
    
- Documentation updated
    
- SAPRS chapters affected
    
- ADR required? (Yes/No)
    
- Breaking change? (Yes/No)
    

---

# 10. Labels

Create a standard label taxonomy:

### Type

- bug
    
- feature
    
- enhancement
    
- documentation
    
- refactor
    
- test
    
- chore
    

### Priority

- P0
    
- P1
    
- P2
    
- P3
    

### Area

- playback
    
- queue
    
- search
    
- builder
    
- UI
    
- API
    
- database
    
- installer
    
- testing
    
- documentation
    

### Status

- needs-triage
    
- in-progress
    
- blocked
    
- ready-for-review
    
- good-first-issue
    
- help-wanted
    

---

# 11. Initial Documentation

The repository should include:

- README.md
    
- CONTRIBUTING.md
    
- CODE_OF_CONDUCT.md
    
- SECURITY.md
    
- CHANGELOG.md
    
- LICENSE
    

These documents should be present before the first feature implementation.

---

# 12. README

The README should answer:

- What is Encore?
    
- Why does it exist?
    
- Screenshots (added later)
    
- Features
    
- Architecture overview
    
- Installation
    
- Development
    
- Documentation map
    
- License
    
- Contributing
    

It should be approachable for both users and contributors.

---

# 13. ADR Initialization

Create the initial ADRs identified in Chapter 15:

- ADR-001: Separate Library Builder from Server
    
- ADR-002: HTMX Instead of SPA
    
- ADR-003: SQLite as the Storage Engine
    
- ADR-004: Internal Event Bus
    
- ADR-005: mpv Playback Engine
    
- ADR-006: Immutable Library Database
    
- ADR-007: Anonymous Guest Model
    
- ADR-008: Appliance-First Philosophy
    

Each ADR should be in place before implementation begins.

---

# 14. Documentation Scaffolding

Populate the documentation tree with placeholders for:

- User Guide
    
- Administrator Guide
    
- Developer Handbook
    
- API Reference
    
- Troubleshooting Guide
    
- Release Process
    

These documents can begin as outlines and evolve with the project.

---

# 15. Development Environment

Provide scripts or documented commands to:

- Create the virtual environment
    
- Install dependencies
    
- Run linting
    
- Run tests
    
- Start the server (once implemented)
    
- Run the builder (once implemented)
    

A new contributor should be productive within minutes.

---

# 16. Test Infrastructure

Set up the testing framework before application code exists.

Include:

- `pytest` configuration
    
- Shared fixtures
    
- Temporary SQLite helpers
    
- Mock `mpv` interface
    
- Synthetic media generator hooks (placeholders)
    
- Coverage reporting
    

This allows tests to be written from the first feature onward.

---

# 17. Release Strategy

Adopt Semantic Versioning:

- 0.x for pre-release development
    
- 1.0.0 for the first stable release
    

Maintain a documented release checklist covering validation, testing, documentation, and packaging.

---

# 18. AI Development Support

Create an `ai/` directory to hold AI-specific working materials:

```text
ai/
├── prompts/
├── context/
├── task_templates/
├── reviews/
└── checklists/
```

Suggested contents:

- Standard prompt header
    
- Task template
    
- Code review prompt
    
- Feature implementation checklist
    
- Regression checklist
    
- Context bundles derived from the AI Implementation Guide
    

This keeps AI artifacts version-controlled alongside the project.

---

# 19. Acceptance Criteria

The Bootstrap Kit is complete when:

- Repository structure exists.
    
- Development tooling is configured.
    
- CI/CD passes on an empty project.
    
- Documentation scaffolding is committed.
    
- Initial ADRs are written.
    
- Issue and PR templates are available.
    
- Labels and branch protections are defined.
    
- Test infrastructure is operational.
    
- AI support materials are organized.
    
- A new contributor can clone the repository, install dependencies, and run quality checks successfully.
    

---

# What Comes Next

At this point, the planning phase is complete.

You now have:

- **Software Architecture & Product Requirements Specification (SAPRS)** — the architectural blueprint.
    
- **AI Implementation Guide (AIG)** — the implementation contract.
    
- **AI Engineering Playbook (AEP)** — engineering standards and workflow.
    
- **Project Bootstrap Kit (PBK)** — repository and project initialization.
    
- **Architectural Decision Records (ADRs)** — institutional memory.
    
- **A phased implementation plan** — a roadmap from an empty repository to a production-ready appliance.
    

## My recommendation for implementation

Rather than asking an AI to generate the entire application in one pass, I would execute the project as a series of focused milestones, each culminating in a working, tested state:

1. **Bootstrap Repository** (PBK only)
    
2. **Core Framework** (configuration, dependency injection, logging, Event Bus)
    
3. **Library Builder**
    
4. **Playback Engine**
    
5. **Queue & Runtime Services**
    
6. **FastAPI + HTMX UI**
    
7. **Administration & Monitoring**
    
8. **Installer & Packaging**
    
9. **Party Simulation & Performance Validation**
    
10. **Release Candidate & Documentation Polish**
    

This incremental approach keeps every stage verifiable and aligns perfectly with the architecture we've defined. By the time you reach Version 1.0, Encore won't just be feature-complete—it will have been built on a disciplined engineering process designed for long-term maintainability and open-source collaboration. I think that's exactly the kind of foundation a project like Encore deserves.