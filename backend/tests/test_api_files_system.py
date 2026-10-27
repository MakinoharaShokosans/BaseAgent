"""文件接口、目录浏览、原生选择、自省。出处：docs/09_api/01 §5-7，03 §4。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

from api_support import HEADERS, ApiRig, build_rig

V1 = "/api/v1"


@pytest.fixture
async def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    async for r in build_rig(tmp_path):
        yield r


def err(response: Any) -> dict[str, Any]:
    return dict(response.json()["error"])


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def files_url(rig: ApiRig, suffix: str) -> str:
    wid = await rig.create_workspace()
    return f"{V1}/workspaces/{wid}/files/{suffix}"


# ==============================================================================
# 文件
# ==============================================================================


@pytest.mark.unit
async def test_tree_hides_noise_hidden_entries_and_outside_symlinks(
    rig: ApiRig, tmp_path: Path
) -> None:
    (rig.root / "src").mkdir()
    (rig.root / "src" / "a.py").write_text("x")
    (rig.root / "node_modules").mkdir()
    (rig.root / ".git").mkdir()
    (rig.root / ".hidden").write_text("x")
    (rig.root / "README.md").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (rig.root / "escape").symlink_to(outside)
    tree = (await rig.client.get(await files_url(rig, "tree"), headers=HEADERS)).json()
    names = [c["name"] for c in tree["children"]]
    assert names == ["src", "README.md"]  # 目录在前；噪音、隐藏项、越界链接都不出现
    assert tree["children"][0]["children"][0]["path"] == "src/a.py"


@pytest.mark.unit
async def test_read_and_save_roundtrip_with_hash_conflict_detection(rig: ApiRig) -> None:
    (rig.root / "a.txt").write_text("v1")
    read_url = (await files_url(rig, "content")) + "?path=a.txt"
    read = (await rig.client.get(read_url, headers=HEADERS)).json()
    assert read["content"] == "v1" and read["hash"] == sha("v1") and read["size"] == 2
    put_url = await files_url(rig, "content")
    saved = await rig.client.put(
        put_url, json={"path": "a.txt", "content": "v2", "base_hash": read["hash"]}, headers=HEADERS
    )
    assert saved.status_code == 200 and saved.json()["hash"] == sha("v2")
    assert (rig.root / "a.txt").read_text() == "v2"
    (rig.root / "a.txt").write_text("agent edit")  # 别处（Agent）改了
    stale = await rig.client.put(
        put_url, json={"path": "a.txt", "content": "v3", "base_hash": read["hash"]}, headers=HEADERS
    )
    assert stale.status_code == 409 and err(stale)["code"] == "FILE_CONFLICT"
    assert (rig.root / "a.txt").read_text() == "agent edit"


@pytest.mark.unit
async def test_saving_creates_new_files_atomically_and_keeps_permissions(rig: ApiRig) -> None:
    url = await files_url(rig, "content")
    created = await rig.client.put(
        url, json={"path": "new/dir/f.sh", "content": "echo"}, headers=HEADERS
    )
    assert created.status_code == 200
    target = rig.root / "new" / "dir" / "f.sh"
    assert target.read_text() == "echo"
    target.chmod(0o755)
    await rig.client.put(url, json={"path": "new/dir/f.sh", "content": "echo 2"}, headers=HEADERS)
    assert target.stat().st_mode & 0o777 == 0o755
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".baseagent-")]


@pytest.mark.unit
async def test_a_base_hash_on_a_missing_file_conflicts(rig: ApiRig) -> None:
    url = await files_url(rig, "content")
    r = await rig.client.put(
        url, json={"path": "gone.txt", "content": "x", "base_hash": "abc"}, headers=HEADERS
    )
    assert r.status_code == 409


@pytest.mark.security
@pytest.mark.parametrize(
    "path", ["../outside.txt", "/etc/passwd", "a/../../x", "sub/../../../etc/passwd"]
)
async def test_path_traversal_is_422_for_read_and_write(rig: ApiRig, path: str) -> None:
    url = await files_url(rig, "content")
    read = await rig.client.get(url, params={"path": path}, headers=HEADERS)
    assert read.status_code == 422 and err(read)["code"] == "PATH_ESCAPE_DETECTED"
    write = await rig.client.put(url, json={"path": path, "content": "x"}, headers=HEADERS)
    assert write.status_code == 422 and err(write)["code"] == "PATH_ESCAPE_DETECTED"


@pytest.mark.security
async def test_symlinks_cannot_be_used_to_leave_the_workspace(rig: ApiRig, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret")
    (rig.root / "link").symlink_to(outside)
    (rig.root / "filelink").symlink_to(outside / "secret.txt")
    url = await files_url(rig, "content")
    for path in ("link/secret.txt", "filelink"):
        r = await rig.client.get(url, params={"path": path}, headers=HEADERS)
        assert r.status_code == 422, path
    w = await rig.client.put(url, json={"path": "link/new.txt", "content": "x"}, headers=HEADERS)
    assert w.status_code == 422 and not (outside / "new.txt").exists()


@pytest.mark.unit
async def test_missing_binary_and_oversized_files(rig: ApiRig) -> None:
    url = await files_url(rig, "content")
    assert (
        await rig.client.get(url, params={"path": "nope.txt"}, headers=HEADERS)
    ).status_code == 404
    (rig.root / "bin").write_bytes(b"\xff\xfe\x00\x01")
    binary = await rig.client.get(url, params={"path": "bin"}, headers=HEADERS)
    assert binary.status_code == 422 and err(binary)["code"] == "NOT_A_TEXT_FILE"
    (rig.root / "big").write_bytes(b"a" * (5 * 1024 * 1024 + 1))
    big = await rig.client.get(url, params={"path": "big"}, headers=HEADERS)
    assert big.status_code == 413
    limit = rig.services.settings.tools.files.write_max_bytes
    too_much = await rig.client.put(
        url, json={"path": "x.txt", "content": "a" * (limit + 1)}, headers=HEADERS
    )
    assert too_much.status_code == 413 and err(too_much)["code"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.unit
async def test_directories_cannot_be_overwritten_as_files(rig: ApiRig) -> None:
    (rig.root / "d").mkdir()
    url = await files_url(rig, "content")
    r = await rig.client.put(url, json={"path": "d", "content": "x"}, headers=HEADERS)
    assert r.status_code == 422


# ==============================================================================
# 目录浏览
# ==============================================================================


@pytest.fixture
async def browsable(tmp_path: Path):  # type: ignore[no-untyped-def]
    home = tmp_path / "browse_root"
    (home / "alpha" / "inner").mkdir(parents=True)
    (home / "beta").mkdir()
    (home / ".secret").mkdir()
    (home / "file.txt").write_text("x")
    async for r in build_rig(
        tmp_path / "rig", filesystem={"browse_roots": [str(home)], "browse_max_entries": 2}
    ):
        yield r, home


@pytest.mark.unit
async def test_browse_lists_only_visible_directories_with_child_flags(browsable) -> None:  # type: ignore[no-untyped-def]
    rig, home = browsable
    listing = (await rig.client.get(f"{V1}/system/fs/directories", headers=HEADERS)).json()
    assert listing["path"] == str(home) and listing["parent"] is None
    assert [(e["name"], e["has_children"]) for e in listing["entries"]] == [
        ("alpha", True),
        ("beta", False),
    ]
    down = (
        await rig.client.get(
            f"{V1}/system/fs/directories", params={"path": str(home / "alpha")}, headers=HEADERS
        )
    ).json()
    assert down["parent"] == str(home) and [e["name"] for e in down["entries"]] == ["inner"]


@pytest.mark.security
async def test_browse_refuses_anything_outside_the_configured_roots(
    browsable, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    rig, home = browsable
    for target in ("/", "/etc", str(home.parent), str(home / ".." / "rig")):
        r = await rig.client.get(
            f"{V1}/system/fs/directories", params={"path": target}, headers=HEADERS
        )
        assert r.status_code == 422 and err(r)["code"] == "PATH_ESCAPE_DETECTED", target
    (home / "evil").symlink_to("/etc")
    r = await rig.client.get(
        f"{V1}/system/fs/directories", params={"path": str(home / "evil")}, headers=HEADERS
    )
    assert r.status_code == 422


@pytest.mark.unit
async def test_browse_caps_entries_and_rejects_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    for i in range(5):
        (root / f"d{i}").mkdir(parents=True)
    (root / "f.txt").write_text("x")
    async for rig in build_rig(
        tmp_path / "rig", filesystem={"browse_roots": [str(root)], "browse_max_entries": 3}
    ):
        listing = (await rig.client.get(f"{V1}/system/fs/directories", headers=HEADERS)).json()
        assert len(listing["entries"]) == 3 and listing["truncated"] is True
        r = await rig.client.get(
            f"{V1}/system/fs/directories", params={"path": str(root / "f.txt")}, headers=HEADERS
        )
        assert r.status_code == 422


# ==============================================================================
# 原生选择与自省
# ==============================================================================


@pytest.mark.unit
async def test_native_picker_unavailable_then_single_flight(rig: ApiRig) -> None:
    unavailable = await rig.client.post(f"{V1}/system/fs/pick-directory", headers=HEADERS)
    assert unavailable.status_code == 503

    gate = asyncio.Event()

    async def picker(seconds: float) -> str | None:
        await gate.wait()
        return "/picked"

    rig.services.native_picker = picker
    first = asyncio.create_task(rig.client.post(f"{V1}/system/fs/pick-directory", headers=HEADERS))
    await asyncio.sleep(0.05)
    busy = await rig.client.post(f"{V1}/system/fs/pick-directory", headers=HEADERS)
    assert busy.status_code == 409 and err(busy)["code"] == "PICKER_BUSY"
    gate.set()
    assert (await first).json() == {"path": "/picked"}


@pytest.mark.unit
async def test_health_dependencies_and_introspection(rig: ApiRig) -> None:
    assert (await rig.client.get(f"{V1}/health")).json() == {"status": "ok"}

    async def checks() -> dict[str, str]:
        return {"bash_shell": "ok", "web_search": "down"}

    rig.services.dependency_checks = checks
    deps = await rig.client.get(f"{V1}/health/dependencies", headers=HEADERS)
    assert deps.json() == {"bash_shell": "ok", "web_search": "down"}
    assert "rag" not in deps.text
    rig.services.introspection.skills = lambda: [{"name": "s", "trust": "trusted"}]
    rig.services.introspection.models = lambda: [{"tier": "fast"}]
    assert (await rig.client.get(f"{V1}/skills", headers=HEADERS)).json() == [
        {"name": "s", "trust": "trusted"}
    ]
    assert (await rig.client.get(f"{V1}/models", headers=HEADERS)).json() == [{"tier": "fast"}]
    assert (await rig.client.get(f"{V1}/tools", headers=HEADERS)).json() == []
    assert (await rig.client.get(f"{V1}/mcp/servers", headers=HEADERS)).json() == []
    assert (await rig.client.get(f"{V1}/tools")).status_code == 401  # 自省同样受令牌保护


@pytest.mark.unit
async def test_limits_expose_concurrency_only_there_is_no_budget(rig: ApiRig) -> None:
    data = (await rig.client.get(f"{V1}/system/limits", headers=HEADERS)).json()
    assert data == {"max_concurrent_tasks": rig.services.settings.server.max_concurrent_tasks}
    assert (await rig.client.get(f"{V1}/system/limits")).status_code == 401
