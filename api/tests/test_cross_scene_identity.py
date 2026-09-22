"""client_op_id 全局身份：跨场次重提、并发争用、故障注入重试、异常数据修复。"""
import concurrent.futures as futures
import sqlite3

import httpx

from server_util import RunningServer, allocate


def test_same_op_id_in_other_scene_conflicts_and_does_not_advance_counter(server):
    # STAGE-A 首次提交，1 号生效
    first = allocate(server.base_url, "STAGE-A", "op-global-1", notes="吊臂全景")
    assert first.status_code == 201
    assert first.json()["shot_number"] == 1

    # 同一标识换到 STAGE-B 再次提交 -> 409，且明确指向首次提交内容
    crossed = allocate(server.base_url, "STAGE-B", "op-global-1", notes="轨道近景")
    assert crossed.status_code == 409
    detail = crossed.json()["detail"]
    assert detail["error"] == "payload_conflict"
    assert detail["existing"] == {"scene_id": "STAGE-A", "notes": "吊臂全景"}

    # STAGE-B 没有新增记录，全新操作仍从 1 开始
    fresh = allocate(server.base_url, "STAGE-B", "op-global-2", notes="轨道近景")
    assert fresh.status_code == 201
    assert fresh.json()["shot_number"] == 1

    # 按操作标识查询任何时候都只能得到唯一的首次映射
    got = httpx.get(f"{server.base_url}/api/operations/op-global-1")
    assert got.status_code == 200
    body = got.json()
    assert body["scene_id"] == "STAGE-A"
    assert body["notes"] == "吊臂全景"
    assert body["shot_number"] == 1

    # 两个场次的列表各自只保留本场次的权威记录
    a = httpx.get(f"{server.base_url}/api/scenes/STAGE-A/shot-numbers").json()
    b = httpx.get(f"{server.base_url}/api/scenes/STAGE-B/shot-numbers").json()
    assert [it["client_op_id"] for it in a] == ["op-global-1"]
    assert [it["client_op_id"] for it in b] == ["op-global-2"]


def test_failure_injection_then_cross_scene_retry_then_original_replay(server):
    op_id = "terminal-07-retry-0001"
    first = httpx.post(
        f"{server.base_url}/api/shot-numbers",
        json={
            "scene_id": "STAGE-A",
            "client_op_id": op_id,
            "notes": "吊臂全景",
            "inject_failure_after_commit": True,
        },
        timeout=10,
    )
    assert first.status_code == 503  # 但 STAGE-A 的 1 号已经持久生效

    # 相同 client_op_id 改投 STAGE-B、关闭故障注入 -> 必须 409
    crossed = httpx.post(
        f"{server.base_url}/api/shot-numbers",
        json={
            "scene_id": "STAGE-B",
            "client_op_id": op_id,
            "notes": "轨道近景",
            "inject_failure_after_commit": False,
        },
        timeout=10,
    )
    assert crossed.status_code == 409
    assert crossed.json()["detail"]["existing"] == {
        "scene_id": "STAGE-A",
        "notes": "吊臂全景",
    }

    # STAGE-B 未推进计数
    assert allocate(server.base_url, "STAGE-B", "brand-new-op", notes="开场").json()[
        "shot_number"
    ] == 1

    # 原请求（STAGE-A 原内容）重试 -> 取回 1 号并标记为重放
    retry = allocate(server.base_url, "STAGE-A", op_id, notes="吊臂全景")
    assert retry.status_code == 200
    body = retry.json()
    assert body["scene_id"] == "STAGE-A"
    assert body["shot_number"] == 1
    assert body["replayed"] is True

    # 操作查询始终只返回首次映射
    got = httpx.get(f"{server.base_url}/api/operations/{op_id}")
    assert got.json()["scene_id"] == "STAGE-A"
    assert got.json()["shot_number"] == 1

    # STAGE-A 计数无缺口
    assert allocate(server.base_url, "STAGE-A", "op-next-a", notes="续").json()[
        "shot_number"
    ] == 2


def test_concurrent_cross_scene_contention_single_first_winner(server):
    op_id = "contested-op"
    scenes = ["STAGE-A", "STAGE-B"]
    # 每个场次各 5 个并发请求争用同一标识；内容互不相同
    tasks = [(scene, f"{scene}-内容-{i}") for scene in scenes for i in range(5)]
    with futures.ThreadPoolExecutor(max_workers=10) as pool:
        calls = [
            pool.submit(allocate, server.base_url, scene, op_id, notes)
            for scene, notes in tasks
        ]
        responses = [c.result() for c in calls]

    created = [r for r in responses if r.status_code == 201]
    conflicts = [r for r in responses if r.status_code == 409]
    # 只能有一个首次成功，其余不同内容请求全部 409
    assert len(created) == 1
    assert len(conflicts) == 9
    for resp in conflicts:
        detail = resp.json()["detail"]
        assert detail["client_op_id"] == op_id
        assert detail["existing"]["scene_id"] == created[0].json()["scene_id"]

    winner_scene = created[0].json()["scene_id"]
    loser_scene = next(s for s in scenes if s != winner_scene)
    # 输家场次不推进计数：全新操作仍从 1 开始
    assert allocate(server.base_url, loser_scene, "loser-fresh", notes="x").json()[
        "shot_number"
    ] == 1
    # 赢家场次下一个号码为 2，无缺口
    assert allocate(server.base_url, winner_scene, "winner-next", notes="x").json()[
        "shot_number"
    ] == 2
    # 操作查询只认首次映射
    got = httpx.get(f"{server.base_url}/api/operations/{op_id}")
    assert got.json()["scene_id"] == winner_scene


def test_concurrent_identical_retries_across_scenes_share_one_number(server):
    op_id = "same-payload-op"
    notes = "完全相同的内容"
    # 两个场次并发提交完全相同的备注（跨场次内容仍不同）
    tasks = [("STAGE-A", notes)] * 4 + [("STAGE-B", notes)] * 4
    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        calls = [
            pool.submit(allocate, server.base_url, scene, op_id, n)
            for scene, n in tasks
        ]
        responses = [c.result() for c in calls]
    assert sum(1 for r in responses if r.status_code == 201) == 1
    # 场次是身份内容的一部分：赢家场次的同内容并发为重放（200），
    # 另一场次的请求内容不同，全部 409。
    statuses = {r.status_code for r in responses}
    assert statuses <= {200, 201, 409}
    assert any(r.status_code == 409 for r in responses)


def test_cross_scene_conflict_survives_restart(db_path):
    srv = RunningServer(db_path).start()
    assert allocate(srv.base_url, "STAGE-A", "op-restart", notes="吊臂全景").status_code == 201
    assert allocate(srv.base_url, "STAGE-B", "op-restart", notes="轨道近景").status_code == 409
    srv.stop()

    srv2 = RunningServer(db_path).start()
    try:
        # 重启后结论不变
        conflict = allocate(srv2.base_url, "STAGE-B", "op-restart", notes="轨道近景")
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["existing"]["scene_id"] == "STAGE-A"

        replay = allocate(srv2.base_url, "STAGE-A", "op-restart", notes="吊臂全景")
        assert replay.status_code == 200
        assert replay.json()["shot_number"] == 1
        assert replay.json()["replayed"] is True
    finally:
        srv2.stop()


def _create_corrupt_database(db_path) -> None:
    """用旧版（有缺陷的）表结构手工写入跨场次重复映射。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """CREATE TABLE scene_counters (
                   scene_id VARCHAR(128) NOT NULL PRIMARY KEY,
                   next_number INTEGER NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE operations (
                   client_op_id VARCHAR(64) NOT NULL,
                   scene_id VARCHAR(128) NOT NULL,
                   notes VARCHAR(2000) NOT NULL,
                   payload_hash VARCHAR(64) NOT NULL,
                   shot_number INTEGER NOT NULL,
                   created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                   CONSTRAINT uq_operations_scene_client_op
                       UNIQUE (scene_id, client_op_id),
                   CONSTRAINT uq_operations_scene_shot
                       UNIQUE (scene_id, shot_number)
               )"""
        )
        # STAGE-A 先提交（权威，1 号）；STAGE-B 后又占了一个 1 号
        conn.execute(
            "INSERT INTO operations (client_op_id, scene_id, notes, payload_hash, shot_number) "
            "VALUES ('dup-op', 'STAGE-A', '吊臂全景', 'hash-a', 1)"
        )
        conn.execute(
            "INSERT INTO operations (client_op_id, scene_id, notes, payload_hash, shot_number) "
            "VALUES ('dup-op', 'STAGE-B', '轨道近景', 'hash-b', 1)"
        )
        # STAGE-A 另有正常的 2 号；两场次计数器都已推进
        conn.execute(
            "INSERT INTO operations (client_op_id, scene_id, notes, payload_hash, shot_number) "
            "VALUES ('op-a-2', 'STAGE-A', '特写', 'hash-c', 2)"
        )
        conn.execute("INSERT INTO scene_counters VALUES ('STAGE-A', 3)")
        conn.execute("INSERT INTO scene_counters VALUES ('STAGE-B', 2)")
        conn.commit()
    finally:
        conn.close()


def test_startup_repairs_preexisting_cross_scene_duplicates(db_path):
    _create_corrupt_database(db_path)

    srv = RunningServer(db_path).start()
    try:
        # 以最早持久提交（STAGE-A）为权威恢复
        got = httpx.get(f"{srv.base_url}/api/operations/dup-op", timeout=10)
        assert got.status_code == 200
        assert got.json()["scene_id"] == "STAGE-A"
        assert got.json()["notes"] == "吊臂全景"
        assert got.json()["shot_number"] == 1

        # STAGE-B 的重复行已清除；列表里不再有该标识
        b = httpx.get(f"{srv.base_url}/api/scenes/STAGE-B/shot-numbers").json()
        assert b == []
        # STAGE-B 计数器回收到 1（其号码全部来自重复映射），不留缺口
        fresh = allocate(srv.base_url, "STAGE-B", "fresh-b", notes="新场次首发")
        assert fresh.status_code == 201
        assert fresh.json()["shot_number"] == 1

        # STAGE-A 权威记录与计数不回退、无缺口
        a = httpx.get(f"{srv.base_url}/api/scenes/STAGE-A/shot-numbers").json()
        assert [(it["client_op_id"], it["shot_number"]) for it in a] == [
            ("dup-op", 1),
            ("op-a-2", 2),
        ]
        nxt = allocate(srv.base_url, "STAGE-A", "op-a-3", notes="三号")
        assert nxt.json()["shot_number"] == 3

        # 修复后跨场次重提仍被拒绝
        assert allocate(srv.base_url, "STAGE-B", "dup-op", notes="轨道近景").status_code == 409
    finally:
        srv.stop()

    # 再重启一次，修复结果保持稳定
    srv2 = RunningServer(db_path).start()
    try:
        got = httpx.get(f"{srv2.base_url}/api/operations/dup-op", timeout=10)
        assert got.json()["scene_id"] == "STAGE-A"
    finally:
        srv2.stop()
