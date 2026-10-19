import pytest

from lyricfloat.diagnostics.smtc_report import Sample, analyse, read_samples, summary_row, write_samples
from lyricfloat.models import PlaybackStatus as St

WALL0 = 1_800_000_000.0


def simulate(seconds=60.0, step=0.05, report_every=1.0, rate=1.0, stale_stamp=False, events=()):
    """Player that publishes Position every `report_every` s with an accurate LastUpdatedTime.

    `events`: list of (time, action, value) with action "seek" (jump by value) or "pause"/"play".
    """
    samples = []
    pos = 10.0
    playing = True
    last_report_t, last_report_pos = 0.0, pos
    pending = sorted(events)
    t = 0.0
    while t <= seconds:
        while pending and pending[0][0] <= t:
            _, action, value = pending.pop(0)
            if action == "seek":
                pos += value
            else:
                playing = action == "play"
            last_report_t, last_report_pos = t, pos  # players report immediately on state change
        if t - last_report_t >= report_every:
            last_report_t, last_report_pos = t, pos
        stamp = WALL0 + (0.0 if stale_stamp else last_report_t)
        samples.append(
            Sample(
                t_mono=t,
                t_wall=WALL0 + t,
                kind="poll",
                status=int(St.PLAYING if playing else St.PAUSED),
                position=last_report_pos,
                duration=600.0,
                last_updated=stamp,
                title="Paper Lanterns",
            )
        )
        if playing:
            pos += step * rate
        t = round(t + step, 6)
    return samples


def test_accurate_stamps_give_near_zero_extrapolation_error():
    run = analyse("spotify", "x.csv", {}, simulate())
    row = summary_row(run)
    assert row["interval_med"] == pytest.approx(1.0, abs=0.06)
    assert row["extrap_p95"] <= 5
    assert row["engine_p95"] <= 5
    # Raw position is a 0..1 s sawtooth; around its own median line that is about ±0.5 s.
    assert 400 <= row["naive_max"] <= 600


def test_stale_stamps_make_extrapolation_useless():
    run = analyse("other", "x.csv", {}, simulate(stale_stamp=True, seconds=30))
    # Stamp stuck at t=0 while position keeps updating: extrapolation overshoots badly
    # until it exceeds the track end guard; the naive value is the better one here.
    assert run.stat("extrap_abs_ms", 0.95) > run.stat("naive_abs_ms", 0.95)


def test_drift_is_reported_in_ms_per_minute():
    run = analyse("ytmusic", "x.csv", {}, simulate(rate=1.01, report_every=0.5))
    assert run.drift == pytest.approx(600, rel=0.05)  # 1% fast = 600 ms per minute


def test_seek_and_pause_split_segments_and_are_counted():
    events = [(20.0, "seek", 30.0), (40.0, "pause", 0), (45.0, "play", 0)]
    run = analyse("spotify", "x.csv", {}, simulate(seconds=70, events=events))
    assert run.seeks == 1
    assert run.pauses == 1
    reasons = [s.end_reason for s in run.segments]
    assert reasons == ["seek", "pause", "end"]
    assert all(s.usable for s in run.segments)
    assert summary_row(run)["engine_p95"] <= 5


def test_correction_size_reflects_drift_between_reports():
    # 1% fast player reporting every second: the display is 10 ms behind at each report.
    run = analyse("spotify", "x.csv", {}, simulate(rate=1.01, report_every=1.0))
    row = summary_row(run)
    assert row["corrections"] >= 50
    assert row["corr_med"] == pytest.approx(10, abs=2)
    assert row["corr_max"] <= 15


def test_accurate_player_needs_almost_no_correction():
    row = summary_row(analyse("spotify", "x.csv", {}, simulate()))
    assert row["corr_p95"] <= 2


def test_report_bursts_count_as_one_update():
    samples = simulate(seconds=30, report_every=1000)  # reports only at start
    # A second report 60 ms after a state change, as Chrome does.
    for s in samples:
        if 10.0 <= s.t_mono < 10.06:
            s.last_updated = WALL0 + 10.0
            s.position = 20.0
        elif s.t_mono >= 10.06:
            s.last_updated = WALL0 + 10.06
            s.position = 20.06
    run = analyse("ytmusic", "x.csv", {}, samples)
    assert run.updates == 2  # start + the burst
    assert run.update_intervals == [pytest.approx(10.0, abs=0.06)]


def test_short_segments_are_listed_but_not_pooled():
    events = [(5.0, "seek", 30.0)]
    run = analyse("spotify", "x.csv", {}, simulate(seconds=30, events=events))
    assert not run.segments[0].usable  # 5 s
    assert run.segments[1].usable


def test_action_latency_is_measured_from_command_to_report():
    samples = simulate(seconds=20, events=[(10.3, "pause", 0)])
    samples.append(Sample(10.0, WALL0 + 10.0, "action:pause", int(St.PLAYING), 0, 600, 0))
    samples.sort(key=lambda s: s.t_mono)
    run = analyse("spotify", "x.csv", {}, samples)
    (latency,) = run.action_latency_ms["pause"]
    assert latency == pytest.approx(300, abs=60)


def test_csv_round_trip(tmp_path):
    samples = simulate(seconds=2)
    path = tmp_path / "raw.csv"
    write_samples(path, samples, {"player": "spotify", "app_id": "Spotify.exe"})
    meta, loaded = read_samples(path)
    assert meta == {"player": "spotify", "app_id": "Spotify.exe"}
    assert len(loaded) == len(samples)
    assert loaded[3].position == pytest.approx(samples[3].position)
    assert loaded[0].title == "Paper Lanterns"
