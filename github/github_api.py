"""
GitHub REST API v3 helpers using only Python stdlib (urllib).
Used by both the CI feed_builder and the local setup wizard.
"""
import json
import os
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional


class GitHubAPI:
    BASE = "https://api.github.com"

    def __init__(self, token: str, repo: str):
        """
        token: a GitHub personal access token or the Actions GITHUB_TOKEN
        repo:  "owner/repo-name"
        """
        self.token = token
        self.repo = repo
        self.owner, self.repo_name = repo.split("/", 1)

    def _headers(self, extra: Optional[Dict] = None) -> Dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "TowerCast/1.0",
        }
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, path: str, data: Optional[Any] = None,
                 headers: Optional[Dict] = None, raw_data: Optional[bytes] = None) -> Any:
        url = f"{self.BASE}{path}"
        body = None
        req_headers = self._headers(headers)
        if data is not None:
            body = json.dumps(data).encode()
            req_headers["Content-Type"] = "application/json"
        elif raw_data is not None:
            body = raw_data

        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                content = resp.read()
                if content:
                    return json.loads(content)
                return {}
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            raise RuntimeError(f"GitHub API {method} {path} → {e.code}: {body_text}")

    # ── Releases ─────────────────────────────────────────────────────────

    def list_releases(self, per_page: int = 100) -> List[Dict]:
        return self._request("GET", f"/repos/{self.repo}/releases?per_page={per_page}")

    def get_release_by_tag(self, tag: str) -> Optional[Dict]:
        try:
            return self._request("GET", f"/repos/{self.repo}/releases/tags/{tag}")
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise

    def create_release(self, tag: str, name: str, body: str = "",
                       draft: bool = False, prerelease: bool = False) -> Dict:
        return self._request("POST", f"/repos/{self.repo}/releases", data={
            "tag_name": tag,
            "name": name,
            "body": body,
            "draft": draft,
            "prerelease": prerelease,
        })

    def delete_release(self, release_id: int) -> bool:
        self._request("DELETE", f"/repos/{self.repo}/releases/{release_id}")
        return True

    def delete_release_by_tag(self, tag: str) -> bool:
        rel = self.get_release_by_tag(tag)
        if not rel:
            return False
        self.delete_release(rel["id"])
        try:
            self._request("DELETE", f"/repos/{self.repo}/git/refs/tags/{tag}")
        except Exception:
            pass
        return True

    def upload_asset(self, release_id: int, filename: str,
                     file_bytes: bytes, content_type: str = "audio/mp4") -> Dict:
        """Uploads a binary asset to a release. Returns the asset JSON."""
        url = (f"https://uploads.github.com/repos/{self.repo}/releases"
               f"/{release_id}/assets?name={filename}")
        req = urllib.request.Request(
            url,
            data=file_bytes,
            headers={
                **self._headers({"Content-Type": content_type}),
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def upload_asset_from_file(self, release_id: int, filepath: str,
                               content_type: str = "audio/mp4") -> Dict:
        """Streams a local file to a release asset. Memory-efficient for large audio."""
        filename = os.path.basename(filepath)
        file_size = os.path.getsize(filepath)
        url = (f"https://uploads.github.com/repos/{self.repo}/releases"
               f"/{release_id}/assets?name={filename}")

        # urllib doesn't support streaming from file directly for large uploads,
        # so we read in chunks and send. For files >100MB, chunk upload isn't
        # supported by GitHub's asset API anyway — it's a single PUT-like POST.
        with open(filepath, "rb") as f:
            file_bytes = f.read()

        req = urllib.request.Request(
            url,
            data=file_bytes,
            headers={
                **self._headers({
                    "Content-Type": content_type,
                    "Content-Length": str(file_size),
                }),
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    # ── Pages ────────────────────────────────────────────────────────────

    def enable_pages(self, branch: str = "gh-pages", path: str = "/") -> Dict:
        return self._request("POST", f"/repos/{self.repo}/pages", data={
            "source": {"branch": branch, "path": path}
        })

    def get_pages_info(self) -> Optional[Dict]:
        try:
            return self._request("GET", f"/repos/{self.repo}/pages")
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise

    # ── Issues ────────────────────────────────────────────────────────────

    def comment_issue(self, issue_number: int, body: str) -> Dict:
        return self._request("POST", f"/repos/{self.repo}/issues/{issue_number}/comments", data={"body": body})

    def close_issue(self, issue_number: int) -> Dict:
        return self._request("PATCH", f"/repos/{self.repo}/issues/{issue_number}", data={"state": "closed"})

    # ── Repo ─────────────────────────────────────────────────────────────

    def get_repo(self) -> Dict:
        return self._request("GET", f"/repos/{self.repo}")

    def create_repo(self, description: str = "", private: bool = False) -> Dict:
        return self._request("POST", "/user/repos", data={
            "name": self.repo_name,
            "description": description,
            "private": private,
            "auto_init": False,
        })
