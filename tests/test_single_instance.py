"""单实例锁（最后启动者胜）单测——依赖注入 fake，不依赖真实 /proc。"""
import json
import time

from server import _acquire_single_instance_lock, _write_lock
from config import Config


def _cfg(tmp_path) -> Config:
    cfg = Config(data_dir=str(tmp_path))
    cfg.data_dir = str(tmp_path)
    return cfg


def _read(lock_path):
    return json.loads(lock_path.read_text(encoding="utf-8"))


def test_first_acquire_writes_lock(tmp_path):
    cfg = _cfg(tmp_path)
    ok = _acquire_single_instance_lock(cfg)
    assert ok is True
    lock = _read(tmp_path / "mcp.lock")
    assert lock["pid"] == __import__("os").getpid()
    assert lock["start_ticks"] >= 0
    assert lock["started_at"] > 0


def test_stale_lock_overwritten(tmp_path):
    """锁记录 pid 已死 → 直接接管，不 kill。"""
    cfg = _cfg(tmp_path)
    lock_path = tmp_path / "mcp.lock"
    lock_path.write_text(json.dumps({"pid": 999999999, "start_ticks": 123}), encoding="utf-8")

    killed = []
    ok = _acquire_single_instance_lock(
        cfg,
        same_process=lambda pid, ticks: False,  # 身份校验失败 → 视为残留
        terminate=lambda pid: killed.append(pid) or True,
    )
    assert ok is True
    assert killed == []  # 残留锁不应触发 kill
    assert _read(lock_path)["pid"] == __import__("os").getpid()


def test_old_instance_replaced(tmp_path):
    """旧实例存活且身份校验通过 → 自动终止接管（最后启动者胜）。"""
    cfg = _cfg(tmp_path)
    lock_path = tmp_path / "mcp.lock"
    lock_path.write_text(json.dumps({"pid": 424242, "start_ticks": 456}), encoding="utf-8")

    killed = []
    ok = _acquire_single_instance_lock(
        cfg,
        same_process=lambda pid, ticks: True,
        terminate=lambda pid: killed.append(pid) or True,
    )
    assert ok is True
    assert killed == [424242]
    assert _read(lock_path)["pid"] == __import__("os").getpid()


def test_unterminable_old_instance_refused(tmp_path):
    """旧实例杀不掉 → 回退为拒绝启动。"""
    cfg = _cfg(tmp_path)
    lock_path = tmp_path / "mcp.lock"
    lock_path.write_text(json.dumps({"pid": 424242, "start_ticks": 456}), encoding="utf-8")

    ok = _acquire_single_instance_lock(
        cfg,
        same_process=lambda pid, ticks: True,
        terminate=lambda pid: False,  # kill 失败
    )
    assert ok is False
    # 锁未被覆盖（仍记录旧 pid）
    assert _read(lock_path)["pid"] == 424242
