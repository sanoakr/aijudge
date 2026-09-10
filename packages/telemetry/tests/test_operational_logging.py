"""運用ログの形を固定する。

このパッケージが守るのは 2 つだけ。
**プロセスによって出方が変わらないこと**と、
**識別子でないものが文脈に載らないこと**（P7）。
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from aijudge_telemetry import (
    ContextValueRejected,
    bind,
    configure_logging,
    current_context,
    redact_query,
    uvicorn_log_config,
)


def _capture(**kwargs) -> tuple[io.StringIO, logging.Logger]:
    stream = io.StringIO()
    configure_logging("test-service", stream=stream, **kwargs)
    return stream, logging.getLogger("aijudge_test")


def test_json_output_is_one_object_per_line() -> None:
    stream, logger = _capture(fmt="json")
    logger.info("graded a submission")
    logger.warning("queue is backing up")

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["level"] == "INFO"
    assert first["service"] == "test-service"
    assert first["logger"] == "aijudge_test"
    assert first["message"] == "graded a submission"
    assert "ts" in first


def test_the_context_travels_with_every_line() -> None:
    """相関 ID を積んだら、その区間のログ全部に載る。

    これが無いと、web の提出とワーカーの失敗を突き合わせられない。
    実際に 2 度、画面からは「採点が遅い」としか見えなかった
    （#60 / #80、docs/RUNNING.md）。
    """
    stream, logger = _capture(fmt="json")
    with bind(request_id="r-1", submission_id="s-9"):
        logger.info("started")
        with bind(job_id="j-3"):
            logger.info("picked up")
    logger.info("unrelated")

    started, picked, unrelated = (json.loads(line) for line in stream.getvalue().splitlines())
    assert started["request_id"] == "r-1"
    assert started["submission_id"] == "s-9"
    assert "job_id" not in started
    # 内側は外側を潰さずに重ねる。
    assert picked["request_id"] == "r-1"
    assert picked["job_id"] == "j-3"
    # 抜けたら元に戻る。
    assert "request_id" not in unrelated
    assert current_context() == {}


def test_extra_fields_become_columns() -> None:
    stream, logger = _capture(fmt="json")
    logger.info("job finished", extra={"job_id": "j-1", "duration_ms": 12})
    event = json.loads(stream.getvalue())
    assert event["job_id"] == "j-1"
    assert event["duration_ms"] == 12


def test_an_exception_is_recorded_without_breaking_the_line() -> None:
    stream, logger = _capture(fmt="json")
    try:
        raise RuntimeError("sandbox died")
    except RuntimeError:
        logger.exception("grading failed")

    assert len(stream.getvalue().splitlines()) == 1
    event = json.loads(stream.getvalue())
    assert event["error"]["type"] == "RuntimeError"
    assert event["error"]["message"] == "sandbox died"
    assert "RuntimeError" in event["error"]["traceback"]


def test_the_context_refuses_anything_that_is_not_an_identifier() -> None:
    """本文をログへ流す経路を塞ぐ（P7）。

    運用ログは journald に出る ── バックアップも暗号化も掛かっていない場所で、
    学習者データを置いてよい場所ではない。規約では守れないので型と長さで弾く。
    """
    with pytest.raises(ContextValueRejected), bind(source_code={"main.c": "int main(void){}"}):
        pass
    with pytest.raises(ContextValueRejected), bind(answer="x" * 2000):
        pass
    # 識別子は通る。
    with bind(submission_id="s-1", attempt=3, blind=True):
        assert current_context()["attempt"] == 3


def test_configuring_twice_does_not_double_the_output() -> None:
    """uvicorn の worker は同じプロセスで何度も組み立てる。"""
    stream, logger = _capture(fmt="json")
    second = io.StringIO()
    configure_logging("test-service", fmt="json", stream=second)
    logger.info("once")
    assert stream.getvalue() == ""
    assert len(second.getvalue().splitlines()) == 1


def test_the_text_format_stays_readable() -> None:
    stream, logger = _capture(fmt="text")
    with bind(submission_id="s-1"):
        logger.info("graded")
    line = stream.getvalue().strip()
    assert "graded" in line
    assert "submission_id=s-1" in line


def test_the_level_is_honoured() -> None:
    stream, logger = _capture(fmt="json", level="WARNING")
    logger.info("quiet")
    logger.warning("loud")
    assert len(stream.getvalue().splitlines()) == 1


def test_the_access_log_drops_the_query_string() -> None:
    """`?token=` の類を平文でログに残さない。"""
    assert redact_query("/courses/1/tasks/2?token=secret") == "/courses/1/tasks/2"
    assert redact_query("/health") == "/health"


def test_uvicorn_is_told_to_use_the_same_shape() -> None:
    config = uvicorn_log_config("learner-web", fmt="json")
    assert config["loggers"]["uvicorn.error"]["handlers"] == ["aijudge"]
    assert config["loggers"]["uvicorn.error"]["level"] == "INFO"
    # uvicorn 自身のアクセスログは止める。出すのはミドルウェアの側 ──
    # uvicorn の行にはクエリ文字列が乗り、request_id が乗らない。
    assert config["loggers"]["uvicorn.access"]["level"] == "WARNING"


def test_libraries_that_log_urls_are_quietened() -> None:
    """`httpx` は「HTTP Request: GET https://... 200 OK」を INFO で出す。

    OIDC のトークン交換をこの段で流すと、認可コードやクエリの秘密が
    journald に平文で残る。**こちらが何も書かなくても破られる側**なので、
    設定の時点で落としておく（P7）。
    """
    stream, _ = _capture(fmt="json")
    logging.getLogger("httpx").info("HTTP Request: GET https://oauth2.example/token?code=SECRET")
    assert stream.getvalue() == ""

    # 失敗は見えないと困るので、WARNING 以上は通す。
    logging.getLogger("httpx").warning("connection reset")
    assert len(stream.getvalue().splitlines()) == 1
