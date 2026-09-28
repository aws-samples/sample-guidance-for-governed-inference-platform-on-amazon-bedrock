# Branch Strategy

## Rule
- **Target branch: `main`**
- Rebase onto the latest `main` before opening a pull request
- Feature branches: `feat/<name>` for large multi-PR features

## Example
```bash
git checkout main
git pull origin main
git checkout -b fix/issue-123
# ... make changes ...
git rebase origin/main  # before the pull request
gh pr create --base main
```
