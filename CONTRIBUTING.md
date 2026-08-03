# 🎯 Contributing to MetaLens

Thank you for your interest in contributing to MetaLens! We welcome bug reports, feature requests, documentation improvements, and code contributions from the community.

---

## 🎯 Welcome

MetaLens is an open-source universal file metadata manager. Whether you're fixing a bug, improving documentation, adding a new metadata handler, or enhancing the UI, your contributions make MetaLens better for everyone.

Before contributing, please review this guide and our [Code of Conduct](./CODE_OF_CONDUCT.md).

---

## 🔀 Branch Strategy

- **Main branch**: `main` (protected, release-only — tagged `vX.Y.Z` with GitHub Release binaries)
- **Development branch**: `develop` (main integration branch, PRs target here)
- **Feature branches**: Create from `develop`, e.g., `feature/csv-export`

### Submitting Code

1. **Fork** the repository on GitHub
2. **Clone** your fork locally
3. **Create a feature branch** from `develop`:
   ```bash
   git checkout develop
   git pull origin develop
   git checkout -b feature/your-feature-name
   ```
4. **Make your changes** and test thoroughly
5. **Commit** with clear messages (see [Code Standards](#-code-standards))
6. **Push** to your fork
7. **Create a Pull Request** targeting `develop` (NOT `main`)

> [!WARNING]
> ⚠️ PRs to `main` will be rejected. Always target `develop`.

---

## 🛠️ Development Setup

### Prerequisites

- **Python**: 3.13 (see [REQUIREMENTS.md](./docs/REQUIREMENTS.md))
- **Node.js**: 20+
- **npm**: 10+
- **Git**

### Setup

```bash
git clone https://github.com/YOUR-USERNAME/MetaLens.git
cd MetaLens

# Python sidecar
cd python
python -m venv .venv
source .venv/bin/activate        # Linux
# .venv\Scripts\activate         # Windows
pip install -r requirements.txt

# Electron + frontend
cd ../electron && npm install
cd ../frontend && npm install

# Run in development mode
cd ../electron && npm start
```

Then enable the repo's git hooks (one-time per clone) so the `Last updated` footer in docs stays in sync automatically:

```bash
git config core.hooksPath .githooks
```

---

## 📝 Code Standards

### Python

- Follow the handler pattern in `python/core/handlers/` — one file per category, implementing `BaseMetadataHandler`
- Register new handlers in `python/core/registry.py`
- Add type hints on public functions

### Frontend

- Components live in `frontend/src/components/`, shared logic in `frontend/src/hooks/`
- Follow the existing Tailwind v4 cyber color palette (`frontend/src/styles/globals.css`) — never hardcode colors outside the defined CSS variables

### Commit Message Format

Follow the conventional commits format:

```
type(scope): description

[optional body]
```

**Types**: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`
**Scope**: `handlers`, `api`, `ui`, `electron`, `deps`, etc.

**Examples**:
- `feat(handlers): add support for HEIC images`
- `fix(diff): correct field alignment when types differ`
- `docs(readme): update supported formats table`

---

## ✅ Testing

### Running Tests

All Python changes must pass the test suite:

```bash
pytest python/tests/ -v
```

### Test Requirements

- **New handlers or routes must include tests** in `python/tests/`
- **All tests must pass**: no regressions allowed
- After frontend changes, verify the production build still compiles:
  ```bash
  cd frontend && npm run build
  ```

---

## 📤 Submitting a Pull Request

### PR Description Template

```markdown
## Summary
Brief description of changes

## Related Issue
Fixes #(issue number) or Relates to #(issue number)

## Type of Change
- [ ] Bug fix (non-breaking)
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Testing
- [ ] Added tests for new functionality
- [ ] `pytest python/tests/ -v` passes locally
- [ ] Tested on Windows/Linux (if applicable)

## Checklist
- [ ] Code follows style guidelines
- [ ] CLAUDE.md / CHANGELOG.md updated (if user-facing change)
- [ ] Version bumped in `python/config.py` (if applicable — see versioning rules below)
```

### PR Guidelines

- **Keep PRs focused** — one feature or bug fix per PR
- **Reference related issues** — use `Fixes #123` to link issues
- **Be responsive** — address feedback promptly
- **Don't force-push** — makes reviewing history difficult

---

## 🔢 Versioning

MetaLens uses `MAJOR.MINOR.PATCH`, with `python/config.py` → `VERSION` as the single source of truth.

| Component | When it bumps |
|---|---|
| **PATCH** | Bug fix, dependency update, behavior correction |
| **MINOR** | New feature, new handler, new panel |
| **MAJOR** | Breaking change, milestone 1.0 |

If your change bumps the version, update all four locations: `python/config.py`, `CHANGELOG.md`, `electron/package.json`, `frontend/package.json`.

---

## 🔧 Dependency Updates

Dependabot proposes routine version bumps automatically, targeting `develop`. Security-alert PRs may target `main` due to a GitHub platform limitation — this is expected, not a misconfiguration.

To update manually:

```bash
# Python
pip list --outdated
pip install --upgrade package_name
pip freeze > python/requirements.txt

# Node (electron/ or frontend/)
npm outdated
npm update package_name
```

Then test thoroughly before committing:

```bash
pytest python/tests/ -v
cd frontend && npm run build
```

---

## ❓ Questions?

- **Documentation**: Check [docs/](./docs/) folder
- **Bugs**: [GitHub Issues](https://github.com/overwrite00/MetaLens/issues)
- **Security**: See [SECURITY.md](./SECURITY.md)

---

## 🙏 Thank You

Thank you for contributing to MetaLens! Your efforts help make file metadata management more accessible and transparent for everyone.

---

*Last updated: 2026-08-03*
*← [Code of Conduct](./CODE_OF_CONDUCT.md) | [Security →](./SECURITY.md)*
