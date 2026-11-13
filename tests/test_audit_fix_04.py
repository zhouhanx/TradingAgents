"""AUDIT-FIX-04: information-crawl storage stays aligned with analysis workers."""

import os
from pathlib import Path

import pytest

from tradingagents.dataflows.info_crawl.base import InfoItem
from tradingagents.dataflows.info_crawl.store import JsonlItemStore
from webui import info_crawl, service


def _saved_job(root: Path, *, status: str = "done") -> tuple[str, str]:
    job_id = "ic-a-000001"
    item = InfoItem(
        source="fed_press",
        title="rates",
        url="https://www.federalreserve.gov/rates",
        published_at="2026-11-04T01:00:00Z",
    )
    disk = JsonlItemStore(root / "info-crawl")
    disk.save_job({"id": job_id, "status": status, "created_at": "2026-11-04T01:00:00Z"})
    disk.save_items(job_id, [item])
    return job_id, item.id


def _selection(job_id: str, item_id: str) -> dict:
    return {
        "analysts": ["news"],
        "research_depth": 1,
        "quick_think_llm": "quick",
        "deep_think_llm": "deep",
        "backend_url": None,
        "llm_provider": "chatgpt",
        "output_language": "Chinese",
        "info_crawl": {"job": job_id, "items": [item_id]},
    }


def test_crawl_store_tracks_default_then_saved_cache_directory(monkeypatch, tmp_path):
    default = tmp_path / "default-cache"
    saved = tmp_path / "saved-cache"
    monkeypatch.setattr(service, "_memory_root", lambda: tmp_path)
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    monkeypatch.delenv("TRADINGAGENTS_CACHE_DIR", raising=False)
    monkeypatch.setitem(service.DEFAULT_CONFIG, "data_cache_dir", str(default))
    info_crawl.set_store(None)
    try:
        assert Path(info_crawl.store().root) == default / "info-crawl"
        service.save_config({"data_cache_dir": str(saved)})
        assert Path(info_crawl.store().root) == saved / "info-crawl"
        selections = _selection(*_saved_job(saved))
        assert service.build_run_config(selections)["data_cache_dir"] == str(saved)
        service._validate_info_crawl_attachment(selections)
    finally:
        info_crawl.set_store(None)


def test_worker_reads_selected_rows_from_the_same_saved_directory(monkeypatch, tmp_path):
    from tradingagents.agents.analysts import news_analyst
    from tradingagents.dataflows import config as data_config

    cache = tmp_path / "cache"
    monkeypatch.setattr(service, "_memory_root", lambda: tmp_path)
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    monkeypatch.delenv("TRADINGAGENTS_CACHE_DIR", raising=False)
    monkeypatch.setitem(service.DEFAULT_CONFIG, "data_cache_dir", str(tmp_path / "default"))
    service.save_config({"data_cache_dir": str(cache)})
    job_id, item_id = _saved_job(cache)
    selections = _selection(job_id, item_id)
    run_config = service.build_run_config(selections)
    with data_config.run_config(run_config):
        block = news_analyst._info_crawl_block("2026-11-04", "NVDA", "stock")
    assert "rates" in block and "https://www.federalreserve.gov/rates" in block


def test_selected_attachment_after_cache_switch_is_explicitly_refused(monkeypatch, tmp_path):
    old = tmp_path / "old-cache"
    new = tmp_path / "new-cache"
    job_id, item_id = _saved_job(old)
    selections = _selection(job_id, item_id)
    with pytest.raises(service.ConsoleError, match="切换了缓存目录|超过 30 天|服务重启") as err:
        service._validate_info_crawl_attachment(selections, {"data_cache_dir": str(new)})
    assert err.value.status == 409
    assert "请返回「信息抓取」重新抓取并勾选" in str(err.value)


def test_interrupted_attachment_is_explicitly_refused(tmp_path):
    job_id, item_id = _saved_job(tmp_path / "cache", status="interrupted")
    selections = _selection(job_id, item_id)
    with pytest.raises(service.ConsoleError, match="服务重启"):
        service._validate_info_crawl_attachment(
            selections, {"data_cache_dir": str(tmp_path / "cache")}
        )


def test_expired_attachment_is_purged_before_worker_validation(tmp_path):
    cache = tmp_path / "cache"
    job_id, item_id = _saved_job(cache)
    disk = JsonlItemStore(cache / "info-crawl")
    old = 0.0
    for path in (disk.root / f"{job_id}.job.json", disk.root / f"{job_id}.jsonl"):
        path.touch()
        path.chmod(0o600)
        os.utime(path, (old, old))
    selections = _selection(job_id, item_id)
    with pytest.raises(service.ConsoleError, match="超过 30 天"):
        service._validate_info_crawl_attachment(
            selections, {"data_cache_dir": str(cache)}
        )


def test_saved_attachment_survives_restart_and_worker_consumes_it(monkeypatch, tmp_path):
    from tradingagents.dataflows.config import run_config
    from tradingagents.dataflows.info_crawl.jobs import get_info_items

    monkeypatch.delenv("TRADINGAGENTS_CACHE_DIR", raising=False)
    monkeypatch.setattr(service, "load_saved_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(service, "resolve_cache_dir", str)
    selections = _selection(*_saved_job(tmp_path))
    info_crawl.set_store(None)
    try:
        assert info_crawl.get_items(selections["info_crawl"]["job"])["items"][0]["title"] == "rates"
        info_crawl.set_store(None)
        config = service.build_run_config(selections)
        service._validate_info_crawl_attachment(selections, config)
        with run_config(config):
            assert "rates" in get_info_items(config["info_crawl_job"], config["info_crawl_items"], as_of="2026-11-05T00:00:00Z")
        (tmp_path / "info-crawl" / "ic-a-000001.jsonl").unlink()
        with pytest.raises(service.ConsoleError, match="附件已失效"):
            service._validate_info_crawl_attachment(selections, config)
    finally:
        info_crawl.set_store(None)


def test_environment_cache_overrides_saved_directory(monkeypatch, tmp_path):
    cache = str(tmp_path / "environment")
    monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", cache)
    monkeypatch.setitem(service.DEFAULT_CONFIG, "data_cache_dir", cache)
    monkeypatch.setattr(service, "load_saved_config", lambda: {"data_cache_dir": str(tmp_path / "saved")})
    selections = _selection(*_saved_job(Path(cache)))
    info_crawl.set_store(None)
    try:
        assert info_crawl.store().root == Path(cache) / "info-crawl"
        assert service.build_run_config(selections)["data_cache_dir"] == cache
        service._validate_info_crawl_attachment(selections)
    finally:
        info_crawl.set_store(None)


def test_checkpoint_clear_obeys_environment_cache_and_preserves_saved_cache(monkeypatch, tmp_path):
    env_cache = tmp_path / "env-cache"
    saved_cache = tmp_path / "saved-cache"
    for cache in (env_cache, saved_cache):
        folder = cache / "checkpoints"
        folder.mkdir(parents=True)
        (folder / "NVDA.db").write_bytes(b"fixture")
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", str(env_cache))
    monkeypatch.setitem(service.DEFAULT_CONFIG, "data_cache_dir", str(env_cache))
    monkeypatch.setattr(service, "load_saved_config", lambda: {"data_cache_dir": str(saved_cache)})
    assert service.clear_checkpoints()["cleared"] == 1
    assert not (env_cache / "checkpoints" / "NVDA.db").exists()
    assert (saved_cache / "checkpoints" / "NVDA.db").exists()
