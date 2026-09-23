"""
TowerCast GitHub Setup Wizard
==============================
Interactive CLI wizard invoked by `python3 tower_cast.py github-setup`.

Guides the user through:
  1. Checking for the `gh` CLI
  2. Collecting GitHub username + repo name
  3. Writing github config into config.json
  4. Creating a .gitignore
  5. Initializing git + creating the GitHub repo
  6. Pushing code + enabling GitHub Pages
  7. Triggering a first manual workflow run
  8. Printing the permanent Overcast feed URL
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.json"


def _run(cmd: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    kwargs = {"shell": True, "text": True}
    if capture:
        kwargs["capture_output"] = True
    return subprocess.run(cmd, **kwargs, check=check)


def _ask(prompt: str, default: str = "") -> str:
    if default:
        val = input(f"{prompt} [{default}]: ").strip()
        return val if val else default
    return input(f"{prompt}: ").strip()


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m"


def _amber(s: str) -> str:
    return f"\033[33m{s}\033[0m"


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m"


def _step(n: int, total: int, msg: str):
    print(f"\n{_amber(f'[{n}/{total}]')} {_bold(msg)}")


def check_gh_cli() -> bool:
    """Returns True if `gh` CLI is installed and authenticated."""
    if not shutil.which("gh"):
        return False
    result = _run("gh auth status", check=False, capture=True)
    return result.returncode == 0


def get_github_username() -> Optional[str]:
    result = _run("gh api user --jq .login", check=False, capture=True)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def write_gitignore():
    gitignore = ROOT / ".gitignore"
    contents = """# TowerCast — do NOT commit downloaded audio or the database
data/audio/
data/towercast.db
gh-pages-output/

# Python
__pycache__/
*.py[cod]
*.egg-info/
.venv/
venv/

# macOS
.DS_Store

# Editor
.vscode/
.idea/
*.swp
"""
    gitignore.write_text(contents)
    print(f"  ✓ Wrote {_amber('.gitignore')} (audio/ and DB excluded from git)")


def update_config(username: str, repo: str, pages_url: str):
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    cfg["github"] = {
        "enabled": True,
        "username": username,
        "repo": f"{username}/{repo}",
        "pages_url": pages_url,
        "comment": "Managed by TowerCast github-setup wizard.",
    }

    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

    print(f"  ✓ Updated {_amber('config.json')} with GitHub settings")


def run_wizard():
    TOTAL_STEPS = 7
    print(_bold("\n🎲 TowerCast — GitHub Setup Wizard"))
    print("   This creates a free, always-on podcast feed hosted on GitHub.\n")

    # ── Step 1: Check gh CLI ─────────────────────────────────────────────
    _step(1, TOTAL_STEPS, "Checking GitHub CLI (gh)")

    if not check_gh_cli():
        print(_red("  ✗ `gh` CLI is not installed or not authenticated."))
        print("  Install it from https://cli.github.com/ then run:")
        print("    gh auth login")
        print("  and re-run this wizard.")
        sys.exit(1)

    detected_user = get_github_username()
    if detected_user:
        print(f"  ✓ Authenticated as {_amber(detected_user)}")
    else:
        print(_red("  ✗ Could not detect GitHub username via `gh api user`."))
        sys.exit(1)

    # ── Step 2: Repo name ────────────────────────────────────────────────
    _step(2, TOTAL_STEPS, "Repository name")
    print("  Your feed will be hosted at:")
    print(f"    {_amber(f'https://{detected_user}.github.io/<repo-name>/feed.xml')}\n")

    default_repo = "towercast"
    repo_name = _ask("  GitHub repo name", default=default_repo)
    if not repo_name:
        print(_red("  Repo name cannot be empty."))
        sys.exit(1)

    full_repo = f"{detected_user}/{repo_name}"
    pages_url = f"https://{detected_user}.github.io/{repo_name}"
    feed_url  = f"{pages_url}/feed.xml"

    print(f"\n  Your permanent Overcast feed URL will be:")
    print(f"    {_amber(feed_url)}")

    confirm = _ask("\n  Proceed? (y/n)", default="y").lower()
    if confirm != "y":
        print("  Aborted.")
        sys.exit(0)

    # ── Step 3: .gitignore ───────────────────────────────────────────────
    _step(3, TOTAL_STEPS, "Creating .gitignore")
    write_gitignore()

    # ── Step 4: Update config.json ───────────────────────────────────────
    _step(4, TOTAL_STEPS, "Writing GitHub config to config.json")
    update_config(detected_user, repo_name, pages_url)

    # ── Step 5: Git init + create GitHub repo ───────────────────────────
    _step(5, TOTAL_STEPS, f"Creating GitHub repository: {full_repo}")

    git_dir = ROOT / ".git"
    if not git_dir.exists():
        _run(f"git -C '{ROOT}' init -b main")
        _run(f"git -C '{ROOT}' add -A")
        _run(f"git -C '{ROOT}' commit -m 'Initial TowerCast commit'")
        print("  ✓ Initialized git repo")
    else:
        _run(f"git -C '{ROOT}' add -A")
        _run(f"git -C '{ROOT}' commit -m 'TowerCast github-setup' --allow-empty")
        print("  ✓ Staged changes in existing git repo")

    # Create GitHub repo (public so Overcast can reach audio files)
    print(f"  Creating public GitHub repo {_amber(full_repo)}...")
    result = _run(
        f"gh repo create {full_repo} --public --source '{ROOT}' --remote origin --push",
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        if "already exists" in result.stderr:
            print(f"  ℹ  Repo {full_repo} already exists — pushing to it...")
            _run(f"git -C '{ROOT}' remote set-url origin https://github.com/{full_repo}.git", check=False)
            _run(f"git -C '{ROOT}' push -u origin main --force")
        else:
            print(_red(f"  ✗ Failed to create repo: {result.stderr}"))
            sys.exit(1)
    else:
        print(f"  ✓ Created and pushed to {_amber(full_repo)}")

    # ── Step 6: Enable GitHub Pages ──────────────────────────────────────
    _step(6, TOTAL_STEPS, "Enabling GitHub Pages (gh-pages branch)")
    pages_result = _run(
        f"gh api repos/{full_repo}/pages -X POST "
        f"-f 'source[branch]=gh-pages' -f 'source[path]=/'",
        check=False, capture=True
    )
    if pages_result.returncode == 0 or "already enabled" in pages_result.stderr:
        print(f"  ✓ GitHub Pages enabled at {_amber(pages_url)}")
    else:
        # Pages might already be configured
        print(f"  ℹ  Pages setup returned: {pages_result.stderr.strip()[:120]}")
        print(f"     (This is OK if Pages is already enabled)")

    # ── Step 7: Trigger first workflow run ───────────────────────────────
    _step(7, TOTAL_STEPS, "Triggering first sync workflow")

    trigger_result = _run(
        f"gh workflow run towercast.yml --repo {full_repo}",
        check=False, capture=True
    )
    if trigger_result.returncode == 0:
        print(f"  ✓ First sync workflow triggered!")
        print(f"     Watch it at: https://github.com/{full_repo}/actions")
    else:
        print(f"  ℹ  Couldn't auto-trigger (workflow may not be registered yet).")
        print(f"     Go to https://github.com/{full_repo}/actions and run it manually.")

    # ── Done ─────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(_bold(_green("  ✅ Setup complete!")))
    print(f"{'─'*60}\n")
    print(f"  Your feed will be live at:")
    print(f"    {_amber(_bold(feed_url))}\n")
    print(f"  In Overcast:")
    print(f"    + → Add URL → paste the URL above → Subscribe\n")
    print(f"  Your feed auto-syncs daily at 6am EDT.")
    print(f"  To sync now: https://github.com/{full_repo}/actions")
    print(f"  To add favorites, edit config.json → favorites.shows\n")
    print(f"  To add a new show keyword, edit config.json → auto_include_keywords")
    print(f"  then push: git -C '{ROOT}' push\n")
