"""client_op_id 全局唯一：跨场次复用同一标识的幂等、冲突与恢复语义。

回归场景：终端在“提交后崩溃”故障重试前误切场次 —— 同一 client_op_id 必须
始终代表首次提交的操作，不能因场次变化再次占号。
"""
import concurrent.futures as futures
import sqlite3

import httpx

from app.service import content_hash
from server_util import RunningServer, allocate

OP_ID = "terminal-07-retry-0001"


def _get(base_url: str, path: str) -> httpx.Response:
    return httpx.get(f"{base_url}{path}", timeout=10)


def _scene_numbers(base_url: str, scene_id: str) -> list[int]:
    return [item["shot_number"] for item in _get(base_url, f"/api/scenes/{scene_id}/shot-numbers").json()]


def test_cross_scene_retry_after_injected_failure_conflicts(server):
    """任务书场景：故障注入 → 误切场次重试 → 409 指向首次提交，原请求可重放。"""
    # 首次提交：注入“提交后崩溃”故障 → 503，但 STAGE-A 的 1 号已经生效
    first = httpx.post(
        f"{server.base_url}/api/shot-numbers",
        json={
            "scene_id": "STAGE-A",
            "client_op_id": OP_ID,
            "notes": "吊臂全景",
            "inject_failure_after_commit": True,
        },
        timeout=10,
    )
    assert first.status_code == 503
    stored = _get(server.base_url, f"/api/operations/{OP_ID}").json()
    assert stored["scene_id"] == "STAGE-A"
    assert stored["shot_number"] == 1

    # 误切场次、改备注、关闭故障注入，同一标识再次提交 → 409 明确指向首次提交
    conflict = allocate(server.base_url, "STAGE-B", OP_ID, notes="轨道近景")
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["error"] == "payload_conflict"
    assert detail["existing"]["scene_id"] == "STAGE-A"
    assert detail["existing"]["notes"] == "吊臂全景"
    assert detail["existing"]["shot_number"] == 1

    # STAGE-B 未新增记录、未推进计数；STAGE-A 仍只有 1 号
    assert _get(server.base_url, "/api/scenes/STAGE-B/shot-numbers").json() == []
    assert _scene_numbers(server.base_url, "STAGE-A") == [1]

    # 原请求重试 → 取回 STAGE-A 的 1 号并标记为重放
    retry = allocate(server.base_url, "STAGE-A", OP_ID, notes="吊臂全景")
    assert retry.status_code == 200
    assert retry.json()["shot_number"] == 1
    assert retry.json()["replayed"] is True

    # STAGE-B 使用全新标识提交时仍从 1 开始
    fresh = allocate(server.base_url, "STAGE-B", "terminal-07-retry-0002", notes="轨道近景")
    assert fresh.status_code == 201
    assert fresh.json()["shot_number"] == 1

    # 按操作标识查询在任何时候都只能得到唯一的首次映射
    again = _get(server.base_url, f"/api/operations/{OP_ID}").json()
    assert again["scene_id"] == "STAGE-A"
    assert again["shot_number"] == 1


def test_concurrent_cross_scene_contention_single_first_commit(server):
    """两个场次并发争用同一标识：只有一个首次成功，其余不同内容请求均为 409。"""
    op = "terminal-race-0001"
    payloads = [
        {"scene_id": "STAGE-A", "client_op_id": op, "notes": "吊臂全景"},
        {"scene_id": "STAGE-B", "client_op_id": op, "notes": "轨道近景"},
    ]
    with futures.ThreadPoolExecutor(max_workers=10) as pool:
        calls = [
            pool.submit(
                httpx.post,
                f"{server.base_url}/api/shot-numbers",
                json=payloads[i % 2],
                timeout=30,
            )
            for i in range(10)
        ]
        responses = [call.result() for call in calls]

    created = [r for r in responses if r.status_code == 201]
    assert len(created) == 1  # 并发下只能有一个首次成功
    winner = created[0].json()

    for resp in responses:
        if resp.status_code == 201:
            continue
        if resp.status_code == 200:
            # 与首次提交同内容的并发重试 → 重放同一号码
            assert resp.json()["scene_id"] == winner["scene_id"]
            assert resp.json()["shot_number"] == winner["shot_number"]
            assert resp.json()["replayed"] is True
        else:
            # 不同内容 → 409 且指向首次提交
            assert resp.status_code == 409
            detail = resp.json()["detail"]
            assert detail["existing"]["scene_id"] == winner["scene_id"]
            assert detail["existing"]["notes"] == winner["notes"]

    # 只有胜出场次留下一条记录，另一场次为空、计数未推进
    loser_scene = "STAGE-B" if winner["scene_id"] == "STAGE-A" else "STAGE-A"
    assert _scene_numbers(server.base_url, winner["scene_id"]) == [1]
    assert _get(server.base_url, f"/api/scenes/{loser_scene}/shot-numbers").json() == []

    stored = _get(server.base_url, f"/api/operations/{op}").json()
    assert stored["scene_id"] == winner["scene_id"]
    assert stored["shot_number"] == 1


def test_restart_keeps_cross_scene_identity_conclusions(db_path):
    """进程重启后：首次映射、冲突判定与场次计数结论不变。"""
    srv = RunningServer(db_path).start()
    payload = dict(
        scene_id="STAGE-A",
        client_op_id=OP_ID,
        notes="吊臂全景",
        inject_failure_after_commit=True,
    )
    assert httpx.post(f"{srv.base_url}/api/shot-numbers", json=payload, timeout=10).status_code == 503
    assert allocate(srv.base_url, "STAGE-B", OP_ID, notes="轨道近景").status_code == 409
    srv.stop()

    srv2 = RunningServer(db_path).start()
    try:
        stored = _get(srv2.base_url, f"/api/operations/{OP_ID}").json()
        assert stored["scene_id"] == "STAGE-A"
        assert stored["shot_number"] == 1

        retry = allocate(srv2.base_url, "STAGE-A", OP_ID, notes="吊臂全景")
        assert retry.status_code == 200
        assert retry.json()["shot_number"] == 1
        assert allocate(srv2.base_url, "STAGE-B", OP_ID, notes="轨道近景").status_code == 409

        # 计数器不回退：STAGE-A 继续发 2 号，STAGE-B 仍从 1 开始
        assert allocate(srv2.base_url, "STAGE-A", "op-a-next", notes="续拍").json()["shot_number"] == 2
        assert allocate(srv2.base_url, "STAGE-B", "op-b-first", notes="轨道近景").json()["shot_number"] == 1
    finally:
        srv2.stop()


def _create_legacy_db_with_duplicate_mappings(db_path) -> None:
    """构造旧版缺陷服务留下的数据库：同一 client_op_id 跨场次重复占号。"""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_op_id VARCHAR(64) NOT NULL,
            scene_id VARCHAR(128) NOT NULL,
            notes VARCHAR(2000) NOT NULL,
            payload_hash VARCHAR(64) NOT NULL,
            shot_number INTEGER NOT NULL,
            created_at DATETIME NOT NULL DEFAULT (CURRENT_TIMESTAMP),
            CONSTRAINT uq_operations_scene_client_op UNIQUE (scene_id, client_op_id),
            CONSTRAINT uq_operations_scene_shot UNIQUE (scene_id, shot_number)
        );
        CREATE INDEX ix_operations_client_op_id ON operations (client_op_id);
        CREATE INDEX ix_operations_scene_id ON operations (scene_id);
        CREATE TABLE scene_counters (
            scene_id VARCHAR(128) PRIMARY KEY,
            next_number INTEGER NOT NULL
        );
        """
    )
    rows = [
        # (client_op_id, scene_id, notes, shot_number) —— 按提交顺序写入
        (OP_ID, "STAGE-A", "吊臂全景", 1),  # 首次持久提交（权威）
        (OP_ID, "STAGE-B", "轨道近景", 1),  # 异常：同一标识在 STAGE-B 再次占号
        ("op-b-normal", "STAGE-B", "补拍特写", 2),
        ("op-a-normal", "STAGE-A", "续拍", 2),
    ]
    for client_op_id, scene_id, notes, shot_number in rows:
        conn.execute(
            "INSERT INTO operations (client_op_id, scene_id, notes, payload_hash, shot_number)"
            " VALUES (?, ?, ?, ?, ?)",
            (client_op_id, scene_id, notes, content_hash(scene_id, notes), shot_number),
        )
    conn.executemany(
        "INSERT INTO scene_counters (scene_id, next_number) VALUES (?, ?)",
        [("STAGE-A", 3), ("STAGE-B", 3)],
    )
    conn.commit()
    conn.close()


def test_startup_recovers_from_legacy_cross_scene_duplicates(db_path):
    """已有异常数据：服务照常启动，以首次持久提交为权威恢复一致状态。"""
    _create_legacy_db_with_duplicate_mappings(db_path)

    srv = RunningServer(db_path).start()  # 存在异常数据时服务仍须能够启动
    try:
        # 按操作标识查询唯一指向首次持久提交
        stored = _get(srv.base_url, f"/api/operations/{OP_ID}").json()
        assert stored["scene_id"] == "STAGE-A"
        assert stored["shot_number"] == 1

        # 原内容重试 → 重放首次映射；跨场次同标识 → 409 指向首次提交
        replay = allocate(srv.base_url, "STAGE-A", OP_ID, notes="吊臂全景")
        assert replay.status_code == 200
        assert replay.json()["shot_number"] == 1
        conflict = allocate(srv.base_url, "STAGE-B", OP_ID, notes="轨道近景")
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["existing"]["scene_id"] == "STAGE-A"

        # 两个场次的列表保持连续无重复：异常行被重新标识而非删除
        scene_a = _get(srv.base_url, "/api/scenes/STAGE-A/shot-numbers").json()
        scene_b = _get(srv.base_url, "/api/scenes/STAGE-B/shot-numbers").json()
        assert [item["shot_number"] for item in scene_a] == [1, 2]
        assert [item["shot_number"] for item in scene_b] == [1, 2]
        assert (
            sum(1 for item in scene_a + scene_b if item["client_op_id"] == OP_ID) == 1
        )

        # 不留号码缺口、不回退计数器：新操作继续发 3 号
        assert allocate(srv.base_url, "STAGE-A", "op-a-new").json()["shot_number"] == 3
        assert allocate(srv.base_url, "STAGE-B", "op-b-new").json()["shot_number"] == 3
    finally:
        srv.stop()

    # 进程重启后结论不变（恢复是幂等的）
    srv2 = RunningServer(db_path).start()
    try:
        stored = _get(srv2.base_url, f"/api/operations/{OP_ID}").json()
        assert stored["scene_id"] == "STAGE-A"
        assert stored["shot_number"] == 1
        assert _scene_numbers(srv2.base_url, "STAGE-A") == [1, 2, 3]
        assert _scene_numbers(srv2.base_url, "STAGE-B") == [1, 2, 3]
    finally:
        srv2.stop()
