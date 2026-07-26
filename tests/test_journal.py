"""Offline tests for the journal resume mechanism (0.5.4): high_water_mark()
ignores pre-resume peaks after mark_resume(), and is unchanged when no
resume marker exists. No network/keys — uses a tmp JOURNAL_DB via
monkeypatch on config so runs don't touch the real journal.
"""

from __future__ import annotations

from bot import config, journal


def test_high_water_mark_all_history_when_no_resume_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    journal.log_equity(1000.0, 1000.0, "start")
    journal.log_equity(1200.0, 1200.0, "peak")
    journal.log_equity(900.0, 900.0, "drawdown")
    assert journal.high_water_mark() == 1200.0


def test_high_water_mark_ignores_pre_resume_peaks(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    # log_equity/mark_resume timestamp at second resolution, and this test's
    # calls run well within one wall-clock second — fake a strictly
    # increasing clock so the ts >= resume-marker comparison is meaningful.
    ticks = iter(f"2024-01-01T00:00:{i:02d}" for i in range(10))
    monkeypatch.setattr(journal, "_now", lambda: next(ticks))

    journal.log_equity(1000.0, 1000.0, "start")
    journal.log_equity(1200.0, 1200.0, "peak")
    journal.log_equity(900.0, 900.0, "drawdown halt")
    journal.mark_resume(900.0, 900.0)
    # A lower peak than the pre-halt 1200 should still register as the HWM
    # once we've resumed — the pre-halt peak must no longer count.
    journal.log_equity(950.0, 950.0, "post-resume")
    assert journal.high_water_mark() == 950.0


def test_mark_resume_writes_resume_note(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOURNAL_DB", tmp_path / "journal.db")
    journal.mark_resume(750.0, 750.0)
    with journal._conn() as c:
        row = c.execute(
            "SELECT equity, cash, note FROM equity WHERE note = ?",
            (journal.RESUME_NOTE,),
        ).fetchone()
    assert row == (750.0, 750.0, journal.RESUME_NOTE)
