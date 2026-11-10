#!/usr/bin/env python3
"""成组配置发布（六台 sing-box）的原子性回归。

为什么必须成组：主配置与 slot2-6 是同一份订阅的六个渲染结果。只提升一部分，
线上就是「主出口跑新节点池、槽位跑旧节点池」—— 换线换到旧池的槽位上，
9router 看到的出口与 asip 认为的出口永远对不上。

跑法: python3 -m pytest test_config_atomic.py -q
"""
import json
import os

import pytest

import daemon


def _slots(tmp_path):
    """六份配置的路径，命名规则与 config.consumer_slot_config_path 一致。"""
    slots = []
    for i in range(1, 7):
        name = "cfg.json" if i == 1 else "cfg.json-slot%d.json" % i
        slots.append({"name": "slot-%d" % i, "config_file": str(tmp_path / name)})
    return slots


def _write(path, marker):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"outbounds": [{"type": "direct", "tag": marker}]}, f)


@pytest.fixture
def six_slots(monkeypatch, tmp_path):
    slots = _slots(tmp_path)
    monkeypatch.setattr(daemon, "consumer_slots", lambda: slots)
    # 真 sing-box 二进制不参与：这里只验编排逻辑，语法校验由 test_subscription_parse 覆盖。
    monkeypatch.setattr(daemon, "_config_check_error", lambda path: "")
    return slots


def test_config_set_pairs_covers_every_slot(six_slots, tmp_path):
    """主配置 + 五个槽位，一对 (final, draft)，草稿名以 .tmp 结尾。"""
    pairs = daemon._config_set_pairs(six_slots[0]["config_file"])

    assert len(pairs) == len(six_slots) == 6
    assert pairs[0][0] == six_slots[0]["config_file"], "第 0 项必须是主配置"
    for final, draft in pairs:
        assert draft == final + ".tmp"
        assert final.endswith(".json")


def test_config_set_pairs_without_slots_is_single_file(monkeypatch, tmp_path):
    """consumer_slots=0 的老部署仍按单文件走，不能凭空要求槽位草稿。"""
    monkeypatch.setattr(daemon, "consumer_slots", lambda: [])
    main = str(tmp_path / "cfg.json")

    assert daemon._config_set_pairs(main) == [(main, main + ".tmp")]


def test_missing_slot_draft_rejects_the_whole_set(six_slots):
    """少一份草稿就不能提升任何一份：那会留下半新半旧的进程组。"""
    main = six_slots[0]["config_file"]
    for i, slot in enumerate(six_slots):
        if i == 3:  # 故意漏掉 slot-4
            continue
        _write(slot["config_file"] + ".tmp", "new")

    err = daemon._config_set_check_error(main)

    assert "cfg.json-slot4.json.tmp" in err, err
    assert "缺少槽位草稿" in err


def test_check_error_names_the_offending_file(six_slots, monkeypatch):
    """哪一份没通过就说哪一份：只说「未通过」会让人去翻六个文件。"""
    main = six_slots[0]["config_file"]
    for slot in six_slots:
        _write(slot["config_file"] + ".tmp", "new")

    monkeypatch.setattr(daemon, "_config_check_error",
                        lambda path: "bad json" if path.endswith("cfg.json-slot5.json.tmp") else "")

    err = daemon._config_set_check_error(main)

    assert "cfg.json-slot5.json" in err and "bad json" in err, err


def test_promote_replaces_only_changed_files(six_slots):
    """内容没变的槽位不进 changed：每换一份都意味着重启一台 sing-box。"""
    main = six_slots[0]["config_file"]
    # 线上已有主配置与 slot-2，内容与新草稿不同 / 相同各一。
    _write(six_slots[0]["config_file"], "old")
    _write(six_slots[1]["config_file"], "same")
    _write(six_slots[1]["config_file"] + ".tmp", "same")
    _write(six_slots[0]["config_file"] + ".tmp", "new")
    for slot in six_slots[2:]:
        _write(slot["config_file"], "old")
        _write(slot["config_file"] + ".tmp", "new")

    changed = daemon._promote_config_set(main)

    changed_finals = {final for final, _tmp in changed}
    assert six_slots[1]["config_file"] not in changed_finals, "内容相同的槽位不该被重载"
    assert six_slots[0]["config_file"] in changed_finals
    with open(six_slots[0]["config_file"], encoding="utf-8") as f:
        assert json.load(f)["outbounds"][0]["tag"] == "new"
    assert not [p for p in os.listdir(str(six_slots[0]["config_file"]).rsplit("/", 1)[0])
                if p.endswith(".rollback")], "回滚副本必须清理干净"


def test_promote_rolls_back_when_a_replace_fails(six_slots, monkeypatch):
    """第 3 份替换失败时，前两份必须退回旧内容。

    os.replace 跨文件没有真正的事务，所以「已经替换的」必须能还原 —— 否则
    一次 ENOSPC 就会让主出口跑新配置、槽位跑旧配置，而且没人再修回来。
    """
    main = six_slots[0]["config_file"]
    for slot in six_slots:
        _write(slot["config_file"], "old")
        _write(slot["config_file"] + ".tmp", "new")

    real_replace = os.replace
    state = {"n": 0}

    def flaky(src, dst):
        state["n"] += 1
        if state["n"] == 3:
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(daemon.os, "replace", flaky)

    with pytest.raises(OSError):
        daemon._promote_config_set(main)

    for slot in six_slots:
        with open(slot["config_file"], encoding="utf-8") as f:
            tag = json.load(f)["outbounds"][0]["tag"]
        assert tag == "old", "%s 未回滚" % slot["config_file"]
    leftovers = [p for p in os.listdir(str(six_slots[0]["config_file"]).rsplit("/", 1)[0])
                 if p.endswith(".rollback")]
    assert not leftovers, "回滚副本残留：%s" % leftovers


def test_discard_removes_every_draft(six_slots):
    """放弃时六份草稿一起删：留下的孤儿草稿下次会被当成「已起草」。"""
    for slot in six_slots:
        _write(slot["config_file"] + ".tmp", "new")

    daemon._discard_config_set(six_slots[0]["config_file"])

    for slot in six_slots:
        assert not os.path.exists(slot["config_file"] + ".tmp"), slot["config_file"]
